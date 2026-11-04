"""协议 v1 常量、数据结构与云盘 JSON 解析校验。"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any

PROTOCOL_VERSION = 1

# 契约硬编码常量（design.md 第 12 节：不做成配置项）
MAX_MANIFEST_BYTES = 2 * 1024 * 1024  # 2 MiB
MAX_INDEX_BYTES = 2 * 1024 * 1024  # 2 MiB
MAX_TRANSCRIPT_BYTES = 128 * 1024  # 128 KiB
MAX_CONTROL_REQUEST_BYTES = 64 * 1024  # 64 KiB
MAX_PROBE_FILE_BYTES = 64 * 1024  # 64 KiB

EXPECTED_SAMPLE_RATE = 16000
EXPECTED_CHANNELS = 1
EXPECTED_ENCODING = "pcm_s16le"
EXPECTED_SAMPLE_WIDTH_BYTES = 2

SESSION_STATES: frozenset[str] = frozenset(
    {
        "waiting_source",
        "receiving",
        "draining",
        "completed",
        "failed",
        "cancelled",
    }
)

TERMINAL_STATES: frozenset[str] = frozenset({"completed", "cancelled"})
VALID_SOURCE_TYPES: frozenset[str] = frozenset({"microphone", "import"})
MAX_TITLE_CHARS = 200

_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_UUID_CANONICAL_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


@dataclass(frozen=True)
class ProtocolErrorObj:
    """统一错误结构：{code, message, retryable}。"""

    code: str
    message: str
    retryable: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }


class ProtocolValidationError(Exception):
    """协议字段或路径校验失败。"""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.error = ProtocolErrorObj(code=code, message=message, retryable=retryable)


def is_valid_sha256_hex(value: object) -> bool:
    return isinstance(value, str) and bool(_SHA256_HEX_RE.fullmatch(value))


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalize_session_id(raw: object) -> str:
    """校验带连字符的 UUID 并归一化为小写。"""
    if not isinstance(raw, str):
        raise ProtocolValidationError(
            "invalid_request", "session_id must be a UUID string"
        )
    lowered = raw.strip().lower()
    if not _UUID_CANONICAL_RE.fullmatch(lowered):
        raise ProtocolValidationError(
            "invalid_request", "session_id must be a canonical hyphenated UUID"
        )
    try:
        parsed = uuid.UUID(lowered)
    except ValueError as exc:
        raise ProtocolValidationError(
            "invalid_request", "session_id is not a valid UUID"
        ) from exc
    if str(parsed) != lowered:
        raise ProtocolValidationError(
            "invalid_request", "session_id must be canonical hyphenated format"
        )
    return lowered


def validate_relative_cloud_path(path: object, *, error_code: str = "invalid_path") -> str:
    """校验相对于云盘根的路径（不以 / 开头、无 scheme、无反斜杠、无空段或 ..）。"""
    if not isinstance(path, str) or not path:
        raise ProtocolValidationError(error_code, "path must be a non-empty string")
    if path.startswith("/") or path.endswith("/"):
        raise ProtocolValidationError(error_code, "path must be relative without leading/trailing slash")
    if "\\" in path:
        raise ProtocolValidationError(error_code, "path must not contain backslashes")
    if "://" in path or ":" in path:
        raise ProtocolValidationError(error_code, "path must not contain URL scheme or drive colon")
    segments = path.split("/")
    for seg in segments:
        if not seg or seg in {".", ".."}:
            raise ProtocolValidationError(error_code, "path contains invalid segment")
    return path


def validate_session_cloud_path(
    cloud_path: object, session_id: str, cloud_root_prefix: str
) -> str:
    """校验开始请求中的 cloud_path 必须等于 `<CLOUD_ROOT_PREFIX>/<session_id>`。"""
    rel = validate_relative_cloud_path(cloud_path, error_code="invalid_cloud_path")
    prefix = cloud_root_prefix.strip("/")
    expected = f"{prefix}/{session_id}"
    if rel != expected:
        raise ProtocolValidationError(
            "invalid_cloud_path",
            f"cloud_path must equal '{expected}'",
        )
    return rel


def validate_probe_path(path: object, cloud_root_prefix: str) -> str:
    """校验探测文件路径：必须位于 `<CLOUD_ROOT_PREFIX>/_probe/<filename>`，恰好三段。"""
    rel = validate_relative_cloud_path(path, error_code="invalid_cloud_path")
    prefix = cloud_root_prefix.strip("/")
    segments = rel.split("/")
    if len(segments) != 3 or segments[0] != prefix or segments[1] != "_probe":
        raise ProtocolValidationError(
            "invalid_cloud_path",
            f"probe path must be '{prefix}/_probe/<filename>'",
        )
    if not segments[2].endswith(".bin") or len(segments[2]) <= 4:
        raise ProtocolValidationError(
            "invalid_cloud_path",
            "probe file must be a non-empty .bin filename",
        )
    return rel


def expected_audio_chunk_path(cloud_path: str, sequence: int) -> str:
    return f"{cloud_path}/audio/{sequence:06d}.wav"


def expected_transcript_chunk_path(cloud_path: str, sequence: int) -> str:
    return f"{cloud_path}/.transcripts/{sequence:06d}.md"


@dataclass(frozen=True)
class ManifestChunk:
    sequence: int
    path: str
    start_sample: int
    sample_count: int
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "path": self.path,
            "start_sample": self.start_sample,
            "sample_count": self.sample_count,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class AudioManifest:
    protocol_version: int
    session_id: str
    revision: int
    sample_rate: int
    channels: int
    encoding: str
    chunks: tuple[ManifestChunk, ...]
    total_samples: int | None = None
    chunk_count: int | None = None
    raw_sha256: str = ""

    def to_dict(self, *, is_final: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {
            "protocol_version": self.protocol_version,
            "session_id": self.session_id,
            "revision": self.revision,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "encoding": self.encoding,
        }
        if is_final or self.total_samples is not None:
            data["total_samples"] = self.total_samples
        if is_final or self.chunk_count is not None:
            data["chunk_count"] = self.chunk_count
        data["chunks"] = [c.to_dict() for c in self.chunks]
        return data


@dataclass(frozen=True)
class IndexChunkEntry:
    sequence: int
    audio_sha256: str
    status: str  # "completed" | "failed"
    path: str | None
    sha256: str | None
    error: ProtocolErrorObj | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "audio_sha256": self.audio_sha256,
            "status": self.status,
            "path": self.path,
            "sha256": self.sha256,
            "error": self.error.to_dict() if self.error is not None else None,
        }


@dataclass(frozen=True)
class TranscriptIndex:
    protocol_version: int
    session_id: str
    revision: int
    chunks: tuple[IndexChunkEntry, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "session_id": self.session_id,
            "revision": self.revision,
            "chunks": [c.to_dict() for c in self.chunks],
        }


@dataclass(frozen=True)
class CompleteChunkEntry:
    sequence: int
    audio_sha256: str
    path: str
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "audio_sha256": self.audio_sha256,
            "path": self.path,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class CompleteMarker:
    protocol_version: int
    session_id: str
    final_manifest_sha256: str
    chunks: tuple[CompleteChunkEntry, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "session_id": self.session_id,
            "final_manifest_sha256": self.final_manifest_sha256,
            "chunks": [c.to_dict() for c in self.chunks],
        }


class ManifestTransientError(Exception):
    """清单尚未就绪、JSON 截断或字段缺失（应继续等待，不将任务置为失败）。"""


class ManifestSemanticError(Exception):
    """最终清单解析成功但内容自相矛盾（应将任务置为 failed / manifest_invalid）。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.error = ProtocolErrorObj(
            code="manifest_invalid", message=message, retryable=True
        )


