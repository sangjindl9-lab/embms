"""Edge gateway registration (users) and the gateway-facing ingest API.

Field gateways connect OUTBOUND over HTTPS to this server; the server never
opens connections to PLC/RTU devices. There is intentionally no write/command
endpoint: remote control stays disabled until P6 safety approval."""
import datetime as dt
import math

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import config
from ..alarms import evaluate_value
from ..audit import audit
from ..db import get_db
from ..deps import Principal, client_ip, get_owned, require_role, tenant_filter
from ..models import Device, Gateway, Reading, Site, Tag, utcnow
from ..security import new_token, token_hash

router = APIRouter(prefix="/api", tags=["gateways"])


class GatewayIn(BaseModel):
    site_id: int
    name: str = Field(min_length=1, max_length=200)


class ReadingIn(BaseModel):
    tag_id: int
    value: float | None = None
    quality: str = Field(default="Good", pattern="^(Good|Bad|Uncertain)$")
    ts: dt.datetime


class ReadingBatch(BaseModel):
    agent_version: str = Field(default="", max_length=40)
    readings: list[ReadingIn] = Field(max_length=5000)


def _gw(g: Gateway):
    return {"id": g.id, "tenant_id": g.tenant_id, "site_id": g.site_id, "name": g.name, "active": g.active,
            "last_seen_at": g.last_seen_at, "agent_version": g.agent_version}


@router.get("/gateways")
def list_gateways(p: Principal = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    return [_gw(g) for g in db.scalars(tenant_filter(select(Gateway), Gateway, p).order_by(Gateway.id))]


@router.post("/gateways", status_code=201)
def create_gateway(data: GatewayIn, request: Request, p: Principal = Depends(require_role("tenant_admin")), db: Session = Depends(get_db)):
    s = get_owned(db, Site, data.site_id, p)
    token = "gw_" + new_token()
    g = Gateway(tenant_id=s.tenant_id, site_id=s.id, name=data.name, token_hash=token_hash(token))
    db.add(g); db.flush()
    audit(db, "gateway.create", actor=p.user, tenant_id=s.tenant_id, target=f"gateway:{g.id}", ip=client_ip(request))
    db.commit()
    return {**_gw(g), "token": token, "note": "토큰은 지금 한 번만 표시됩니다. 게이트웨이 환경변수에만 저장하세요."}


@router.post("/gateways/{gateway_id}/rotate-token")
def rotate_gateway_token(gateway_id: int, request: Request, p: Principal = Depends(require_role("tenant_admin")), db: Session = Depends(get_db)):
    g = get_owned(db, Gateway, gateway_id, p)
    token = "gw_" + new_token()
    g.token_hash = token_hash(token)
    audit(db, "gateway.rotate_token", actor=p.user, tenant_id=g.tenant_id, target=f"gateway:{g.id}", ip=client_ip(request))
    db.commit()
    return {"id": g.id, "token": token}


# ---------------- gateway-facing endpoints
def current_gateway(x_gateway_token: str = Header(default=""), db: Session = Depends(get_db)) -> Gateway:
    if not x_gateway_token:
        raise HTTPException(401, "Gateway token required")
    g = db.scalar(select(Gateway).where(Gateway.token_hash == token_hash(x_gateway_token)))
    if not g or not g.active:
        raise HTTPException(401, "Invalid gateway token")
    return g


@router.get("/gw/config")
def gateway_config(g: Gateway = Depends(current_gateway), db: Session = Depends(get_db)):
    g.last_seen_at = utcnow(); db.commit()
    devices = db.scalars(select(Device).where(Device.gateway_id == g.id, Device.tenant_id == g.tenant_id,
                                             Device.protocol != "simulator")).all()
    out = []
    for d in devices:
        tags = db.scalars(select(Tag).where(Tag.device_id == d.id)).all()
        out.append({"id": d.id, "name": d.name, "protocol": d.protocol, "host": d.host, "port": d.port,
                    "unit_id": d.unit_id, "serial_port": d.serial_port, "baudrate": d.baudrate,
                    "poll_interval_s": d.poll_interval_s, "enabled": d.enabled, "control_enabled": False,
                    "tags": [{"id": t.id, "name": t.name, "function_code": t.function_code, "address": t.address,
                              "data_type": t.data_type, "word_order": t.word_order, "byte_order": t.byte_order,
                              "scale": t.scale, "offset": t.offset} for t in tags]})
    return {"gateway_id": g.id, "remote_control_enabled": config.REMOTE_CONTROL_ENABLED, "devices": out}


@router.post("/gw/readings")
def gateway_readings(batch: ReadingBatch, g: Gateway = Depends(current_gateway), db: Session = Depends(get_db)):
    allowed = {t.id: t for t in db.scalars(
        select(Tag).join(Device, Device.id == Tag.device_id).where(Device.gateway_id == g.id, Tag.tenant_id == g.tenant_id))}
    rejected, inserted, duplicates = 0, 0, 0
    now = utcnow()
    by_tag: dict[int, list[ReadingIn]] = {}
    for r in batch.readings:
        if r.tag_id not in allowed:
            rejected += 1
            continue
        ts = r.ts if r.ts.tzinfo else r.ts.replace(tzinfo=dt.timezone.utc)
        if ts > now + dt.timedelta(minutes=5):  # clock skew guard
            rejected += 1
            continue
        if r.value is not None and not math.isfinite(r.value):
            r.value, r.quality = None, "Bad"
        r.ts = ts
        by_tag.setdefault(r.tag_id, []).append(r)
    for tag_id, rows in by_tag.items():
        # idempotent re-upload: skip (tag, ts) pairs already stored
        tss = [r.ts for r in rows]
        existing = {(e if e.tzinfo else e.replace(tzinfo=dt.timezone.utc))
                    for e in db.scalars(select(Reading.ts).where(Reading.tag_id == tag_id, Reading.ts >= min(tss), Reading.ts <= max(tss)))}
        latest = None
        for r in rows:
            if r.ts in existing:
                duplicates += 1
                continue
            existing.add(r.ts)
            db.add(Reading(tenant_id=g.tenant_id, tag_id=tag_id, value=r.value, quality=r.quality, ts=r.ts))
            inserted += 1
            if latest is None or r.ts > latest.ts:
                latest = r
        t = allowed[tag_id]
        cur = t.source_ts if (t.source_ts is None or t.source_ts.tzinfo) else t.source_ts.replace(tzinfo=dt.timezone.utc)
        if latest and (cur is None or latest.ts >= cur):
            t.value, t.quality, t.source_ts, t.updated_at = latest.value, latest.quality, latest.ts, now
            evaluate_value(db, t, latest.value, latest.quality)
    g.last_seen_at = now
    if batch.agent_version:
        g.agent_version = batch.agent_version
    db.commit()
    return {"inserted": inserted, "duplicates": duplicates, "rejected": rejected}
