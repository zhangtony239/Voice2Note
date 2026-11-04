"""日志格式与敏感凭据脱敏过滤器（design.md 第 12 节）。

日志统一输出到标准错误，格式为：
`时间 级别 [任务标识前 8 位] 消息`
所有日志级别下严禁出现连接 token、任务 token、云盘账号或云盘密码。
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterable


class SecretRedactingFilter(logging.Filter):
    """将日志消息与参数中可能出现的敏感字符串替换为 `[REDACTED]`。"""

    def __init__(self, secrets_to_redact: Iterable[str] = ()) -> None:
        super().__init__()
        self._secrets: list[str] = [s for s in secrets_to_redact if s and len(s) >= 3]

    def add_secret(self, secret: str) -> None:
        if secret and len(secret) >= 3 and secret not in self._secrets:
            self._secrets.append(secret)

    def _scrub(self, text: str) -> str:
        out = text
        for sec in self._secrets:
            if sec in out:
                out = out.replace(sec, "[REDACTED]")
        return out

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "session_short"):
            sid = getattr(record, "session_id", None)
            if isinstance(sid, str) and sid:
                record.session_short = sid[:8]
            else:
                record.session_short = "daemon"
        msg = record.getMessage()
        scrubbed = self._scrub(msg)
        record.msg = scrubbed
        record.args = ()
        return True


def configure_daemon_logger(
    level_name: str = "INFO",
    *,
    secrets_to_redact: Iterable[str] = (),
    stream=None,
) -> tuple[logging.Logger, SecretRedactingFilter]:
    """配置并返回 `voice2note.rec_daemon` 专用 logger 与脱敏过滤器。"""
    logger = logging.getLogger("voice2note.rec_daemon")
    logger.setLevel(getattr(logging, level_name.upper(), logging.INFO))
    logger.propagate = False

    for h in list(logger.handlers):
        logger.removeHandler(h)

    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s [%(session_short)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    redactor = SecretRedactingFilter(secrets_to_redact)
    handler.setFormatter(formatter)
    handler.addFilter(redactor)
    logger.addHandler(handler)
    return logger, redactor
