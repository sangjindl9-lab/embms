"""Authentication, RBAC and tenant-scoping dependencies."""
import datetime as dt
import os
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import get_db
from .models import AuthSession, User, utcnow
from .security import token_hash
import hmac

COOKIE_NAME = "eum_session"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
ROLE_RANK = {"viewer": 1, "operator": 2, "tenant_admin": 3, "superadmin": 4}
TRUSTED_PROXY_HOPS = int(os.getenv("TRUSTED_PROXY_HOPS", "0"))


def client_ip(request: Request) -> str:
    """Pick the client address added by the N-th trusted proxy (right-most entries).
    Clients can prepend arbitrary X-Forwarded-For values, so we never trust the left-most one."""
    if TRUSTED_PROXY_HOPS > 0:
        xff = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
        if len(xff) >= TRUSTED_PROXY_HOPS:
            return xff[-TRUSTED_PROXY_HOPS][:64]
    return (request.client.host if request.client else "unknown")[:64]


@dataclass
class Principal:
    user: User
    session: AuthSession
    via_cookie: bool

    @property
    def role(self) -> str:
        return self.user.role

    @property
    def tenant_id(self) -> int | None:
        return self.user.tenant_id

    @property
    def is_super(self) -> bool:
        return self.user.role == "superadmin"


def _unauth(detail="Authentication required"):
    return HTTPException(401, detail, headers={"WWW-Authenticate": 'Bearer realm="EUM BMS PRO"'})


def current_principal(request: Request, db: Session = Depends(get_db)) -> Principal:
    token, via_cookie = None, False
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    elif request.cookies.get(COOKIE_NAME):
        token, via_cookie = request.cookies[COOKIE_NAME], True
    if not token:
        raise _unauth()
    sess = db.scalar(select(AuthSession).where(AuthSession.token_hash == token_hash(token)))
    if not sess:
        raise _unauth("Invalid session")
    exp = sess.expires_at if sess.expires_at.tzinfo else sess.expires_at.replace(tzinfo=dt.timezone.utc)
    if exp < utcnow():
        db.delete(sess)
        db.commit()
        raise _unauth("Session expired")
    user = db.get(User, sess.user_id)
    if not user or not user.active:
        raise _unauth("Account disabled")
    if via_cookie and request.method not in SAFE_METHODS:
        csrf = request.headers.get("x-csrf-token", "")
        if not csrf or not hmac.compare_digest(token_hash(csrf), sess.csrf_hash):
            raise HTTPException(403, "CSRF token missing or invalid")
    return Principal(user=user, session=sess, via_cookie=via_cookie)


def require_role(min_role: str):
    def dep(p: Principal = Depends(current_principal)) -> Principal:
        if ROLE_RANK.get(p.role, 0) < ROLE_RANK[min_role]:
            raise HTTPException(403, "Insufficient role")
        return p
    return dep


def resolve_tenant(p: Principal, requested: int | None) -> int:
    """Tenant used for a write. Non-superadmins are pinned to their own tenant."""
    if p.is_super:
        if requested is None:
            raise HTTPException(400, "tenant_id is required for platform administrators")
        return requested
    if requested is not None and requested != p.tenant_id:
        raise HTTPException(404, "Not found")
    return p.tenant_id


def tenant_filter(stmt, model, p: Principal, requested: int | None = None):
    if p.is_super:
        return stmt.where(model.tenant_id == requested) if requested is not None else stmt
    return stmt.where(model.tenant_id == p.tenant_id)


def get_owned(db: Session, model, obj_id: int, p: Principal):
    """Fetch by id; another tenant's object is reported as 404 (no existence leak)."""
    obj = db.get(model, obj_id)
    if not obj or (not p.is_super and obj.tenant_id != p.tenant_id):
        raise HTTPException(404, f"{model.__name__} not found")
    return obj
