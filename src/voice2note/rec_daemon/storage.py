"""云盘（OpenList WebDAV）客户端、路径拼接、六步音频校验与探测闭环。"""

from __future__ import annotations

import hashlib
import json
import os
import wave
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from voice2note.rec_daemon.protocol import (
    EXPECTED_CHANNELS,
    EXPECTED_SAMPLE_RATE,
    EXPECTED_SAMPLE_WIDTH_BYTES,
    MAX_PROBE_FILE_BYTES,
    MAX_TRANSCRIPT_BYTES,
    PROTOCOL_VERSION,
    ProtocolErrorObj,
    ProtocolValidationError,
    is_valid_sha256_hex,
    sha256_hex,
    validate_probe_path,
    validate_relative_cloud_path,
)


class StorageAccessError(Exception):
    """WebDAV 读写失败、网络超时或遇到禁止跟随的 3xx 重定向。"""


class StorageLimitExceededError(Exception):
    """读取或写入内容超过协议体积上限。"""


@dataclass(frozen=True)
class ProbeRoundtripResult:
    ok: bool
    asr_ready: bool
    model_loaded: bool
    read_ok: bool
    write_ok: bool
    receipt: dict[str, str] | None
    error: ProtocolErrorObj | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "ok": self.ok,
            "asr_ready": self.asr_ready,
            "model_loaded": self.model_loaded,
            "read_ok": self.read_ok,
            "write_ok": self.write_ok,
            "receipt": self.receipt,
            "error": self.error.to_dict() if self.error is not None else None,
        }


