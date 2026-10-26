"""Instance-level backup and recovery for the SQLite database (plan M10).

Backups snapshot the whole deployment database (all workspaces), not a single
workspace, so they are administered through the workspace-owner gate and every
action lands in the append-only audit log.

Implementation notes
--------------------
- The SQLite online backup API (``sqlite3.Connection.backup``) copies a
  consistent snapshot from the live database to the target file without
  stopping the server.
- Restore runs the same API in the reverse direction (backup file -> live
  database path), verifies the stored SHA-256 checksum first, creates a
  pre-restore safety snapshot, and checks ``PRAGMA integrity_check`` after the
  swap. Restore requires an explicit ``confirm`` flag and is audited as R4.
- Retention pruning removes the oldest backup files and marks their records
  ``pruned`` so history is preserved even after the file is gone.
- Automatic backups are opt-in via ``BACKUP_ENABLED``; the loop runs only when
  enabled, so it never changes existing scheduler behaviour.
"""
from __future__ import annotations

import asyncio
import hashlib
import pathlib
import re
import sqlite3
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .audit import record
from .config import settings
from .models import BackupRecord, Decision

_SQLITE_URL_RE = re.compile(r"^sqlite:///(.*)$")


class BackupError(Exception):
    """Raised for any backup/restore failure; mapped to HTTP 409 by routes."""


def _db_path() -> pathlib.Path:
    match = _SQLITE_URL_RE.match(settings.database_url)
    if match is None:
        raise BackupError("backup is only supported for the SQLite backend")
    return pathlib.Path(match.group(1))


def _backup_dir() -> pathlib.Path:
    """Resolve the user-prepared backup target and verify it exists and is writable.

    The operator supplies the target (local disk folder or a mounted cloud-drive
    sync folder via ``BACKUP_DIR``); we never pick a random drive. A missing or
    non-writable target fails loudly instead of silently dropping snapshots.
    """
    raw = (settings.backup_dir or "").strip()
    if not raw:
        raise BackupError("backup_dir is not configured")
    directory = pathlib.Path(raw)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".krama_write_probe"
        probe.write_bytes(b"ok")
        probe.unlink()
    except OSError as exc:
        raise BackupError(f"backup target not writable: {raw} ({exc})") from exc
    return directory


