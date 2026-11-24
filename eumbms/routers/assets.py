"""Sites, buildings, devices, tags and readings — all tenant-scoped."""
import datetime as dt
import ipaddress

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..audit import audit
from ..db import get_db
from ..deps import Principal, client_ip, get_owned, require_role, resolve_tenant, tenant_filter
from ..models import Building, Device, Gateway, Reading, Site, Tag

router = APIRouter(prefix="/api", tags=["assets"])

DATA_TYPES = ("bool", "uint16", "int16", "uint32", "int32", "float32", "uint64", "int64", "float64")


class SiteIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    address: str = Field(default="", max_length=300)
    tenant_id: int | None = None


class BuildingIn(BaseModel):
    site_id: int
    name: str = Field(min_length=1, max_length=200)


class DeviceIn(BaseModel):
    site_id: int
    gateway_id: int | None = None
    name: str = Field(min_length=1, max_length=200)
    protocol: str = Field(default="modbus_tcp", pattern="^(modbus_tcp|modbus_rtu|simulator)$")
    host: str = Field(default="", max_length=255)
    port: int = Field(default=502, ge=1, le=65535)
    unit_id: int = Field(default=1, ge=0, le=247)
    serial_port: str = Field(default="", max_length=100)
    baudrate: int = Field(default=9600, ge=1200, le=115200)
    template: str | None = Field(default=None, max_length=40)
    poll_interval_s: int = Field(default=10, ge=1, le=3600)

    @field_validator("host")
    @classmethod
    def private_only(cls, v: str):
        # Central-server sanity check only. Authoritative allow-listing happens on the gateway.
        if v:
            try:
                ip = ipaddress.ip_address(v)
            except ValueError:
                raise ValueError("host must be an IP address")
            if not ip.is_private or ip.is_loopback or ip.is_link_local:
                raise ValueError("only private (field network) IP addresses are permitted")
        return v


class DevicePatch(BaseModel):
    enabled: bool | None = None
    gateway_id: int | None = None
    poll_interval_s: int | None = Field(default=None, ge=1, le=3600)


class TagIn(BaseModel):
    device_id: int
    name: str = Field(min_length=1, max_length=200)
    function_code: int = Field(default=3)
    address: int = Field(ge=0, le=65535)
    data_type: str = Field(default="uint16")
    word_order: str = Field(default="big", pattern="^(big|little)$")
    byte_order: str = Field(default="big", pattern="^(big|little)$")
    scale: float = Field(default=1.0, ge=-1e6, le=1e6)
    offset: float = Field(default=0.0, ge=-1e9, le=1e9)
    unit: str = Field(default="", max_length=20)
    category: str = Field(default="other", pattern="^(voltage|current|power|energy|pf|frequency|temperature|state|other)$")

    @field_validator("function_code")
    @classmethod
    def fc(cls, v):
        if v not in (1, 2, 3, 4):
            raise ValueError("function_code must be 1, 2, 3 or 4 (read-only functions)")
        return v

    @field_validator("data_type")
    @classmethod
    def dtype(cls, v):
        if v not in DATA_TYPES:
            raise ValueError(f"data_type must be one of {DATA_TYPES}")
        return v


def _site(s: Site):
    return {"id": s.id, "tenant_id": s.tenant_id, "name": s.name, "address": s.address,
            "main_power_tag_id": s.main_power_tag_id, "main_energy_tag_id": s.main_energy_tag_id}


class SitePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    address: str | None = Field(default=None, max_length=300)
    main_power_tag_id: int | None = None
    main_energy_tag_id: int | None = None


def _device(d: Device):
    return {"id": d.id, "tenant_id": d.tenant_id, "site_id": d.site_id, "gateway_id": d.gateway_id, "name": d.name,
            "protocol": d.protocol, "host": d.host, "port": d.port, "unit_id": d.unit_id, "serial_port": d.serial_port,
            "baudrate": d.baudrate, "poll_interval_s": d.poll_interval_s, "enabled": d.enabled,
            "control_enabled": False}


def _tag(t: Tag):
    return {"id": t.id, "tenant_id": t.tenant_id, "device_id": t.device_id, "name": t.name,
            "function_code": t.function_code, "address": t.address, "data_type": t.data_type,
            "word_order": t.word_order, "byte_order": t.byte_order, "scale": t.scale, "offset": t.offset,
            "unit": t.unit, "category": t.category, "value": t.value, "quality": t.quality, "source_ts": t.source_ts, "updated_at": t.updated_at}


