"""Local operations readiness checks (pure, offline, no network, no business writes).

Shared by the CLI script ``scripts/selfcheck.py`` and the read-only
``/ops/selfcheck`` endpoint. ``run_checks`` returns a plain dict instead of
printing or exiting, so it can be embedded in the API safely.

External real credentials (Karma/ecommerce/logistics) are optional: their
absence records a WARN and never blocks startup; nothing here claims a
channel is connected.
"""
from __future__ import annotations

import os
import pathlib
import sys


def _default_root() -> pathlib.Path:
    # app/ops_check.py -> project root is one level up from app/.
    return pathlib.Path(__file__).resolve().parent.parent


def _writable_dir(path: pathlib.Path) -> tuple[bool, str]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_probe"
        probe.write_bytes(b"ok")
        probe.unlink()
        return True, str(path)
    except OSError as exc:  # noqa: PERF203 - report the path that failed
        return False, f"{path}: {exc}"


def run_checks(root: pathlib.Path | str | None = None) -> dict:
    """Run every local check and return ``{summary, checks}``."""
    root = pathlib.Path(root) if root is not None else _default_root()
    checks: list[dict] = []

    # Python version
    py_ok = sys.version_info >= (3, 10)
    checks.append({
        "level": "PASS" if py_ok else "FAIL", "name": "Python 版本 >= 3.10",
        "ok": py_ok, "detail": f"{sys.version.split()[0]} @ {sys.executable}",
    })

    # Core imports
    required = ["fastapi", "sqlalchemy", "httpx", "cryptography", "pydantic"]
    missing = []
    for mod in required:
        try:
            __import__(mod)
        except Exception:  # noqa: BLE001 - report each missing dependency
            missing.append(mod)
    checks.append({
        "level": "PASS" if not missing else "FAIL", "name": "核心依赖可导入",
        "ok": not missing,
        "detail": "缺失: " + ", ".join(missing) if missing
        else "fastapi/sqlalchemy/httpx/cryptography/pydantic 均可导入",
    })

    # Database liveness
    try:
        from app.db import ping_db

        db_ok, db_detail = ping_db()
        checks.append({
            "level": "PASS" if db_ok else "FAIL", "name": "数据库探活 SELECT 1",
            "ok": db_ok, "detail": db_detail,
        })
    except Exception as exc:  # noqa: BLE001
        checks.append({
            "level": "FAIL", "name": "数据库探活 SELECT 1",
            "ok": False, "detail": f"无法连接: {exc}",
        })

    # Writable directories
    from app.config import settings

    targets = {
        "备份目录": pathlib.Path(settings.backup_dir or "./backups"),
        "日志目录": root / "logs",
        "数据目录": root / "data",
    }
    for label, path in targets.items():
        ok, detail = _writable_dir(path)
        checks.append({
            "level": "PASS" if ok else "FAIL", "name": f"{label}可写",
            "ok": ok, "detail": detail,
        })

    # Encryption secret (dev may auto-generate; WARN, not FAIL)
    has_env = bool(os.getenv("KRAMA_SECRET_KEY"))
    has_file = (root / ".secret.key").exists()
    secret_ok = has_env or has_file
    checks.append({
        "level": "PASS" if secret_ok else "WARN",
        "name": "加密密钥（开发可自动生成）", "ok": secret_ok,
        "detail": "密钥已就位（值不明文回显）" if secret_ok
        else "未设置 KRAMA_SECRET_KEY 且无 .secret.key；首次运行将自动生成，生产请外置托管",
    })

    # Optional external credentials (WARN only, never fake connectivity)
    optional = {
        "KRAMA_API_KEY": "Karma 资金结算",
        "ECOM_INDEP_ENDPOINT": "独立站商城",
        "SF_PARTNER_ID": "顺丰物流",
    }
    present = [label for env, label in optional.items() if os.getenv(env)]
    checks.append({
        "level": "PASS" if present else "WARN",
        "name": "外部渠道凭证（非必需）", "ok": True,
        "detail": "已配置: " + ", ".join(present) if present
        else "外部渠道凭证均未配置（离线演练模式，不影响本机启动）",
    })

    fails = [c for c in checks if c["level"] == "FAIL"]
    warns = [c for c in checks if c["level"] == "WARN"]
    summary = {
        "pass": sum(1 for c in checks if c["ok"] and c["level"] == "PASS"),
        "fail": len(fails),
        "warn": len(warns),
        "healthy": len(fails) == 0,
    }
    return {"summary": summary, "checks": checks}