def _is_strict_int(val: object) -> bool:
    return isinstance(val, int) and not isinstance(val, bool)


def parse_manifest_bytes(
    raw: bytes,
    *,
    expected_session_id: str,
    cloud_path: str,
    is_final: bool = False,
) -> AudioManifest:
    """解析滚动清单或最终清单字节。

    - JSON 截断、顶层必填字段缺失或类型错误抛出 `ManifestTransientError`
    - 最终清单内容自相矛盾抛出 `ManifestSemanticError`
    """
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ProtocolValidationError(
            "payload_too_large", "manifest exceeds 2 MiB limit"
        )
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestTransientError(f"manifest JSON parse failed: {exc}") from exc

    if not isinstance(data, dict):
        raise ManifestTransientError("manifest root must be an object")

    required_top = {
        "protocol_version",
        "session_id",
        "revision",
        "sample_rate",
        "channels",
        "encoding",
        "chunks",
    }
    if is_final:
        required_top |= {"total_samples", "chunk_count"}

    if not required_top.issubset(data.keys()):
        raise ManifestTransientError("manifest missing required top-level fields")

    if data["protocol_version"] != PROTOCOL_VERSION:
        raise ManifestTransientError("unsupported protocol_version in manifest")

    if not isinstance(data["session_id"], str):
        raise ManifestTransientError("session_id must be string")
    if data["session_id"].strip().lower() != expected_session_id.lower():
        if is_final:
            raise ManifestSemanticError("final manifest session_id mismatch")
        raise ManifestTransientError("rolling manifest session_id mismatch")

    if not _is_strict_int(data["revision"]) or data["revision"] < 1:
        raise ManifestTransientError("manifest revision must be positive integer")

    if (
        data["sample_rate"] != EXPECTED_SAMPLE_RATE
        or data["channels"] != EXPECTED_CHANNELS
        or data["encoding"] != EXPECTED_ENCODING
    ):
        if is_final:
            raise ManifestSemanticError("final manifest audio parameters invalid")
        raise ManifestTransientError("manifest audio parameters invalid")

    chunks_raw = data["chunks"]
    if not isinstance(chunks_raw, list):
        raise ManifestTransientError("manifest chunks must be a list")

    parsed_chunks: list[ManifestChunk] = []
    for idx, item in enumerate(chunks_raw):
        if not isinstance(item, dict):
            raise ManifestTransientError(f"chunk[{idx}] must be an object")
        req_chunk = {"sequence", "path", "start_sample", "sample_count", "sha256"}
        if not req_chunk.issubset(item.keys()):
            raise ManifestTransientError(f"chunk[{idx}] missing required fields")
        seq = item["sequence"]
        path = item["path"]
        start_sample = item["start_sample"]
        sample_count = item["sample_count"]
        sha = item["sha256"]
        if (
            not _is_strict_int(seq)
            or seq < 1
            or not isinstance(path, str)
            or not _is_strict_int(start_sample)
            or start_sample < 0
            or not _is_strict_int(sample_count)
            or sample_count < 0
            or not is_valid_sha256_hex(sha)
        ):
            if is_final:
                raise ManifestSemanticError(f"chunk[{idx}] has invalid field values")
            raise ManifestTransientError(f"chunk[{idx}] has invalid field values")

        parsed_chunks.append(
            ManifestChunk(
                sequence=seq,
                path=path,
                start_sample=start_sample,
                sample_count=sample_count,
                sha256=sha,
            )
        )

    total_samples: int | None = None
    chunk_count: int | None = None
    if is_final:
        ts = data["total_samples"]
        cc = data["chunk_count"]
        if not _is_strict_int(ts) or ts < 0 or not _is_strict_int(cc) or cc < 0:
            raise ManifestTransientError("final manifest total_samples/chunk_count invalid")
        total_samples = ts
        chunk_count = cc

        # 语义一致性校验
        if len(parsed_chunks) != chunk_count:
            raise ManifestSemanticError(
                f"chunk_count ({chunk_count}) != len(chunks) ({len(parsed_chunks)})"
            )
        cursor = 0
        for expected_seq, ch in enumerate(parsed_chunks, start=1):
            if ch.sequence != expected_seq:
                raise ManifestSemanticError(
                    f"chunk sequence not contiguous at expected {expected_seq}, got {ch.sequence}"
                )
            expected_path = expected_audio_chunk_path(cloud_path, ch.sequence)
            if ch.path != expected_path:
                raise ManifestSemanticError(
                    f"chunk {ch.sequence} path '{ch.path}' != expected '{expected_path}'"
                )
            if ch.sample_count <= 0:
                raise ManifestSemanticError(
                    f"chunk {ch.sequence} has non-positive sample_count"
                )
            if ch.start_sample != cursor:
                raise ManifestSemanticError(
                    f"chunk {ch.sequence} start_sample ({ch.start_sample}) != expected ({cursor})"
                )
            cursor += ch.sample_count
        if cursor != total_samples:
            raise ManifestSemanticError(
                f"sum of sample_count ({cursor}) != total_samples ({total_samples})"
            )

    return AudioManifest(
        protocol_version=PROTOCOL_VERSION,
        session_id=expected_session_id,
        revision=data["revision"],
        sample_rate=data["sample_rate"],
        channels=data["channels"],
        encoding=data["encoding"],
        chunks=tuple(parsed_chunks),
        total_samples=total_samples,
        chunk_count=chunk_count,
        raw_sha256=sha256_hex(raw),
    )


