"""P4: virtual devices, alarms (limit/state/comm), trend, dashboard, diagrams, templates, CSV."""
import datetime as dt

from eumbms.alarms import check_comm
from eumbms.db import SessionLocal
from eumbms.models import Tag, utcnow
from eumbms.simulator import run_simulators


def test_demo_dashboard_trend_and_isolation(client, tenant_pair):
    a, b = tenant_pair["a"], tenant_pair["b"]
    assert client.post("/api/demo", json={}, headers=a["operator"]).status_code == 403
    r = client.post("/api/demo", json={}, headers=a["tenant_admin"])
    assert r.status_code == 201, r.text
    site_id = r.json()["site_id"]
    # more samples a minute later
    with SessionLocal() as db:
        run_simulators(db, utcnow() + dt.timedelta(minutes=1), force=True); db.commit()
    d = client.get("/api/dashboard", headers=a["viewer"]).json()
    s = next(x for x in d["sites"] if x["id"] == site_id)
    assert s["devices_total"] == 5 and s["devices_online"] == 5 and s["power_kw"] > 0
    assert s["today_kwh"] is not None and s["today_kwh"] > 0
    assert s["peak_kw_today"] >= s["power_kw"]  # peak is of the same (main) meter
    tags_all = {t["id"]: t for t in client.get("/api/tags", headers=a["viewer"]).json()}
    assert abs(tags_all[s["main_power_tag_id"]]["value"] - s["power_kw"]) < 0.01  # not a sum of feeders
    feeders = [t for t in tags_all.values() if t["category"] == "power" and t["id"] != s["main_power_tag_id"]
               and t["device_id"] in {d["id"] for d in client.get("/api/devices", headers=a["viewer"]).json()}]
    assert all(f["value"] < s["power_kw"] for f in feeders if f["value"] is not None)
    tags = client.get("/api/tags", headers=a["viewer"]).json()
    pw = next(t for t in tags if t["category"] == "power")
    tr = client.get(f"/api/trend?tag_ids={pw['id']}&hours=2&points=60", headers=a["viewer"]).json()
    assert tr["series"][0]["raw_count"] >= 2 and tr["series"][0]["points"][0]["avg"] > 0
    # other tenant sees nothing
    db_ = client.get("/api/dashboard", headers=b["viewer"]).json()
    assert all(x["id"] != site_id for x in db_["sites"])
    assert client.get(f"/api/trend?tag_ids={pw['id']}", headers=b["viewer"]).status_code == 404
    assert client.get(f"/api/diagrams/{site_id}", headers=b["viewer"]).status_code == 404
    # diagram present and references own tags only
    dg = client.get(f"/api/diagrams/{site_id}", headers=a["viewer"]).json()
    assert len(dg["spec"]["elements"]) == 9
    # CSV
    csv = client.get(f"/api/tags/{pw['id']}/export.csv?hours=2", headers=a["viewer"])
    assert csv.status_code == 200 and "시각(KST)" in csv.text and csv.text.count("\n") >= 3


