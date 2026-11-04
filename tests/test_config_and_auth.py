"""测试服务配置加载、全部校验分支、脱敏输出、相对路径解析与两级 Token 鉴权。"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest
import yaml

from voice2note import cli
from voice2note.rec_daemon.auth import (
    derive_session_token,
    generate_credentials,
    hash_connection_token,
    rebuild_session_token_map,
    verify_connection_token,
    verify_session_token_for_id,
)
from voice2note.rec_daemon.config import (
    ALL_DAEMON_FIELDS,
    DaemonConfigError,
    load_daemon_config,
    read_daemon_template,
)
from voice2note.rec_daemon.logging_setup import configure_daemon_logger

LLM_FIELD_NAMES = {"LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "SYSTEM_PROMPT", "NOTE_PATH"}


def _valid_config_dict() -> dict[str, Any]:
    creds = generate_credentials()
    return {
        "TORCH_BACKEND": "xpu",
        "ASR_LANGUAGE": "zh",
        "DISABLE_MMAP": True,
        "SERVER_HOST": "127.0.0.1",
        "SERVER_PORT": 8765,
        "SERVER_AUTH_TOKEN_HASH": creds.connection_token_hash,
        "SESSION_TOKEN_SECRET": creds.session_token_secret,
        "OPENLIST_WEBDAV_URL": "http://127.0.0.1:5244/dav",
        "OPENLIST_USERNAME": "dav_user",
        "OPENLIST_PASSWORD": "dav_super_secret_password_998877",
        "OPENLIST_TIMEOUT_SECONDS": 30,
        "OPENLIST_VERIFY_TLS": True,
        "CLOUD_ROOT_PREFIX": "RecNoteRemote",
        "SESSION_STATE_PATH": ".rec-daemon/state",
        "SESSION_WORK_PATH": ".rec-daemon/work",
        "POLL_INTERVAL_SECONDS": 2,
        "POLL_MAX_INTERVAL_SECONDS": 30,
        "ASR_MAX_CONCURRENCY": 1,
        "MAX_ACTIVE_SESSIONS": 5,
        "MAX_AUDIO_CHUNK_BYTES": 16777216,
        "SESSION_IDLE_WARN_HOURS": 4,
        "LOG_LEVEL": "INFO",
    }


def _write_yaml(path: Path, data: dict[str, Any]) -> Path:
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


def test_template_has_22_fields_and_no_llm_fields(tmp_path: Path) -> None:
    """Task 4.1 & 4.6: 模板含 22 个字段、无 LLM 字段，README 列出的字段与模板一致。"""
    raw = read_daemon_template()
    for llm_f in LLM_FIELD_NAMES:
        assert llm_f not in raw

    parsed = yaml.safe_load(raw)
    assert isinstance(parsed, dict)
    # 模板中 DISABLE_MMAP 默认注释，加上注释中的 DISABLE_MMAP 共 22 个字段
    assert "# DISABLE_MMAP:" in raw
    uncommented = raw.replace("# DISABLE_MMAP: true", "DISABLE_MMAP: true")
    parsed_all = yaml.safe_load(uncommented)
    assert set(parsed_all.keys()) == set(ALL_DAEMON_FIELDS)
    assert len(ALL_DAEMON_FIELDS) == 22

    # 校验 README.md 包含全部 22 个字段名与快速启动命令
    readme = (Path(__file__).parent.parent / "README.md").read_text(encoding="utf-8")
    for field in ALL_DAEMON_FIELDS:
        assert f"`{field}`" in readme
    assert "v2n rec-daemon init-config" in readme
    assert "v2n rec-daemon token" in readme

    # 验证 init-config 子命令
    cfg_path = tmp_path / "rec-daemon.yaml"
    rc = cli.main(["rec-daemon", "init-config", "--config", str(cfg_path)])
    assert rc == 0
    assert cfg_path.is_file()


def test_missing_multiple_required_fields_listed_at_once(tmp_path: Path) -> None:
    """Task 4.2: 缺少多个必填字段时一次性列出全部缺失字段名。"""
    data = _valid_config_dict()
    del data["SERVER_PORT"]
    del data["OPENLIST_PASSWORD"]
    del data["MAX_ACTIVE_SESSIONS"]
    cfg_file = _write_yaml(tmp_path / "rec-daemon.yaml", data)

    with pytest.raises(DaemonConfigError) as exc_info:
        load_daemon_config(cfg_file)
    msg = str(exc_info.value)
    assert "SERVER_PORT" in msg
    assert "OPENLIST_PASSWORD" in msg
    assert "MAX_ACTIVE_SESSIONS" in msg


@pytest.mark.parametrize(
    ("field", "bad_value", "expected_field_in_err"),
    [
        ("TORCH_BACKEND", "cpu", "TORCH_BACKEND"),
        ("DISABLE_MMAP", "yes", "DISABLE_MMAP"),
        ("SERVER_PORT", 0, "SERVER_PORT"),
        ("SERVER_PORT", 70000, "SERVER_PORT"),
        ("SERVER_PORT", True, "SERVER_PORT"),
        ("SERVER_AUTH_TOKEN_HASH", "md5:1234", "SERVER_AUTH_TOKEN_HASH"),
        ("SERVER_AUTH_TOKEN_HASH", "sha256:GGGG", "SERVER_AUTH_TOKEN_HASH"),
        ("SESSION_TOKEN_SECRET", "short_secret_only_16_chr", "SESSION_TOKEN_SECRET"),
        ("OPENLIST_WEBDAV_URL", "ftp://127.0.0.1/dav", "OPENLIST_WEBDAV_URL"),
        ("OPENLIST_WEBDAV_URL", "http:///dav", "OPENLIST_WEBDAV_URL"),
        ("OPENLIST_VERIFY_TLS", "true", "OPENLIST_VERIFY_TLS"),
        ("CLOUD_ROOT_PREFIX", "/RecNoteRemote", "CLOUD_ROOT_PREFIX"),
        ("CLOUD_ROOT_PREFIX", "RecNoteRemote/../evil", "CLOUD_ROOT_PREFIX"),
        ("CLOUD_ROOT_PREFIX", "RecNote\\Remote", "CLOUD_ROOT_PREFIX"),
        ("POLL_INTERVAL_SECONDS", 0, "POLL_INTERVAL_SECONDS"),
        ("POLL_MAX_INTERVAL_SECONDS", 1, "POLL_MAX_INTERVAL_SECONDS"),  # < POLL_INTERVAL_SECONDS(2)
        ("ASR_MAX_CONCURRENCY", 2, "ASR_MAX_CONCURRENCY"),
        ("MAX_ACTIVE_SESSIONS", -1, "MAX_ACTIVE_SESSIONS"),
        ("MAX_AUDIO_CHUNK_BYTES", 0, "MAX_AUDIO_CHUNK_BYTES"),
        ("SESSION_IDLE_WARN_HOURS", False, "SESSION_IDLE_WARN_HOURS"),
        ("LOG_LEVEL", "VERBOSE", "LOG_LEVEL"),
    ],
)
def test_each_field_validation_failure(
    tmp_path: Path, field: str, bad_value: Any, expected_field_in_err: str
) -> None:
    """Task 4.3: 每个校验项各有一个失败用例。"""
    data = _valid_config_dict()
    data[field] = bad_value
    cfg_file = _write_yaml(tmp_path / "rec-daemon.yaml", data)
    with pytest.raises(DaemonConfigError) as exc_info:
        load_daemon_config(cfg_file)
    assert expected_field_in_err in str(exc_info.value)


def test_directory_creation_failure_validation(tmp_path: Path) -> None:
    """Task 4.3: 状态或工作目录无法创建（例如路径已存在且是一个普通文件）时校验失败。"""
    blocker = tmp_path / "blocker_file"
    blocker.write_text("not a dir", encoding="utf-8")
    data = _valid_config_dict()
    data["SESSION_STATE_PATH"] = str(blocker)
    cfg_file = _write_yaml(tmp_path / "rec-daemon.yaml", data)
    with pytest.raises(DaemonConfigError) as exc_info:
        load_daemon_config(cfg_file)
    assert "SESSION_STATE_PATH" in str(exc_info.value)


def test_config_error_and_logs_never_leak_credentials(tmp_path: Path) -> None:
    """Task 4.4 & 5.4: 配置错误与全级别日志均不泄露云盘密码与 token。"""
    synthetic_pass = "SynthCloudPass_#987654321_NeverLeak"
    creds = generate_credentials()
    session_tok = derive_session_token(
        creds.session_token_secret, "3f2b8c1e-9a4d-4f7b-8e21-5c6d7a9b0e13"
    )

    # 构造带换行符的非法云盘密码触发校验失败
    data = _valid_config_dict()
    data["OPENLIST_PASSWORD"] = f"{synthetic_pass}\ninvalid_line"
    cfg_file = _write_yaml(tmp_path / "rec-daemon.yaml", data)

    with pytest.raises(DaemonConfigError) as exc_info:
        load_daemon_config(cfg_file)
    err_msg = str(exc_info.value)
    assert "OPENLIST_PASSWORD" in err_msg
    assert synthetic_pass not in err_msg

    # 在 DEBUG / INFO / WARNING / ERROR 四个级别下测试日志脱敏
    for level in ("DEBUG", "INFO", "WARNING", "ERROR"):
        buf = io.StringIO()
        logger, _ = configure_daemon_logger(
            level,
            secrets_to_redact=[
                synthetic_pass,
                creds.connection_token,
                creds.session_token_secret,
                session_tok,
                "dav_user",
            ],
            stream=buf,
        )
        logger.debug("debug line with %s and %s", synthetic_pass, creds.connection_token)
        logger.info("info line with %s and %s", session_tok, "dav_user")
        logger.warning("warn line with %s", creds.session_token_secret)
        logger.error("error line with %s", synthetic_pass)

        output = buf.getvalue()
        assert synthetic_pass not in output
        assert creds.connection_token not in output
        assert creds.session_token_secret not in output
        assert session_tok not in output
        assert "dav_user" not in output


def test_storage_paths_resolved_relative_to_config_file_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 4.5: 存储路径按配置文件所在目录解析，从任意工作目录启动一致。"""
    cfg_dir = tmp_path / "conf_folder"
    other_cwd = tmp_path / "other_working_dir"
    cfg_dir.mkdir()
    other_cwd.mkdir()

    data = _valid_config_dict()
    data["SESSION_STATE_PATH"] = "rel_state"
    data["SESSION_WORK_PATH"] = "rel_work"
    cfg_file = _write_yaml(cfg_dir / "rec-daemon.yaml", data)

    monkeypatch.chdir(other_cwd)
    cfg = load_daemon_config(cfg_file)
    assert cfg.session_state_path == (cfg_dir / "rel_state").resolve()
    assert cfg.session_work_path == (cfg_dir / "rel_work").resolve()
    assert cfg.session_state_path.is_dir()
    assert cfg.session_work_path.is_dir()


