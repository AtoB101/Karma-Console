"""Runtime configuration. Environment-driven, no external settings library."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./krama.db")
    openai_base_url: str = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-5.6-luna")
    default_model_provider: str = os.getenv("DEFAULT_MODEL_PROVIDER", "mock")
    krama_region: str = os.getenv("KRAMA_REGION", "CN")
    karma_api_base_url: str = os.getenv("KRAMA_API_BASE_URL", "https://api.karma.xyz/v1")
    karma_api_key: str = os.getenv("KRAMA_API_KEY", "")
    karma_agent_id: str = os.getenv("KRAMA_AGENT_ID", "")
    karma_identity_id: str = os.getenv("KRAMA_IDENTITY_ID", "")
    function_allow_private: bool = os.getenv("FUNCTION_ALLOW_PRIVATE", "0") in ("1", "true", "True")
    function_timeout_seconds: float = float(os.getenv("FUNCTION_TIMEOUT_SECONDS", "15"))
    secret_key: str = os.getenv("KRAMA_SECRET_KEY", "")
    secret_key_file: str = os.getenv("KRAMA_SECRET_KEY_FILE", "./.secret.key")
    chat_inbound_rate_limit: int = int(os.getenv("CHAT_INBOUND_RATE_LIMIT", "60"))
    chat_inbound_rate_window: int = int(os.getenv("CHAT_INBOUND_RATE_WINDOW", "60"))
    execute_rate_limit: int = int(os.getenv("EXECUTE_RATE_LIMIT", "300"))
    execute_rate_window: int = int(os.getenv("EXECUTE_RATE_WINDOW", "60"))
    task_resume_timeout_seconds: int = int(os.getenv("TASK_RESUME_TIMEOUT_SECONDS", "600"))
    workflow_max_retries: int = int(os.getenv("WORKFLOW_MAX_RETRIES", "2"))
    workflow_retry_base_seconds: float = float(os.getenv("WORKFLOW_RETRY_BASE_SECONDS", "1.0"))
    workflow_retry_cap_seconds: float = float(os.getenv("WORKFLOW_RETRY_CAP_SECONDS", "30.0"))
    reconcile_max_consecutive_failures: int = int(os.getenv("RECONCILE_MAX_CONSECUTIVE_FAILURES", "3"))
    scheduler_enabled: bool = os.getenv("ENABLE_BACKGROUND_SCHEDULER", "1") in ("1", "true", "True")
    scheduler_interval_seconds: int = int(os.getenv("SCHEDULER_INTERVAL_SECONDS", "60"))
    api_key_prefix: str = "krama_"
    approval_risk_threshold: int = 3  # R3 and above require explicit approval
    approval_reminder_minutes: int = int(os.getenv("APPROVAL_REMINDER_MINUTES", "10"))
    retention_enabled: bool = os.getenv("RETENTION_ENABLED", "1") in ("1", "true", "True")
    retention_messages_days: int = int(os.getenv("RETENTION_MESSAGES_DAYS", "30"))
    retention_conversations_days: int = int(os.getenv("RETENTION_CONVERSATIONS_DAYS", "30"))
    retention_stock_movements_days: int = int(os.getenv("RETENTION_STOCK_MOVEMENTS_DAYS", "90"))
    retention_todos_days: int = int(os.getenv("RETENTION_TODOS_DAYS", "30"))
    smtp_host: str = os.getenv("SMTP_HOST", "")
    smtp_port: int = int(os.getenv("SMTP_PORT", "465"))
    smtp_user: str = os.getenv("SMTP_USER", "")
    smtp_password: str = os.getenv("SMTP_PASSWORD", "")
    smtp_from: str = os.getenv("SMTP_FROM", "")
    smtp_tls: bool = os.getenv("SMTP_TLS", "1") in ("1", "true", "True")
    owner_email: str = os.getenv("OWNER_EMAIL", "")
    backup_dir: str = os.getenv("BACKUP_DIR", "./backups")
    backup_enabled: bool = os.getenv("BACKUP_ENABLED", "0") in ("1", "true", "True")
    backup_interval_hours: int = int(os.getenv("BACKUP_INTERVAL_HOURS", "24"))
    backup_retention: int = int(os.getenv("BACKUP_RETENTION", "7"))


settings = Settings()
