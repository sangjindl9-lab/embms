"""Alarm evaluation: limit/state alarms on new values, communication-loss alarms on a timer."""
import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AlarmEvent, AlarmRule, Device, Tag, utcnow

KIND_KO = {"high": "상한 초과", "low": "하한 미달", "state": "상태 경보", "comm": "통신 두절"}


def _aware(t: dt.datetime | None) -> dt.datetime | None:
    if t is None:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def _fmt(v):
    return "—" if v is None else (f"{v:.2f}".rstrip("0").rstrip(".") if isinstance(v, float) else str(v))


def _active_event(db: Session, rule_id: int) -> AlarmEvent | None:
    return db.scalar(select(AlarmEvent).where(AlarmEvent.rule_id == rule_id, AlarmEvent.active.is_(True)))


def _raise(db, rule: AlarmRule, tag: Tag, value, text):
    db.add(AlarmEvent(tenant_id=rule.tenant_id, rule_id=rule.id, tag_id=tag.id, severity=rule.severity,
                      message=(rule.message or f"{tag.name} {KIND_KO[rule.kind]}") + f" — {text}", value=value))


def _clear(ev: AlarmEvent):
    ev.active = False
    ev.cleared_at = utcnow()


def evaluate_value(db: Session, tag: Tag, value: float | None, quality: str):
    """Evaluate high/low/state rules for a new value. Caller commits."""
    if value is None or quality != "Good":
        return
    rules = db.scalars(select(AlarmRule).where(AlarmRule.tag_id == tag.id, AlarmRule.enabled.is_(True),
                                               AlarmRule.kind.in_(("high", "low", "state")))).all()
    for r in rules:
        ev = _active_event(db, r.id)
        if r.kind == "high":
            trip, reset = value > r.threshold, value <= r.threshold - r.deadband
            text = f"{_fmt(value)}{tag.unit} > {_fmt(r.threshold)}{tag.unit}"
        elif r.kind == "low":
            trip, reset = value < r.threshold, value >= r.threshold + r.deadband
            text = f"{_fmt(value)}{tag.unit} < {_fmt(r.threshold)}{tag.unit}"
        else:
            trip = abs(value - r.threshold) < 1e-9
            reset = not trip
            text = f"상태값 {_fmt(value)}"
        if trip and ev is None:
            _raise(db, r, tag, value, text)
        elif reset and ev is not None:
            _clear(ev)


def check_comm(db: Session, now: dt.datetime | None = None) -> int:
    """Raise/clear communication-loss alarms. Returns number of state changes."""
    now = now or utcnow()
    changes = 0
    rows = db.execute(select(AlarmRule, Tag, Device).join(Tag, Tag.id == AlarmRule.tag_id)
                      .join(Device, Device.id == Tag.device_id)
                      .where(AlarmRule.kind == "comm", AlarmRule.enabled.is_(True))).all()
    for rule, tag, dev in rows:
        if not dev.enabled:
            continue
        limit = dt.timedelta(seconds=max(3 * dev.poll_interval_s, 120))
        ts = _aware(tag.source_ts)
        stale = tag.quality == "Bad" or ts is None or now - ts > limit
        ev = _active_event(db, rule.id)
        if stale and ev is None:
            why = "수신 없음" if ts is None else ("품질 Bad" if tag.quality == "Bad" else f"마지막 수신 {int((now - ts).total_seconds())}초 전")
            _raise(db, rule, tag, None, why)
            changes += 1
        elif not stale and ev is not None:
            _clear(ev)
            changes += 1
    return changes
