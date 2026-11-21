"""P2: gateway agent against a local pymodbus simulator (no real field devices)."""
import asyncio
import socket
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gateway"))
import agent as gw  # noqa: E402
from codec import encode  # noqa: E402

from pymodbus.datastore import ModbusSequentialDataBlock, ModbusServerContext, ModbusSlaveContext  # noqa: E402
from pymodbus.server import ModbusTcpServer  # noqa: E402


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


async def _start_sim(port):
    hr = [0] * 100
    hr[0:2] = encode(230.25, "float32")             # voltage ABCD
    hr[2:4] = encode(12345678, "uint32", "little")  # energy CDAB
    hr[4] = encode(-15, "int16")[0]
    ir = [0] * 100
    ir[10] = 987
    coils = [False] * 100
    coils[3] = True
    # pymodbus 3.8 datastore adds +1 to the protocol address, so blocks start at 1 (= protocol address 0)
    store = ModbusSlaveContext(hr=ModbusSequentialDataBlock(1, hr), ir=ModbusSequentialDataBlock(1, ir),
                               co=ModbusSequentialDataBlock(1, coils), di=ModbusSequentialDataBlock(1, [False] * 100))
    server = ModbusTcpServer(ModbusServerContext(slaves={1: store}, single=False), address=("127.0.0.1", port))
    task = asyncio.create_task(server.serve_forever())
    for _ in range(50):
        await asyncio.sleep(0.05)
        try:
            r, w = await asyncio.open_connection("127.0.0.1", port); w.close(); break
        except OSError:
            pass
    return server, task


TAGS = [
    {"id": 1, "name": "V", "function_code": 3, "address": 0, "data_type": "float32", "word_order": "big", "byte_order": "big", "scale": 1, "offset": 0},
    {"id": 2, "name": "kWh", "function_code": 3, "address": 2, "data_type": "uint32", "word_order": "little", "byte_order": "big", "scale": 0.1, "offset": 0},
    {"id": 3, "name": "T", "function_code": 3, "address": 4, "data_type": "int16", "word_order": "big", "byte_order": "big", "scale": 0.1, "offset": 0},
    {"id": 4, "name": "IR", "function_code": 4, "address": 10, "data_type": "uint16", "word_order": "big", "byte_order": "big", "scale": 1, "offset": 0},
    {"id": 5, "name": "CB", "function_code": 1, "address": 3, "data_type": "bool", "word_order": "big", "byte_order": "big", "scale": 1, "offset": 0},
]


def test_poll_simulator_and_allowlist():
    async def run():
        port = _free_port()
        server, task = await _start_sim(port)
        try:
            dev = {"id": 1, "protocol": "modbus_tcp", "host": "127.0.0.1", "port": port, "unit_id": 1, "tags": TAGS}
            rows = await gw.poll_device(dev, gw.parse_networks("127.0.0.0/8"))
            vals = {r["tag_id"]: r for r in rows}
            assert all(r["quality"] == "Good" for r in rows), rows
            assert vals[1]["value"] == pytest.approx(230.25)
            assert vals[2]["value"] == pytest.approx(1234567.8)
            assert vals[3]["value"] == pytest.approx(-1.5)
            assert vals[4]["value"] == 987 and vals[5]["value"] == 1.0
            # host outside the gateway allowlist is never contacted
            blocked = await gw.poll_device(dev, gw.parse_networks("192.168.0.0/16"))
            assert all(r["quality"] == "Bad" and r["value"] is None for r in blocked)
            # unreachable device -> Bad, no exception
            dead = {**dev, "port": _free_port()}
            assert all(r["quality"] == "Bad" for r in await gw.poll_device(dead, gw.parse_networks("127.0.0.0/8")))
        finally:
            await server.shutdown(); task.cancel()
    asyncio.run(run())


def test_agent_end_to_end_with_outage_buffering(client, tenant_pair, tmp_path):
    """Simulator -> gateway agent -> (WAN outage, local buffer) -> central API -> tenant-scoped tag values."""
    from eumbms.main import app
    a = tenant_pair["a"]
    s = client.post("/api/sites", json={"name": "시뮬현장"}, headers=a["operator"]).json()
    g = client.post("/api/gateways", json={"site_id": s["id"], "name": "sim-gw"}, headers=a["tenant_admin"]).json()
    d = client.post("/api/devices", json={"site_id": s["id"], "gateway_id": g["id"], "name": "sim-meter", "host": "10.20.0.7", "poll_interval_s": 1}, headers=a["operator"]).json()
    client.patch(f"/api/devices/{d['id']}", json={"enabled": True}, headers=a["operator"])
    t_v = client.post("/api/tags", json={"device_id": d["id"], "name": "V", "address": 0, "data_type": "float32", "unit": "V"}, headers=a["operator"]).json()
    t_e = client.post("/api/tags", json={"device_id": d["id"], "name": "kWh", "address": 2, "data_type": "uint32", "word_order": "little", "scale": 0.1}, headers=a["operator"]).json()

    async def run():
        port = _free_port()
        server, task = await _start_sim(port)
        real = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://central")
        down = {"on": False}

        class Flaky(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                if down["on"]:
                    raise httpx.ConnectError("WAN down")
                return await real._transport.handle_async_request(request)

        http = httpx.AsyncClient(transport=Flaky(), base_url="http://central")
        # the DB says 10.20.0.7; in the lab that address is mapped to the local simulator
        factory = lambda dev: gw.make_client({**dev, "host": "127.0.0.1", "port": port})
        buf = gw.Buffer(str(tmp_path / "buf.db"))
        ag = gw.Agent("http://central", g["token"], gw.parse_networks("10.20.0.0/16"), buf, http=http, client_factory=factory)
        try:
            await ag.refresh_config(force=True)
            down["on"] = True
            assert await ag.step() > 1          # backoff while WAN is down
            assert buf.size() == 2              # readings kept locally
            ag._next_poll.clear()
            await ag.step()
            assert buf.size() == 4
            down["on"] = False
            assert await ag.step() == 1.0       # recovered
            assert buf.size() == 0
        finally:
            await server.shutdown(); task.cancel(); await real.aclose()
    asyncio.run(run())

    tags = {t["id"]: t for t in client.get("/api/tags", headers=a["viewer"]).json()}
    assert tags[t_v["id"]]["value"] == pytest.approx(230.25) and tags[t_v["id"]]["quality"] == "Good"
    assert tags[t_e["id"]]["value"] == pytest.approx(1234567.8)
    hist = client.get(f"/api/tags/{t_v['id']}/readings", headers=a["viewer"]).json()
    assert len(hist) >= 2
    gws = client.get("/api/gateways", headers=a["viewer"]).json()
    assert next(x for x in gws if x["id"] == g["id"])["agent_version"] == gw.AGENT_VERSION
