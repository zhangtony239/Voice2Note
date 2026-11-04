"""`v2n rec-daemon` 常驻服务启动装配（uvicorn workers=1, proxy_headers=True）。"""

from __future__ import annotations

from pathlib import Path

import uvicorn

from voice2note.rec_daemon.app import create_daemon_app
from voice2note.rec_daemon.config import load_daemon_config
from voice2note.rec_daemon.logging_setup import configure_daemon_logger
from voice2note.rec_daemon.session_manager import SessionManager
from voice2note.rec_daemon.storage import WebDAVStorageClient


def run_daemon_server(
    *,
    config_file: Path | None = None,
    host_override: str | None = None,
    port_override: int | None = None,
    log_level_override: str | None = None,
) -> int:
    """加载配置、恢复未完成任务并在前台运行 uvicorn（单 worker，禁用默认访问日志）。"""
    cfg = load_daemon_config(
        config_file,
        host_override=host_override,
        port_override=port_override,
        log_level_override=log_level_override,
    )
    logger, _ = configure_daemon_logger(
        cfg.log_level,
        secrets_to_redact=[
            cfg.openlist_username,
            cfg.openlist_password,
            cfg.session_token_secret,
            cfg.server_auth_token_hash,
        ],
    )
    storage = WebDAVStorageClient(
        base_url=cfg.openlist_webdav_url,
        username=cfg.openlist_username,
        password=cfg.openlist_password,
        timeout_seconds=cfg.openlist_timeout_seconds,
        verify_tls=cfg.openlist_verify_tls,
    )
    manager = SessionManager(
        cfg,
        storage,
        logger=logger,
        enable_background_workers=True,
    )
    recovered = manager.recover_sessions()
    logger.info(
        "启动 v2n rec-daemon host=%s port=%d recovered_sessions=%d",
        cfg.server_host,
        cfg.server_port,
        recovered,
    )
    app = create_daemon_app(cfg, storage, manager, logger=logger)
    try:
        uvicorn.run(
            app,
            host=cfg.server_host,
            port=cfg.server_port,
            workers=1,
            proxy_headers=True,
            forwarded_allow_ips="127.0.0.1",
            access_log=False,
            log_config=None,
        )
    finally:
        manager.shutdown()
        storage.close()
    return 0
