"""P4 monitoring: dashboard summary, trends, alarms, single-line diagrams, CSV export,
device templates and a demo site with virtual devices. Read-only with respect to field devices."""
import csv
import datetime as dt
import io
import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..alarms import _aware
from ..audit import audit
from ..db import get_db
from ..deps import Principal, client_ip, get_owned, require_role, resolve_tenant, tenant_filter
from ..models import (SEVERITIES, AlarmEvent, AlarmRule, Device, Diagram, Reading, Site, Tag, utcnow)
from ..templates import TEMPLATES, tag_defaults

router = APIRouter(prefix="/api", tags=["scada"])
KST = dt.timezone(dt.timedelta(hours=9))
SEV_RANK = {s: i for i, s in enumerate(SEVERITIES)}


def _main_tag(tag_id, site_tags, category):
    """Configured representative tag, else the first tag of that category in the site."""
    if tag_id:
        t = next((t for t in site_tags if t.id == tag_id), None)
        if t:
            return t
    return next((t for t in sorted(site_tags, key=lambda x: x.id) if t.category == category), None)


def _online(dev: Device, tags: list[Tag], now) -> bool:
    limit = dt.timedelta(seconds=max(3 * dev.poll_interval_s, 120))
    return any(t.quality == "Good" and t.source_ts and now - _aware(t.source_ts) <= limit for t in tags)


