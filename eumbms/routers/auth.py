import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .. import config
from ..audit import audit
from ..db import get_db
from ..deps import COOKIE_NAME, Principal, client_ip, current_principal
from ..models import AuthSession, LoginAttempt, User, utcnow
from ..security import DUMMY_HASH, hash_password, new_token, token_hash, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=256)


class PasswordIn(BaseModel):
    current_password: str = Field(max_length=256)
    new_password: str = Field(min_length=config.MIN_PASSWORD_LENGTH, max_length=256)


def _throttled(db: Session, ip: str, username: str) -> bool:
    since = utcnow() - dt.timedelta(seconds=config.LOGIN_WINDOW_SECONDS)
    base = select(func.count()).select_from(LoginAttempt).where(
        LoginAttempt.success.is_(False), LoginAttempt.created_at >= since)
    by_ip = db.scalar(base.where(LoginAttempt.ip == ip))
    by_user = db.scalar(base.where(LoginAttempt.username == username))
    return by_ip >= config.LOGIN_MAX_FAILURES_IP or by_user >= config.LOGIN_MAX_FAILURES


@router.post("/login")
def login(data: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
    ip, username = client_ip(request), data.username.strip().lower()
    if _throttled(db, ip, username):
        raise HTTPException(429, "Too many failed login attempts", headers={"Retry-After": str(config.LOGIN_WINDOW_SECONDS)})
    user = db.scalar(select(User).where(User.username == username))
    ok = verify_password(data.password, user.password_hash if user else DUMMY_HASH)
    if not (user and ok and user.active):
        db.add(LoginAttempt(ip=ip, username=username, success=False))
        audit(db, "auth.login_failed", tenant_id=user.tenant_id if user else None, target=username, ip=ip)
        db.commit()
        raise HTTPException(401, "Invalid username or password")
    token, csrf = new_token(), new_token()
    sess = AuthSession(user_id=user.id, token_hash=token_hash(token), csrf_hash=token_hash(csrf),
                       expires_at=utcnow() + dt.timedelta(hours=config.SESSION_HOURS), ip=ip)
    user.last_login_at = utcnow()
    db.add(sess)
    db.add(LoginAttempt(ip=ip, username=username, success=True))
    # housekeeping: expired sessions and old attempts
    db.execute(delete(AuthSession).where(AuthSession.expires_at < utcnow()))
    db.execute(delete(LoginAttempt).where(LoginAttempt.created_at < utcnow() - dt.timedelta(days=30)))
    audit(db, "auth.login", actor=user, target=username, ip=ip)
    db.commit()
    response.set_cookie(COOKIE_NAME, token, httponly=True, secure=config.COOKIE_SECURE, samesite="strict",
                        max_age=config.SESSION_HOURS * 3600, path="/")
    response.headers["Cache-Control"] = "no-store"
    return {"access_token": token, "token_type": "bearer", "csrf_token": csrf,
            "expires_at": sess.expires_at, "user": _me(user)}


@router.post("/logout")
def logout(response: Response, request: Request, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    db.delete(db.get(AuthSession, p.session.id))
    audit(db, "auth.logout", actor=p.user, ip=client_ip(request))
    db.commit()
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


def _me(u: User):
    return {"id": u.id, "username": u.username, "role": u.role, "tenant_id": u.tenant_id}


@router.get("/me")
def me(p: Principal = Depends(current_principal)):
    return _me(p.user)


@router.post("/password")
def change_password(data: PasswordIn, request: Request, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    user = db.get(User, p.user.id)
    if not verify_password(data.current_password, user.password_hash):
        raise HTTPException(400, "Current password is incorrect")
    user.password_hash = hash_password(data.new_password)
    # revoke all other sessions
    db.execute(delete(AuthSession).where(AuthSession.user_id == user.id, AuthSession.id != p.session.id))
    audit(db, "auth.password_changed", actor=user, ip=client_ip(request))
    db.commit()
    return {"ok": True}
