"""Virtual devices (protocol="simulator") so dashboards, trends and alarms can be tested
without hardware. Generates plausible 3-phase LV switchgear values. Never touches real devices."""
import datetime as dt
import math
import random

from sqlalchemy import select
from sqlalchemy.orm import Session

from .alarms import evaluate_value
from .models import Device, Reading, Tag, utcnow

_last_run: dict[int, dt.datetime] = {}


def _value(tag: Tag, now: dt.datetime, prev: float | None, dt_s: float) -> float:
    t = now.timestamp()
    ph = (tag.id * 977) % 3600
    # daily load curve (KST): low at night, high 10-17h
    hour = (now.astimezone(dt.timezone(dt.timedelta(hours=9))).hour + now.minute / 60)
    load = 0.45 + 0.45 * max(0.0, math.sin(math.pi * (hour - 6) / 14)) if 6 <= hour <= 20 else 0.35
    spike = 1.55 if (now.minute % 30) < 2 else 1.0  # brief overload twice an hour -> exercises alarms
    c = tag.category
    if c == "voltage":
        return round(380 + 4 * math.sin((t + ph) / 600) + random.uniform(-0.8, 0.8), 1)
    if c == "current":
        return round(120 * load * spike + random.uniform(-3, 3), 2)
    if c == "power":
        return round(1.732 * 0.38 * 120 * load * spike * 0.93 + random.uniform(-1, 1), 2)
    if c == "energy":
        kw = 1.732 * 0.38 * 120 * load * 0.93
        return round((prev if prev is not None else 10000.0) + kw * dt_s / 3600, 3)
    if c == "pf":
        return round(0.93 + 0.02 * math.sin((t + ph) / 900), 3)
    if c == "frequency":
        return round(60 + random.uniform(-0.03, 0.03), 2)
    if c == "temperature":
        return round(38 + 18 * load * spike + random.uniform(-0.5, 0.5), 1)
    if c == "state":
        return 1.0
    return round(50 + 20 * math.sin((t + ph) / 1200), 2)


def run_simulators(db: Session, now: dt.datetime | None = None, force: bool = False) -> int:
    now = now or utcnow()
    n = 0
    for dev in db.scalars(select(Device).where(Device.protocol == "simulator", Device.enabled.is_(True))):
        last = _last_run.get(dev.id)
        interval = max(dev.poll_interval_s, 30)
        if not force and last and (now - last).total_seconds() < interval:
            continue
        dt_s = (now - last).total_seconds() if last else interval
        _last_run[dev.id] = now
        for tag in db.scalars(select(Tag).where(Tag.device_id == dev.id)):
            v = _value(tag, now, tag.value, min(dt_s, 3600))
            db.add(Reading(tenant_id=tag.tenant_id, tag_id=tag.id, value=v, quality="Good", ts=now))
            tag.value, tag.quality, tag.source_ts, tag.updated_at = v, "Good", now, now
            evaluate_value(db, tag, v, "Good")
            n += 1
    return n