def test_token_cli_and_two_tier_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Task 5.1, 5.2, 5.3, 5.4: token 子命令、连接 token 摘要校验、任务 token 派生与反向映射、两级隔离。"""
    monkeypatch.chdir(tmp_path)
    rc1 = cli.main(["rec-daemon", "token"])
    out1 = capsys.readouterr().out
    rc2 = cli.main(["rec-daemon", "token"])
    out2 = capsys.readouterr().out
    assert rc1 == 0 and rc2 == 0
    assert list(tmp_path.iterdir()) == []  # 不修改或创建任何文件
    assert "CONNECTION_TOKEN:" in out1
    assert "SERVER_AUTH_TOKEN_HASH: sha256:" in out1
    assert "SESSION_TOKEN_SECRET:" in out1
    assert out1 != out2  # 再次执行得到不同凭据

    # 5.2 连接 token 常数时间摘要比较
    creds = generate_credentials()
    assert verify_connection_token(creds.connection_token, creds.connection_token_hash)
    assert not verify_connection_token("wrong-token", creds.connection_token_hash)
    assert not verify_connection_token("", creds.connection_token_hash)

    # 5.3 任务 token 派生与反向映射重建
    sid_a = "3f2b8c1e-9a4d-4f7b-8e21-5c6d7a9b0e13"
    sid_b = "7a1d2c3e-4b5f-4a6b-9c8d-1e2f3a4b5c6d"
    tok_a1 = derive_session_token(creds.session_token_secret, sid_a)
    tok_a2 = derive_session_token(creds.session_token_secret, sid_a.upper())
    tok_b = derive_session_token(creds.session_token_secret, sid_b)
    assert len(tok_a1) == 43
    assert tok_a1 == tok_a2
    assert tok_a1 != tok_b

    rebuilt = rebuild_session_token_map(creds.session_token_secret, [sid_a, sid_b])
    assert rebuilt[tok_a1] == sid_a
    assert rebuilt[tok_b] == sid_b

    # 5.4 任务 token 与连接 token 互不通用
    assert not verify_connection_token(tok_a1, creds.connection_token_hash)
    assert not verify_session_token_for_id(
        creds.connection_token, creds.session_token_secret, sid_a
    )
    assert not verify_session_token_for_id(tok_a1, creds.session_token_secret, sid_b)
    assert verify_session_token_for_id(tok_a1, creds.session_token_secret, sid_a)
    assert hash_connection_token(creds.connection_token) == creds.connection_token_hash
