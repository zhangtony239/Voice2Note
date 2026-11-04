"""测试 HTTP 控制面（Tasks 6.1–6.9）：7 个接口、64 KiB 上限、探测闭环与限流、全部 15 个接口层错误码。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
import yaml
from starlette.types import ASGIApp

from tests.mock_webdav import FaultRule, MockWebDAVServer
from voice2note.rec_daemon.app import ERROR_CODE_TO_HTTP_STATUS, create_daemon_app
from voice2note.rec_daemon.auth import derive_session_token, generate_credentials
from voice2note.rec_daemon.config import DaemonConfig, load_daemon_config
from voice2note.rec_daemon.session_manager import SessionManager
from voice2note.rec_daemon.storage import WebDAVStorageClient

SID_1 = "3f2b8c1e-9a4d-4f7b-8e21-5c6d7a9b0e13"
SID_2 = "7a1d2c3e-4b5f-4a6b-9c8d-1e2f3a4b5c6d"
SID_3 = "9b2e3d4f-5c6a-4b7c-8d9e-0f1a2b3c4d5e"


class SyncASGITransport(httpx.BaseTransport):
    """在内存同步驱动 ASGI 应用对象（无需启动真实端口或创建系统 socketpair）。"""

    def __init__(self, app: ASGIApp, *, omit_content_length: bool = False) -> None:
        self.app = app
        self.omit_content_length = omit_content_length

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        raw_headers: list[tuple[bytes, bytes]] = []
        for k, v in request.headers.raw:
            if self.omit_content_length and k.lower() == b"content-length":
                continue
            raw_headers.append((k.lower(), v))

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": request.method,
            "scheme": request.url.scheme or "http",
            "path": request.url.path,
            "raw_path": request.url.raw_path.split(b"?", 1)[0],
            "query_string": request.url.query,
            "headers": raw_headers,
            "client": ("127.0.0.1", 50000),
            "server": (request.url.host or "testserver", request.url.port or 80),
        }

        body_bytes = request.read()
        body_sent = False
        sent_messages: list[dict[str, Any]] = []

        async def receive() -> dict[str, Any]:
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {
                    "type": "http.request",
                    "body": body_bytes,
                    "more_body": False,
                }
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            sent_messages.append(message)

        coro = self.app(scope, receive, send)
        try:
            coro.send(None)
        except StopIteration:
            pass
        finally:
            coro.close()

        status_code = 500
        resp_headers: list[tuple[bytes, bytes]] = []
        resp_body_parts: list[bytes] = []
        for msg in sent_messages:
            if msg["type"] == "http.response.start":
                status_code = msg["status"]
                resp_headers = list(msg.get("headers", []))
            elif msg["type"] == "http.response.body":
                resp_body_parts.append(msg.get("body", b""))

        return httpx.Response(
            status_code=status_code,
            headers=[(k.decode("latin1"), v.decode("latin1")) for k, v in resp_headers],
            content=b"".join(resp_body_parts),
            request=request,
        )


def make_test_env(
    tmp_path: Path,
    *,
    max_active_sessions: int = 5,
    asr_ready_fn=None,
    storage_ready_fn=None,
) -> tuple[
    DaemonConfig,
    str,
    MockWebDAVServer,
    WebDAVStorageClient,
    SessionManager,
    Any,
]:
    creds = generate_credentials()
    cfg_data = {
        "TORCH_BACKEND": "xpu",
        "ASR_LANGUAGE": "zh",
        "DISABLE_MMAP": True,
        "SERVER_HOST": "127.0.0.1",
        "SERVER_PORT": 8765,
        "SERVER_AUTH_TOKEN_HASH": creds.connection_token_hash,
        "SESSION_TOKEN_SECRET": creds.session_token_secret,
        "OPENLIST_WEBDAV_URL": "http://localhost/dav",
        "OPENLIST_USERNAME": "testuser",
        "OPENLIST_PASSWORD": "testpassword",
        "OPENLIST_TIMEOUT_SECONDS": 10,
        "OPENLIST_VERIFY_TLS": False,
        "CLOUD_ROOT_PREFIX": "RecNoteRemote",
        "SESSION_STATE_PATH": "state",
        "SESSION_WORK_PATH": "work",
        "POLL_INTERVAL_SECONDS": 2,
        "POLL_MAX_INTERVAL_SECONDS": 10,
        "ASR_MAX_CONCURRENCY": 1,
        "MAX_ACTIVE_SESSIONS": max_active_sessions,
        "MAX_AUDIO_CHUNK_BYTES": 16777216,
        "SESSION_IDLE_WARN_HOURS": 4,
        "LOG_LEVEL": "INFO",
    }
    cfg_path = tmp_path / "rec-daemon.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg_data), encoding="utf-8")
    cfg = load_daemon_config(cfg_path)

    dav = MockWebDAVServer()
    storage = WebDAVStorageClient(
        base_url=cfg.openlist_webdav_url,
        username=cfg.openlist_username,
        password=cfg.openlist_password,
        timeout_seconds=cfg.openlist_timeout_seconds,
        verify_tls=cfg.openlist_verify_tls,
        transport=dav.transport,
    )
    mgr = SessionManager(
        cfg,
        storage,
        transcribe_fn=lambda _p, _c: "固定转写文本",
        enable_background_workers=False,
    )
    app = create_daemon_app(
        cfg,
        storage,
        mgr,
        asr_ready_fn=asr_ready_fn,
        storage_ready_fn=storage_ready_fn,
    )
    return cfg, creds.connection_token, dav, storage, mgr, app


def test_health_endpoint_and_no_credential_leak(tmp_path: Path) -> None:
    """Task 6.3: 健康检查返回规定字段且不含任何凭据。"""
    cfg, conn_token, _, _, _, app = make_test_env(tmp_path)
    with httpx.Client(
        transport=SyncASGITransport(app), base_url="http://testserver"
    ) as client:
        r = client.get(
            "/api/v1/health",
            headers={"Authorization": f"Bearer {conn_token}"},
        )
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/json")
        body = r.json()
        assert body == {
            "protocol_version": 1,
            "service": "voice2note",
            "version": "0.1.0",
            "asr_ready": True,
            "model_loaded": False,
            "storage_ready": True,
            "capabilities": ["probe", "session-control", "asr"],
        }
        raw_text = r.text
        assert conn_token not in raw_text
        assert cfg.openlist_password not in raw_text
        assert cfg.session_token_secret not in raw_text


def test_body_size_limit_middleware_64kib(tmp_path: Path) -> None:
    """Task 6.2: 请求体超过 64 KiB 返回 413 payload_too_large（含 Content-Length 与流式无 Content-Length 两种路径）。"""
    _, conn_token, _, _, _, app = make_test_env(tmp_path)
    huge_payload = json.dumps(
        {
            "protocol_version": 1,
            "token": conn_token,
            "padding": "x" * (65 * 1024),
        }
    ).encode("utf-8")

    with httpx.Client(
        transport=SyncASGITransport(app), base_url="http://testserver"
    ) as client:
        r = client.post("/api/v1/probe", content=huge_payload)
        assert r.status_code == 413
        assert r.json()["error"]["code"] == "payload_too_large"
        assert r.json()["error"]["retryable"] is False

    with httpx.Client(
        transport=SyncASGITransport(app, omit_content_length=True),
        base_url="http://testserver",
    ) as client_stream:
        r2 = client_stream.post("/api/v1/probe", content=huge_payload)
        assert r2.status_code == 413
        assert r2.json()["error"]["code"] == "payload_too_large"


def test_probe_four_step_roundtrip_and_rate_limit(tmp_path: Path) -> None:
    """Task 6.4 & 6.5: 探测四步往返（成功、读取失败、写入失败）、不建任务、越界拒绝与每分钟 10 次限流。"""
    _, conn_token, dav, _, mgr, app = make_test_env(tmp_path)
    probe_rel = "RecNoteRemote/_probe/6f1d0c9a-8b2e-4d3f-9a71-2c5b8e4d1f60.bin"
    probe_bytes = b"probe-test-payload-123"
    probe_sha = hashlib.sha256(probe_bytes).hexdigest()

    with httpx.Client(
        transport=SyncASGITransport(app), base_url="http://testserver"
    ) as client:
        # 1. 读取失败（文件不存在 -> probe_missing）
        r_missing = client.post(
            "/api/v1/probe",
            json={
                "protocol_version": 1,
                "token": conn_token,
                "path": probe_rel,
                "sha256": probe_sha,
            },
        )
        assert r_missing.status_code == 200
        b_missing = r_missing.json()
        assert b_missing["ok"] is False
        assert b_missing["read_ok"] is False
        assert b_missing["write_ok"] is False
        assert b_missing["error"]["code"] == "probe_missing"

        # 2. 写入探测文件但模拟回执写入失败（silent_put_drop）
        dav.seed_file(probe_rel, probe_bytes)
        receipt_rel = f"{probe_rel}.receipt.json"
        dav.set_fault(receipt_rel, FaultRule(kind="silent_put_drop"))
        r_write_fail = client.post(
            "/api/v1/probe",
            json={
                "protocol_version": 1,
                "token": conn_token,
                "path": probe_rel,
                "sha256": probe_sha,
            },
        )
        assert r_write_fail.status_code == 200
        b_wf = r_write_fail.json()
        assert b_wf["ok"] is False
        assert b_wf["read_ok"] is True
        assert b_wf["write_ok"] is False
        assert b_wf["error"]["code"] == "storage_not_ready"

        # 3. 清除故障，探测成功，手机可回读回执并校验 sha256
        dav.clear_fault(receipt_rel)
        r_ok = client.post(
            "/api/v1/probe",
            json={
                "protocol_version": 1,
                "token": conn_token,
                "path": probe_rel,
                "sha256": probe_sha,
            },
        )
        assert r_ok.status_code == 200
        b_ok = r_ok.json()
        assert b_ok["ok"] is True
        assert b_ok["read_ok"] is True
        assert b_ok["write_ok"] is True
        assert b_ok["receipt"]["path"] == receipt_rel
        receipt_on_cloud = dav.read_seeded_file(receipt_rel)
        assert receipt_on_cloud is not None
        assert (
            hashlib.sha256(receipt_on_cloud).hexdigest()
            == b_ok["receipt"]["sha256"]
        )
        # 探测不建任务
        assert mgr.total_session_count() == 0

        # 4. 越界路径被拒绝（400 invalid_cloud_path）
        for bad_path in (
            "RecNoteRemote/../evil.bin",
            "/RecNoteRemote/_probe/a.bin",
            "OtherRoot/_probe/a.bin",
            "RecNoteRemote/session/a.bin",
        ):
            r_bad = client.post(
                "/api/v1/probe",
                json={
                    "protocol_version": 1,
                    "token": conn_token,
                    "path": bad_path,
                    "sha256": probe_sha,
                },
            )
            assert r_bad.status_code == 400
            assert r_bad.json()["error"]["code"] == "invalid_cloud_path"

        # 5. 同一来源 IP 每分钟超过 10 次返回 429 too_many_requests
        for _ in range(10):
            r_hit = client.post(
                "/api/v1/probe",
                headers={"X-Forwarded-For": "203.0.113.99"},
                json={
                    "protocol_version": 1,
                    "token": conn_token,
                    "path": probe_rel,
                    "sha256": probe_sha,
                },
            )
            assert r_hit.status_code == 200
        r_limited = client.post(
            "/api/v1/probe",
            headers={"X-Forwarded-For": "203.0.113.99"},
            json={
                "protocol_version": 1,
                "token": conn_token,
                "path": probe_rel,
                "sha256": probe_sha,
            },
        )
        assert r_limited.status_code == 429
        assert r_limited.json()["error"]["code"] == "too_many_requests"
        assert r_limited.json()["error"]["retryable"] is True


def test_session_control_endpoints_and_all_error_codes(tmp_path: Path) -> None:
    """Task 6.1, 6.6, 6.7, 6.8, 6.9: 覆盖开始、结束、重试、取消、查询及 design.md 第 4 节全部错误码。"""
    triggered_codes: set[str] = set()
    asr_ready_flag = True
    storage_ready_flag = True

    cfg, conn_token, dav, _, mgr, app = make_test_env(
        tmp_path,
        max_active_sessions=2,
        asr_ready_fn=lambda: asr_ready_flag,
        storage_ready_fn=lambda: storage_ready_flag,
    )

    def record_err(resp: httpx.Response) -> str:
        code = resp.json()["error"]["code"]
        triggered_codes.add(code)
        assert resp.status_code == ERROR_CODE_TO_HTTP_STATUS[code]
        return code

    with httpx.Client(
        transport=SyncASGITransport(app), base_url="http://testserver"
    ) as client:
        # 1. not_found (404)
        r = client.get("/api/v1/nonexistent")
        assert record_err(r) == "not_found"

        # 2. invalid_request (400) - 畸形 JSON / 缺字段 / 枚举非法
        r = client.post("/api/v1/sessions/start", content=b"{bad-json")
        assert record_err(r) == "invalid_request"

        r = client.post(
            "/api/v1/sessions/start",
            json={
                "protocol_version": 1,
                "token": conn_token,
                "session_id": SID_1,
                "cloud_path": f"RecNoteRemote/{SID_1}",
                "title": "测试",
                "source_type": "invalid_enum",
            },
        )
        assert record_err(r) == "invalid_request"

        # 3. unsupported_protocol_version (422)
        r = client.post(
            "/api/v1/sessions/start",
            json={
                "protocol_version": 2,
                "token": conn_token,
                "session_id": SID_1,
                "cloud_path": f"RecNoteRemote/{SID_1}",
                "title": "测试",
                "source_type": "microphone",
            },
        )
        assert record_err(r) == "unsupported_protocol_version"

        # 4. unauthorized (401)
        r = client.get("/api/v1/health")
        assert record_err(r) == "unauthorized"

        # 5. invalid_cloud_path (400)
        r = client.post(
            "/api/v1/sessions/start",
            json={
                "protocol_version": 1,
                "token": conn_token,
                "session_id": SID_1,
                "cloud_path": "RecNoteRemote/wrong-folder",
                "title": "测试",
                "source_type": "microphone",
            },
        )
        assert record_err(r) == "invalid_cloud_path"

        # 6. asr_not_ready (503) & storage_not_ready (503)
        asr_ready_flag = False
        r = client.post(
            "/api/v1/sessions/start",
            json={
                "protocol_version": 1,
                "token": conn_token,
                "session_id": SID_1,
                "cloud_path": f"RecNoteRemote/{SID_1}",
                "title": "测试",
                "source_type": "microphone",
            },
        )
        assert record_err(r) == "asr_not_ready"
        asr_ready_flag = True

        storage_ready_flag = False
        r = client.post(
            "/api/v1/sessions/start",
            json={
                "protocol_version": 1,
                "token": conn_token,
                "session_id": SID_1,
                "cloud_path": f"RecNoteRemote/{SID_1}",
                "title": "测试",
                "source_type": "microphone",
            },
        )
        assert record_err(r) == "storage_not_ready"
        storage_ready_flag = True

        # 7. 正常开始任务 1（含未知字段 future_field 被静默忽略）
        r_start1 = client.post(
            "/api/v1/sessions/start",
            json={
                "protocol_version": 1,
                "token": conn_token,
                "session_id": SID_1,
                "cloud_path": f"RecNoteRemote/{SID_1}",
                "title": "示例课程",
                "source_type": "microphone",
                "future_extra_field": {"ignored": True},
            },
        )
        assert r_start1.status_code == 200
        tok_1 = r_start1.json()["session_token"]
        assert r_start1.json()["state"] == "waiting_source"

        # 8. session_id_conflict (409)：同 session_id 但不同路径
        mgr.get_session(SID_1).cloud_path = "RecNoteRemote/custom_override"  # type: ignore[union-attr]
        r_conflict = client.post(
            "/api/v1/sessions/start",
            json={
                "protocol_version": 1,
                "token": conn_token,
                "session_id": SID_1,
                "cloud_path": f"RecNoteRemote/{SID_1}",
                "title": "示例课程",
                "source_type": "microphone",
            },
        )
        assert record_err(r_conflict) == "session_id_conflict"
        mgr.get_session(SID_1).cloud_path = f"RecNoteRemote/{SID_1}"  # type: ignore[union-attr]

        # 9. 开始任务 2，然后尝试开始任务 3 触发 too_many_requests (429)
        r_start2 = client.post(
            "/api/v1/sessions/start",
            json={
                "protocol_version": 1,
                "token": conn_token,
                "session_id": SID_2,
                "cloud_path": f"RecNoteRemote/{SID_2}",
                "title": "课程2",
                "source_type": "import",
            },
        )
        assert r_start2.status_code == 200
        tok_2 = r_start2.json()["session_token"]

        r_over = client.post(
            "/api/v1/sessions/start",
            json={
                "protocol_version": 1,
                "token": conn_token,
                "session_id": SID_3,
                "cloud_path": f"RecNoteRemote/{SID_3}",
                "title": "课程3",
                "source_type": "microphone",
            },
        )
        assert record_err(r_over) == "too_many_requests"

        # 10. 查询任务进度 & forbidden (403) & unknown_session (404)
        r_q1 = client.get(
            f"/api/v1/sessions/{SID_1}",
            headers={"Authorization": f"Bearer {tok_1}"},
        )
        assert r_q1.status_code == 200
        q1_body = r_q1.json()
        assert q1_body["session_id"] == SID_1
        assert q1_body["state"] == "waiting_source"
        assert "transcript" not in q1_body

        # 用任务 1 的 token 查询任务 2 -> 403 forbidden
        r_forbid = client.get(
            f"/api/v1/sessions/{SID_2}",
            headers={"Authorization": f"Bearer {tok_1}"},
        )
        assert record_err(r_forbid) == "forbidden"

        # 用未建立的任务 3 的合法派生 token 查询任务 3 -> 404 unknown_session
        tok_3 = derive_session_token(cfg.session_token_secret, SID_3)
        r_unknown = client.get(
            f"/api/v1/sessions/{SID_3}",
            headers={"Authorization": f"Bearer {tok_3}"},
        )
        assert record_err(r_unknown) == "unknown_session"

        # 11. 连接 token 不能结束任务 -> 401 unauthorized
        r_conn_end = client.post(
            "/api/v1/sessions/end", json={"token": conn_token}
        )
        assert record_err(r_conn_end) == "unauthorized"

        # 12. 正常结束与重复结束恒返回 200
        r_end1 = client.post("/api/v1/sessions/end", json={"token": tok_1})
        r_end2 = client.post("/api/v1/sessions/end", json={"token": tok_1})
        assert r_end1.status_code == 200
        assert r_end2.status_code == 200
        assert r_end2.json()["state"] == "draining"
        assert r_end2.json()["end_received"] is True

        # 13. 取消任务 2 与重复取消恒返回 200，对已取消任务执行 retry 或 start 返回 409 session_terminal
        r_can1 = client.post(
            "/api/v1/sessions/cancel", json={"token": tok_2}
        )
        r_can2 = client.post(
            "/api/v1/sessions/cancel", json={"token": tok_2}
        )
        assert r_can1.status_code == 200
        assert r_can2.status_code == 200
        assert r_can2.json()["state"] == "cancelled"

        r_retry_term = client.post(
            "/api/v1/sessions/retry", json={"token": tok_2}
        )
        assert record_err(r_retry_term) == "session_terminal"

        r_start_term = client.post(
            "/api/v1/sessions/start",
            json={
                "protocol_version": 1,
                "token": conn_token,
                "session_id": SID_2,
                "cloud_path": f"RecNoteRemote/{SID_2}",
                "title": "重开已取消任务",
                "source_type": "microphone",
            },
        )
        assert record_err(r_start_term) == "session_terminal"

        # 14. payload_too_large (413)
        r_big = client.post(
            "/api/v1/sessions/end",
            content=b'{"token":"' + (b"a" * 70000) + b'"}',
        )
        assert record_err(r_big) == "payload_too_large"

        # 15. invalid_path (400) & internal_error (500)
        def raise_invalid_path():
            from voice2note.rec_daemon.protocol import ProtocolValidationError

            raise ProtocolValidationError("invalid_path", "bad relative path")

        app_err_path = create_daemon_app(
            cfg,
            storage=dav,  # type: ignore[arg-type]
            session_manager=mgr,
            asr_ready_fn=raise_invalid_path,
        )
        with httpx.Client(
            transport=SyncASGITransport(app_err_path),
            base_url="http://testserver",
        ) as c2:
            r_ip = c2.get(
                "/api/v1/health",
                headers={"Authorization": f"Bearer {conn_token}"},
            )
            assert record_err(r_ip) == "invalid_path"

        def raise_boom():
            raise RuntimeError("unexpected failure")

        app_err_500 = create_daemon_app(
            cfg,
            storage=dav,  # type: ignore[arg-type]
            session_manager=mgr,
            asr_ready_fn=raise_boom,
        )
        with httpx.Client(
            transport=SyncASGITransport(app_err_500),
            base_url="http://testserver",
        ) as c3:
            r_500 = c3.get(
                "/api/v1/health",
                headers={"Authorization": f"Bearer {conn_token}"},
            )
            assert record_err(r_500) == "internal_error"

    assert triggered_codes == set(ERROR_CODE_TO_HTTP_STATUS.keys())