def _checksum(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _snapshot(source: pathlib.Path, target: pathlib.Path) -> None:
    """Consistent online copy via the SQLite backup API (either direction)."""
    src = sqlite3.connect(str(source))
    dst = sqlite3.connect(str(target))
    try:
        with dst:
            src.backup(dst)
    finally:
        src.close()
        dst.close()


def _integrity_ok(db_path: pathlib.Path) -> bool:
    con = sqlite3.connect(str(db_path))
    try:
        row = con.execute("PRAGMA integrity_check").fetchone()
        return bool(row) and row[0] == "ok"
    finally:
        con.close()


def create_backup(db: Session, actor, *, reason: str) -> BackupRecord:
    """Create a consistent snapshot of the live database and record it."""
    db_path = _db_path()
    if not db_path.exists():
        raise BackupError(f"database file not found: {db_path}")

    filename = (
        f"backup_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_"
        f"{uuid.uuid4().hex[:8]}.db"
    )
    target = _backup_dir() / filename
    try:
        _snapshot(db_path, target)
    except sqlite3.Error as exc:
        record(
            db, workspace_id=actor.workspace_id, actor_id=actor.user.id,
            action="backup.create", risk_level="R1", decision=Decision.DENIED,
            reason=f"backup failed: {exc}", data_tier="SENSITIVE",
        )
        db.commit()
        raise BackupError(f"backup failed: {exc}") from exc

    backup = BackupRecord(
        workspace_id=actor.workspace_id,
        filename=filename,
        size_bytes=target.stat().st_size,
        checksum=_checksum(target),
        reason=reason,
        created_by=actor.user.email,
        status="ok",
    )
    db.add(backup)
    db.flush()
    record(
        db, workspace_id=actor.workspace_id, actor_id=actor.user.id,
        action="backup.create", risk_level="R1", decision=Decision.ALLOWED,
        resource=f"backup:{backup.id}", evidence={"filename": filename},
        reason=reason, data_tier="SENSITIVE",
    )
    db.commit()
    db.refresh(backup)
    return backup


def list_backups(db: Session) -> list[BackupRecord]:
    return db.execute(
        select(BackupRecord).order_by(BackupRecord.created_at.desc())
    ).scalars().all()


def prune_backups(db: Session, *, keep: int | None = None) -> int:
    """Keep the newest `keep` healthy backups; delete older files, mark pruned."""
    keep = settings.backup_retention if keep is None else keep
    rows = db.execute(
        select(BackupRecord)
        .where(BackupRecord.status == "ok")
        .order_by(BackupRecord.created_at.desc())
    ).scalars().all()
    pruned = 0
    for backup in rows[keep:]:
        path = _backup_dir() / backup.filename
        if path.exists():
            path.unlink()
        backup.status = "pruned"
        pruned += 1
    if pruned:
        db.commit()
    return pruned


def restore_backup(db: Session, actor, backup_id: str, *, confirm: bool) -> dict:
    """Restore the database to a backup's state.

    Guards (in order): owner gate (enforced by the route), explicit confirm,
    record exists and is healthy, file present and checksum matches. A
    pre-restore safety snapshot is created first, and integrity is verified
    after the swap. The restored record is marked ``restored``; the safety
    snapshot remains available as a manual fallback.
    """
    if not confirm:
        raise BackupError("restore requires explicit confirmation (confirm=true)")

    backup = db.get(BackupRecord, backup_id)
    if backup is None:
        raise BackupError("backup not found")
    if backup.status != "ok":
        raise BackupError(f"backup cannot be restored from status {backup.status}")

    db_path = _db_path()
    backup_file = _backup_dir() / backup.filename
    if not backup_file.exists():
        backup.status = "failed"
        record(
            db, workspace_id=actor.workspace_id, actor_id=actor.user.id,
            action="backup.restore", risk_level="R4", decision=Decision.DENIED,
            resource=f"backup:{backup.id}", reason="backup file missing",
            data_tier="CRITICAL",
        )
        db.commit()
        raise BackupError(f"backup file missing: {backup_file}")

    if _checksum(backup_file) != backup.checksum:
        backup.status = "failed"
        record(
            db, workspace_id=actor.workspace_id, actor_id=actor.user.id,
            action="backup.restore", risk_level="R4", decision=Decision.DENIED,
            resource=f"backup:{backup.id}", reason="checksum mismatch - backup file corrupted",
            data_tier="CRITICAL",
        )
        db.commit()
        raise BackupError("checksum mismatch: backup file is corrupted and will not be restored")

    # Safety snapshot of the current state before overwriting.
    safety = create_backup(db, actor, reason=f"pre-restore safety snapshot (restoring {backup_id})")

    try:
        _snapshot(backup_file, db_path)
    except sqlite3.Error as exc:
        backup.status = "failed"
        record(
            db, workspace_id=actor.workspace_id, actor_id=actor.user.id,
            action="backup.restore", risk_level="R4", decision=Decision.DENIED,
            resource=f"backup:{backup.id}", reason=f"restore failed: {exc}",
            evidence={"safety_backup_id": safety.id}, data_tier="CRITICAL",
        )
        db.commit()
        raise BackupError(f"restore failed (database busy or corrupted): {exc}") from exc

    if not _integrity_ok(db_path):
        backup.status = "failed"
        record(
            db, workspace_id=actor.workspace_id, actor_id=actor.user.id,
            action="backup.restore", risk_level="R4", decision=Decision.DENIED,
            resource=f"backup:{backup.id}", reason="post-restore integrity check failed",
            evidence={"safety_backup_id": safety.id}, data_tier="CRITICAL",
        )
        db.commit()
        raise BackupError("post-restore integrity check failed; safety snapshot retained")

    now = datetime.now(timezone.utc)
    # The restored snapshot predates both the backup record and the safety
    # record, so re-insert them into the restored database with a fresh
    # session: their files still exist on disk and the restore trail must
    # survive the swap.
    from .db import SessionLocal

    meta = SessionLocal()
    try:
        meta.add(BackupRecord(
            id=backup.id, workspace_id=backup.workspace_id, filename=backup.filename,
            size_bytes=backup.size_bytes, checksum=backup.checksum, reason=backup.reason,
            created_by=backup.created_by, status="restored", restored_at=now,
            created_at=backup.created_at,
        ))
        meta.add(BackupRecord(
            id=safety.id, workspace_id=safety.workspace_id, filename=safety.filename,
            size_bytes=safety.size_bytes, checksum=safety.checksum, reason=safety.reason,
            created_by=safety.created_by, status="ok", created_at=safety.created_at,
        ))
        record(
            meta, workspace_id=actor.workspace_id, actor_id=actor.user.id,
            action="backup.restore", risk_level="R4", decision=Decision.ALLOWED,
            resource=f"backup:{backup.id}",
            evidence={"safety_backup_id": safety.id, "integrity": "ok"},
            data_tier="CRITICAL",
        )
        meta.commit()
    finally:
        meta.close()
    # The live SQLite file was swapped underneath the shared engine. Dispose the
    # pool so every later query reconnects to the restored file instead of a
    # stale handle to the overwritten one (also avoids transient Windows
    # ERROR_ACCESS_DENIED on the first child process spawned right after).
    from .db import engine

    engine.dispose()
    backup.restored_at = now
    return {
        "backup_id": backup.id,
        "restored_at": now.isoformat(),
        "safety_backup_id": safety.id,
        "integrity": "ok",
    }


def maybe_auto_backup(db: Session) -> BackupRecord | None:
    """Create a scheduled backup if the newest healthy one is stale (opt-in)."""
    if not settings.backup_enabled:
        return None
    newest = db.execute(
        select(BackupRecord)
        .where(BackupRecord.status == "ok")
        .order_by(BackupRecord.created_at.desc())
    ).scalars().first()
    if newest is not None:
        created = newest.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age_hours = (datetime.now(timezone.utc) - created).total_seconds() / 3600
        if age_hours < settings.backup_interval_hours:
            return None

    # Run as any workspace owner (mirrors scheduler's actor resolution).
    from .models import Membership, Role, User
    from .security import Actor

    owner = db.execute(
        select(Membership).where(Membership.role == Role.OWNER.value)
    ).scalars().first()
    if owner is None:
        return None
    user = db.get(User, owner.user_id)
    actor = Actor(user=user, workspace_id=owner.workspace_id, role=Role.OWNER)
    backup = create_backup(db, actor, reason="scheduled")
    prune_backups(db)
    return backup


async def _backup_loop() -> None:
    from .db import SessionLocal

    while True:
        await asyncio.sleep(15 * 60)
        db = SessionLocal()
        try:
            created = maybe_auto_backup(db)
            if created is not None:
                db.commit()
        except Exception:  # noqa: BLE001 - background loop must survive transient errors
            db.rollback()
        finally:
            db.close()


def start_auto_backup(app) -> asyncio.Task | None:
    if not settings.backup_enabled:
        return None
    task = asyncio.create_task(_backup_loop())
    app.state.backup_loop = task
    return task


async def stop_auto_backup(app) -> None:
    task = getattr(app.state, "backup_loop", None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
