"""EUM BMS PRO edge gateway agent.

Runs inside the field network. Reads devices (read-only Modbus FC1-4), buffers
readings in a local SQLite file, and uploads them OUTBOUND over HTTPS to the
central server. The central server never connects to field devices.

Environment:
  EUM_SERVER_URL        https://<central-server>      (https required unless EUM_ALLOW_INSECURE=1 for local tests)
  EUM_GATEWAY_TOKEN     token issued by the server (one-time display)
  EUM_ALLOWED_NETWORKS  comma-separated CIDRs the gateway may poll over TCP, e.g. 192.168.10.0/24
                        (not needed for serial Modbus RTU devices)
  EUM_BUFFER_PATH       local buffer file (default ./gateway_buffer.db)
  EUM_CONFIG_REFRESH_S  config refresh period (default 60)
"""
import asyncio
import datetime as dt
import inspect
import ipaddress
import json
import logging
import os
import sqlite3
import time

import httpx

try:
    from .codec import REGISTER_COUNT, decode, engineering
except ImportError:  # executed as a script / packaged exe
    from codec import REGISTER_COUNT, decode, engineering

AGENT_VERSION = "gw-0.2.0"
log = logging.getLogger("eum-gateway")


class Buffer:
    """Durable local queue so readings survive WAN outages and restarts."""

    def __init__(self, path: str, max_rows: int = 500_000):
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS q (id INTEGER PRIMARY KEY, body TEXT NOT NULL)")
        self.db.commit()
        self.max_rows = max_rows

    def put(self, readings: list[dict]):
        self.db.executemany("INSERT INTO q(body) VALUES (?)", [(json.dumps(r),) for r in readings])
        # drop oldest beyond capacity
        self.db.execute("DELETE FROM q WHERE id <= (SELECT MAX(id) FROM q) - ?", (self.max_rows,))
        self.db.commit()

    def peek(self, n: int) -> list[tuple[int, dict]]:
        return [(i, json.loads(b)) for i, b in self.db.execute("SELECT id, body FROM q ORDER BY id LIMIT ?", (n,))]

    def ack(self, last_id: int):
        self.db.execute("DELETE FROM q WHERE id <= ?", (last_id,))
        self.db.commit()

    def size(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM q").fetchone()[0]


def parse_networks(spec: str) -> list:
    return [ipaddress.ip_network(s.strip(), strict=False) for s in spec.split(",") if s.strip()]


def host_allowed(host: str, networks: list) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(ip in n for n in networks)


def _unit_kw(method) -> str:
    """pymodbus renamed slave= to device_id= in 3.10; detect at runtime."""
    params = inspect.signature(method).parameters
    return "device_id" if "device_id" in params else "slave"


async def read_tag(client, tag: dict, unit_id: int) -> float:
    fc, addr = tag["function_code"], tag["address"]
    count = REGISTER_COUNT[tag["data_type"]] if fc in (3, 4) else 1
    method = {1: client.read_coils, 2: client.read_discrete_inputs,
              3: client.read_holding_registers, 4: client.read_input_registers}[fc]
    rr = await method(addr, count=count, **{_unit_kw(method): unit_id})
    if rr.isError():
        raise IOError(str(rr))
    if fc in (1, 2):
        return 1.0 if rr.bits[0] else 0.0
    raw = decode(rr.registers, tag["data_type"], tag["word_order"], tag["byte_order"])
    return engineering(raw, tag["scale"], tag["offset"])


def make_client(dev: dict):
    from pymodbus.client import AsyncModbusSerialClient, AsyncModbusTcpClient
    if dev["protocol"] == "modbus_tcp":
        return AsyncModbusTcpClient(dev["host"], port=dev["port"], timeout=2, retries=1)
    return AsyncModbusSerialClient(dev["serial_port"], baudrate=dev["baudrate"], timeout=2, retries=1)


async def poll_device(dev: dict, networks: list, client_factory=make_client) -> list[dict]:
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    if dev["protocol"] == "modbus_tcp" and not host_allowed(dev["host"], networks):
        log.warning("device %s host %s not in EUM_ALLOWED_NETWORKS — skipped", dev["id"], dev["host"])
        return [{"tag_id": t["id"], "value": None, "quality": "Bad", "ts": now} for t in dev["tags"]]
    client = client_factory(dev)
    out = []
    try:
        if not await client.connect():
            raise ConnectionError("connect failed")
        for tag in dev["tags"]:
            try:
                out.append({"tag_id": tag["id"], "value": await read_tag(client, tag, dev["unit_id"]),
                            "quality": "Good", "ts": dt.datetime.now(dt.timezone.utc).isoformat()})
            except Exception as exc:
                log.info("tag %s read failed: %s", tag["id"], exc)
                out.append({"tag_id": tag["id"], "value": None, "quality": "Bad", "ts": now})
    except Exception as exc:
        log.warning("device %s unreachable: %s", dev["id"], exc)
        out = [{"tag_id": t["id"], "value": None, "quality": "Bad", "ts": now} for t in dev["tags"]]
    finally:
        client.close()
    return out


class Agent:
    def __init__(self, server: str, token: str, networks: list, buffer: Buffer, http: httpx.AsyncClient | None = None,
                 config_refresh_s: int = 60, client_factory=make_client):
        self.server, self.networks, self.buffer = server.rstrip("/"), networks, buffer
        self.http = http or httpx.AsyncClient(timeout=15)
        self.headers = {"X-Gateway-Token": token, "User-Agent": AGENT_VERSION}
        self.config_refresh_s, self.client_factory = config_refresh_s, client_factory
        self.devices: list[dict] = []
        self._cfg_at = 0.0
        self._next_poll: dict[int, float] = {}
        self._backoff = 1.0

    async def refresh_config(self, force=False):
        if not force and time.monotonic() - self._cfg_at < self.config_refresh_s:
            return
        r = await self.http.get(self.server + "/api/gw/config", headers=self.headers)
        r.raise_for_status()
        cfg = r.json()
        if cfg.get("remote_control_enabled"):
            log.error("server reports remote control enabled — this agent has no write path and ignores it")
        self.devices = [d for d in cfg["devices"] if d["enabled"]]
        self._cfg_at = time.monotonic()

    async def poll_due(self) -> int:
        now, n = time.monotonic(), 0
        for dev in self.devices:
            if now >= self._next_poll.get(dev["id"], 0):
                rows = await poll_device(dev, self.networks, self.client_factory)
                self.buffer.put(rows)
                n += len(rows)
                self._next_poll[dev["id"]] = now + dev["poll_interval_s"]
        return n

    async def upload(self, batch_size=1000) -> int:
        sent = 0
        while True:
            items = self.buffer.peek(batch_size)
            if not items:
                return sent
            r = await self.http.post(self.server + "/api/gw/readings", headers=self.headers,
                                     json={"agent_version": AGENT_VERSION, "readings": [b for _, b in items]})
            r.raise_for_status()
            self.buffer.ack(items[-1][0])
            sent += len(items)

    async def step(self):
        try:
            await self.refresh_config()
        except Exception as exc:
            log.warning("config refresh failed (using cached): %s", exc)
        await self.poll_due()
        try:
            await self.upload()
            self._backoff = 1.0
        except Exception as exc:
            log.warning("upload failed, %d rows buffered: %s", self.buffer.size(), exc)
            self._backoff = min(self._backoff * 2, 300)
            return self._backoff
        return 1.0

    async def run(self):
        while True:
            await asyncio.sleep(await self.step())


# ------------------------------------------------------------------ configuration & CLI
def base_dir() -> str:
    """Folder of the .exe when packaged, else the script folder."""
    import sys
    return os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))