# ---------------------------------------------------------------- dashboard
@router.get("/dashboard")
def dashboard(tenant_id: int | None = None, p: Principal = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    now = utcnow()
    day_start = dt.datetime.now(KST).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(dt.timezone.utc)
    sites = db.scalars(tenant_filter(select(Site), Site, p, tenant_id).order_by(Site.id)).all()
    site_ids = [s.id for s in sites]
    devices = db.scalars(select(Device).where(Device.site_id.in_(site_ids))).all() if site_ids else []
    dev_ids = [d.id for d in devices]
    tags = db.scalars(select(Tag).where(Tag.device_id.in_(dev_ids))).all() if dev_ids else []
    tags_by_dev: dict[int, list[Tag]] = {}
    for t in tags:
        tags_by_dev.setdefault(t.device_id, []).append(t)
    tag_site = {t.id: next(d.site_id for d in devices if d.id == t.device_id) for t in tags}
    alarms = db.scalars(tenant_filter(select(AlarmEvent), AlarmEvent, p, tenant_id)
                        .where(AlarmEvent.active.is_(True)).order_by(AlarmEvent.raised_at.desc())).all()

    out_sites = []
    for s in sites:
        sdevs = [d for d in devices if d.site_id == s.id]
        stags = [t for t in tags if tag_site[t.id] == s.id]
        main_p = _main_tag(s.main_power_tag_id, stags, "power")
        main_e = _main_tag(s.main_energy_tag_id, stags, "energy")
        today_kwh = None
        if main_e:
            first = db.scalar(select(Reading.value).where(Reading.tag_id == main_e.id, Reading.ts >= day_start,
                                                         Reading.value.is_not(None)).order_by(Reading.ts).limit(1))
            if first is not None and main_e.value is not None:
                today_kwh = max(0.0, main_e.value - first)
        peak = None
        if main_p:
            peak = db.scalar(select(func.max(Reading.value)).where(Reading.tag_id == main_p.id, Reading.ts >= day_start))
        sal = [a for a in alarms if tag_site.get(a.tag_id) == s.id]
        out_sites.append({
            "id": s.id, "name": s.name, "address": s.address,
            "devices_total": len(sdevs), "devices_online": sum(1 for d in sdevs if _online(d, tags_by_dev.get(d.id, []), now)),
            "devices_enabled": sum(1 for d in sdevs if d.enabled),
            "main_power_tag_id": main_p.id if main_p else None, "main_energy_tag_id": main_e.id if main_e else None,
            "power_kw": round(main_p.value, 2) if main_p and main_p.value is not None else None,
            "today_kwh": round(today_kwh, 2) if today_kwh is not None else None,
            "peak_kw_today": round(peak, 2) if peak is not None else None,
            "active_alarms": len(sal),
            "worst_severity": max((a.severity for a in sal), key=lambda x: SEV_RANK.get(x, 0), default=None),
        })
    return {
        "generated_at": now, "sites": out_sites,
        "totals": {"sites": len(sites), "devices": len(devices),
                   "devices_online": sum(x["devices_online"] for x in out_sites),
                   "tags": len(tags), "tags_good": sum(1 for t in tags if t.quality == "Good"),
                   "active_alarms": len(alarms),
                   "unacked_alarms": sum(1 for a in alarms if a.acked_at is None),
                   "power_kw": round(sum(x["power_kw"] or 0 for x in out_sites), 2)},
        "devices": [{"id": d.id, "site_id": d.site_id, "name": d.name, "protocol": d.protocol, "enabled": d.enabled,
                     "online": _online(d, tags_by_dev.get(d.id, []), now)} for d in devices],
        "recent_alarms": [_alarm_out(a, {t.id: t for t in tags}) for a in alarms[:10]],
    }


# ---------------------------------------------------------------- trend
@router.get("/trend")
def trend(tag_ids: str = Query(..., max_length=200), hours: int = 24, points: int = 240,
          p: Principal = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    try:
        ids = [int(x) for x in tag_ids.split(",") if x.strip()][:8]
    except ValueError:
        raise HTTPException(422, "tag_ids must be comma-separated integers")
    hours = min(max(hours, 1), 24 * 31)
    points = min(max(points, 10), 1000)
    end = utcnow()
    start = end - dt.timedelta(hours=hours)
    width = (end - start).total_seconds() / points
    series = []
    for tid in ids:
        t = get_owned(db, Tag, tid, p)
        rows = db.execute(select(Reading.ts, Reading.value).where(Reading.tag_id == t.id, Reading.ts >= start,
                                                                  Reading.value.is_not(None))
                          .order_by(Reading.ts).limit(100_000)).all()
        buckets: dict[int, list[float]] = {}
        for ts, v in rows:
            b = int((_aware(ts) - start).total_seconds() // width)
            buckets.setdefault(min(b, points - 1), []).append(v)
        pts = [{"t": (start + dt.timedelta(seconds=(b + 0.5) * width)).isoformat(),
                "avg": round(sum(v) / len(v), 4), "min": min(v), "max": max(v)} for b, v in sorted(buckets.items())]
        dev = db.get(Device, t.device_id)
        series.append({"tag_id": t.id, "name": f"{dev.name} · {t.name}" if dev else t.name, "unit": t.unit, "category": t.category,
                       "raw_count": len(rows), "points": pts})
    return {"start": start, "end": end, "bucket_seconds": round(width, 1), "series": series}


@router.get("/tags/{tag_id}/export.csv")
def export_csv(tag_id: int, hours: int = 24, p: Principal = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    t = get_owned(db, Tag, tag_id, p)
    since = utcnow() - dt.timedelta(hours=min(max(hours, 1), 24 * 92))
    rows = db.execute(select(Reading.ts, Reading.value, Reading.quality).where(Reading.tag_id == t.id, Reading.ts >= since)
                      .order_by(Reading.ts).limit(500_000)).all()
    buf = io.StringIO()
    buf.write("﻿")  # Excel-friendly UTF-8
    w = csv.writer(buf)
    w.writerow(["시각(KST)", f"{t.name} ({t.unit})" if t.unit else t.name, "품질"])
    for ts, v, q in rows:
        w.writerow([_aware(ts).astimezone(KST).strftime("%Y-%m-%d %H:%M:%S"), "" if v is None else v, q])
    fname = f"tag{t.id}_{hours}h.csv"
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{fname}"'})


# ---------------------------------------------------------------- alarms
def _alarm_out(a: AlarmEvent, tags: dict | None = None):
    t = (tags or {}).get(a.tag_id)
    return {"id": a.id, "tag_id": a.tag_id, "tag_name": t.name if t else None, "severity": a.severity,
            "message": a.message, "value": a.value, "active": a.active, "raised_at": a.raised_at,
            "cleared_at": a.cleared_at, "acked_at": a.acked_at, "acked_by": a.acked_by}


@router.get("/alarms")
def list_alarms(state: str = Query("active", pattern="^(active|all)$"), limit: int = 200, tenant_id: int | None = None,
                p: Principal = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    stmt = tenant_filter(select(AlarmEvent), AlarmEvent, p, tenant_id)
    if state == "active":
        stmt = stmt.where(AlarmEvent.active.is_(True))
    rows = db.scalars(stmt.order_by(AlarmEvent.raised_at.desc()).limit(min(max(limit, 1), 1000))).all()
    tags = {t.id: t for t in db.scalars(select(Tag).where(Tag.id.in_({a.tag_id for a in rows})))} if rows else {}
    return [_alarm_out(a, tags) for a in rows]


@router.post("/alarms/{alarm_id}/ack")
def ack_alarm(alarm_id: int, request: Request, p: Principal = Depends(require_role("operator")), db: Session = Depends(get_db)):
    a = get_owned(db, AlarmEvent, alarm_id, p)
    if a.acked_at is None:
        a.acked_at, a.acked_by = utcnow(), p.user.username
        audit(db, "alarm.ack", actor=p.user, tenant_id=a.tenant_id, target=f"alarm:{a.id}", ip=client_ip(request))
        db.commit()
    return _alarm_out(a)


class RuleIn(BaseModel):
    tag_id: int
    kind: str = Field(pattern="^(high|low|state|comm)$")
    threshold: float = Field(default=0.0, ge=-1e12, le=1e12)
    deadband: float = Field(default=0.0, ge=0, le=1e9)
    severity: str = Field(default="major", pattern="^(info|minor|major|critical)$")
    message: str = Field(default="", max_length=200)


def _rule_out(r: AlarmRule):
    return {"id": r.id, "tag_id": r.tag_id, "kind": r.kind, "threshold": r.threshold, "deadband": r.deadband,
            "severity": r.severity, "message": r.message, "enabled": r.enabled}


@router.get("/alarm-rules")
def list_rules(tag_id: int | None = None, p: Principal = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    stmt = tenant_filter(select(AlarmRule), AlarmRule, p)
    if tag_id is not None:
        stmt = stmt.where(AlarmRule.tag_id == tag_id)
    return [_rule_out(r) for r in db.scalars(stmt.order_by(AlarmRule.id))]


@router.post("/alarm-rules", status_code=201)
def create_rule(data: RuleIn, request: Request, p: Principal = Depends(require_role("operator")), db: Session = Depends(get_db)):
    t = get_owned(db, Tag, data.tag_id, p)
    r = AlarmRule(tenant_id=t.tenant_id, **data.model_dump())
    db.add(r); db.flush()
    audit(db, "alarm_rule.create", actor=p.user, tenant_id=t.tenant_id, target=f"rule:{r.id}", detail=data.model_dump(), ip=client_ip(request))
    db.commit()
    return _rule_out(r)


@router.delete("/alarm-rules/{rule_id}")
def delete_rule(rule_id: int, request: Request, p: Principal = Depends(require_role("operator")), db: Session = Depends(get_db)):
    r = get_owned(db, AlarmRule, rule_id, p)
    db.query(AlarmEvent).filter(AlarmEvent.rule_id == r.id).delete()
    db.delete(r)
    audit(db, "alarm_rule.delete", actor=p.user, tenant_id=r.tenant_id, target=f"rule:{r.id}", ip=client_ip(request))
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- single-line diagrams
ELEMENT_TYPES = {"bus", "breaker", "transformer", "meter", "load", "generator", "label"}


class DiagramIn(BaseModel):
    spec: dict


def _validate_spec(db: Session, spec: dict, tenant_id: int) -> dict:
    els, lines = spec.get("elements", []), spec.get("lines", [])
    if not isinstance(els, list) or not isinstance(lines, list) or len(els) > 300 or len(lines) > 600:
        raise HTTPException(422, "spec must contain elements (≤300) and lines (≤600) lists")
    tag_ids = set()
    clean_els = []
    for e in els:
        if not isinstance(e, dict) or e.get("type") not in ELEMENT_TYPES:
            raise HTTPException(422, f"unknown element type: {e.get('type') if isinstance(e, dict) else e}")
        try:
            x, y = float(e["x"]), float(e["y"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(422, "each element needs numeric x and y")
        ce = {"type": e["type"], "x": x, "y": y, "label": str(e.get("label", ""))[:60]}
        if e["type"] == "bus":
            ce["w"] = float(e.get("w", 400))
        for k in ("tag_id", "state_tag_id"):
            if e.get(k) is not None:
                ce[k] = int(e[k]); tag_ids.add(ce[k])
        clean_els.append(ce)
    clean_lines = []
    for ln in lines:
        try:
            pts = [[float(a), float(b)] for a, b in ln["points"]][:20]
        except (KeyError, TypeError, ValueError):
            raise HTTPException(422, "each line needs points [[x,y],...]")
        if len(pts) < 2:
            raise HTTPException(422, "a line needs at least 2 points")
        clean_lines.append({"points": pts})
    if tag_ids:
        owned = set(db.scalars(select(Tag.id).where(Tag.id.in_(tag_ids), Tag.tenant_id == tenant_id)))
        if owned != tag_ids:
            raise HTTPException(404, "Tag not found")
    return {"elements": clean_els, "lines": clean_lines}


@router.get("/diagrams/{site_id}")
def get_diagram(site_id: int, p: Principal = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    s = get_owned(db, Site, site_id, p)
    d = db.scalar(select(Diagram).where(Diagram.site_id == s.id))
    return {"site_id": s.id, "spec": json.loads(d.spec) if d else {"elements": [], "lines": []},
            "updated_at": d.updated_at if d else None, "updated_by": d.updated_by if d else ""}


@router.put("/diagrams/{site_id}")
def put_diagram(site_id: int, data: DiagramIn, request: Request, p: Principal = Depends(require_role("operator")), db: Session = Depends(get_db)):
    s = get_owned(db, Site, site_id, p)
    spec = _validate_spec(db, data.spec, s.tenant_id)
    d = db.scalar(select(Diagram).where(Diagram.site_id == s.id))
    if not d:
        d = Diagram(tenant_id=s.tenant_id, site_id=s.id)
        db.add(d)
    d.spec, d.updated_at, d.updated_by = json.dumps(spec, ensure_ascii=False), utcnow(), p.user.username
    audit(db, "diagram.save", actor=p.user, tenant_id=s.tenant_id, target=f"site:{s.id}",
          detail={"elements": len(spec["elements"]), "lines": len(spec["lines"])}, ip=client_ip(request))
    db.commit()
    return {"site_id": s.id, "spec": spec}


# ---------------------------------------------------------------- templates & demo
@router.get("/device-templates")
def list_templates(p: Principal = Depends(require_role("viewer"))):
    return [{"key": k, "name": v["name"], "protocol": v["protocol"], "notes": v.get("notes", ""),
             "verification": v.get("verification", ""), "baudrate": v.get("baudrate"), "unit_id": v.get("unit_id"),
             "tags": [t["name"] for t in v["tags"]]} for k, v in TEMPLATES.items()]


class ApplyTemplateIn(BaseModel):
    template: str


@router.post("/devices/{device_id}/apply-template", status_code=201)
def apply_template(device_id: int, data: ApplyTemplateIn, request: Request, p: Principal = Depends(require_role("operator")), db: Session = Depends(get_db)):
    d = get_owned(db, Device, device_id, p)
    tpl = TEMPLATES.get(data.template)
    if not tpl:
        raise HTTPException(404, "Template not found")
    if tpl["protocol"] != d.protocol:
        raise HTTPException(400, f"이 템플릿은 {tpl['protocol']} 장치용입니다 (현재 장치: {d.protocol})")
    created = []
    for t in tpl["tags"]:
        tag = Tag(tenant_id=d.tenant_id, device_id=d.id, **tag_defaults(t))
        db.add(tag); created.append(tag)
    db.flush()
    audit(db, "device.apply_template", actor=p.user, tenant_id=d.tenant_id, target=f"device:{d.id}",
          detail={"template": data.template}, ip=client_ip(request))
    db.commit()
    return {"device_id": d.id, "created_tags": [{"id": t.id, "name": t.name} for t in created]}


class DemoIn(BaseModel):
    tenant_id: int | None = None


@router.post("/demo", status_code=201)
def create_demo(data: DemoIn, request: Request, p: Principal = Depends(require_role("tenant_admin")), db: Session = Depends(get_db)):
    """Create a demo substation with virtual (simulator) devices, alarm rules and a single-line diagram."""
    tid = resolve_tenant(p, data.tenant_id)
    base = "데모 수변전실"
    name, i = base, 1
    while db.scalar(select(Site.id).where(Site.tenant_id == tid, Site.name == name)):
        i += 1; name = f"{base} {i}"
    s = Site(tenant_id=tid, name=name, address="가상 데이터 (시뮬레이터)")
    db.add(s); db.flush()

    def dev(dname, tags):
        d = Device(tenant_id=tid, site_id=s.id, name=dname, protocol="simulator", poll_interval_s=60, enabled=True)
        db.add(d); db.flush()
        out = {}
        for nm, cat, unit in tags:
            t = Tag(tenant_id=tid, device_id=d.id, name=nm, category=cat, unit=unit, function_code=3, address=0, data_type="float32")
            db.add(t); db.flush(); out[cat] = t
        return d, out

    _, main = dev("수전반 계량기 (MOF)", [("전압", "voltage", "V"), ("전류", "current", "A"), ("유효전력", "power", "kW"),
                                         ("누적전력량", "energy", "kWh"), ("역률", "pf", ""), ("주파수", "frequency", "Hz")])
    _, vcb = dev("VCB 주차단기", [("VCB 상태", "state", "")])
    _, tr = dev("변압기 TR-1", [("권선온도", "temperature", "°C")])
    _, f1 = dev("분기반 L-1 (동관)", [("전류", "current", "A"), ("유효전력", "power", "kW")])
    _, f2 = dev("분기반 L-2 (공용부)", [("전류", "current", "A"), ("유효전력", "power", "kW")])
    for tag, share in ((f1["current"], 0.55), (f1["power"], 0.55), (f2["current"], 0.4), (f2["power"], 0.4)):
        tag.scale = share
    s.main_power_tag_id, s.main_energy_tag_id = main["power"].id, main["energy"].id

    rules = [(main["current"], "high", 160, 5, "major", "주 전류 과부하"), (main["voltage"], "low", 370, 2, "minor", "저전압"),
             (main["voltage"], "high", 400, 2, "minor", "과전압"), (tr["temperature"], "high", 85, 3, "critical", "변압기 과열"),
             (vcb["state"], "state", 0, 0, "critical", "VCB 트립(개방)"), (main["voltage"], "comm", 0, 0, "major", "수전반 계량기 통신 두절"),
             (f1["current"], "high", 70, 3, "minor", "L-1 과전류")]
    for tag, kind, th, db_, sev, msg in rules:
        db.add(AlarmRule(tenant_id=tid, tag_id=tag.id, kind=kind, threshold=th, deadband=db_, severity=sev, message=msg))

    spec = {"elements": [
        {"type": "label", "x": 40, "y": 30, "label": "22.9kV 수전"},
        {"type": "meter", "x": 300, "y": 60, "label": "MOF", "tag_id": main["power"].id},
        {"type": "breaker", "x": 300, "y": 150, "label": "VCB", "state_tag_id": vcb["state"].id},
        {"type": "transformer", "x": 300, "y": 250, "label": "TR-1 22.9kV/380V", "tag_id": tr["temperature"].id},
        {"type": "bus", "x": 100, "y": 340, "w": 400, "label": "LV BUS 380V", "tag_id": main["voltage"].id},
        {"type": "breaker", "x": 180, "y": 410, "label": "ACB L-1"},
        {"type": "breaker", "x": 420, "y": 410, "label": "ACB L-2"},
        {"type": "load", "x": 180, "y": 500, "label": "동관", "tag_id": f1["power"].id},
        {"type": "load", "x": 420, "y": 500, "label": "공용부", "tag_id": f2["power"].id}],
        "lines": [{"points": [[300, 20], [300, 40]]}, {"points": [[300, 80], [300, 130]]}, {"points": [[300, 170], [300, 220]]},
                  {"points": [[300, 280], [300, 340]]}, {"points": [[180, 340], [180, 390]]}, {"points": [[420, 340], [420, 390]]},
                  {"points": [[180, 430], [180, 480]]}, {"points": [[420, 430], [420, 480]]}]}
    db.add(Diagram(tenant_id=tid, site_id=s.id, spec=json.dumps(spec, ensure_ascii=False), updated_by=p.user.username))
    audit(db, "demo.create", actor=p.user, tenant_id=tid, target=f"site:{s.id}", ip=client_ip(request))
    db.commit()
    from ..simulator import run_simulators
    run_simulators(db, force=True)
    db.commit()
    return {"site_id": s.id, "name": s.name}
