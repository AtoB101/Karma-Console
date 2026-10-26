"""Database engine, session factory and declarative base (SQLite)."""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings

connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}

engine = create_engine(settings.database_url, connect_args=connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


class Base(DeclarativeBase):
    pass


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def ping_db() -> tuple[bool, str]:
    """Lightweight liveness probe: run ``SELECT 1`` on the current engine.

    Returns ``(ok, detail)``; never raises so the readiness route can map it to
    HTTP 503 instead of crashing the probe.
    """
    from sqlalchemy import text

    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True, "ok"
    except Exception as exc:  # noqa: BLE001 - probe must report, not raise
        return False, str(exc)


def create_all() -> None:
    from . import models  # noqa: F401  ensure models are registered

    Base.metadata.create_all(bind=engine)


def migrate() -> None:
    """Minimal additive migration for SQLite dev databases created before a schema change."""
    if not settings.database_url.startswith("sqlite"):
        return
    from sqlalchemy import text

    with engine.begin() as conn:
        layout_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(layout_areas)"))}
        if "parent_id" not in layout_cols:
            conn.execute(text("ALTER TABLE layout_areas ADD COLUMN parent_id VARCHAR(32)"))
        if "stage" not in layout_cols:
            conn.execute(text("ALTER TABLE layout_areas ADD COLUMN stage VARCHAR(16) NOT NULL DEFAULT 'live'"))
        product_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(products)"))}
        if "layout_area_id" not in product_cols:
            conn.execute(text("ALTER TABLE products ADD COLUMN layout_area_id VARCHAR(32)"))
        if "stock" not in product_cols:
            conn.execute(text("ALTER TABLE products ADD COLUMN stock INTEGER NOT NULL DEFAULT 0"))
        if "photo_url" not in product_cols:
            conn.execute(text("ALTER TABLE products ADD COLUMN photo_url VARCHAR(2048) NOT NULL DEFAULT ''"))
        if "specs" not in product_cols:
            conn.execute(text("ALTER TABLE products ADD COLUMN specs TEXT"))
        if "detail" not in product_cols:
            conn.execute(text("ALTER TABLE products ADD COLUMN detail TEXT NOT NULL DEFAULT ''"))
        if "qualifications" not in product_cols:
            conn.execute(text("ALTER TABLE products ADD COLUMN qualifications TEXT"))
        customer_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(customers)"))}
        if "stage" not in customer_cols:
            conn.execute(text("ALTER TABLE customers ADD COLUMN stage VARCHAR(16) NOT NULL DEFAULT 'new'"))
        workspace_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(workspaces)"))}
        if "settings" not in workspace_cols:
            conn.execute(text("ALTER TABLE workspaces ADD COLUMN settings TEXT"))
        function_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(workspace_functions)"))}
        if "stage" not in function_cols:
            conn.execute(text("ALTER TABLE workspace_functions ADD COLUMN stage VARCHAR(16) NOT NULL DEFAULT 'live'"))
        if "parent_area_id" not in function_cols:
            conn.execute(text("ALTER TABLE workspace_functions ADD COLUMN parent_area_id VARCHAR(32)"))
        approval_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(approval_requests)"))}
        if "confirm_code" not in approval_cols:
            conn.execute(text("ALTER TABLE approval_requests ADD COLUMN confirm_code VARCHAR(12) NOT NULL DEFAULT ''"))
        if "reminder_sent" not in approval_cols:
            conn.execute(text("ALTER TABLE approval_requests ADD COLUMN reminder_sent BOOLEAN NOT NULL DEFAULT 0"))
        todo_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(todos)"))}
        if "reminder_sent" not in todo_cols:
            conn.execute(text("ALTER TABLE todos ADD COLUMN reminder_sent BOOLEAN NOT NULL DEFAULT 0"))
        audit_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(audit_logs)"))}
        if "data_tier" not in audit_cols:
            conn.execute(text("ALTER TABLE audit_logs ADD COLUMN data_tier VARCHAR(16) NOT NULL DEFAULT 'INTERNAL'"))
        if "data_labels" not in audit_cols:
            conn.execute(text("ALTER TABLE audit_logs ADD COLUMN data_labels VARCHAR(255) NOT NULL DEFAULT ''"))
        if "prev_hash" not in audit_cols:
            conn.execute(text("ALTER TABLE audit_logs ADD COLUMN prev_hash VARCHAR(64) NOT NULL DEFAULT ''"))
        if "content_hash" not in audit_cols:
            conn.execute(text("ALTER TABLE audit_logs ADD COLUMN content_hash VARCHAR(64) NOT NULL DEFAULT ''"))
        exec_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(executions)"))}
        if "workspace_id" not in exec_cols:
            conn.execute(text("ALTER TABLE executions ADD COLUMN workspace_id VARCHAR(32) NOT NULL DEFAULT ''"))
        # P0 获客底座：线索评分/分级/来源/负责经理人/跟进时间，全部可空带默认，向后兼容
        cust_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(customers)"))}
        if "score" not in cust_cols:
            conn.execute(text("ALTER TABLE customers ADD COLUMN score INTEGER NOT NULL DEFAULT 0"))
        if "grade" not in cust_cols:
            conn.execute(text("ALTER TABLE customers ADD COLUMN grade VARCHAR(8) NOT NULL DEFAULT ''"))
        if "source" not in cust_cols:
            conn.execute(text("ALTER TABLE customers ADD COLUMN source VARCHAR(64) NOT NULL DEFAULT ''"))
        if "owner_id" not in cust_cols:
            conn.execute(text("ALTER TABLE customers ADD COLUMN owner_id VARCHAR(32) NOT NULL DEFAULT ''"))
        if "last_touch_at" not in cust_cols:
            conn.execute(text("ALTER TABLE customers ADD COLUMN last_touch_at DATETIME"))
        if "next_follow_at" not in cust_cols:
            conn.execute(text("ALTER TABLE customers ADD COLUMN next_follow_at DATETIME"))
        if "satisfaction" not in cust_cols:
            conn.execute(text("ALTER TABLE customers ADD COLUMN satisfaction REAL"))
        # 受控开发变更单：老库补 submitted_at 列（新库由 create_all 自动创建）
        cr_tbl = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='change_requests'")
        ).scalar()
        if cr_tbl:
            cr_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(change_requests)"))}
            if "submitted_at" not in cr_cols:
                conn.execute(text("ALTER TABLE change_requests ADD COLUMN submitted_at DATETIME"))
        # P1 工单表：老库兜底建表（新库由 create_all 自动创建）
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS tickets ("
            "id VARCHAR(32) PRIMARY KEY,"
            "workspace_id VARCHAR(32) NOT NULL,"
            "customer_id VARCHAR(32) DEFAULT '',"
            "order_id VARCHAR(32) DEFAULT '',"
            "channel VARCHAR(32) DEFAULT 'chat',"
            "type VARCHAR(24) DEFAULT 'other',"
            "priority VARCHAR(8) DEFAULT 'P2',"
            "status VARCHAR(16) DEFAULT 'new',"
            "assignee_id VARCHAR(32) DEFAULT '',"
            "title VARCHAR(200) DEFAULT '',"
            "description TEXT DEFAULT '',"
            "created_at DATETIME,"
            "updated_at DATETIME,"
            "closed_at DATETIME)"
        ))
        # P1 FAQ 表：老库兜底建表（新库由 create_all 自动创建）
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS faqs ("
            "id VARCHAR(32) PRIMARY KEY,"
            "workspace_id VARCHAR(32) NOT NULL,"
            "question VARCHAR(200) DEFAULT '',"
            "answer TEXT DEFAULT '',"
            "tags TEXT DEFAULT '[]',"
            "hits INTEGER NOT NULL DEFAULT 0,"
            "created_at DATETIME,"
            "updated_at DATETIME)"
        ))
        # P1 工单审计时间线：老库兜底建表（新库由 create_all 自动创建）
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS ticket_events ("
            "id VARCHAR(32) PRIMARY KEY,"
            "workspace_id VARCHAR(32) NOT NULL,"
            "ticket_id VARCHAR(32) NOT NULL,"
            "event VARCHAR(24) DEFAULT 'created',"
            "from_status VARCHAR(16) DEFAULT '',"
            "to_status VARCHAR(16) DEFAULT '',"
            "actor_label VARCHAR(80) DEFAULT '',"
            "note TEXT DEFAULT '',"
            "created_at DATETIME)"
        ))
        # 可执行 DAG 工作流图：老库兜底建表（新库由 create_all 自动创建）
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS workflow_graphs ("
            "id VARCHAR(32) PRIMARY KEY,"
            "workspace_id VARCHAR(32) NOT NULL,"
            "name VARCHAR(200) DEFAULT '未命名工作流',"
            "nodes TEXT DEFAULT '[]',"
            "edges TEXT DEFAULT '[]',"
            "status VARCHAR(16) DEFAULT 'draft',"
            "created_at DATETIME,"
            "updated_at DATETIME)"
        ))
        # 受控开发 Agent 变更单：老库兜底建表（新库由 create_all 自动创建）
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS change_requests ("
            "id VARCHAR(32) PRIMARY KEY,"
            "workspace_id VARCHAR(32) NOT NULL,"
            "title VARCHAR(200) DEFAULT '',"
            "requirement TEXT DEFAULT '',"
            "domains TEXT DEFAULT '[]',"
            "change_points TEXT DEFAULT '[]',"
            "impact TEXT DEFAULT '[]',"
            "test_plan TEXT DEFAULT '[]',"
            "rollback TEXT DEFAULT '',"
            "draft TEXT DEFAULT '',"
            "risk_level VARCHAR(4) DEFAULT 'R1',"
            "status VARCHAR(16) DEFAULT 'draft',"
            "regression_status VARCHAR(8) DEFAULT 'pending',"
            "forbidden_hits TEXT DEFAULT '[]',"
            "requested_by VARCHAR(80) DEFAULT '',"
            "reviewed_by VARCHAR(80) DEFAULT '',"
            "decision_note TEXT DEFAULT '',"
            "created_at DATETIME,"
            "submitted_at DATETIME,"
            "decided_at DATETIME,"
            "applied_at DATETIME)"
        ))
        # 内容流水线（文案/视频）：老库兜底建表（新库由 create_all 自动创建）
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS content_items ("
            "id VARCHAR(32) PRIMARY KEY,"
            "workspace_id VARCHAR(32) NOT NULL,"
            "kind VARCHAR(8) DEFAULT 'copy',"
            "title VARCHAR(200) DEFAULT '',"
            "topic TEXT DEFAULT '',"
            "body TEXT DEFAULT '',"
            "assets TEXT DEFAULT '{}',"
            "model_id VARCHAR(32) DEFAULT '',"
            "status VARCHAR(18) DEFAULT 'draft',"
            "reviewer_note TEXT DEFAULT '',"
            "created_by VARCHAR(80) DEFAULT '',"
            "reviewed_by VARCHAR(80) DEFAULT '',"
            "target_channels TEXT DEFAULT '[]',"
            "source_ref VARCHAR(120) DEFAULT '',"
            "metrics TEXT DEFAULT '{}',"
            "decided_at DATETIME,"
            "published_at DATETIME,"
            "created_at DATETIME,"
            "updated_at DATETIME)"
        ))
        # 社交运营：评论箱 + 热门视频建档（老库兜底建表，新库由 create_all 自动创建）
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS social_comments ("
            "id VARCHAR(32) PRIMARY KEY,"
            "workspace_id VARCHAR(32) NOT NULL,"
            "content_id VARCHAR(32) DEFAULT '',"
            "platform VARCHAR(24) DEFAULT 'manual',"
            "external_id VARCHAR(80) DEFAULT '',"
            "author VARCHAR(80) DEFAULT '',"
            "text TEXT DEFAULT '',"
            "sentiment VARCHAR(12) DEFAULT 'neutral',"
            "status VARCHAR(16) DEFAULT 'new',"
            "reply_draft TEXT DEFAULT '',"
            "received_at DATETIME,"
            "created_at DATETIME)"
        ))
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS trending_videos ("
            "id VARCHAR(32) PRIMARY KEY,"
            "workspace_id VARCHAR(32) NOT NULL,"
            "platform VARCHAR(24) DEFAULT '',"
            "url VARCHAR(500) DEFAULT '',"
            "title VARCHAR(200) DEFAULT '',"
            "category VARCHAR(60) DEFAULT '',"
            "metrics TEXT DEFAULT '{}',"
            "hot_points TEXT DEFAULT '[]',"
            "summary TEXT DEFAULT '',"
            "analyzed_at DATETIME,"
            "created_at DATETIME)"
        ))
        # 轨迹自学习：lessons 表（老库兜底建表，新库由 create_all 自动创建）
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS lessons ("
            "id VARCHAR(32) PRIMARY KEY,"
            "workspace_id VARCHAR(32) NOT NULL,"
            "pattern_key VARCHAR(120) NOT NULL,"
            "tool_name VARCHAR(120) DEFAULT '',"
            "status VARCHAR(16) DEFAULT '',"
            "lesson TEXT DEFAULT '',"
            "success_count INTEGER DEFAULT 0,"
            "fail_count INTEGER DEFAULT 0,"
            "confidence REAL DEFAULT 0.0,"
            "applied BOOLEAN DEFAULT 0,"
            "created_at DATETIME,"
            "updated_at DATETIME,"
            "last_used_at DATETIME,"
            "UNIQUE(workspace_id, pattern_key))"
        ))
        # 统一渠道账号：老库兜底建表
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS channel_accounts ("
            "id VARCHAR(32) PRIMARY KEY,"
            "workspace_id VARCHAR(32) NOT NULL,"
            "channel_type VARCHAR(24) DEFAULT 'webchat',"
            "account_label VARCHAR(80) DEFAULT '',"
            "identifier VARCHAR(160) DEFAULT '',"
            "credential_enc TEXT DEFAULT '',"
            "enabled INTEGER DEFAULT 1,"
            "status VARCHAR(20) DEFAULT 'credential_required',"
            "risk_note VARCHAR(200) DEFAULT '',"
            "capabilities TEXT DEFAULT '[]',"
            "last_checked_at DATETIME,"
            "created_by VARCHAR(32) DEFAULT '',"
            "created_at DATETIME,"
            "updated_at DATETIME)"
        ))