class WebDAVStorageClient:
    """线程安全的本机 OpenList WebDAV 客户端（禁用重定向，统一相对路径基准）。"""

    def __init__(
        self,
        *,
        base_url: str,
        username: str,
        password: str,
        timeout_seconds: int = 30,
        verify_tls: bool = True,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            auth=(username, password),
            timeout=float(timeout_seconds),
            verify=verify_tls,
            follow_redirects=False,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def build_url(self, rel_path: str) -> str:
        """校验相对云盘根路径并拼接到 WebDAV 根地址。"""
        clean = validate_relative_cloud_path(rel_path, error_code="invalid_path")
        return f"{self.base_url}/{clean}"

    def _check_no_redirect(self, response: httpx.Response, rel_path: str) -> None:
        if 300 <= response.status_code < 400:
            raise StorageAccessError(
                f"WebDAV redirect ({response.status_code}) rejected for '{rel_path}'"
            )

    def check_ready(self, cloud_root_prefix: str) -> bool:
        """健康检查用：探测云盘根或前缀是否可达（不跟随重定向）。"""
        try:
            url = f"{self.base_url}/"
            resp = self._client.request("PROPFIND", url, headers={"Depth": "0"})
            if 300 <= resp.status_code < 400:
                return False
            return resp.status_code in {200, 204, 207, 404, 405}
        except httpx.HTTPError:
            return False

    def ensure_dir(self, rel_dir: str) -> None:
        """逐级创建云盘目录（已存在返回 405 视为成功）。"""
        clean = validate_relative_cloud_path(rel_dir, error_code="invalid_path")
        parts = clean.split("/")
        cur = ""
        for part in parts:
            cur = f"{cur}/{part}" if cur else part
            url = f"{self.base_url}/{cur}"
            try:
                resp = self._client.request("MKCOL", url)
            except httpx.HTTPError as exc:
                raise StorageAccessError(f"MKCOL failed for '{cur}': {exc}") from exc
            self._check_no_redirect(resp, cur)
            if resp.status_code not in {200, 201, 204, 405}:
                raise StorageAccessError(
                    f"MKCOL '{cur}' returned unexpected status {resp.status_code}"
                )

    def read_bytes(self, rel_path: str, *, max_bytes: int) -> bytes | None:
        """流式读取云盘文件，超过 max_bytes 立即中止并抛 StorageLimitExceededError；404 返回 None。"""
        url = self.build_url(rel_path)
        try:
            with self._client.stream("GET", url) as resp:
                self._check_no_redirect(resp, rel_path)
                if resp.status_code == 404:
                    return None
                if resp.status_code != 200:
                    raise StorageAccessError(
                        f"GET '{rel_path}' failed with status {resp.status_code}"
                    )
                chunks: list[bytes] = []
                total = 0
                for chunk in resp.iter_bytes(chunk_size=65536):
                    total += len(chunk)
                    if total > max_bytes:
                        raise StorageLimitExceededError(
                            f"'{rel_path}' exceeds size limit of {max_bytes} bytes"
                        )
                    chunks.append(chunk)
                return b"".join(chunks)
        except httpx.HTTPError as exc:
            raise StorageAccessError(f"GET '{rel_path}' network error: {exc}") from exc

    def write_bytes(self, rel_path: str, data: bytes) -> None:
        """写入字节到云盘路径（自动确保父目录存在）。"""
        clean = validate_relative_cloud_path(rel_path, error_code="invalid_path")
        if "/" in clean:
            parent = clean.rsplit("/", 1)[0]
            self.ensure_dir(parent)
        url = f"{self.base_url}/{clean}"
        try:
            resp = self._client.put(url, content=data)
        except httpx.HTTPError as exc:
            raise StorageAccessError(f"PUT '{clean}' network error: {exc}") from exc
        self._check_no_redirect(resp, clean)
        if resp.status_code not in {200, 201, 204}:
            raise StorageAccessError(
                f"PUT '{clean}' failed with status {resp.status_code}"
            )

    def download_and_validate_audio_chunk(
        self,
        *,
        rel_path: str,
        dest_file: Path,
        expected_sha256: str,
        expected_sample_count: int,
        max_chunk_bytes: int,
    ) -> ProtocolErrorObj | None:
        """流式下载音频块并执行六步校验（Task 8.5）：

        1. 流式下载，体积超过 max_chunk_bytes 立即中止并删除临时文件 -> audio_too_large (retryable=False)
        2. 404 或读取失败，删除临时文件 -> audio_missing (retryable=True)
        3. SHA-256 校验值不符，删除临时文件 -> audio_sha256_mismatch (retryable=True)
        4. 原子替换为正式文件
        5. 解析 WAV 头：16kHz、单声道、16位 PCM、帧数 == expected_sample_count、实际可读数据长度一致 -> 不符返回 audio_format_invalid (retryable=False) 并删除文件
        6. expected_sample_count <= 0 -> audio_format_invalid (retryable=False) 并删除文件
        """
        url = self.build_url(rel_path)
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_file = dest_file.with_suffix(dest_file.suffix + ".tmp")

        hasher = hashlib.sha256()
        total = 0
        try:
            with self._client.stream("GET", url) as resp:
                self._check_no_redirect(resp, rel_path)
                if resp.status_code == 404:
                    return ProtocolErrorObj(
                        code="audio_missing",
                        message=f"audio chunk not found at '{rel_path}'",
                        retryable=True,
                    )
                if resp.status_code != 200:
                    return ProtocolErrorObj(
                        code="audio_missing",
                        message=f"audio chunk unreadable (HTTP {resp.status_code}) at '{rel_path}'",
                        retryable=True,
                    )
                with tmp_file.open("wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=65536):
                        total += len(chunk)
                        if total > max_chunk_bytes:
                            f.close()
                            tmp_file.unlink(missing_ok=True)
                            return ProtocolErrorObj(
                                code="audio_too_large",
                                message=f"audio chunk exceeds limit ({max_chunk_bytes} bytes)",
                                retryable=False,
                            )
                        hasher.update(chunk)
                        f.write(chunk)
                    f.flush()
                    os.fsync(f.fileno())
        except (httpx.HTTPError, StorageAccessError, OSError) as exc:
            tmp_file.unlink(missing_ok=True)
            return ProtocolErrorObj(
                code="audio_missing",
                message=f"audio chunk download failed: {exc}",
                retryable=True,
            )

        actual_sha = hasher.hexdigest()
        if actual_sha != expected_sha256.lower():
            tmp_file.unlink(missing_ok=True)
            return ProtocolErrorObj(
                code="audio_sha256_mismatch",
                message=f"audio sha256 mismatch for '{rel_path}'",
                retryable=True,
            )

        os.replace(tmp_file, dest_file)

        # 第 5 & 6 步：样本数为零或 WAV 头部字段不符
        header_err = validate_wav_file(dest_file, expected_sample_count=expected_sample_count)
        if header_err is not None:
            dest_file.unlink(missing_ok=True)
            return header_err

        return None

    def run_probe_roundtrip(
        self,
        *,
        probe_path: str,
        expected_sha256: str,
        cloud_root_prefix: str,
        asr_ready: bool,
        model_loaded: bool,
    ) -> ProbeRoundtripResult:
        """执行云盘探测四步往返（Task 6.4）：
        1. 读取探测文件并校验体积（<= 64 KiB）与 sha256
        2. 创建探测目录
        3. 写回回执 `<probe_path>.receipt.json`
        4. 回读回执并自校验 sha256
        """
        clean_path = validate_probe_path(probe_path, cloud_root_prefix)
        if not is_valid_sha256_hex(expected_sha256):
            raise ProtocolValidationError(
                "invalid_request", "sha256 must be 64 lowercase hex characters"
            )

        try:
            raw = self.read_bytes(clean_path, max_bytes=MAX_PROBE_FILE_BYTES)
        except StorageLimitExceededError:
            return ProbeRoundtripResult(
                ok=False,
                asr_ready=asr_ready,
                model_loaded=model_loaded,
                read_ok=False,
                write_ok=False,
                receipt=None,
                error=ProtocolErrorObj(
                    code="probe_too_large",
                    message="probe file exceeds 64 KiB limit",
                    retryable=False,
                ),
            )
        except StorageAccessError as exc:
            return ProbeRoundtripResult(
                ok=False,
                asr_ready=asr_ready,
                model_loaded=model_loaded,
                read_ok=False,
                write_ok=False,
                receipt=None,
                error=ProtocolErrorObj(
                    code="probe_missing",
                    message=f"failed to read probe file: {exc}",
                    retryable=True,
                ),
            )

        if raw is None:
            return ProbeRoundtripResult(
                ok=False,
                asr_ready=asr_ready,
                model_loaded=model_loaded,
                read_ok=False,
                write_ok=False,
                receipt=None,
                error=ProtocolErrorObj(
                    code="probe_missing",
                    message=f"probe file not found: '{clean_path}'",
                    retryable=True,
                ),
            )

        actual_sha = sha256_hex(raw)
        if actual_sha != expected_sha256.lower():
            return ProbeRoundtripResult(
                ok=False,
                asr_ready=asr_ready,
                model_loaded=model_loaded,
                read_ok=False,
                write_ok=False,
                receipt=None,
                error=ProtocolErrorObj(
                    code="probe_sha256_mismatch",
                    message="probe file sha256 mismatch",
                    retryable=True,
                ),
            )

        # 第 2、3、4 步：创建目录、写回回执、回读校验
        receipt_path = f"{clean_path}.receipt.json"
        receipt_payload = {
            "protocol_version": PROTOCOL_VERSION,
            "probe_path": clean_path,
            "probe_sha256": actual_sha,
            "asr_ready": asr_ready,
            "timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        receipt_bytes = json.dumps(receipt_payload, ensure_ascii=False, indent=2).encode(
            "utf-8"
        )
        expected_receipt_sha = sha256_hex(receipt_bytes)

        try:
            self.write_bytes(receipt_path, receipt_bytes)
            readback = self.read_bytes(receipt_path, max_bytes=MAX_PROBE_FILE_BYTES)
        except (StorageAccessError, StorageLimitExceededError) as exc:
            return ProbeRoundtripResult(
                ok=False,
                asr_ready=asr_ready,
                model_loaded=model_loaded,
                read_ok=True,
                write_ok=False,
                receipt=None,
                error=ProtocolErrorObj(
                    code="storage_not_ready",
                    message=f"probe receipt write/readback failed: {exc}",
                    retryable=True,
                ),
            )

        if readback is None or sha256_hex(readback) != expected_receipt_sha:
            return ProbeRoundtripResult(
                ok=False,
                asr_ready=asr_ready,
                model_loaded=model_loaded,
                read_ok=True,
                write_ok=False,
                receipt=None,
                error=ProtocolErrorObj(
                    code="storage_not_ready",
                    message="probe receipt readback verification failed",
                    retryable=True,
                ),
            )

        overall_ok = bool(asr_ready)
        return ProbeRoundtripResult(
            ok=overall_ok,
            asr_ready=asr_ready,
            model_loaded=model_loaded,
            read_ok=True,
            write_ok=True,
            receipt={"path": receipt_path, "sha256": expected_receipt_sha},
            error=None,
        )


def validate_wav_file(wav_path: Path, *, expected_sample_count: int) -> ProtocolErrorObj | None:
    """使用标准库 wave 解析 WAV 头，校验 16kHz、单声道、16位、样本数 > 0 且与数据长度一致。"""
    if expected_sample_count <= 0:
        return ProtocolErrorObj(
            code="audio_format_invalid",
            message="sample_count must be positive",
            retryable=False,
        )
    try:
        with wave.open(str(wav_path), "rb") as wf:
            channels = wf.getnchannels()
            sample_width = wf.getsampwidth()
            sample_rate = wf.getframerate()
            nframes = wf.getnframes()
            comptype = wf.getcomptype()
            if (
                comptype != "NONE"
                or channels != EXPECTED_CHANNELS
                or sample_width != EXPECTED_SAMPLE_WIDTH_BYTES
                or sample_rate != EXPECTED_SAMPLE_RATE
                or nframes != expected_sample_count
            ):
                return ProtocolErrorObj(
                    code="audio_format_invalid",
                    message=(
                        f"WAV format mismatch: got rate={sample_rate}, ch={channels}, "
                        f"width={sample_width}, frames={nframes}, expected frames={expected_sample_count}"
                    ),
                    retryable=False,
                )
            raw_frames = wf.readframes(expected_sample_count + 1)
            expected_pcm_bytes = (
                expected_sample_count * EXPECTED_CHANNELS * EXPECTED_SAMPLE_WIDTH_BYTES
            )
            if len(raw_frames) != expected_pcm_bytes:
                return ProtocolErrorObj(
                    code="audio_format_invalid",
                    message=(
                        f"WAV data length ({len(raw_frames)} bytes) does not match "
                        f"sample_count ({expected_sample_count} frames)"
                    ),
                    retryable=False,
                )
    except (wave.Error, EOFError, OSError) as exc:
        return ProtocolErrorObj(
            code="audio_format_invalid",
            message=f"invalid WAV header: {exc}",
            retryable=False,
        )
    return None


def write_local_transcript_atomic(dest_path: Path, transcript_text: str) -> tuple[bytes, str]:
    """将识别文本加上单个结尾换行（空块为 1 字节 `\\n`），校验体积 <= 128 KiB 并原子写盘。"""
    normalized = transcript_text.rstrip("\r\n") + "\n"
    raw_bytes = normalized.encode("utf-8")
    if len(raw_bytes) > MAX_TRANSCRIPT_BYTES:
        raise StorageLimitExceededError(
            f"transcript exceeds 128 KiB limit ({len(raw_bytes)} bytes)"
        )
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest_path.with_suffix(dest_path.suffix + ".tmp")
    with tmp_path.open("wb") as f:
        f.write(raw_bytes)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, dest_path)
    return raw_bytes, sha256_hex(raw_bytes)
