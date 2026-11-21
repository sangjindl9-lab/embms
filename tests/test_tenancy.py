"""Tenant isolation and RBAC — cross-tenant access must be impossible."""


def _seed(client, t):
    s = client.post("/api/sites", json={"name": "본사"}, headers=t["operator"]).json()
    d = client.post("/api/devices", json={"site_id": s["id"], "name": "MCCB-1", "host": "192.168.10.20"}, headers=t["operator"]).json()
    tag = client.post("/api/tags", json={"device_id": d["id"], "name": "kW", "address": 0}, headers=t["operator"]).json()
    return s, d, tag


def test_cross_tenant_read_write_blocked(client, tenant_pair):
    a, b = tenant_pair["a"], tenant_pair["b"]
    s, d, tag = _seed(client, a)
    # B sees nothing of A
    for path in ("/api/sites", "/api/devices", "/api/tags", "/api/gateways"):
        ids = [x["id"] for x in client.get(path, headers=b["tenant_admin"]).json()]
        assert s["id"] not in ids and d["id"] not in ids and tag["id"] not in ids, path
    assert client.get(f"/api/sites/{s['id']}", headers=b["tenant_admin"]).status_code == 404
    assert client.get(f"/api/tags/{tag['id']}/readings", headers=b["viewer"]).status_code == 404
    # B cannot attach things to A's objects
    assert client.post("/api/devices", json={"site_id": s["id"], "name": "x", "host": "10.0.0.1"}, headers=b["operator"]).status_code == 404
    assert client.post("/api/tags", json={"device_id": d["id"], "name": "x", "address": 1}, headers=b["operator"]).status_code == 404
    assert client.post("/api/buildings", json={"site_id": s["id"], "name": "x"}, headers=b["operator"]).status_code == 404
    assert client.patch(f"/api/devices/{d['id']}", json={"enabled": True}, headers=b["tenant_admin"]).status_code == 404
    assert client.delete(f"/api/devices/{d['id']}", headers=b["tenant_admin"]).status_code == 404
    assert client.post("/api/gateways", json={"site_id": s["id"], "name": "x"}, headers=b["tenant_admin"]).status_code == 404
    # B cannot write into A by passing tenant_id
    assert client.post("/api/sites", json={"name": "evil", "tenant_id": a["id"]}, headers=b["operator"]).status_code == 404
    assert client.post("/api/users", json={"username": "evilx", "password": "password-123456", "role": "viewer", "tenant_id": a["id"]}, headers=b["tenant_admin"]).status_code == 404
    # A still sees its own
    assert any(x["id"] == s["id"] for x in client.get("/api/sites", headers=a["viewer"]).json())


def test_users_and_audit_scoped(client, tenant_pair):
    a, b = tenant_pair["a"], tenant_pair["b"]
    _seed(client, a)
    users_b = client.get("/api/users", headers=b["tenant_admin"]).json()
    assert users_b and all(u["tenant_id"] == b["id"] for u in users_b)
    audit_b = client.get("/api/audit", headers=b["tenant_admin"]).json()
    assert all(x["tenant_id"] == b["id"] for x in audit_b)
    assert any(x["action"] == "site.create" for x in client.get("/api/audit", headers=a["tenant_admin"]).json())
    a_user = next(u for u in client.get("/api/users", headers=a["tenant_admin"]).json())
    assert client.patch(f"/api/users/{a_user['id']}", json={"active": False}, headers=b["tenant_admin"]).status_code == 404
    tenants = client.get("/api/tenants", headers=b["viewer"]).json()
    assert [t["id"] for t in tenants] == [b["id"]]


def test_rbac(client, tenant_pair):
    a = tenant_pair["a"]
    assert client.post("/api/sites", json={"name": "v"}, headers=a["viewer"]).status_code == 403
    assert client.get("/api/users", headers=a["operator"]).status_code == 403
    assert client.get("/api/audit", headers=a["viewer"]).status_code == 403
    assert client.post("/api/tenants", json={"name": "t"}, headers=a["tenant_admin"]).status_code == 403
    assert client.post("/api/users", json={"username": "wannabe", "password": "password-123456", "role": "superadmin"}, headers=a["tenant_admin"]).status_code == 403
    assert client.delete("/api/devices/999999", headers=a["operator"]).status_code == 403


def test_superadmin_must_choose_tenant(client, root, tenant_pair):
    assert client.post("/api/sites", json={"name": "no-tenant"}, headers=root).status_code == 400
    r = client.post("/api/sites", json={"name": "by-root", "tenant_id": tenant_pair["a"]["id"]}, headers=root)
    assert r.status_code == 201 and r.json()["tenant_id"] == tenant_pair["a"]["id"]


def test_device_input_validation(client, tenant_pair):
    a = tenant_pair["a"]
    s = client.post("/api/sites", json={"name": "검증"}, headers=a["operator"]).json()
    for host in ("8.8.8.8", "127.0.0.1", "169.254.1.1", "plc.example.com"):
        assert client.post("/api/devices", json={"site_id": s["id"], "name": "x", "host": host}, headers=a["operator"]).status_code == 422, host
    d = client.post("/api/devices", json={"site_id": s["id"], "name": "ok", "host": "10.1.2.3"}, headers=a["operator"]).json()
    assert d["enabled"] is False and d["control_enabled"] is False
    assert client.post("/api/tags", json={"device_id": d["id"], "name": "w", "address": 0, "function_code": 6}, headers=a["operator"]).status_code == 422
    assert client.post("/api/tags", json={"device_id": d["id"], "name": "c", "address": 0, "function_code": 1, "data_type": "uint16"}, headers=a["operator"]).status_code == 422
