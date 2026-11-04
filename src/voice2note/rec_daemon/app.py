"""FastAPI 控制面应用装配、全局错误处理器、64 KiB 体积上限中间件与七个接口。"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

import voice2note
from voice2note.asr import is_model_loaded
from voice2note.rec_daemon.auth import (
    verify_connection_token,
    verify_session_token_for_id,
)
from voice2note.rec_daemon.config import DaemonConfig
from voice2note.rec_daemon.protocol import (
    MAX_CONTROL_REQUEST_BYTES,
    PROTOCOL_VERSION,
    ProtocolErrorObj,
    ProtocolValidationError,
    normalize_session_id,
)
from voice2note.rec_daemon.session_manager import SessionManager
from voice2note.rec_daemon.storage import WebDAVStorageClient

ERROR_CODE_TO_HTTP_STATUS: dict[str, int] = {
    "invalid_request": 400,
    "invalid_cloud_path": 400,
    "invalid_path": 400,
    "unauthorized": 401,
    "forbidden": 403,
    "unknown_session": 404,
    "not_found": 404,
    "session_id_conflict": 409,
    "session_terminal": 409,
    "payload_too_large": 413,
    "unsupported_protocol_version": 422,
    "too_many_requests": 429,
    "internal_error": 500,
    "asr_not_ready": 503,
    "storage_not_ready": 503,
}


def make_error_response(
    code: str,
    message: str,
    *,
    retryable: bool | None = None,
    status_code: int | None = None,
) -> JSONResponse:
    http_status = (
        status_code
        if status_code is not None
        else ERROR_CODE_TO_HTTP_STATUS.get(code, 400)
    )
    if retryable is None:
        retryable = code in {
            "too_many_requests",
            "internal_error",
            "asr_not_ready",
            "storage_not_ready",
        }
    err = ProtocolErrorObj(code=code, message=message, retryable=retryable)
    return JSONResponse(
        status_code=http_status,
        content={"error": err.to_dict()},
        media_type="application/json; charset=utf-8",
    )


def make_json_response(data: dict[str, Any], status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=data,
        media_type="application/json; charset=utf-8",
    )


class MaxBodySizeLimitMiddleware:
    """ASGI 请求体体积上限中间件：超过 64 KiB 立即中止读取并返回 413 payload_too_large（Task 6.2）。"""

    def __init__(self, app: ASGIApp, max_bytes: int = MAX_CONTROL_REQUEST_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # 1. 先检查 Content-Length 头，若已超限则不读取任何请求体直接返回 413
        headers = dict(scope.get("headers") or [])
        cl_raw = headers.get(b"content-length")
        if cl_raw is not None:
            try:
                if int(cl_raw.decode("ascii")) > self.max_bytes:
                    resp = make_error_response(
                        "payload_too_large",
                        f"request body exceeds {self.max_bytes} bytes limit",
                        retryable=False,
                    )
                    await resp(scope, receive, send)
                    return
            except ValueError:
                pass

        # 2. 流式拦截 receive，累计字节超过 max_bytes 立即抛超限异常
        total_read = 0

        async def limited_receive() -> Message:
            nonlocal total_read
            message = await receive()
            if message["type"] == "http.request":
                body = message.get("body", b"")
                total_read += len(body)
                if total_read > self.max_bytes:
                    raise ProtocolValidationError(
                        "payload_too_large",
                        f"request body exceeds {self.max_bytes} bytes limit",
                        retryable=False,
                    )
            return message

        try:
            await self.app(scope, limited_receive, send)
        except ProtocolValidationError as exc:
            resp = make_error_response(
                exc.error.code,
                exc.error.message,
                retryable=exc.error.retryable,
            )
            await resp(scope, receive, send)
        except Exception as exc:  # noqa: BLE001
            logging.getLogger("voice2note.rec_daemon").error(
                "未预期服务异常: %s", exc.__class__.__name__
            )
            resp = make_error_response(
                "internal_error",
                "internal server error",
                retryable=True,
                status_code=500,
            )
            await resp(scope, receive, send)


class ProbeRateLimiter:
    """按客户端来源地址限制探测频率：同一来源每 60 秒最多 10 次（Task 6.5）。"""

    def __init__(self, max_requests: int = 10, window_seconds: float = 60.0) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = {}

    def allow(self, client_ip: str) -> bool:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            q = self._hits.setdefault(client_ip, deque())
            while q and q[0] <= cutoff:
                q.popleft()
            if len(q) >= self.max_requests:
                return False
            q.append(now)
            return True


def _extract_client_ip(request: Request) -> str:
    """支持本机 cloudflared 代理转发的 X-Forwarded-For / CF-Connecting-IP。"""
    peer_ip = request.client.host if request.client else "127.0.0.1"
    if peer_ip in {"127.0.0.1", "::1", "testclient"}:
        cf_ip = request.headers.get("cf-connecting-ip")
        if cf_ip:
            return cf_ip.strip()
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
    return peer_ip


def _extract_bearer_token(request: Request) -> str:
    """仅从 `Authorization: Bearer <token>` 提取 token；拒绝 query string token。"""
    if "token" in request.query_params:
        raise ProtocolValidationError(
            "unauthorized", "token in query parameters is not allowed"
        )
    auth_header = request.headers.get("authorization")
    if not auth_header:
        raise ProtocolValidationError("unauthorized", "missing Authorization header")
    parts = auth_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise ProtocolValidationError(
            "unauthorized", "invalid Authorization Bearer header"
        )
    return parts[1].strip()


async def _parse_json_object(request: Request) -> dict[str, Any]:
    """解析 JSON 对象请求体，拒绝 Authorization 头混用。"""
    if "authorization" in request.headers:
        raise ProtocolValidationError(
            "invalid_request",
            "POST control endpoints only accept token in JSON body, not Authorization header",
        )
    raw = await request.body()
    if not raw:
        raise ProtocolValidationError("invalid_request", "empty request body")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolValidationError("invalid_request", "malformed JSON body") from exc
    if not isinstance(data, dict):
        raise ProtocolValidationError(
            "invalid_request", "request body must be a JSON object"
        )
    return data


def _check_protocol_version(
    body: dict[str, Any], *, required: bool = True
) -> None:
    if "protocol_version" not in body:
        if required:
            raise ProtocolValidationError(
                "invalid_request", "missing required field 'protocol_version'"
            )
        return
    pv = body["protocol_version"]
    if isinstance(pv, bool) or not isinstance(pv, int):
        raise ProtocolValidationError(
            "invalid_request", "'protocol_version' must be an integer"
        )
    if pv != PROTOCOL_VERSION:
        raise ProtocolValidationError(
            "unsupported_protocol_version",
            f"unsupported protocol_version {pv}, expected {PROTOCOL_VERSION}",
        )


def _default_asr_ready_check(config: DaemonConfig) -> bool:
    return (
        config.torch_backend in {"xpu", "cuda"}
        and config.session_state_path.is_dir()
        and config.session_work_path.is_dir()
    )


def create_daemon_app(
    config: DaemonConfig,
    storage: WebDAVStorageClient,
    session_manager: SessionManager,
    *,
    asr_ready_fn: Callable[[], bool] | None = None,
    storage_ready_fn: Callable[[], bool] | None = None,
    model_loaded_fn: Callable[[], bool] | None = None,
    logger: logging.Logger | None = None,
) -> FastAPI:
    """装配 FastAPI 控制面应用（覆盖默认校验错误响应，注册 64 KiB 限制中间件与 7 个接口）。"""
    log = logger or logging.getLogger("voice2note.rec_daemon")
    check_asr_ready = (
        asr_ready_fn
        if asr_ready_fn is not None
        else (lambda: _default_asr_ready_check(config))
    )
    check_storage_ready = (
        storage_ready_fn
        if storage_ready_fn is not None
        else (lambda: storage.check_ready(config.cloud_root_prefix))
    )
    check_model_loaded = (
        model_loaded_fn if model_loaded_fn is not None else is_model_loaded
    )
    probe_limiter = ProbeRateLimiter(max_requests=10, window_seconds=60.0)

    app = FastAPI(
        title="Voice2Note rec-daemon",
        version=voice2note.__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(
        MaxBodySizeLimitMiddleware, max_bytes=MAX_CONTROL_REQUEST_BYTES
    )

    @app.exception_handler(ProtocolValidationError)
    async def _handle_protocol_error(
        _request: Request, exc: ProtocolValidationError
    ) -> JSONResponse:
        return make_error_response(
            exc.error.code, exc.error.message, retryable=exc.error.retryable
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_fastapi_validation_error(
        _request: Request, _exc: RequestValidationError
    ) -> JSONResponse:
        return make_error_response(
            "invalid_request", "request validation failed", retryable=False
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(
        _request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        if exc.status_code == 404:
            return make_error_response(
                "not_found", "endpoint not found", retryable=False, status_code=404
            )
        if exc.status_code == 405:
            return make_error_response(
                "invalid_request",
                "method not allowed",
                retryable=False,
                status_code=400,
            )
        return make_error_response(
            "invalid_request",
            str(exc.detail),
            retryable=False,
            status_code=exc.status_code,
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected_exception(
        _request: Request, exc: Exception
    ) -> JSONResponse:
        log.error("未预期服务异常: %s", exc.__class__.__name__)
        return make_error_response(
            "internal_error",
            "internal server error",
            retryable=True,
            status_code=500,
        )

    @app.get("/api/v1/health")
    async def get_health(request: Request) -> JSONResponse:
        token = _extract_bearer_token(request)
        if not verify_connection_token(token, config.server_auth_token_hash):
            raise ProtocolValidationError("unauthorized", "invalid connection token")

        return make_json_response(
            {
                "protocol_version": PROTOCOL_VERSION,
                "service": "voice2note",
                "version": voice2note.__version__,
                "asr_ready": bool(check_asr_ready()),
                "model_loaded": bool(check_model_loaded()),
                "storage_ready": bool(check_storage_ready()),
                "capabilities": ["probe", "session-control", "asr"],
            }
        )

    @app.post("/api/v1/probe")
    async def post_probe(request: Request) -> JSONResponse:
        body = await _parse_json_object(request)
        _check_protocol_version(body, required=False)

        token = body.get("token")
        if not verify_connection_token(token, config.server_auth_token_hash):
            raise ProtocolValidationError("unauthorized", "invalid connection token")

        client_ip = _extract_client_ip(request)
        if not probe_limiter.allow(client_ip):
            raise ProtocolValidationError(
                "too_many_requests",
                "probe rate limit exceeded (max 10 per minute)",
                retryable=True,
            )

        path = body.get("path")
        sha256_val = body.get("sha256")
        if not isinstance(path, str) or not path or not isinstance(sha256_val, str) or not sha256_val:
            raise ProtocolValidationError(
                "invalid_request", "missing or invalid 'path' or 'sha256'"
            )

        result = storage.run_probe_roundtrip(
            probe_path=path,
            expected_sha256=sha256_val,
            cloud_root_prefix=config.cloud_root_prefix,
            asr_ready=bool(check_asr_ready()),
            model_loaded=bool(check_model_loaded()),
        )
        return make_json_response(result.to_dict())

    @app.post("/api/v1/sessions/start")
    async def post_sessions_start(request: Request) -> JSONResponse:
        body = await _parse_json_object(request)
        _check_protocol_version(body, required=True)

        token = body.get("token")
        if not verify_connection_token(token, config.server_auth_token_hash):
            raise ProtocolValidationError("unauthorized", "invalid connection token")

        for req_key in ("session_id", "cloud_path", "title", "source_type"):
            if req_key not in body or not isinstance(body[req_key], str):
                raise ProtocolValidationError(
                    "invalid_request",
                    f"missing or non-string required field '{req_key}'",
                )

        if not check_asr_ready():
            raise ProtocolValidationError(
                "asr_not_ready", "ASR subsystem is not ready", retryable=True
            )
        if not check_storage_ready():
            raise ProtocolValidationError(
                "storage_not_ready",
                "cloud storage is not reachable",
                retryable=True,
            )

        rec, session_token = session_manager.start_session(
            session_id=body["session_id"],
            cloud_path=body["cloud_path"],
            title=body["title"],
            source_type=body["source_type"],
        )
        return make_json_response(
            {
                "protocol_version": PROTOCOL_VERSION,
                "session_id": rec.session_id,
                "session_token": session_token,
                "state": rec.state,
            }
        )

    @app.post("/api/v1/sessions/end")
    async def post_sessions_end(request: Request) -> JSONResponse:
        body = await _parse_json_object(request)
        _check_protocol_version(body, required=False)

        token = body.get("token")
        if not isinstance(token, str) or not token:
            raise ProtocolValidationError("invalid_request", "missing 'token'")

        rec = session_manager.end_session(token)
        return make_json_response(
            {
                "protocol_version": PROTOCOL_VERSION,
                "session_id": rec.session_id,
                "state": rec.state,
                "end_received": rec.end_received,
            }
        )

    @app.post("/api/v1/sessions/retry")
    async def post_sessions_retry(request: Request) -> JSONResponse:
        body = await _parse_json_object(request)
        _check_protocol_version(body, required=False)

        token = body.get("token")
        if not isinstance(token, str) or not token:
            raise ProtocolValidationError("invalid_request", "missing 'token'")

        rec, reset_chunks = session_manager.retry_session(token)
        return make_json_response(
            {
                "protocol_version": PROTOCOL_VERSION,
                "session_id": rec.session_id,
                "state": rec.state,
                "reset_chunks": reset_chunks,
            }
        )

    @app.post("/api/v1/sessions/cancel")
    async def post_sessions_cancel(request: Request) -> JSONResponse:
        body = await _parse_json_object(request)
        _check_protocol_version(body, required=False)

        token = body.get("token")
        if not isinstance(token, str) or not token:
            raise ProtocolValidationError("invalid_request", "missing 'token'")

        rec = session_manager.cancel_session(token)
        return make_json_response(
            {
                "protocol_version": PROTOCOL_VERSION,
                "session_id": rec.session_id,
                "state": rec.state,
            }
        )

    @app.get("/api/v1/sessions/{session_id}")
    async def get_session_status(
        session_id: str, request: Request
    ) -> JSONResponse:
        norm_sid = normalize_session_id(session_id)
        token = _extract_bearer_token(request)

        # 若该 token 属于本服务的另一个已知任务 -> 403 forbidden
        token_owner = session_manager.lookup_session_by_token(token)
        if token_owner is not None and token_owner.session_id != norm_sid:
            raise ProtocolValidationError(
                "forbidden", "session token does not match requested session_id"
            )

        # 校验该 token 是否为请求 session_id 的合法派生 token
        if not verify_session_token_for_id(
            token, config.session_token_secret, norm_sid
        ):
            raise ProtocolValidationError("unauthorized", "invalid session token")

        rec = session_manager.get_session(norm_sid)
        if rec is None:
            raise ProtocolValidationError(
                "unknown_session", f"session '{norm_sid}' not found"
            )
        return make_json_response(rec.to_status_response())

    return app