def parse_index_bytes(raw: bytes, *, cloud_path: str | None = None) -> TranscriptIndex:
    """解析结果索引 index.json。"""
    if len(raw) > MAX_INDEX_BYTES:
        raise ProtocolValidationError("payload_too_large", "index exceeds 2 MiB limit")
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ProtocolValidationError("invalid_request", "index root must be object")
    if data.get("protocol_version") != PROTOCOL_VERSION:
        raise ProtocolValidationError(
            "unsupported_protocol_version", "unsupported protocol_version"
        )
    session_id = normalize_session_id(data.get("session_id"))
    revision = data.get("revision")
    if not _is_strict_int(revision) or revision < 1:
        raise ProtocolValidationError("invalid_request", "invalid index revision")
    chunks_raw = data.get("chunks")
    if not isinstance(chunks_raw, list):
        raise ProtocolValidationError("invalid_request", "index chunks must be list")

    entries: list[IndexChunkEntry] = []
    for item in chunks_raw:
        if not isinstance(item, dict):
            raise ProtocolValidationError("invalid_request", "index chunk must be object")
        seq = item.get("sequence")
        audio_sha = item.get("audio_sha256")
        status = item.get("status")
        path = item.get("path")
        sha = item.get("sha256")
        err_raw = item.get("error")
        if (
            not _is_strict_int(seq)
            or seq < 1
            or not is_valid_sha256_hex(audio_sha)
            or status not in {"completed", "failed"}
        ):
            raise ProtocolValidationError("invalid_request", "invalid index chunk entry")
        if path is not None:
            validate_relative_cloud_path(path, error_code="invalid_path")
            if cloud_path is not None and path != expected_transcript_chunk_path(cloud_path, seq):
                raise ProtocolValidationError(
                    "invalid_path", f"transcript path '{path}' outside session transcript dir"
                )
        if sha is not None and not is_valid_sha256_hex(sha):
            raise ProtocolValidationError("invalid_request", "invalid transcript sha256")
        err_obj = None
        if err_raw is not None:
            if not isinstance(err_raw, dict):
                raise ProtocolValidationError("invalid_request", "invalid error object")
            err_obj = ProtocolErrorObj(
                code=str(err_raw["code"]),
                message=str(err_raw["message"]),
                retryable=bool(err_raw["retryable"]),
            )
        entries.append(
            IndexChunkEntry(
                sequence=seq,
                audio_sha256=audio_sha,
                status=status,
                path=path,
                sha256=sha,
                error=err_obj,
            )
        )
    return TranscriptIndex(
        protocol_version=PROTOCOL_VERSION,
        session_id=session_id,
        revision=revision,
        chunks=tuple(entries),
    )


