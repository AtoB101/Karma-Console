"""Authentication, tenant context and role-based permission checks."""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .models import ApiKey, Membership, Role, User
from .risk import rank


def hash_key(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def issue_api_key(db: Session, user: User) -> tuple[str, str]:
    secret = f"{settings.api_key_prefix}{secrets.token_urlsafe(24)}"
    prefix = secret[:12]
    db.add(ApiKey(user_id=user.id, key_hash=hash_key(secret), prefix=prefix))
    db.commit()
    return secret, prefix


def authenticate(db: Session, token: str) -> User | None:
    if not token.startswith(settings.api_key_prefix):
        return None
    row = db.execute(
        select(ApiKey).where(ApiKey.key_hash == hash_key(token), ApiKey.active.is_(True))
    ).scalar_one_or_none()
    if row is None:
        return None
    user = db.get(User, row.user_id)
    return user


@dataclass(frozen=True)
class Actor:
    user: User
    workspace_id: str
    role: Role
    source: str = "console"  # console | customer


def _membership_role(db: Session, workspace_id: str, user_id: str) -> Role | None:
    row = db.execute(
        select(Membership).where(
            Membership.workspace_id == workspace_id, Membership.user_id == user_id
        )
    ).scalar_one_or_none()
    return Role(row.role) if row else None


def require_actor(
    authorization: str | None = Header(default=None),
    x_workspace_id: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> Actor:
    if not authorization:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing API key")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Malformed authorization header")
    user = authenticate(db, token)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid API key")
    if not x_workspace_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Missing X-Workspace-Id header")
    role = _membership_role(db, x_workspace_id, user.id)
    if role is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not a member of this workspace")
    return Actor(user=user, workspace_id=x_workspace_id, role=role)


def can_execute(actor: Actor, risk_level: str) -> bool:
    level = rank(risk_level)
    if actor.role is Role.VIEWER:
        return level == 0
    if actor.role is Role.MEMBER:
        return level <= 2
    return True  # owner: subject only to approval flow for R3+


def can_execute_reason(actor: Actor, risk_level: str) -> str | None:
    if can_execute(actor, risk_level):
        return None
    return f"role {actor.role.value} is not allowed to execute risk level {risk_level}"

def require_user(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> User:
    if not authorization:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing API key")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Malformed authorization header")
    user = authenticate(db, token)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid API key")
    return user


def require_owner(actor: Actor = Depends(require_actor)) -> Actor:
    """Workspace-owner gate for instance-level administration (backups, ...).

    The platform has no global admin today; an owner of any workspace may
    administer deployment-level resources. Production deployments should add a
    dedicated platform-admin role before opening these endpoints broadly.
    """
    if actor.role is not Role.OWNER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "only the workspace owner can perform this operation")
    return actor
