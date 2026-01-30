from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any

import yaml


@dataclass(slots=True)
class SchedulerJobConfig:
    name: str
    cron: str
    chat_id: str | None = None
    message: str | None = None


@dataclass(slots=True)
class MonitorConfig:
    name: str
    type: str
    threshold: float
    interval_seconds: int = 60
    chat_id: str | None = None
    enabled: bool = True


@dataclass(slots=True)
class WebhookConfig:
    host: str = "0.0.0.0"
    port: int = 8080
    token: str | None = None


@dataclass(slots=True)
class TriggersConfig:
    scheduler: list[SchedulerJobConfig]
    monitors: list[MonitorConfig]
    webhook: WebhookConfig


@dataclass(slots=True)
class TelegramConfig:
    token: str
    startup_chat_id: str | None = None
    startup_message: str | None = None


@dataclass(slots=True)
class CodexConfig:
    workspace_dir: str
    exec_path: str = "codex"
    timeout_seconds: int = 120
    max_retries: int = 2
    retry_backoff_seconds: float = 0.5


@dataclass(slots=True)
class StorageConfig:
    db_path: str
    session_dir: str


@dataclass(slots=True)
class LoggingConfig:
    level: str = "INFO"
    file: str | None = None
    max_bytes: int = 10 * 1024 * 1024
    backup_count: int = 5


@dataclass(slots=True)
class OutputConfig:
    verbosity: str = "full"


@dataclass(slots=True)
class SkillSourceConfig:
    name: str
    type: str = "github"
    repo: str = ""
    path: str = ""
    ref: str | None = None
    token_env: str | None = None


@dataclass(slots=True)
class SkillsConfig:
    sources: list[SkillSourceConfig]


@dataclass(slots=True)
class AppConfig:
    telegram: TelegramConfig
    codex: CodexConfig
    storage: StorageConfig
    logging: LoggingConfig
    triggers: TriggersConfig
    output: OutputConfig
    skills: SkillsConfig
    config_path: str | None = None


class ConfigError(RuntimeError):
    pass


def _require(mapping: dict[str, Any], key: str) -> Any:
    if key not in mapping:
        raise ConfigError(f"Missing required config key: {key}")
    return mapping[key]


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path).expanduser()
    data = yaml.safe_load(config_path.read_text())
    if not isinstance(data, dict):
        raise ConfigError("Config file root must be a mapping")

    telegram_raw = _require(data, "telegram")
    codex_raw = _require(data, "codex")
    storage_raw = _require(data, "storage")
    logging_raw = data.get("logging", {})
    output_raw = data.get("output", {})
    triggers_raw = data.get("triggers", {})
    skills_raw = data.get("skills", {})

    app_config = AppConfig(
        telegram=TelegramConfig(
            token=_require(telegram_raw, "token"),
            startup_chat_id=_optional_str(telegram_raw.get("startup_chat_id")),
            startup_message=_optional_str(telegram_raw.get("startup_message")),
        ),
        codex=CodexConfig(
            workspace_dir=_require(codex_raw, "workspace_dir"),
            exec_path=codex_raw.get("exec_path", "codex"),
            timeout_seconds=int(codex_raw.get("timeout_seconds", 120)),
            max_retries=int(codex_raw.get("max_retries", 2)),
            retry_backoff_seconds=float(codex_raw.get("retry_backoff_seconds", 0.5)),
        ),
        storage=StorageConfig(
            db_path=_require(storage_raw, "db_path"),
            session_dir=_require(storage_raw, "session_dir"),
        ),
        logging=LoggingConfig(
            level=logging_raw.get("level", "INFO"),
            file=logging_raw.get("file"),
            max_bytes=int(logging_raw.get("max_bytes", 10 * 1024 * 1024)),
            backup_count=int(logging_raw.get("backup_count", 5)),
        ),
        output=OutputConfig(
            verbosity=str(output_raw.get("verbosity", "full")),
        ),
        triggers=_parse_triggers(triggers_raw),
        skills=_parse_skills(skills_raw),
        config_path=str(config_path),
    )
    return _apply_env_overrides(app_config)


