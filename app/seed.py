"""Database bootstrap: create tables and seed region policy placeholders."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import SessionLocal, create_all, migrate
from .models import RegionPolicyRule

DEFAULT_RULES = [
    {
        "region": "CN",
        "rule_type": "require_approval",
        "config": {"actions": ["make_payment"]},
        "note": "PLACEHOLDER: China payment rules need legal review before launch.",
    },
    {
        "region": "EU",
        "rule_type": "require_approval",
        "config": {"actions": ["publish_product", "bulk_update_prices"]},
        "note": "PLACEHOLDER: EU consumer/marketing rules need legal review before launch.",
    },
]


def seed(db: Session) -> None:
    for rule in DEFAULT_RULES:
        exists = db.execute(
            select(RegionPolicyRule).where(
                RegionPolicyRule.region == rule["region"],
                RegionPolicyRule.rule_type == rule["rule_type"],
            )
        ).scalar_one_or_none()
        if exists is None:
            db.add(RegionPolicyRule(**rule))
    db.commit()


def bootstrap(database_url: str | None = None) -> None:
    if database_url:
        import os

        os.environ["DATABASE_URL"] = database_url
    create_all()
    migrate()
    db = SessionLocal()
    try:
        seed(db)
        from .connectors import ensure_default_connectors
        from .models import Workspace

        for (workspace_id,) in db.execute(select(Workspace.id)).all():
            ensure_default_connectors(db, workspace_id)
        db.commit()
    finally:
        db.close()
