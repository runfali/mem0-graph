import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from auth import (
    consume_refresh_jti,
    create_access_token,
    create_refresh_token,
    decode_token,
    dummy_verify_password,
    hash_password,
    require_auth,
    verify_password,
)
from db import get_db
from models import User
from rate_limit import limiter
from schemas import MessageResponse
from telemetry import capture_admin_registered, capture_onboarding_completed

router = APIRouter(prefix="/auth", tags=["auth"])

MIN_PASSWORD_LENGTH = 8


def _require_password_length(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Password must be at least {MIN_PASSWORD_LENGTH} characters.",
        )


class RegisterRequest(BaseModel):
    name: str
    email: EmailStr
    password: str


class OnboardingCompleteRequest(BaseModel):
    use_case: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class UpdateProfileRequest(BaseModel):
    name: str | None = None
    email: EmailStr | None = None


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class UserResponse(BaseModel):
    id: uuid.UUID
    name: str
    email: str
    role: str
    created_at: datetime

    model_config = {"from_attributes": True}


class SetupStatusResponse(BaseModel):
    needsSetup: bool


@router.get("/setup-status", response_model=SetupStatusResponse)
def setup_status(db: Session = Depends(get_db)):
    count = db.scalar(select(func.count(User.id)))
    return SetupStatusResponse(needsSetup=count == 0)


@router.post("/register", response_model=TokenResponse)
@limiter.limit("5/minute")
def register(request: Request, body: RegisterRequest, db: Session = Depends(get_db)):
    """Create the first admin account. Blocked once any user exists."""
    _require_password_length(body.password)

    # 四轮审计：INSERT...SELECT WHERE NOT EXISTS 在 PG READ COMMITTED 下并非
    # 真原子（语句快照看不到并发未提交行）——并发不同 email 仍可双插入；
    # 真正的单 admin 兜底是迁移 004 的 partial unique index
    # （ix_users_only_one_admin，role='admin' 单例），第二个 admin 插入被阻塞
    # → IntegrityError → 403。models.py 已等价声明该索引（测试环境同构）。
    import sqlalchemy as _sa

    # 五轮审计：IntegrityError 覆盖整个执行段——PG 下并发第二 admin 撞
    # partial unique index 时冲突在 db.execute 语句执行期抛出（非 commit 期），
    # except 只包 commit 会让 500 逃逸（sqlite 测试因写锁串行化走不到该路径）
    try:
        result = db.execute(
            _sa.insert(User)
            .from_select(
                [
                    User.name,
                    User.email,
                    User.password_hash,
                    User.role,
                ],
                _sa.select(
                    _sa.literal(body.name),
                    _sa.literal(body.email),
                    _sa.literal(hash_password(body.password)),
                    _sa.literal("admin"),
                ).where(_sa.not_(_sa.select(User.id).exists())),
            )
            .returning(User.id)
        )
        # 三轮审计：必须先消费 RETURNING 结果再 commit——sqlite 在未取回结果时
        # commit 抛 "cannot commit transaction - SQL statements in progress"
        row = result.fetchone()
        if row is None:
            db.rollback()
            raise HTTPException(status_code=403, detail="Registration is closed. An admin account already exists.")
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=403, detail="Registration is closed. An admin account already exists.")
    # 三轮审计：旧代码的 user 变量已被 from_select 重构删除，此处改用返回行
    user_id = str(row[0])

    capture_admin_registered(email=body.email)

    return TokenResponse(
        access_token=create_access_token(user_id, "admin"),
        refresh_token=create_refresh_token(user_id, db),
    )


@router.post("/login", response_model=TokenResponse)
@limiter.limit("10/minute")
def login(request: Request, body: LoginRequest, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == body.email))
    if user is None:
        dummy_verify_password()
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    if not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    user.last_login_at = datetime.now(timezone.utc)
    db.commit()

    return TokenResponse(
        access_token=create_access_token(str(user.id), user.role),
        refresh_token=create_refresh_token(str(user.id), db),
    )


