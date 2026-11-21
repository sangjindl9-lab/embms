import datetime as dt


def _setup(client, t):
    s = client.post("/api/sites", json={"name": "GW현장"}, headers=t["operator"]).json()
    g = client.post("/api/gateways", json={"site_id": s["id"], "name": "edge-1"}, headers=t["tenant_admin"]).json()
    d = client.post("/api/devices", json={"site_id": s["id"], "gateway_id": g["id"], "name": "meter", "host": "10.0.0.5"}, headers=t["operator"]).json()
    client.patch(f"/api/devices/{d['id']}", json={"enabled": True}, headers=t["operator"])
    tag = client.post("/api/tags", json={"device_id": d["id"], "name": "V", "address": 0, "data_type": "float32"}, headers=t["operator"]).json()
    return s, g, d, tag


def test_gateway_flow(client, tenant_pair):
    a, b = tenant_pair["a"], tenant_pair["b"]
    s, g, d, tag = _setup(client, a)
    _, gb, _, tag_b = _setup(client, b)
    H = {"X-Gateway-Token": g["token"]}
    assert client.get("/api/gw/config").status_code == 401
    assert client.get("/api/gw/config", headers={"X-Gateway-Token": "gw_bad"}).status_code == 401
    cfg = client.get("/api/gw/config", headers=H).json()
    assert cfg["remote_control_enabled"] is False
    assert [x["id"] for x in cfg["devices"]] == [d["id"]] and cfg["devices"][0]["tags"][0]["data_type"] == "float32"
    ts = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    body = {"agent_version": "test", "readings": [
        {"tag_id": tag["id"], "value": 220.5, "ts": ts.isoformat()},
        {"tag_id": tag["id"], "value": 221.0, "ts": (ts + dt.timedelta(seconds=1)).isoformat()},
        {"tag_id": tag_b["id"], "value": 1, "ts": ts.isoformat()},  # other tenant's tag
        {"tag_id": tag["id"], "value": 1, "ts": (ts + dt.timedelta(days=1)).isoformat()},  # future
    ]}
    assert client.post("/api/gw/readings", json=body, headers=H).json() == {"inserted": 2, "duplicates": 0, "rejected": 2}
    # idempotent retry
    assert client.post("/api/gw/readings", json=body, headers=H).json()["duplicates"] == 2
    t = next(x for x in client.get("/api/tags", headers=a["viewer"]).json() if x["id"] == tag["id"])
    assert t["value"] == 221.0 and t["quality"] == "Good"
    hist = client.get(f"/api/tags/{tag['id']}/readings", headers=a["viewer"]).json()
    assert [h["value"] for h in hist] == [221.0, 220.5]
    # tenant B's data untouched
    tb = next(x for x in client.get("/api/tags", headers=b["viewer"]).json() if x["id"] == tag_b["id"])
    assert tb["value"] is None
    # token rotation invalidates old token
    client.post(f"/api/gateways/{g['id']}/rotate-token", headers=a["tenant_admin"])
    assert client.get("/api/gw/config", headers=H).status_code == 401


def test_gateway_token_not_stored_plain(client, tenant_pair):
    a = tenant_pair["a"]
    _, g, _, _ = _setup(client, a)
    from eumbms.db import SessionLocal
    from eumbms.models import Gateway
    with SessionLocal() as db:
        assert db.get(Gateway, g["id"]).token_hash != g["token"]
    assert "token" not in client.get("/api/gateways", headers=a["viewer"]).json()[0]
