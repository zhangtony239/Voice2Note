"""`v2n rec-daemon` 服务配置加载、校验与脱敏错误报告。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from urllib.parse import urlparse

import yaml

DAEMON_CONFIG_FILE_NAME = "rec-daemon.yaml"
DAEMON_TEMPLATE_RESOURCE = "rec-daemon-template.yaml"

ALL_DAEMON_FIELDS: tuple[str, ...] = (
    "TORCH_BACKEND",
    "ASR_LANGUAGE",
    "DISABLE_MMAP",
    "SERVER_HOST",
    "SERVER_PORT",
    "SERVER_AUTH_TOKEN_HASH",
    "SESSION_TOKEN_SECRET",
    "OPENLIST_WEBDAV_URL",
    "OPENLIST_USERNAME",
    "OPENLIST_PASSWORD",
    "OPENLIST_TIMEOUT_SECONDS",
    "OPENLIST_VERIFY_TLS",
    "CLOUD_ROOT_PREFIX",
    "SESSION_STATE_PATH",
    "SESSION_WORK_PATH",
    "POLL_INTERVAL_SECONDS",
    "POLL_MAX_INTERVAL_SECONDS",
    "ASR_MAX_CONCURRENCY",
    "MAX_ACTIVE_SESSIONS",
    "MAX_AUDIO_CHUNK_BYTES",
    "SESSION_IDLE_WARN_HOURS",
    "LOG_LEVEL",
)

REQUIRED_DAEMON_FIELDS: tuple[str, ...] = tuple(
    f for f in ALL_DAEMON_FIELDS if f != "DISABLE_MMAP"
)

VALID_TORCH_BACKENDS: tuple[str, ...] = ("xpu", "cuda")
VALID_LOG_LEVELS: tuple[str, ...] = ("DEBUG", "INFO", "WARNING", "ERROR")
_DISABLE_MMAP_DEFAULTS: dict[str, bool] = {"xpu": True, "cuda": False}
_TOKEN_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class DaemonConfigError(Exception):
    """rec-daemon 配置文件缺失、无法解析或字段校验失败（错误消息已脱敏，不含字段值）。"""


@dataclass(frozen=True)
class DaemonConfig:
    """校验后的 rec-daemon 服务配置。"""

    config_file: Path
    torch_backend: str
    asr_language: str
    disable_mmap: bool
    server_host: str
    server_port: int
    server_auth_token_hash: str
    session_token_secret: str
    openlist_webdav_url: str
    openlist_username: str
    openlist_password: str
    openlist_timeout_seconds: int
    openlist_verify_tls: bool
    cloud_root_prefix: str
    session_state_path: Path
    session_work_path: Path
    poll_interval_seconds: int
    poll_max_interval_seconds: int
    asr_max_concurrency: int
    max_active_sessions: int
    max_audio_chunk_bytes: int
    session_idle_warn_hours: int
    log_level: str


def daemon_config_path(config_file: Path | None = None) -> Path:
    if config_file is not None:
        return config_file.resolve()
    return (Path.cwd() / DAEMON_CONFIG_FILE_NAME).resolve()


def read_daemon_template() -> str:
    return (
        resources.files("voice2note.assets")
        .joinpath(DAEMON_TEMPLATE_RESOURCE)
        .read_text(encoding="utf-8")
    )


def create_daemon_config(config_file: Path | None = None) -> Path:
    target = daemon_config_path(config_file)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(read_daemon_template(), encoding="utf-8")
    return target


def _is_missing(value: object) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def _require_non_empty_single_line_str(data: dict, field: str) -> str:
    val = data.get(field)
    if not isinstance(val, str):
        raise DaemonConfigError(f"字段 {field} 必须是字符串类型。")
    stripped = val.strip()
    if not stripped:
        raise DaemonConfigError(f"字段 {field} 不能为空。")
    if "\r" in val or "\n" in val:
        raise DaemonConfigError(f"字段 {field} 不能包含换行符。")
    return stripped


def _require_positive_int(data: dict, field: str) -> int:
    val = data.get(field)
    if isinstance(val, bool) or not isinstance(val, int) or val <= 0:
        raise DaemonConfigError(f"字段 {field} 必须是正整数。")
    return val


def _resolve_dir_path(base_dir: Path, raw_path: str, field: str) -> Path:
    p = Path(raw_path)
    resolved = (base_dir / p).resolve() if not p.is_absolute() else p.resolve()
    try:
        resolved.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DaemonConfigError(
            f"字段 {field} 指定的目录无法创建（{exc.__class__.__name__}）。"
        ) from exc
    if not resolved.is_dir():
        raise DaemonConfigError(f"字段 {field} 指定的路径不是有效目录。")
    return resolved


def load_daemon_config(
    config_file: Path | None = None,
    *,
    host_override: str | None = None,
    port_override: int | None = None,
    log_level_override: str | None = None,
) -> DaemonConfig:
    """加载并严格校验 rec-daemon.yaml，所有错误消息仅含字段名与原因，绝不输出字段值。"""
    path = daemon_config_path(config_file)
    if not path.is_file():
        raise DaemonConfigError(
            f"未找到服务配置文件: {path.name}。请先运行 `v2n rec-daemon init-config` 创建配置文件。"
        )

    try:
        raw_text = path.read_text(encoding="utf-8")
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise DaemonConfigError(
            f"服务配置文件 {path.name} 不是合法的 YAML 格式。"
        ) from exc

    if not isinstance(data, dict):
        raise DaemonConfigError(
            f"服务配置文件 {path.name} 的顶层结构必须是键值映射（mapping）。"
        )

    missing = [f for f in REQUIRED_DAEMON_FIELDS if _is_missing(data.get(f))]
    if missing:
        raise DaemonConfigError(
            f"服务配置文件 {path.name} 缺少以下必填字段:\n"
            + "\n".join(f"  - {f}" for f in missing)
        )

    torch_backend = _require_non_empty_single_line_str(data, "TORCH_BACKEND").lower()
    if torch_backend not in VALID_TORCH_BACKENDS:
        raise DaemonConfigError(
            f"字段 TORCH_BACKEND 取值非法，仅允许 {' | '.join(VALID_TORCH_BACKENDS)}。"
        )

    asr_language = _require_non_empty_single_line_str(data, "ASR_LANGUAGE")

    if "DISABLE_MMAP" in data and data["DISABLE_MMAP"] is not None and data["DISABLE_MMAP"] != "":
        raw_mmap = data["DISABLE_MMAP"]
        if not isinstance(raw_mmap, bool):
            raise DaemonConfigError("字段 DISABLE_MMAP 必须是布尔值（true 或 false）。")
        disable_mmap = raw_mmap
    else:
        disable_mmap = _DISABLE_MMAP_DEFAULTS[torch_backend]

    server_host = (
        host_override.strip()
        if host_override is not None
        else _require_non_empty_single_line_str(data, "SERVER_HOST")
    )
    if not server_host:
        raise DaemonConfigError("字段 SERVER_HOST 不能为空。")

    raw_port = port_override if port_override is not None else data.get("SERVER_PORT")
    if isinstance(raw_port, bool) or not isinstance(raw_port, int) or not (1 <= raw_port <= 65535):
        raise DaemonConfigError("字段 SERVER_PORT 必须是 1 到 65535 之间的整数。")
    server_port = raw_port

    token_hash = _require_non_empty_single_line_str(data, "SERVER_AUTH_TOKEN_HASH")
    if not _TOKEN_HASH_RE.fullmatch(token_hash):
        raise DaemonConfigError(
            "字段 SERVER_AUTH_TOKEN_HASH 格式非法，必须为 sha256:<64位小写十六进制>。"
        )

    session_secret = _require_non_empty_single_line_str(data, "SESSION_TOKEN_SECRET")
    if len(session_secret) < 32:
        raise DaemonConfigError("字段 SESSION_TOKEN_SECRET 长度不足，至少需要 32 个字符。")

    webdav_url = _require_non_empty_single_line_str(data, "OPENLIST_WEBDAV_URL")
    parsed_url = urlparse(webdav_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise DaemonConfigError(
            "字段 OPENLIST_WEBDAV_URL 格式非法，必须是包含主机名的 http:// 或 https:// 地址。"
        )

    openlist_username = _require_non_empty_single_line_str(data, "OPENLIST_USERNAME")
    openlist_password = _require_non_empty_single_line_str(data, "OPENLIST_PASSWORD")
    openlist_timeout = _require_positive_int(data, "OPENLIST_TIMEOUT_SECONDS")

    verify_tls = data.get("OPENLIST_VERIFY_TLS")
    if not isinstance(verify_tls, bool):
        raise DaemonConfigError("字段 OPENLIST_VERIFY_TLS 必须是布尔值（true 或 false）。")

    cloud_root_prefix = _require_non_empty_single_line_str(data, "CLOUD_ROOT_PREFIX")
    if (
        cloud_root_prefix.startswith("/")
        or cloud_root_prefix.endswith("/")
        or "\\" in cloud_root_prefix
        or ".." in cloud_root_prefix.split("/")
        or ":" in cloud_root_prefix
    ):
        raise DaemonConfigError(
            "字段 CLOUD_ROOT_PREFIX 格式非法，必须是不含首尾斜杠、反斜杠或上级目录的相对前缀。"
        )

    raw_state_path = _require_non_empty_single_line_str(data, "SESSION_STATE_PATH")
    raw_work_path = _require_non_empty_single_line_str(data, "SESSION_WORK_PATH")

    poll_interval = _require_positive_int(data, "POLL_INTERVAL_SECONDS")
    poll_max_interval = _require_positive_int(data, "POLL_MAX_INTERVAL_SECONDS")
    if poll_max_interval < poll_interval:
        raise DaemonConfigError(
            "字段 POLL_MAX_INTERVAL_SECONDS 必须大于或等于 POLL_INTERVAL_SECONDS。"
        )

    asr_concurrency = _require_positive_int(data, "ASR_MAX_CONCURRENCY")
    if asr_concurrency != 1:
        raise DaemonConfigError("字段 ASR_MAX_CONCURRENCY 必须为 1。")

    max_active_sessions = _require_positive_int(data, "MAX_ACTIVE_SESSIONS")
    max_audio_chunk_bytes = _require_positive_int(data, "MAX_AUDIO_CHUNK_BYTES")
    session_idle_warn_hours = _require_positive_int(data, "SESSION_IDLE_WARN_HOURS")

    raw_log_level = (
        log_level_override.strip()
        if log_level_override is not None
        else _require_non_empty_single_line_str(data, "LOG_LEVEL")
    ).upper()
    if raw_log_level not in VALID_LOG_LEVELS:
        raise DaemonConfigError(
            f"字段 LOG_LEVEL 取值非法，仅允许 {' | '.join(VALID_LOG_LEVELS)}。"
        )

    base_dir = path.parent
    state_dir = _resolve_dir_path(base_dir, raw_state_path, "SESSION_STATE_PATH")
    work_dir = _resolve_dir_path(base_dir, raw_work_path, "SESSION_WORK_PATH")

    return DaemonConfig(
        config_file=path,
        torch_backend=torch_backend,
        asr_language=asr_language,
        disable_mmap=disable_mmap,
        server_host=server_host,
        server_port=server_port,
        server_auth_token_hash=token_hash,
        session_token_secret=session_secret,
        openlist_webdav_url=webdav_url.rstrip("/"),
        openlist_username=openlist_username,
        openlist_password=openlist_password,
        openlist_timeout_seconds=openlist_timeout,
        openlist_verify_tls=verify_tls,
        cloud_root_prefix=cloud_root_prefix,
        session_state_path=state_dir,
        session_work_path=work_dir,
        poll_interval_seconds=poll_interval,
        poll_max_interval_seconds=poll_max_interval,
        asr_max_concurrency=asr_concurrency,
        max_active_sessions=max_active_sessions,
        max_audio_chunk_bytes=max_audio_chunk_bytes,
        session_idle_warn_hours=session_idle_warn_hours,
        log_level=raw_log_level,
    )
