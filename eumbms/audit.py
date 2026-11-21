import json

from sqlalchemy.orm import Session

from .models import AuditLog


def audit(db: Session, action: str, *, actor=None, tenant_id=None, target="", detail=None, ip=""):
    """Append an audit record. Caller commits together with the business change."""
    db.add(AuditLog(
        tenant_id=tenant_id if tenant_id is not None else getattr(actor, "tenant_id", None),
        user_id=getattr(actor, "id", None),
        actor=getattr(actor, "username", "") or "",
        action=action, target=str(target)[:200],
        detail=json.dumps(detail, ensure_ascii=False, default=str)[:4000] if detail else "",
        ip=ip,
    ))
