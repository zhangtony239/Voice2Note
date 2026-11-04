"""本地模拟网盘（WebDAV 子集）服务与故障注入。

支持最小读写子集：
- MKCOL 创建目录
- PUT 写入文件
- GET 读取文件
- DELETE 删除文件或目录
- HEAD / PROPFIND 探测存在

支持八种故障注入（Task 3.2）：
1. not_visible：指定路径始终返回 404（文件尚未可见）
2. partial_json：GET 返回截断的半写 JSON 字节
3. delayed_visible：前 N 次 GET 返回 404，第 N+1 次起正常返回
4. tampered_content：GET 返回被篡改的字节
5. reject_zero_byte_put：PUT 0 字节时拒绝写入（返回 400）
6. slow_response：触发超时或延迟响应
7. redirect：返回 302 重定向到外部地址
8. silent_put_drop：PUT 返回 201 成功但实际不落盘（或落盘损坏内容）
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from urllib.parse import unquote

import httpx


@dataclass
class FaultRule:
    kind: str
    remaining_404s: int = 0
    tampered_bytes: bytes = b"TAMPERED"
    partial_cutoff: int = 10
    redirect_location: str = "https://external.example.com/hijack"
    delay_seconds: float = 0.0
    raise_timeout: bool = False


class MockWebDAVServer:
    """内存模拟 WebDAV 服务，可通过 `httpx.MockTransport` 挂载到 `httpx.Client`。"""

    def __init__(
        self,
        *,
        base_path: str = "/dav",
        username: str = "testuser",
        password: str = "testpass",
        reject_zero_byte_put: bool = False,
    ) -> None:
        self.base_path = "/" + base_path.strip("/")
        self.username = username
        self.password = password
        self.reject_zero_byte_put = reject_zero_byte_put

        self._lock = threading.Lock()
        self.files: dict[str, bytes] = {}
        self.dirs: set[str] = {""}
        self.faults: dict[str, FaultRule] = {}
        self.operation_log: list[tuple[str, str]] = []
        self.transport = httpx.MockTransport(self._handle_request)

    def _normalize_rel(self, rel_path: str) -> str:
        rel = unquote(rel_path).strip("/")
        return rel

    def seed_dir(self, rel_path: str) -> None:
        rel = self._normalize_rel(rel_path)
        with self._lock:
            parts = rel.split("/") if rel else []
            cur = ""
            self.dirs.add("")
            for part in parts:
                cur = f"{cur}/{part}" if cur else part
                self.dirs.add(cur)

    def seed_file(self, rel_path: str, data: bytes) -> None:
        rel = self._normalize_rel(rel_path)
        parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
        self.seed_dir(parent)
        with self._lock:
            self.files[rel] = bytes(data)

    def read_seeded_file(self, rel_path: str) -> bytes | None:
        rel = self._normalize_rel(rel_path)
        with self._lock:
            return self.files.get(rel)

    def set_fault(self, rel_path: str, rule: FaultRule) -> None:
        rel = self._normalize_rel(rel_path)
        with self._lock:
            self.faults[rel] = rule

    def clear_fault(self, rel_path: str) -> None:
        rel = self._normalize_rel(rel_path)
        with self._lock:
            self.faults.pop(rel, None)

    def clear_all_faults(self) -> None:
        with self._lock:
            self.faults.clear()

    def _extract_rel_path(self, url_path: str) -> str | None:
        raw = unquote(url_path)
        if raw == self.base_path or raw == self.base_path + "/":
            return ""
        prefix = self.base_path + "/"
        if not raw.startswith(prefix):
            return None
        rest = raw[len(prefix) :]
        dup_prefix = self.base_path.lstrip("/") + "/"
        if dup_prefix != "/" and rest.startswith(dup_prefix):
            rest = rest[len(dup_prefix) :]
        return rest.strip("/")

    def _handle_request(self, request: httpx.Request) -> httpx.Response:
        rel = self._extract_rel_path(request.url.path)
        if rel is None:
            return httpx.Response(404, text="Outside WebDAV base path")

        method = request.method.upper()
        with self._lock:
            self.operation_log.append((method, rel))
            fault = self.faults.get(rel) or self.faults.get("*")

        if fault is not None:
            if fault.kind == "slow_response":
                if fault.delay_seconds > 0:
                    time.sleep(fault.delay_seconds)
                if fault.raise_timeout:
                    raise httpx.ReadTimeout("Simulated slow response timeout", request=request)
            elif fault.kind == "redirect":
                return httpx.Response(
                    302,
                    headers={"Location": fault.redirect_location},
                    text="Redirecting",
                )

        if method == "MKCOL":
            return self._handle_mkcol(rel)
        if method == "PUT":
            return self._handle_put(rel, request.content, fault)
        if method == "GET":
            return self._handle_get(rel, fault)
        if method == "DELETE":
            return self._handle_delete(rel)
        if method in {"HEAD", "PROPFIND"}:
            return self._handle_exists(method, rel, fault)
        return httpx.Response(405, text=f"Unsupported method {method}")

    def _handle_mkcol(self, rel: str) -> httpx.Response:
        with self._lock:
            if rel in self.dirs:
                return httpx.Response(405, text="Collection already exists")
            parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
            if parent not in self.dirs:
                return httpx.Response(409, text="Parent collection does not exist")
            self.dirs.add(rel)
            return httpx.Response(201, text="Created")

    def _handle_put(
        self, rel: str, body: bytes, fault: FaultRule | None
    ) -> httpx.Response:
        if (self.reject_zero_byte_put or (fault and fault.kind == "reject_zero_byte_put")) and len(body) == 0:
            return httpx.Response(400, text="Zero-byte PUT rejected")

        if fault and fault.kind == "silent_put_drop":
            # 写入返回成功但不落盘
            return httpx.Response(201, text="Created (dropped)")

        parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
        self.seed_dir(parent)
        with self._lock:
            is_new = rel not in self.files
            self.files[rel] = bytes(body)
        return httpx.Response(201 if is_new else 204)

    def _handle_get(self, rel: str, fault: FaultRule | None) -> httpx.Response:
        with self._lock:
            if fault is not None:
                if fault.kind == "not_visible":
                    return httpx.Response(404, text="File not visible yet")
                if fault.kind == "delayed_visible" and fault.remaining_404s > 0:
                    fault.remaining_404s -= 1
                    return httpx.Response(404, text="Delayed visibility")
            data = self.files.get(rel)
            if data is None:
                return httpx.Response(404, text="Not found")
            if fault is not None:
                if fault.kind == "partial_json":
                    cutoff = max(1, min(len(data) - 1, fault.partial_cutoff))
                    return httpx.Response(200, content=data[:cutoff])
                if fault.kind == "tampered_content":
                    return httpx.Response(200, content=fault.tampered_bytes)
            return httpx.Response(200, content=data)

    def _handle_delete(self, rel: str) -> httpx.Response:
        with self._lock:
            if rel in self.files:
                del self.files[rel]
                return httpx.Response(204)
            if rel in self.dirs and rel != "":
                prefix = rel + "/"
                for k in [k for k in self.files if k.startswith(prefix)]:
                    del self.files[k]
                for d in [d for d in self.dirs if d == rel or d.startswith(prefix)]:
                    self.dirs.discard(d)
                return httpx.Response(204)
            return httpx.Response(404, text="Not found")

    def _handle_exists(
        self, method: str, rel: str, fault: FaultRule | None
    ) -> httpx.Response:
        with self._lock:
            if fault is not None and fault.kind == "not_visible":
                return httpx.Response(404)
            exists = (rel in self.files) or (rel in self.dirs)
        if not exists:
            return httpx.Response(404)
        return httpx.Response(207 if method == "PROPFIND" else 200)