def test_limit_state_and_comm_alarms(client, tenant_pair):
    a, b = tenant_pair["a"], tenant_pair["b"]
    s = client.post("/api/sites", json={"name": "경보시험"}, headers=a["operator"]).json()
    g = client.post("/api/gateways", json={"site_id": s["id"], "name": "gw"}, headers=a["tenant_admin"]).json()
    d = client.post("/api/devices", json={"site_id": s["id"], "gateway_id": g["id"], "name": "m", "host": "10.0.0.9", "poll_interval_s": 10}, headers=a["operator"]).json()
    client.patch(f"/api/devices/{d['id']}", json={"enabled": True}, headers=a["operator"])
    t = client.post("/api/tags", json={"device_id": d["id"], "name": "I", "address": 0, "unit": "A", "category": "current"}, headers=a["operator"]).json()
    st = client.post("/api/tags", json={"device_id": d["id"], "name": "CB", "address": 1, "category": "state"}, headers=a["operator"]).json()
    for body in ({"tag_id": t["id"], "kind": "high", "threshold": 100, "deadband": 5, "severity": "major"},
                 {"tag_id": st["id"], "kind": "state", "threshold": 0, "severity": "critical", "message": "CB 트립"},
                 {"tag_id": t["id"], "kind": "comm", "severity": "major"}):
        assert client.post("/api/alarm-rules", json=body, headers=a["operator"]).status_code == 201
    assert client.post("/api/alarm-rules", json={"tag_id": t["id"], "kind": "high", "threshold": 1}, headers=b["operator"]).status_code == 404
    H = {"X-Gateway-Token": g["token"]}
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)

    def push(sec, i_val, cb):
        ts = (now + dt.timedelta(seconds=sec)).isoformat()
        client.post("/api/gw/readings", json={"readings": [{"tag_id": t["id"], "value": i_val, "ts": ts},
                                                           {"tag_id": st["id"], "value": cb, "ts": ts}]}, headers=H)

    def active():
        return client.get("/api/alarms", headers=a["viewer"]).json()

    push(0, 120, 1)
    al = [x for x in active() if x["tag_id"] in (t["id"], st["id"])]
    assert len(al) == 1 and "120A > 100A" in al[0]["message"]
    push(1, 97, 1)   # inside deadband -> still active
    assert len([x for x in active() if x["tag_id"] == t["id"]]) == 1
    push(2, 90, 0)   # cleared; breaker trips
    al = [x for x in active() if x["tag_id"] in (t["id"], st["id"])]
    assert len(al) == 1 and al[0]["severity"] == "critical" and "CB 트립" in al[0]["message"]
    assert client.post(f"/api/alarms/{al[0]['id']}/ack", headers=a["viewer"]).status_code == 403
    assert client.post(f"/api/alarms/{al[0]['id']}/ack", headers=b["operator"]).status_code == 404
    ack = client.post(f"/api/alarms/{al[0]['id']}/ack", headers=a["operator"]).json()
    assert ack["acked_by"] and ack["active"] is True
    hist = client.get("/api/alarms?state=all", headers=a["viewer"]).json()
    assert any(x["tag_id"] == t["id"] and x["cleared_at"] for x in hist)
    # comm loss: no data for > 120 s
    with SessionLocal() as db:
        assert check_comm(db, utcnow() + dt.timedelta(minutes=5)) >= 1; db.commit()
    assert any("통신" in x["message"] or "수신" in x["message"] for x in active() if x["tag_id"] == t["id"])
    with SessionLocal() as db:
        check_comm(db, utcnow()); db.commit()  # fresh again -> cleared
    assert not any("마지막 수신" in x["message"] for x in active() if x["tag_id"] == t["id"])


def test_diagram_validation(client, tenant_pair):
    a, b = tenant_pair["a"], tenant_pair["b"]
    s = client.post("/api/sites", json={"name": "결선도"}, headers=a["operator"]).json()
    sb = client.post("/api/sites", json={"name": "B결선"}, headers=b["operator"]).json()
    d = client.post("/api/devices", json={"site_id": sb["id"], "name": "x", "host": "10.0.0.2"}, headers=b["operator"]).json()
    tb = client.post("/api/tags", json={"device_id": d["id"], "name": "kw", "address": 0}, headers=b["operator"]).json()
    ok = {"elements": [{"type": "bus", "x": 10, "y": 10, "w": 200, "label": "BUS"}], "lines": [{"points": [[0, 0], [1, 1]]}]}
    assert client.put(f"/api/diagrams/{s['id']}", json={"spec": ok}, headers=a["operator"]).status_code == 200
    assert client.put(f"/api/diagrams/{s['id']}", json={"spec": ok}, headers=a["viewer"]).status_code == 403
    bad = {"elements": [{"type": "script", "x": 1, "y": 1}], "lines": []}
    assert client.put(f"/api/diagrams/{s['id']}", json={"spec": bad}, headers=a["operator"]).status_code == 422
    foreign = {"elements": [{"type": "meter", "x": 1, "y": 1, "tag_id": tb["id"]}], "lines": []}
    assert client.put(f"/api/diagrams/{s['id']}", json={"spec": foreign}, headers=a["operator"]).status_code == 404
    assert client.put(f"/api/diagrams/{sb['id']}", json={"spec": ok}, headers=a["operator"]).status_code == 404


