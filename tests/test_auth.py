from conftest import ROOT_PW


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["database"] == "ok" and body["schema_revision"] == "0003" and body["remote_control"] == "disabled"


def test_unauthenticated_401(client):
    client.cookies.clear()
    for path in ("/api/sites", "/api/devices", "/api/tags", "/api/users", "/api/audit", "/api/auth/me"):
        assert client.get(path).status_code == 401, path


def test_bad_token_and_bad_password_401(client):
    client.cookies.clear()
    assert client.get("/api/sites", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/api/sites", headers={"Authorization": "Basic cm9vdDp4"}).status_code == 401
    assert client.post("/api/auth/login", json={"username": "root", "password": "wrong"}).status_code == 401


def test_admin_login_200(client, root):
    assert client.get("/api/sites", headers=root).status_code == 200
    me = client.get("/api/auth/me", headers=root).json()
    assert me["role"] == "superadmin" and me["tenant_id"] is None


def test_password_hash_not_plaintext():
    from eumbms.db import SessionLocal
    from eumbms.models import User
    from sqlalchemy import select
    with SessionLocal() as db:
        u = db.scalar(select(User).where(User.username == "root"))
        assert u.password_hash.startswith("scrypt$") and ROOT_PW not in u.password_hash


def test_login_throttle_db_backed(client):
    for _ in range(10):
        assert client.post("/api/auth/login", json={"username": "ghost-user", "password": "x"}).status_code == 401
    r = client.post("/api/auth/login", json={"username": "ghost-user", "password": "x"})
    assert r.status_code == 429 and r.headers["Retry-After"]
    from eumbms.db import SessionLocal
    from eumbms.models import LoginAttempt
    from sqlalchemy import select, func
    with SessionLocal() as db:  # shared state lives in DB, not process memory
        assert db.scalar(select(func.count()).select_from(LoginAttempt).where(LoginAttempt.username == "ghost-user")) == 10


def test_xff_spoof_does_not_bypass_username_throttle(client):
    for i in range(10):
        client.post("/api/auth/login", json={"username": "ghost2", "password": "x"}, headers={"X-Forwarded-For": f"1.2.3.{i}"})
    assert client.post("/api/auth/login", json={"username": "ghost2", "password": "x"}, headers={"X-Forwarded-For": "9.9.9.9"}).status_code == 429


def test_cookie_session_requires_csrf(client, tenant_pair):
    a = tenant_pair["a"]
    # log in via cookie as a fresh operator
    client.cookies.clear()
    client.post("/api/users", json={"username": f"cookieop{a['id']}", "password": "cookie-password-1", "role": "operator", "tenant_id": a["id"]}, headers=a["tenant_admin"])
    r = client.post("/api/auth/login", json={"username": f"cookieop{a['id']}", "password": "cookie-password-1"})
    csrf = r.json()["csrf_token"]
    assert "eum_session" in client.cookies
    assert "httponly" in r.headers["set-cookie"].lower() and "samesite=strict" in r.headers["set-cookie"].lower()
    assert client.get("/api/sites").status_code == 200
    assert client.post("/api/sites", json={"name": "no-csrf"}).status_code == 403
    assert client.post("/api/sites", json={"name": "with-csrf"}, headers={"X-CSRF-Token": csrf}).status_code == 201
    assert client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf}).status_code == 200
    assert client.get("/api/sites").status_code == 401
    client.cookies.clear()


def test_security_headers_no_unsafe_inline(client):
    r = client.get("/")
    assert r.status_code == 200
    csp = r.headers["content-security-policy"]
    assert "unsafe-inline" not in csp and "frame-ancestors 'none'" in csp
    assert r.headers["x-frame-options"] == "DENY"
    assert "<script>" not in r.text and "style=" not in r.text and "onclick" not in r.text


def test_password_change_revokes_other_sessions(client, tenant_pair):
    a = tenant_pair["a"]
    client.post("/api/users", json={"username": f"pwuser{a['id']}", "password": "original-password", "role": "viewer", "tenant_id": a["id"]}, headers=a["tenant_admin"])
    from conftest import login
    s1 = login(client, f"pwuser{a['id']}", "original-password")
    s2 = login(client, f"pwuser{a['id']}", "original-password")
    assert client.post("/api/auth/password", json={"current_password": "original-password", "new_password": "brand-new-password"}, headers=s1).status_code == 200
    assert client.get("/api/sites", headers=s2).status_code == 401
    assert client.get("/api/sites", headers=s1).status_code == 200


def test_ip_throttle(client, monkeypatch):
    from eumbms import config
    monkeypatch.setattr(config, "LOGIN_MAX_FAILURES_IP", 5)
    for i in range(5):
        client.post("/api/auth/login", json={"username": f"spray{i}", "password": "x"})
    assert client.post("/api/auth/login", json={"username": "spray-new", "password": "x"}).status_code == 429