def parse_complete_bytes(raw: bytes, *, cloud_path: str | None = None) -> CompleteMarker:
    """解析完成标记 complete.json。"""
    if len(raw) > MAX_INDEX_BYTES:
        raise ProtocolValidationError("payload_too_large", "complete.json exceeds limit")
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ProtocolValidationError("invalid_request", "complete root must be object")
    if data.get("protocol_version") != PROTOCOL_VERSION:
        raise ProtocolValidationError(
            "unsupported_protocol_version", "unsupported protocol_version"
        )
    session_id = normalize_session_id(data.get("session_id"))
    final_sha = data.get("final_manifest_sha256")
    if not is_valid_sha256_hex(final_sha):
        raise ProtocolValidationError("invalid_request", "invalid final_manifest_sha256")
    chunks_raw = data.get("chunks")
    if not isinstance(chunks_raw, list):
        raise ProtocolValidationError("invalid_request", "complete chunks must be list")

    entries: list[CompleteChunkEntry] = []
    for item in chunks_raw:
        if not isinstance(item, dict):
            raise ProtocolValidationError("invalid_request", "complete chunk must be object")
        seq = item.get("sequence")
        audio_sha = item.get("audio_sha256")
        path = item.get("path")
        sha = item.get("sha256")
        if (
            not _is_strict_int(seq)
            or seq < 1
            or not is_valid_sha256_hex(audio_sha)
            or not isinstance(path, str)
            or not is_valid_sha256_hex(sha)
        ):
            raise ProtocolValidationError("invalid_request", "invalid complete chunk")
        validate_relative_cloud_path(path, error_code="invalid_path")
        if cloud_path is not None and path != expected_transcript_chunk_path(cloud_path, seq):
            raise ProtocolValidationError("invalid_path", "invalid complete chunk path")
        entries.append(
            CompleteChunkEntry(
                sequence=seq,
                audio_sha256=audio_sha,
                path=path,
                sha256=sha,
            )
        )
    return CompleteMarker(
        protocol_version=PROTOCOL_VERSION,
        session_id=session_id,
        final_manifest_sha256=final_sha,
        chunks=tuple(entries),
    )