# ---- sites
@router.get("/sites")
def list_sites(tenant_id: int | None = None, p: Principal = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    return [_site(s) for s in db.scalars(tenant_filter(select(Site), Site, p, tenant_id).order_by(Site.id))]


@router.post("/sites", status_code=201)
def create_site(data: SiteIn, request: Request, p: Principal = Depends(require_role("operator")), db: Session = Depends(get_db)):
    tid = resolve_tenant(p, data.tenant_id)
    if db.scalar(select(Site).where(Site.tenant_id == tid, Site.name == data.name)):
        raise HTTPException(409, "Site name already exists")
    s = Site(tenant_id=tid, name=data.name, address=data.address)
    db.add(s); db.flush()
    audit(db, "site.create", actor=p.user, tenant_id=tid, target=f"site:{s.id}", detail={"name": s.name}, ip=client_ip(request))
    db.commit()
    return _site(s)


@router.get("/sites/{site_id}")
def get_site(site_id: int, p: Principal = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    return _site(get_owned(db, Site, site_id, p))


@router.patch("/sites/{site_id}")
def update_site(site_id: int, data: SitePatch, request: Request, p: Principal = Depends(require_role("operator")), db: Session = Depends(get_db)):
    s = get_owned(db, Site, site_id, p)
    changes = data.model_dump(exclude_unset=True)
    for k in ("main_power_tag_id", "main_energy_tag_id"):
        if changes.get(k) is not None:
            t = get_owned(db, Tag, changes[k], p)
            dev = db.get(Device, t.device_id)
            if dev.site_id != s.id:
                raise HTTPException(400, "Tag belongs to a different site")
    for k, v in changes.items():
        setattr(s, k, v)
    audit(db, "site.update", actor=p.user, tenant_id=s.tenant_id, target=f"site:{s.id}", detail=changes, ip=client_ip(request))
    db.commit()
    return _site(s)


@router.delete("/sites/{site_id}")
def delete_site(site_id: int, request: Request, p: Principal = Depends(require_role("tenant_admin")), db: Session = Depends(get_db)):
    s = get_owned(db, Site, site_id, p)
    if db.scalar(select(Device.id).where(Device.site_id == s.id).limit(1)):
        raise HTTPException(409, "Site has devices; remove them first")
    for b in db.scalars(select(Building).where(Building.site_id == s.id)):
        db.delete(b)
    db.delete(s)
    audit(db, "site.delete", actor=p.user, tenant_id=s.tenant_id, target=f"site:{s.id}", ip=client_ip(request))
    db.commit()
    return {"ok": True}


# ---- buildings
@router.get("/buildings")
def list_buildings(site_id: int | None = None, p: Principal = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    stmt = tenant_filter(select(Building), Building, p)
    if site_id is not None:
        stmt = stmt.where(Building.site_id == site_id)
    return [{"id": b.id, "site_id": b.site_id, "name": b.name} for b in db.scalars(stmt.order_by(Building.id))]


@router.post("/buildings", status_code=201)
def create_building(data: BuildingIn, request: Request, p: Principal = Depends(require_role("operator")), db: Session = Depends(get_db)):
    s = get_owned(db, Site, data.site_id, p)
    b = Building(tenant_id=s.tenant_id, site_id=s.id, name=data.name)
    db.add(b); db.flush()
    audit(db, "building.create", actor=p.user, tenant_id=s.tenant_id, target=f"building:{b.id}", ip=client_ip(request))
    db.commit()
    return {"id": b.id, "site_id": b.site_id, "name": b.name}


# ---- devices
@router.get("/devices")
def list_devices(site_id: int | None = None, p: Principal = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    stmt = tenant_filter(select(Device), Device, p)
    if site_id is not None:
        stmt = stmt.where(Device.site_id == site_id)
    return [_device(d) for d in db.scalars(stmt.order_by(Device.id))]


@router.post("/devices", status_code=201)
def create_device(data: DeviceIn, request: Request, p: Principal = Depends(require_role("operator")), db: Session = Depends(get_db)):
    s = get_owned(db, Site, data.site_id, p)
    if data.gateway_id is not None:
        g = get_owned(db, Gateway, data.gateway_id, p)
        if g.site_id != s.id:
            raise HTTPException(400, "Gateway belongs to a different site")
    if data.protocol == "modbus_tcp" and not data.host:
        raise HTTPException(422, "host is required for modbus_tcp")
    if data.protocol == "modbus_rtu" and not data.serial_port:
        raise HTTPException(422, "serial_port is required for modbus_rtu")
    from ..templates import TEMPLATES, tag_defaults
    tpl = TEMPLATES.get(data.template) if data.template else None
    if data.template and not tpl:
        raise HTTPException(404, "Template not found")
    if tpl and tpl["protocol"] != data.protocol:
        raise HTTPException(400, f"이 템플릿은 {tpl['protocol']} 장치용입니다")
    d = Device(tenant_id=s.tenant_id, enabled=False, control_enabled=False, **data.model_dump(exclude={"template"}))
    db.add(d); db.flush()
    if tpl:
        for t in tpl["tags"]:
            db.add(Tag(tenant_id=d.tenant_id, device_id=d.id, **tag_defaults(t)))
    audit(db, "device.create", actor=p.user, tenant_id=s.tenant_id, target=f"device:{d.id}", detail=data.model_dump(), ip=client_ip(request))
    db.commit()
    return _device(d)


@router.patch("/devices/{device_id}")
def update_device(device_id: int, data: DevicePatch, request: Request, p: Principal = Depends(require_role("operator")), db: Session = Depends(get_db)):
    d = get_owned(db, Device, device_id, p)
    changes = data.model_dump(exclude_unset=True)
    if "gateway_id" in changes and changes["gateway_id"] is not None:
        g = get_owned(db, Gateway, changes["gateway_id"], p)
        if g.site_id != d.site_id:
            raise HTTPException(400, "Gateway belongs to a different site")
    for k, v in changes.items():
        setattr(d, k, v)
    audit(db, "device.update", actor=p.user, tenant_id=d.tenant_id, target=f"device:{d.id}", detail=changes, ip=client_ip(request))
    db.commit()
    return _device(d)


@router.delete("/devices/{device_id}")
def delete_device(device_id: int, request: Request, p: Principal = Depends(require_role("tenant_admin")), db: Session = Depends(get_db)):
    d = get_owned(db, Device, device_id, p)
    for t in db.scalars(select(Tag).where(Tag.device_id == d.id)):
        db.query(Reading).filter(Reading.tag_id == t.id).delete()
        db.delete(t)
    db.delete(d)
    audit(db, "device.delete", actor=p.user, tenant_id=d.tenant_id, target=f"device:{d.id}", ip=client_ip(request))
    db.commit()
    return {"ok": True}


# ---- tags
@router.get("/tags")
def list_tags(device_id: int | None = None, p: Principal = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    stmt = tenant_filter(select(Tag), Tag, p)
    if device_id is not None:
        stmt = stmt.where(Tag.device_id == device_id)
    return [_tag(t) for t in db.scalars(stmt.order_by(Tag.id))]


@router.post("/tags", status_code=201)
def create_tag(data: TagIn, request: Request, p: Principal = Depends(require_role("operator")), db: Session = Depends(get_db)):
    d = get_owned(db, Device, data.device_id, p)
    if data.function_code in (1, 2) and data.data_type != "bool":
        raise HTTPException(422, "coil/discrete-input tags must use data_type=bool")
    t = Tag(tenant_id=d.tenant_id, **data.model_dump())
    db.add(t); db.flush()
    audit(db, "tag.create", actor=p.user, tenant_id=d.tenant_id, target=f"tag:{t.id}", detail=data.model_dump(), ip=client_ip(request))
    db.commit()
    return _tag(t)


@router.get("/tags/{tag_id}/readings")
def tag_readings(tag_id: int, hours: int = 24, limit: int = 1000, p: Principal = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    t = get_owned(db, Tag, tag_id, p)
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=min(max(hours, 1), 24 * 31))
    rows = db.scalars(select(Reading).where(Reading.tag_id == t.id, Reading.ts >= since)
                      .order_by(Reading.ts.desc()).limit(min(max(limit, 1), 10000)))
    return [{"ts": r.ts, "value": r.value, "quality": r.quality} for r in rows]