def load_settings(path: str | None = None) -> dict:
    """gateway.ini next to the program; environment variables override it."""
    import configparser
    cp = configparser.ConfigParser()
    ini = path or os.path.join(base_dir(), "gateway.ini")
    if os.path.exists(ini):
        cp.read(ini, encoding="utf-8-sig")
    g = lambda sec, key, default="": cp.get(sec, key, fallback=default).strip()
    return {
        "server": os.getenv("EUM_SERVER_URL") or g("server", "url"),
        "token": os.getenv("EUM_GATEWAY_TOKEN") or g("server", "token"),
        "networks": os.getenv("EUM_ALLOWED_NETWORKS") or g("network", "allowed_networks"),
        "buffer": os.getenv("EUM_BUFFER_PATH") or g("buffer", "path") or os.path.join(base_dir(), "gateway_buffer.db"),
        "refresh": int(os.getenv("EUM_CONFIG_REFRESH_S") or g("server", "config_refresh_s", "60") or 60),
        "insecure": (os.getenv("EUM_ALLOW_INSECURE") or g("server", "allow_insecure", "0")) == "1",
        "ini": ini,
    }


def list_ports() -> list[tuple[str, str]]:
    from serial.tools import list_ports as lp
    return [(p.device, p.description or "") for p in lp.comports()]


def _templates():
    try:
        from .device_templates import TEMPLATES, tag_defaults
    except ImportError:
        from device_templates import TEMPLATES, tag_defaults
    return TEMPLATES, tag_defaults


