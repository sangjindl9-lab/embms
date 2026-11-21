from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .. import config
from ..audit import audit
from ..db import get_db
from ..deps import ROLE_RANK, Principal, client_ip, require_role, resolve_tenant, tenant_filter
from ..models import AuditLog, AuthSession, Tenant, User
from ..security import hash_password

router = APIRouter(prefix="/api", tags=["admin"])


class TenantIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class UserIn(BaseModel):
    username: str = Field(min_length=3, max_length=100, pattern=r"^[A-Za-z0-9_.@-]+$")
    password: str = Field(min_length=config.MIN_PASSWORD_LENGTH, max_length=256)
    role: str = Field(pattern="^(superadmin|tenant_admin|operator|viewer)$")
    tenant_id: int | None = None


class UserPatch(BaseModel):
    role: str | None = Field(default=None, pattern="^(tenant_admin|operator|viewer)$")
    active: bool | None = None
    password: str | None = Field(default=None, min_length=config.MIN_PASSWORD_LENGTH, max_length=256)


def _user_out(u: User):
    return {"id": u.id, "username": u.username, "role": u.role, "tenant_id": u.tenant_id,
            "active": u.active, "last_login_at": u.last_login_at}


@router.get("/tenants")
def list_tenants(p: Principal = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    stmt = select(Tenant).order_by(Tenant.id)
    if not p.is_super:
        stmt = stmt.where(Tenant.id == p.tenant_id)
    return [{"id": t.id, "name": t.name, "active": t.active} for t in db.scalars(stmt)]


@router.post("/tenants", status_code=201)
def create_tenant(data: TenantIn, request: Request, p: Principal = Depends(require_role("superadmin")), db: Session = Depends(get_db)):
    if db.scalar(select(Tenant).where(Tenant.name == data.name)):
        raise HTTPException(409, "Tenant already exists")
    t = Tenant(name=data.name)
    db.add(t)
    db.flush()
    audit(db, "tenant.create", actor=p.user, tenant_id=t.id, target=t.name, ip=client_ip(request))
    db.commit()
    return {"id": t.id, "name": t.name}


@router.get("/users")
def list_users(tenant_id: int | None = None, p: Principal = Depends(require_role("tenant_admin")), db: Session = Depends(get_db)):
    return [_user_out(u) for u in db.scalars(tenant_filter(select(User), User, p, tenant_id).order_by(User.id))]


@router.post("/users", status_code=201)
def create_user(data: UserIn, request: Request, p: Principal = Depends(require_role("tenant_admin")), db: Session = Depends(get_db)):
    if data.role == "superadmin":
        if not p.is_super:
            raise HTTPException(403, "Only platform administrators can create superadmins")
        tenant_id = None
    else:
        tenant_id = resolve_tenant(p, data.tenant_id)
        if not db.get(Tenant, tenant_id):
            raise HTTPException(404, "Tenant not found")
    if ROLE_RANK[data.role] > ROLE_RANK[p.role]:
        raise HTTPException(403, "Cannot grant a role above your own")
    username = data.username.lower()
    if db.scalar(select(User).where(User.username == username)):
        raise HTTPException(409, "Username already exists")
    u = User(username=username, password_hash=hash_password(data.password), role=data.role, tenant_id=tenant_id)
    db.add(u)
    db.flush()
    audit(db, "user.create", actor=p.user, tenant_id=tenant_id, target=username, detail={"role": data.role}, ip=client_ip(request))
    db.commit()
    return _user_out(u)


@router.patch("/users/{user_id}")
def update_user(user_id: int, data: UserPatch, request: Request, p: Principal = Depends(require_role("tenant_admin")), db: Session = Depends(get_db)):
    u = db.get(User, user_id)
    if not u or (not p.is_super and u.tenant_id != p.tenant_id):
        raise HTTPException(404, "User not found")
    if u.role == "superadmin" and not p.is_super:
        raise HTTPException(404, "User not found")
    changes = {}
    if data.role is not None:
        u.role = data.role; changes["role"] = data.role
    if data.active is not None:
        if u.id == p.user.id and not data.active:
            raise HTTPException(400, "Cannot deactivate yourself")
        u.active = data.active; changes["active"] = data.active
    if data.password is not None:
        u.password_hash = hash_password(data.password); changes["password"] = "reset"
    if changes.get("active") is False or "password" in changes:
        db.execute(delete(AuthSession).where(AuthSession.user_id == u.id))
    audit(db, "user.update", actor=p.user, tenant_id=u.tenant_id, target=u.username, detail=changes, ip=client_ip(request))
    db.commit()
    return _user_out(u)


@router.get("/audit")
def list_audit(tenant_id: int | None = None, limit: int = 200, p: Principal = Depends(require_role("tenant_admin")), db: Session = Depends(get_db)):
    stmt = tenant_filter(select(AuditLog), AuditLog, p, tenant_id).order_by(AuditLog.id.desc()).limit(min(max(limit, 1), 1000))
    return [{"id": a.id, "tenant_id": a.tenant_id, "actor": a.actor, "action": a.action, "target": a.target,
             "detail": a.detail, "ip": a.ip, "created_at": a.created_at} for a in db.scalars(stmt)]


@router.get("/admin/request-info")
def request_info(request: Request, p: Principal = Depends(require_role("superadmin"))):
    """Ops diagnostic: how the client address is resolved behind the hosting proxy."""
    names = ("x-forwarded-for", "x-real-ip", "cf-connecting-ip", "true-client-ip", "x-forwarded-proto")
    return {"resolved_client_ip": client_ip(request), "peer": request.client.host if request.client else None,
            "headers": {n: request.headers.get(n) for n in names}}
