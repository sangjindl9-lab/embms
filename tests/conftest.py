import os
import sys
import tempfile
from pathlib import Path

_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL", f"sqlite:///{_tmp}/test.db")
os.environ["ADMIN_USER"] = "root"
os.environ["ADMIN_PASSWORD"] = "test-bootstrap-password-0123456789"
os.environ["COOKIE_SECURE"] = "false"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

command.upgrade(Config(str(ROOT / "alembic.ini")), "head")

from fastapi.testclient import TestClient  # noqa: E402
from eumbms.main import app  # noqa: E402

ROOT_PW = os.environ["ADMIN_PASSWORD"]


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


def login(client, username, password):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    client.cookies.clear()  # use bearer tokens in tests unless a test wants cookies
    return {"Authorization": "Bearer " + r.json()["access_token"]}


@pytest.fixture(scope="session")
def root(client):
    return login(client, "root", ROOT_PW)


_n = [0]


@pytest.fixture
def tenant_pair(client, root):
    """Two tenants, each with an admin, operator and viewer."""
    _n[0] += 1
    out = {}
    for label in ("a", "b"):
        name = f"tenant-{label}-{_n[0]}"
        tid = client.post("/api/tenants", json={"name": name}, headers=root).json()["id"]
        users = {}
        for role in ("tenant_admin", "operator", "viewer"):
            u = f"{label}{_n[0]}-{role}".replace("_", "")
            r = client.post("/api/users", json={"username": u, "password": "password-for-" + u, "role": role, "tenant_id": tid}, headers=root)
            assert r.status_code == 201, r.text
            users[role] = login(client, u, "password-for-" + u)
        out[label] = {"id": tid, **users}
    return out


@pytest.fixture(autouse=True)
def _reset_login_attempts():
    yield
    from eumbms.db import SessionLocal
    from eumbms.models import LoginAttempt
    with SessionLocal() as db:
        db.query(LoginAttempt).delete(); db.commit()