async def test_device(target: str, template: str, unit_id: int = 1, baudrate: int | None = None, port: int = 502,
                      client_factory=make_client) -> list[tuple[str, str, str]]:
    """Read every tag of a template once, without the server. target = COM port / tty path or IP address."""
    TEMPLATES, tag_defaults = _templates()
    tpl = TEMPLATES[template]
    is_ip = all(c.isdigit() or c == "." for c in target)
    dev = {"id": 0, "protocol": "modbus_tcp" if is_ip else "modbus_rtu", "host": target if is_ip else "",
           "port": port, "serial_port": "" if is_ip else target, "baudrate": baudrate or tpl.get("baudrate", 9600),
           "unit_id": unit_id, "tags": [dict(tag_defaults(t), id=i) for i, t in enumerate(tpl["tags"])]}
    nets = parse_networks(target + "/32") if is_ip else []
    rows = await poll_device(dev, nets, client_factory)
    out = []
    for r, t in zip(rows, dev["tags"]):
        v = "—" if r["value"] is None else f"{r['value']:.4g}"
        out.append((t["name"], f"{v} {t['unit']}".strip(), r["quality"]))
    return out


async def check_server(settings: dict) -> str:
    async with httpx.AsyncClient(timeout=20) as http:
        r = await http.get(settings["server"].rstrip("/") + "/api/gw/config", headers={"X-Gateway-Token": settings["token"]})
        if r.status_code == 401:
            return "실패: 토큰이 올바르지 않습니다 (웹의 사업장·게이트웨이 탭에서 토큰을 다시 발급하세요)"
        r.raise_for_status()
        cfg = r.json()
        devs = cfg["devices"]
        lines = [f"성공: 서버 연결됨 (게이트웨이 ID {cfg['gateway_id']}), 배정된 장치 {len(devs)}대"]
        for d in devs:
            addr = d["serial_port"] if d["protocol"] == "modbus_rtu" else f"{d['host']}:{d['port']}"
            lines.append(f"  - {d['name']} ({addr}, 국번 {d['unit_id']}) 태그 {len(d['tags'])}개, 수집 {'켜짐' if d['enabled'] else '꺼짐 — 웹에서 수집 시작을 누르세요'}")
        return "\n".join(lines)


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="EUM-Gateway", description="EUM BMS PRO 현장 게이트웨이")
    ap.add_argument("--ports", action="store_true", help="연결된 COM(시리얼) 포트 목록")
    ap.add_argument("--test", metavar="포트또는IP", help="서버 없이 장비를 한 번 읽어 봄 (예: COM3)")
    ap.add_argument("--template", default="pzem016", help="장비 템플릿 (pzem016, sdm120)")
    ap.add_argument("--unit", type=int, default=1, help="국번(Unit ID)")
    ap.add_argument("--baud", type=int, default=None, help="통신속도 (생략 시 템플릿 기본값)")
    ap.add_argument("--port", type=int, default=502, help="Modbus TCP 포트 (IP 시험 시)")
    ap.add_argument("--check", action="store_true", help="서버 연결과 토큰 확인")
    ap.add_argument("--config", default=None, help="설정 파일 경로 (기본: 프로그램 폴더의 gateway.ini)")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if a.ports:
        ports = list_ports()
        print("연결된 포트가 없습니다. USB-RS485 컨버터를 꽂고 드라이버(CH340)를 확인하세요." if not ports
              else "\n".join(f"{d}\t{desc}" for d, desc in ports))
        return 0
    if a.test:
        TEMPLATES, _ = _templates()
        if a.template not in TEMPLATES or TEMPLATES[a.template]["protocol"] == "simulator":
            print("템플릿 이름 오류. 사용 가능:", ", ".join(k for k, v in TEMPLATES.items() if v["protocol"] != "simulator"))
            return 2
        rows = asyncio.run(test_device(a.test, a.template, a.unit, a.baud, a.port))
        print(f"[{TEMPLATES[a.template]['name']}] {a.test} 국번 {a.unit}")
        for name, val, q in rows:
            print(f"  {name:<10} {val:<16} {'정상' if q == 'Good' else '실패'}")
        if all(q != "Good" for _, _, q in rows):
            print("\n모두 실패: 배선(A·B 바뀜), 포트 번호, 국번, 통신속도를 확인하세요.")
            return 1
        return 0

    st = load_settings(a.config)
    if not st["server"] or not st["token"]:
        print(f"설정이 비어 있습니다. {st['ini']} 파일에 서버 주소(url)와 토큰(token)을 넣으세요.")
        return 2
    if not st["server"].startswith("https://") and not st["insecure"]:
        print("서버 주소는 https:// 로 시작해야 합니다.")
        return 2
    if a.check:
        try:
            print(asyncio.run(check_server(st)))
            return 0
        except Exception as exc:
            print("실패: 서버에 연결할 수 없습니다 —", exc)
            return 1
    networks = parse_networks(st["networks"])
    if not networks:
        log.warning("allowed_networks 미설정: Modbus TCP 장치는 차단되고, 시리얼(RTU) 장치만 수집합니다")
    log.info("EUM 게이트웨이 %s 시작 — 서버 %s (종료: Ctrl+C)", AGENT_VERSION, st["server"])
    agent = Agent(st["server"], st["token"], networks, Buffer(st["buffer"]), config_refresh_s=st["refresh"])
    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        log.info("종료")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
