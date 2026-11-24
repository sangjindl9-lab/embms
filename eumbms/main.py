import asyncio
import datetime as dt
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text

from . import config
from .db import SessionLocal, engine
from .models import User
from .routers import admin, assets, auth, gateways, scada
from .security import hash_password

log = logging.getLogger("eumbms")
STATIC = Path(__file__).parent / "static"

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


def bootstrap_admin():
    """Create the first platform admin from ADMIN_USER/ADMIN_PASSWORD if no superadmin exists.
    Never overwrites an existing account."""
    with SessionLocal() as db:
        if db.scalar(select(User.id).where(User.role == "superadmin").limit(1)):
            return
        user, pwd = config.BOOTSTRAP_ADMIN_USER.strip().lower(), config.BOOTSTRAP_ADMIN_PASSWORD
        if not user or len(pwd) < 20:
            raise RuntimeError("No superadmin exists: set ADMIN_USER and ADMIN_PASSWORD (min 20 chars) for bootstrap")
        if db.scalar(select(User.id).where(User.username == user)):
            raise RuntimeError("Bootstrap username already used by a non-admin account")
        db.add(User(username=user, password_hash=hash_password(pwd), role="superadmin", tenant_id=None))
        db.commit()
        log.warning("bootstrap superadmin '%s' created", user)


RETENTION_DAYS = int(os.getenv("READINGS_RETENTION_DAYS", "90"))


def background_tick(now=None):
    """One pass of server-side housekeeping: virtual devices, comm-loss alarms, retention."""
    from .alarms import check_comm
    from .models import AlarmEvent, Reading, utcnow
    from .simulator import run_simulators
    now = now or utcnow()
    with SessionLocal() as db:
        run_simulators(db, now)
        check_comm(db, now)
        if now.minute == 0 and now.second < 20:
            db.query(Reading).filter(Reading.ts < now - dt.timedelta(days=RETENTION_DAYS)).delete()
            db.query(AlarmEvent).filter(AlarmEvent.active.is_(False),
                                        AlarmEvent.raised_at < now - dt.timedelta(days=365)).delete()
        db.commit()


async def background_loop():
    while True:
        try:
            await asyncio.to_thread(background_tick)
        except Exception:
            log.exception("background tick failed")
        await asyncio.sleep(15)


@asynccontextmanager
async def lifespan(app):
    bootstrap_admin()
    task = asyncio.create_task(background_loop()) if os.getenv("BACKGROUND_TASKS", "true") == "true" else None
    yield
    if task:
        task.cancel()


app = FastAPI(title="EUM BMS PRO", version=config.VERSION, lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    h = response.headers
    h.setdefault("Cache-Control", "no-store")
    h["X-Content-Type-Options"] = "nosniff"
    h["X-Frame-Options"] = "DENY"
    h["Referrer-Policy"] = "no-referrer"
    h["Content-Security-Policy"] = CSP
    h["Strict-Transport-Security"] = "max-age=31536000"
    h["Permissions-Policy"] = "geolocation=(), camera=(), microphone=()"
    return response


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse({"detail": "Internal server error"}, status_code=500)


@app.get("/health")
def health():
    db_ok, revision = False, None
    try:
        with engine.connect() as c:
            c.execute(text("SELECT 1"))
            revision = c.execute(text("SELECT version_num FROM alembic_version")).scalar()
            db_ok = True
    except Exception as exc:  # report, don't leak details
        log.error("health db check failed: %s", type(exc).__name__)
    body = {"status": "ok" if db_ok else "degraded", "version": config.VERSION, "database": "ok" if db_ok else "error",
            "schema_revision": revision, "remote_control": "disabled"}
    return JSONResponse(body, status_code=200 if db_ok else 503)


for r in (auth.router, admin.router, assets.router, gateways.router, scada.router):
    app.include_router(r)

app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")