@router.post("/refresh", response_model=TokenResponse)
@limiter.limit("20/minute")
def refresh(request: Request, body: RefreshRequest, db: Session = Depends(get_db)):
    payload = decode_token(body.refresh_token)
    if payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Invalid token type.")

    jti = payload.get("jti")
    if not jti:
        raise HTTPException(status_code=401, detail="Refresh token is no longer valid.")

    try:
        # sub 缺失/畸形直接 401：payload["sub"] 的 KeyError 与非法 UUID 的
        # DataError 都不应冒泡成 500（模式同 auth.consume_refresh_jti）
        user_id = uuid.UUID(str(payload.get("sub")))
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="Refresh token is no longer valid.")
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="User not found.")

    consume_refresh_jti(jti, db)

    return TokenResponse(
        access_token=create_access_token(str(user.id), user.role),
        refresh_token=create_refresh_token(str(user.id), db),
    )


@router.post("/revoke-refresh")
@limiter.limit("20/minute")
def revoke_refresh(request: Request, body: RefreshRequest, db: Session = Depends(get_db)):
    """吊销一个 refresh token（登出用，四轮审计）。

    此前登出仅清浏览器 cookie，服务端 RefreshTokenJti 仍有效至过期——
    被泄露的 refresh token 30 天内仍可换新 access。吊销幂等：未知/已吊销/
    过期 jti 一律返回成功（登出不应因 token 已失效而报错）。
    """
    try:
        payload = decode_token(body.refresh_token)
    except HTTPException:
        # 五轮审计：过期/签名无效 token 也是"吊销目标"——幂等成功
        # （登出不应因 token 已失效而报错）
        return {"ok": True}
    jti = payload.get("jti")
    if jti:
        try:
            consume_refresh_jti(jti, db)
        except HTTPException:
            # 已消费/过期：视为已吊销，幂等成功
            pass
    return {"ok": True}


@router.get("/me", response_model=UserResponse)
def me(user: User = Depends(require_auth)):
    return user


@router.patch("/me", response_model=UserResponse)
def update_me(
    body: UpdateProfileRequest,
    user: User = Depends(require_auth),
    db: Session = Depends(get_db),
):
    # require_auth resolves the user in its own short-lived session, so `user` is
    # detached from this request's `db`. Load a session-managed copy to mutate.
    db_user = db.get(User, user.id)
    if db_user is None:
        raise HTTPException(status_code=404, detail="User not found.")

    if body.name is not None and body.name.strip():
        db_user.name = body.name.strip()

    if body.email is not None and body.email != db_user.email:
        collision = db.scalar(select(User).where(User.email == body.email, User.id != db_user.id))
        if collision is not None:
            raise HTTPException(status_code=409, detail="Email is already in use.")
        db_user.email = body.email

    db.commit()
    return db_user


@router.post("/change-password", response_model=MessageResponse)
def change_password(
    body: ChangePasswordRequest,
    user: User = Depends(require_auth),
    db: Session = Depends(get_db),
):
    # require_auth resolves the user in its own short-lived session, so `user` is
    # detached from this request's `db`. Load a session-managed copy to mutate.
    db_user = db.get(User, user.id)
    if db_user is None or not verify_password(body.current_password, db_user.password_hash):
        raise HTTPException(status_code=401, detail="Current password is incorrect.")

    _require_password_length(body.new_password)

    db_user.password_hash = hash_password(body.new_password)
    # 四轮审计：改密吊销该用户全部未消费 refresh token——否则被盗的旧
    # refresh token 在 30 天有效期内仍可轮换新 access
    from models import RefreshTokenJti

    db.execute(
        update(RefreshTokenJti)
        .where(
            RefreshTokenJti.user_id == db_user.id,
            RefreshTokenJti.used_at.is_(None),
        )
        .values(used_at=datetime.now(timezone.utc))
    )
    db.commit()
    return MessageResponse(message="Password updated.")


@router.post("/onboarding-complete", response_model=MessageResponse)
def onboarding_complete(body: OnboardingCompleteRequest, user: User = Depends(require_auth)):
    """Fire the one-shot telemetry event after the setup wizard reaches its success state."""
    capture_onboarding_completed(email=user.email, use_case=body.use_case)
    return MessageResponse(message="Onboarding completed.")