def _apply_env_overrides(config: AppConfig) -> AppConfig:
    telegram_token = os.getenv("TELEGRAM_TOKEN")
    if telegram_token:
        config.telegram.token = telegram_token
    startup_chat_id = os.getenv("TELEGRAM_STARTUP_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID")
    if startup_chat_id:
        config.telegram.startup_chat_id = startup_chat_id
    startup_message = os.getenv("TELEGRAM_STARTUP_MESSAGE")
    if startup_message:
        config.telegram.startup_message = startup_message

    codex_workspace = os.getenv("CODEX_WORKSPACE_DIR")
    if codex_workspace:
        config.codex.workspace_dir = codex_workspace
    codex_exec = os.getenv("CODEX_EXEC_PATH")
    if codex_exec:
        config.codex.exec_path = codex_exec

    db_path = os.getenv("JARVIS_DB_PATH")
    if db_path:
        config.storage.db_path = db_path
    session_dir = os.getenv("JARVIS_SESSION_DIR")
    if session_dir:
        config.storage.session_dir = session_dir

    webhook_token = os.getenv("WEBHOOK_TOKEN")
    if webhook_token:
        config.triggers.webhook.token = webhook_token
    webhook_host = os.getenv("WEBHOOK_HOST")
    if webhook_host:
        config.triggers.webhook.host = webhook_host
    webhook_port = os.getenv("WEBHOOK_PORT")
    if webhook_port:
        try:
            config.triggers.webhook.port = int(webhook_port)
        except ValueError:
            pass

    log_level = os.getenv("JARVIS_LOG_LEVEL")
    if log_level:
        config.logging.level = log_level
    log_file = os.getenv("JARVIS_LOG_FILE")
    if log_file:
        config.logging.file = log_file

    verbosity = os.getenv("JARVIS_VERBOSITY")
    if verbosity:
        config.output.verbosity = verbosity

    return config


def _parse_triggers(raw: Any) -> TriggersConfig:
    if not isinstance(raw, dict):
        raw = {}
    scheduler_raw = raw.get("scheduler", []) or []
    monitors_raw = raw.get("monitors", []) or []
    webhook_raw = raw.get("webhook", {}) or {}

    scheduler = [
        SchedulerJobConfig(
            name=str(job.get("name", "")),
            cron=str(job.get("cron", "")),
            chat_id=_optional_str(job.get("chat_id")),
            message=_optional_str(job.get("message")),
        )
        for job in scheduler_raw
        if isinstance(job, dict)
    ]
    monitors = [
        MonitorConfig(
            name=str(monitor.get("name", "")),
            type=str(monitor.get("type", "")),
            threshold=float(monitor.get("threshold", 0)),
            interval_seconds=int(monitor.get("interval_seconds", 60)),
            chat_id=_optional_str(monitor.get("chat_id")),
            enabled=bool(monitor.get("enabled", True)),
        )
        for monitor in monitors_raw
        if isinstance(monitor, dict)
    ]
    webhook = WebhookConfig(
        host=str(webhook_raw.get("host", "0.0.0.0")),
        port=int(webhook_raw.get("port", 8080)),
        token=_optional_str(webhook_raw.get("token")),
    )
    return TriggersConfig(scheduler=scheduler, monitors=monitors, webhook=webhook)


def _parse_skills(raw: Any) -> SkillsConfig:
    if not isinstance(raw, dict):
        raw = {}
    sources_raw = raw.get("sources", []) or []
    sources: list[SkillSourceConfig] = []
    for entry in sources_raw:
        if not isinstance(entry, dict):
            continue
        sources.append(
            SkillSourceConfig(
                name=str(entry.get("name", "")),
                type=str(entry.get("type", "github")),
                repo=str(entry.get("repo", "")),
                path=str(entry.get("path", "")),
                ref=_optional_str(entry.get("ref")),
                token_env=_optional_str(entry.get("token_env")),
            )
        )
    return SkillsConfig(sources=sources)


def persist_skill_source(config_path: str, source: SkillSourceConfig) -> bool:
    path = Path(config_path).expanduser()
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ConfigError("Config file root must be a mapping")
    skills = data.get("skills")
    if not isinstance(skills, dict):
        skills = {}
        data["skills"] = skills
    sources = skills.get("sources")
    if not isinstance(sources, list):
        sources = []
        skills["sources"] = sources

    payload = {
        "name": source.name,
        "type": source.type,
        "repo": source.repo,
        "path": source.path,
    }
    if source.ref:
        payload["ref"] = source.ref
    if source.token_env:
        payload["token_env"] = source.token_env

    updated = False
    for idx, entry in enumerate(sources):
        if isinstance(entry, dict) and entry.get("name") == source.name:
            sources[idx] = payload
            updated = True
            break
    if not updated:
        sources.append(payload)

    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    return updated


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)