def test_device_templates(client, tenant_pair):
    a = tenant_pair["a"]
    keys = [t["key"] for t in client.get("/api/device-templates", headers=a["viewer"]).json()]
    assert {"pzem016", "sdm120", "sim_meter"} <= set(keys)
    s = client.post("/api/sites", json={"name": "템플릿"}, headers=a["operator"]).json()
    d = client.post("/api/devices", json={"site_id": s["id"], "name": "PZEM", "protocol": "modbus_rtu", "serial_port": "COM3",
                                          "template": "pzem016"}, headers=a["operator"]).json()
    tags = [t for t in client.get(f"/api/tags?device_id={d['id']}", headers=a["viewer"]).json()]
    cur = next(t for t in tags if t["name"] == "전류")
    assert len(tags) == 6 and cur["function_code"] == 4 and cur["data_type"] == "uint32" and cur["word_order"] == "little"
    assert client.post("/api/devices", json={"site_id": s["id"], "name": "x", "protocol": "modbus_tcp", "host": "10.0.0.3",
                                             "template": "pzem016"}, headers=a["operator"]).status_code == 400


def test_pzem016_register_decoding():
    """PZEM-016 sample frame: 230.1V, 1.234A, 283.9W, 12345Wh — decoded with the template settings."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gateway"))
    from codec import decode, engineering
    from eumbms.templates import TEMPLATES
    regs = [2301, 1234, 0, 2839, 0, 12345, 0, 600, 92, 0]
    got = {}
    for t in TEMPLATES["pzem016"]["tags"]:
        raw = decode(regs[t["address"]:], t["data_type"], t.get("word_order", "big"))
        got[t["name"]] = engineering(raw, t.get("scale", 1), 0)
    assert abs(got["전압"] - 230.1) < 1e-6 and abs(got["전류"] - 1.234) < 1e-6
    assert abs(got["유효전력"] - 0.2839) < 1e-6 and abs(got["누적전력량"] - 12.345) < 1e-6
    assert abs(got["주파수"] - 60.0) < 1e-6 and abs(got["역률"] - 0.92) < 1e-6


def test_gateway_config_excludes_simulators(client, tenant_pair):
    a = tenant_pair["a"]
    s = client.post("/api/sites", json={"name": "혼합"}, headers=a["operator"]).json()
    g = client.post("/api/gateways", json={"site_id": s["id"], "name": "gw"}, headers=a["tenant_admin"]).json()
    client.post("/api/devices", json={"site_id": s["id"], "gateway_id": g["id"], "name": "sim", "protocol": "simulator"}, headers=a["operator"])
    cfg = client.get("/api/gw/config", headers={"X-Gateway-Token": g["token"]}).json()
    assert cfg["devices"] == []


def test_site_main_meter_patch(client, tenant_pair):
    a, b = tenant_pair["a"], tenant_pair["b"]
    s1 = client.post("/api/sites", json={"name": "대표1"}, headers=a["operator"]).json()
    s2 = client.post("/api/sites", json={"name": "대표2"}, headers=a["operator"]).json()
    d2 = client.post("/api/devices", json={"site_id": s2["id"], "name": "m", "protocol": "simulator", "template": "sim_meter"}, headers=a["operator"]).json()
    t2 = next(t for t in client.get(f"/api/tags?device_id={d2['id']}", headers=a["viewer"]).json() if t["category"] == "power")
    assert client.patch(f"/api/sites/{s1['id']}", json={"main_power_tag_id": t2["id"]}, headers=a["operator"]).status_code == 400
    r = client.patch(f"/api/sites/{s2['id']}", json={"main_power_tag_id": t2["id"]}, headers=a["operator"])
    assert r.status_code == 200 and r.json()["main_power_tag_id"] == t2["id"]
    assert client.patch(f"/api/sites/{s2['id']}", json={"main_power_tag_id": t2["id"]}, headers=b["operator"]).status_code == 404
    assert client.patch(f"/api/sites/{s2['id']}", json={"main_power_tag_id": None}, headers=a["viewer"]).status_code == 403
