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
except ImportError:  # executed as a script
    from codec import REGISTER_COUNT, decode, engineering

AGENT_VERSION = "gw-0.1.0"
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


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    server = os.environ["EUM_SERVER_URL"]
    if not server.startswith("https://") and os.getenv("EUM_ALLOW_INSECURE") != "1":
        raise SystemExit("EUM_SERVER_URL must use https://")
    networks = parse_networks(os.getenv("EUM_ALLOWED_NETWORKS", ""))
    if not networks:
        log.warning("EUM_ALLOWED_NETWORKS not set: Modbus TCP devices are blocked; serial (RTU) devices still work")
    agent = Agent(server, os.environ["EUM_GATEWAY_TOKEN"], networks,
                  Buffer(os.getenv("EUM_BUFFER_PATH", "gateway_buffer.db")),
                  config_refresh_s=int(os.getenv("EUM_CONFIG_REFRESH_S", "60")))
    asyncio.run(agent.run())


if __name__ == "__main__":
    main()
