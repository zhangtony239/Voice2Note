"""录音任务生命周期管理、状态机、原子落盘、云盘轮询与串行识别调度。"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from voice2note.asr import AsrSettings, transcribe
from voice2note.rec_daemon.auth import derive_session_token
from voice2note.rec_daemon.config import DaemonConfig
from voice2note.rec_daemon.protocol import (
    PROTOCOL_VERSION,
    SESSION_STATES,
    TERMINAL_STATES,
    VALID_SOURCE_TYPES,
    CompleteChunkEntry,
    CompleteMarker,
    IndexChunkEntry,
    ManifestChunk,
    ManifestSemanticError,
    ManifestTransientError,
    ProtocolErrorObj,
    ProtocolValidationError,
    TranscriptIndex,
    expected_audio_chunk_path,
    expected_transcript_chunk_path,
    normalize_session_id,
    parse_manifest_bytes,
    sha256_hex,
    validate_session_cloud_path,
)
from voice2note.rec_daemon.storage import (
    StorageAccessError,
    StorageLimitExceededError,
    WebDAVStorageClient,
    write_local_transcript_atomic,
)

# 合法的状态转移表（Task 7.1）
ALLOWED_STATE_TRANSITIONS: dict[str, frozenset[str]] = {
    "waiting_source": frozenset(
        {"waiting_source", "receiving", "draining", "failed", "cancelled"}
    ),
    "receiving": frozenset({"receiving", "draining", "failed", "cancelled"}),
    "draining": frozenset({"draining", "completed", "failed", "cancelled"}),
    "failed": frozenset({"failed", "receiving", "draining", "cancelled"}),
    "completed": frozenset({"completed", "cancelled"}),
    "cancelled": frozenset({"cancelled"}),
}


class InvalidStateTransitionError(ValueError):
    """非法任务状态转移。"""


def validate_state_transition(current_state: str, target_state: str) -> str:
    if current_state not in SESSION_STATES or target_state not in SESSION_STATES:
        raise InvalidStateTransitionError(
            f"unknown state in transition {current_state!r} -> {target_state!r}"
        )
    allowed = ALLOWED_STATE_TRANSITIONS[current_state]
    if target_state not in allowed:
        raise InvalidStateTransitionError(
            f"illegal state transition: {current_state} -> {target_state}"
        )
    return target_state


def _utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class ChunkProgress:
    sequence: int
    path: str
    start_sample: int
    sample_count: int
    audio_sha256: str
    status: str = "pending"  # "pending" | "completed" | "failed"
    audio_verified: bool = False
    asr_completed: bool = False
    transcript_uploaded: bool = False
    transcript_path: str | None = None
    transcript_sha256: str | None = None
    error: ProtocolErrorObj | None = None
    retry_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "path": self.path,
            "start_sample": self.start_sample,
            "sample_count": self.sample_count,
            "audio_sha256": self.audio_sha256,
            "status": self.status,
            "audio_verified": self.audio_verified,
            "asr_completed": self.asr_completed,
            "transcript_uploaded": self.transcript_uploaded,
            "transcript_path": self.transcript_path,
            "transcript_sha256": self.transcript_sha256,
            "error": self.error.to_dict() if self.error is not None else None,
            "retry_count": self.retry_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChunkProgress:
        err_raw = data.get("error")
        err_obj = (
            ProtocolErrorObj(
                code=str(err_raw["code"]),
                message=str(err_raw["message"]),
                retryable=bool(err_raw["retryable"]),
            )
            if isinstance(err_raw, dict)
            else None
        )
        return cls(
            sequence=int(data["sequence"]),
            path=str(data["path"]),
            start_sample=int(data["start_sample"]),
            sample_count=int(data["sample_count"]),
            audio_sha256=str(data["audio_sha256"]),
            status=str(data.get("status", "pending")),
            audio_verified=bool(data.get("audio_verified", False)),
            asr_completed=bool(data.get("asr_completed", False)),
            transcript_uploaded=bool(data.get("transcript_uploaded", False)),
            transcript_path=data.get("transcript_path"),
            transcript_sha256=data.get("transcript_sha256"),
            error=err_obj,
            retry_count=int(data.get("retry_count", 0)),
        )


@dataclass
class SessionStateRecord:
    session_id: str
    cloud_path: str
    title: str = ""
    source_type: str = "microphone"
    state: str = "waiting_source"
    end_received: bool = False
    manifest_revision: int = 0
    manifest_sha256: str = ""
    final_manifest_adopted: bool = False
    final_manifest_sha256: str = ""
    final_chunk_count: int | None = None
    final_total_samples: int | None = None
    index_revision: int = 0
    last_published_index_digest: str = ""
    created_at: str = field(default_factory=_utc_now_iso)
    updated_at: str = field(default_factory=_utc_now_iso)
    error: ProtocolErrorObj | None = None
    chunks: dict[int, ChunkProgress] = field(default_factory=dict)

    def transition_to(self, new_state: str, *, error: ProtocolErrorObj | None = None) -> bool:
        validate_state_transition(self.state, new_state)
        changed = (self.state != new_state) or (self.error != error)
        self.state = new_state
        self.error = error
        if changed:
            self.updated_at = _utc_now_iso()
        return changed

    @property
    def chunks_total(self) -> int:
        if self.final_chunk_count is not None:
            return self.final_chunk_count
        return len(self.chunks)

    @property
    def chunks_completed(self) -> int:
        return sum(1 for c in self.chunks.values() if c.status == "completed")

    @property
    def chunks_failed(self) -> int:
        return sum(1 for c in self.chunks.values() if c.status == "failed")

    def to_status_response(self) -> dict[str, Any]:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "session_id": self.session_id,
            "cloud_path": self.cloud_path,
            "state": self.state,
            "end_received": self.end_received,
            "chunks_total": self.chunks_total,
            "chunks_completed": self.chunks_completed,
            "chunks_failed": self.chunks_failed,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "error": self.error.to_dict() if self.error is not None else None,
        }

    def to_state_file_dict(self) -> dict[str, Any]:
        if self.state == "cancelled":
            # 取消后的最小占位记录（Task 7.6）
            return {
                "protocol_version": PROTOCOL_VERSION,
                "session_id": self.session_id,
                "cloud_path": self.cloud_path,
                "state": "cancelled",
                "end_received": self.end_received,
                "created_at": self.created_at,
                "updated_at": self.updated_at,
            }
        return {
            "protocol_version": PROTOCOL_VERSION,
            "session_id": self.session_id,
            "cloud_path": self.cloud_path,
            "title": self.title,
            "source_type": self.source_type,
            "state": self.state,
            "end_received": self.end_received,
            "manifest_revision": self.manifest_revision,
            "manifest_sha256": self.manifest_sha256,
            "final_manifest_adopted": self.final_manifest_adopted,
            "final_manifest_sha256": self.final_manifest_sha256,
            "final_chunk_count": self.final_chunk_count,
            "final_total_samples": self.final_total_samples,
            "index_revision": self.index_revision,
            "last_published_index_digest": self.last_published_index_digest,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "error": self.error.to_dict() if self.error is not None else None,
            "chunks": [
                self.chunks[seq].to_dict() for seq in sorted(self.chunks.keys())
            ],
        }

    @classmethod
    def from_state_file_dict(cls, data: dict[str, Any]) -> SessionStateRecord:
        err_raw = data.get("error")
        err_obj = (
            ProtocolErrorObj(
                code=str(err_raw["code"]),
                message=str(err_raw["message"]),
                retryable=bool(err_raw["retryable"]),
            )
            if isinstance(err_raw, dict)
            else None
        )
        chunks_map: dict[int, ChunkProgress] = {}
        for item in data.get("chunks", []):
            cp = ChunkProgress.from_dict(item)
            chunks_map[cp.sequence] = cp
        return cls(
            session_id=str(data["session_id"]).lower(),
            cloud_path=str(data["cloud_path"]),
            title=str(data.get("title", "")),
            source_type=str(data.get("source_type", "microphone")),
            state=str(data["state"]),
            end_received=bool(data.get("end_received", False)),
            manifest_revision=int(data.get("manifest_revision", 0)),
            manifest_sha256=str(data.get("manifest_sha256", "")),
            final_manifest_adopted=bool(data.get("final_manifest_adopted", False)),
            final_manifest_sha256=str(data.get("final_manifest_sha256", "")),
            final_chunk_count=data.get("final_chunk_count"),
            final_total_samples=data.get("final_total_samples"),
            index_revision=int(data.get("index_revision", 0)),
            last_published_index_digest=str(
                data.get("last_published_index_digest", "")
            ),
            created_at=str(data.get("created_at", _utc_now_iso())),
            updated_at=str(data.get("updated_at", _utc_now_iso())),
            error=err_obj,
            chunks=chunks_map,
        )


def save_session_record_atomic(state_dir: Path, record: SessionStateRecord) -> Path:
    """原子保存任务状态文件：同目录临时文件 -> 落盘 fsync -> os.replace（Task 7.2）。"""
    state_dir.mkdir(parents=True, exist_ok=True)
    target = state_dir / f"{record.session_id}.json"
    tmp = state_dir / f"{record.session_id}.json.tmp.{threading.get_ident()}"
    raw = json.dumps(
        record.to_state_file_dict(), ensure_ascii=False, indent=2
    ).encode("utf-8")
    with tmp.open("wb") as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, target)
    return target


def load_session_record(path: Path) -> SessionStateRecord:
    raw = path.read_bytes()
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise TypeError(f"state file {path} root is not object")
    return SessionStateRecord.from_state_file_dict(data)


TransrcibeFn = Callable[[Path, AsrSettings], str]


class SessionManager:
    """管理全部录音会话、任务 token 映射、后台轮询线程与全局 ASR 串行锁。"""

    def __init__(
        self,
        config: DaemonConfig,
        storage: WebDAVStorageClient,
        *,
        transcribe_fn: TransrcibeFn | None = None,
        logger: logging.Logger | None = None,
        enable_background_workers: bool = True,
    ) -> None:
        self.config = config
        self.storage = storage
        self.transcribe_fn: TransrcibeFn = (
            transcribe_fn if transcribe_fn is not None else transcribe
        )
        self.logger = logger or logging.getLogger("voice2note.rec_daemon")
        self.enable_background_workers = enable_background_workers

        self._lock = threading.RLock()
        self._asr_lock = threading.Lock()
        self._sessions: dict[str, SessionStateRecord] = {}
        self._session_locks: dict[str, threading.RLock] = {}
        self._token_to_session_id: dict[str, str] = {}
        self._stop_events: dict[str, threading.Event] = {}
        self._wake_events: dict[str, threading.Event] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._shutdown = threading.Event()

    def _get_session_lock(self, session_id: str) -> threading.RLock:
        with self._lock:
            lock = self._session_locks.get(session_id)
            if lock is None:
                lock = threading.RLock()
                self._session_locks[session_id] = lock
            return lock

    def _persist(self, record: SessionStateRecord) -> None:
        save_session_record_atomic(self.config.session_state_path, record)

    def session_work_dir(self, session_id: str) -> Path:
        return self.config.session_work_path / session_id

    def session_audio_dir(self, session_id: str) -> Path:
        return self.session_work_dir(session_id) / "audio"

    def session_transcripts_dir(self, session_id: str) -> Path:
        return self.session_work_dir(session_id) / "transcripts"

    def active_session_count(self) -> int:
        with self._lock:
            return sum(
                1
                for rec in self._sessions.values()
                if rec.state not in TERMINAL_STATES
            )

    def total_session_count(self) -> int:
        with self._lock:
            return len(self._sessions)

    def lookup_session_by_token(self, session_token: object) -> SessionStateRecord | None:
        if not isinstance(session_token, str) or not session_token:
            return None
        with self._lock:
            sid = self._token_to_session_id.get(session_token)
            if sid is None:
                return None
            return self._sessions.get(sid)

    def get_session(self, session_id: str) -> SessionStateRecord | None:
        with self._lock:
            return self._sessions.get(session_id.lower())

    def recover_sessions(self) -> int:
        """启动时扫描状态目录恢复会话、重建 token 映射并拉起未结束任务的轮询线程（Task 7.7）。"""
        count = 0
        if not self.config.session_state_path.is_dir():
            return 0
        for file_path in sorted(self.config.session_state_path.glob("*.json")):
            try:
                rec = load_session_record(file_path)
            except Exception as exc:  # noqa: BLE001
                self.logger.warning(
                    "跳过损坏的状态文件 %s: %s", file_path.name, exc
                )
                continue
            tok = derive_session_token(
                self.config.session_token_secret, rec.session_id
            )
            with self._lock:
                self._sessions[rec.session_id] = rec
                self._token_to_session_id[tok] = rec.session_id
                self._get_session_lock(rec.session_id)
            count += 1
            if rec.state not in TERMINAL_STATES and self.enable_background_workers:
                self._ensure_worker_thread(rec.session_id)
        return count

    def shutdown(self, timeout: float = 5.0) -> None:
        self._shutdown.set()
        with self._lock:
            events = list(self._stop_events.values())
            wakes = list(self._wake_events.values())
            threads = list(self._threads.values())
        for ev in events:
            ev.set()
        for wk in wakes:
            wk.set()
        for t in threads:
            if t.is_alive():
                t.join(timeout=timeout)

    def _ensure_worker_thread(self, session_id: str) -> None:
        with self._lock:
            existing = self._threads.get(session_id)
            if existing is not None and existing.is_alive():
                return
            stop_ev = threading.Event()
            wake_ev = threading.Event()
            self._stop_events[session_id] = stop_ev
            self._wake_events[session_id] = wake_ev
            t = threading.Thread(
                target=self._poll_loop,
                args=(session_id, stop_ev, wake_ev),
                name=f"session-worker-{session_id[:8]}",
                daemon=True,
            )
            self._threads[session_id] = t
            t.start()

    def _wake_worker(self, session_id: str) -> None:
        with self._lock:
            wk = self._wake_events.get(session_id)
        if wk is not None:
            wk.set()

    def start_session(
        self,
        *,
        session_id: str,
        cloud_path: str,
        title: str,
        source_type: str,
    ) -> tuple[SessionStateRecord, str]:
        """建立或幂等返回任务（Task 7.3 & 7.8）。"""
        norm_sid = normalize_session_id(session_id)
        clean_cloud_path = validate_session_cloud_path(
            cloud_path, norm_sid, self.config.cloud_root_prefix
        )
        if not isinstance(title, str) or len(title) > 200:
            raise ProtocolValidationError(
                "invalid_request", "title must be a string up to 200 characters"
            )
        if source_type not in VALID_SOURCE_TYPES:
            raise ProtocolValidationError(
                "invalid_request",
                "source_type must be 'microphone' or 'import'",
            )

        with self._lock:
            existing = self._sessions.get(norm_sid)
            if existing is not None:
                if existing.state == "cancelled":
                    raise ProtocolValidationError(
                        "session_terminal",
                        "session has been cancelled and cannot be restarted",
                    )
                if existing.cloud_path != clean_cloud_path:
                    raise ProtocolValidationError(
                        "session_id_conflict",
                        "session_id already exists with a different cloud_path",
                    )
                tok = derive_session_token(
                    self.config.session_token_secret, norm_sid
                )
                return existing, tok

            if self.active_session_count() >= self.config.max_active_sessions:
                raise ProtocolValidationError(
                    "too_many_requests",
                    "maximum active sessions reached",
                    retryable=True,
                )

            rec = SessionStateRecord(
                session_id=norm_sid,
                cloud_path=clean_cloud_path,
                title=title,
                source_type=source_type,
                state="waiting_source",
            )
            tok = derive_session_token(self.config.session_token_secret, norm_sid)
            self._sessions[norm_sid] = rec
            self._token_to_session_id[tok] = norm_sid
            self._get_session_lock(norm_sid)
            self.session_audio_dir(norm_sid).mkdir(parents=True, exist_ok=True)
            self.session_transcripts_dir(norm_sid).mkdir(parents=True, exist_ok=True)
            self._persist(rec)

        self.logger.info(
            "建立任务 session_id=%s cloud_path=%s",
            norm_sid,
            clean_cloud_path,
            extra={"session_id": norm_sid},
        )
        if self.enable_background_workers:
            self._ensure_worker_thread(norm_sid)
        return rec, tok

    def end_session(self, session_token: str) -> SessionStateRecord:
        """幂等处理结束通知（Task 7.4）：进入收尾或保持已完成/已取消，恒返回成功。"""
        rec = self.lookup_session_by_token(session_token)
        if rec is None:
            raise ProtocolValidationError("unauthorized", "invalid session token")

        s_lock = self._get_session_lock(rec.session_id)
        with s_lock:
            changed = False
            if not rec.end_received:
                rec.end_received = True
                changed = True
            if rec.state in {"waiting_source", "receiving"} and rec.transition_to(
                "draining", error=rec.error
            ):
                changed = True
            if changed:
                rec.updated_at = _utc_now_iso()
                self._persist(rec)
        self.logger.info(
            "收到结束通知 state=%s", rec.state, extra={"session_id": rec.session_id}
        )
        self._wake_worker(rec.session_id)
        return rec

    def retry_session(self, session_token: str) -> tuple[SessionStateRecord, list[int]]:
        """执行重试的六项动作（Task 7.5）：
        1. 对终态（completed / cancelled）返回 409 session_terminal
        2. 把失败块与未完成块重置为 pending（保留已完成块与已上传原稿）
        3. 若本地原稿已就绪但此前仅云盘上传失败，保留 asr_completed=True 免重跑 ASR；
           若音频已通过校验且文件仍在，保留 audio_verified=True 免重新下载
        4. 会话级失败（failed）的任务清除会话错误并退回 draining 或 receiving，以便重读清单
        5. 索引版本号保持不回退
        6. 返回本次重置的块编号列表并唤醒轮询
        """
        rec = self.lookup_session_by_token(session_token)
        if rec is None:
            raise ProtocolValidationError("unauthorized", "invalid session token")

        s_lock = self._get_session_lock(rec.session_id)
        with s_lock:
            if rec.state in TERMINAL_STATES:
                raise ProtocolValidationError(
                    "session_terminal",
                    f"cannot retry session in terminal state '{rec.state}'",
                )
            reset_sequences: list[int] = []
            for seq in sorted(rec.chunks.keys()):
                cp = rec.chunks[seq]
                if cp.status != "completed":
                    # 若之前是因为 sha256 不符或格式非法，删除残留音频
                    if cp.error and cp.error.code in {
                        "audio_sha256_mismatch",
                        "audio_format_invalid",
                        "audio_too_large",
                    }:
                        audio_file = (
                            self.session_audio_dir(rec.session_id)
                            / f"{seq:06d}.wav"
                        )
                        audio_file.unlink(missing_ok=True)
                        cp.audio_verified = False
                    cp.status = "pending"
                    cp.error = None
                    cp.retry_count += 1
                    reset_sequences.append(seq)

            if rec.state == "failed":
                # 允许重读最终清单
                rec.final_manifest_adopted = False
                rec.final_manifest_sha256 = ""
                rec.final_chunk_count = None
                rec.final_total_samples = None
                target = "draining" if rec.end_received else "receiving"
                rec.transition_to(target, error=None)
            else:
                rec.updated_at = _utc_now_iso()

            self._persist(rec)

        self.logger.info(
            "触发任务重试 reset_chunks=%s state=%s",
            reset_sequences,
            rec.state,
            extra={"session_id": rec.session_id},
        )
        if self.enable_background_workers:
            self._ensure_worker_thread(rec.session_id)
        self._wake_worker(rec.session_id)
        return rec, reset_sequences

    def cancel_session(self, session_token: str) -> SessionStateRecord:
        """幂等取消任务（Task 7.6）：
        1. 停止轮询线程
        2. 先写 `cancelled` 最小占位记录落盘
        3. 再删除本地工作目录 `<SESSION_WORK_PATH>/<session_id>/`
        4. 不删除云盘上的任何文件
        """
        rec = self.lookup_session_by_token(session_token)
        if rec is None:
            raise ProtocolValidationError("unauthorized", "invalid session token")

        with self._lock:
            stop_ev = self._stop_events.get(rec.session_id)
            wake_ev = self._wake_events.get(rec.session_id)
        if stop_ev is not None:
            stop_ev.set()
        if wake_ev is not None:
            wake_ev.set()

        s_lock = self._get_session_lock(rec.session_id)
        with s_lock:
            rec.transition_to("cancelled", error=None)
            rec.chunks.clear()
            # 先写占位记录，再删工作目录
            self._persist(rec)
            work_dir = self.session_work_dir(rec.session_id)
            if work_dir.exists():
                shutil.rmtree(work_dir, ignore_errors=True)

        self.logger.info(
            "取消任务并清理本地工作目录", extra={"session_id": rec.session_id}
        )
        return rec

    def _poll_loop(
        self,
        session_id: str,
        stop_ev: threading.Event,
        wake_ev: threading.Event,
    ) -> None:
        interval = float(self.config.poll_interval_seconds)
        max_interval = float(self.config.poll_max_interval_seconds)
        last_progress_ts = time.monotonic()
        warned_idle = False

        while not stop_ev.is_set() and not self._shutdown.is_set():
            rec = self.get_session(session_id)
            if rec is None or rec.state in TERMINAL_STATES:
                break

            progressed = False
            try:
                progressed = self.step_session(session_id)
            except Exception as exc:  # noqa: BLE001
                self.logger.error(
                    "轮询异常: %s", exc, extra={"session_id": session_id}
                )

            rec = self.get_session(session_id)
            if rec is None or rec.state in TERMINAL_STATES:
                break

            now = time.monotonic()
            if progressed:
                interval = float(self.config.poll_interval_seconds)
                last_progress_ts = now
                warned_idle = False
            else:
                interval = min(max_interval, interval * 2.0)
                idle_hours = (now - last_progress_ts) / 3600.0
                if (
                    not warned_idle
                    and idle_hours >= float(self.config.session_idle_warn_hours)
                ):
                    self.logger.warning(
                        "任务已空闲超过 %d 小时，状态保持 %s",
                        self.config.session_idle_warn_hours,
                        rec.state,
                        extra={"session_id": session_id},
                    )
                    warned_idle = True

            wake_ev.wait(timeout=interval)
            wake_ev.clear()

    def step_session(self, session_id: str) -> bool:
        """执行单轮任务推进（可在测试中同步调用，也可由后台轮询线程调用）。
        返回本轮是否有实质状态/版本/块进度变化。
        """
        rec = self.get_session(session_id)
        if rec is None:
            return False

        s_lock = self._get_session_lock(session_id)
        with s_lock:
            if rec.state in TERMINAL_STATES or rec.state == "failed":
                return False

            progressed = False

            # 1. 读取滚动清单 manifest.json（若尚未采用最终清单）
            if not rec.final_manifest_adopted and self._poll_rolling_manifest(rec):
                progressed = True

            # 2. 若已收到结束通知，尝试读取并校验最终清单 final.json
            if rec.end_received:
                if self._poll_final_manifest(rec):
                    progressed = True
                if rec.state == "failed":
                    self._persist(rec)
                    return True

            # 3. 逐块处理待处理音频块
            pending_seqs = [
                seq
                for seq in sorted(rec.chunks.keys())
                if rec.chunks[seq].status == "pending"
            ]
            for seq in pending_seqs:
                if rec.state == "cancelled" or self._shutdown.is_set():
                    break
                if self._process_single_chunk(rec, rec.chunks[seq]):
                    progressed = True
                    # 块级处理后立即更新结果索引（先原稿后索引）
                    self._publish_index_if_changed(rec)
                    self._persist(rec)

            # 4. 若已采用最终清单且全部块完成，执行回读校验、发布最终索引与完成标记
            if (
                rec.final_manifest_adopted
                and rec.state != "cancelled"
                and self._try_finalize_session(rec)
            ):
                progressed = True
                self._persist(rec)

            if progressed:
                self._persist(rec)
            return progressed

    def _merge_manifest_chunks(
        self, rec: SessionStateRecord, chunks: tuple[ManifestChunk, ...]
    ) -> None:
        for ch in chunks:
            existing = rec.chunks.get(ch.sequence)
            if existing is None:
                rec.chunks[ch.sequence] = ChunkProgress(
                    sequence=ch.sequence,
                    path=ch.path,
                    start_sample=ch.start_sample,
                    sample_count=ch.sample_count,
                    audio_sha256=ch.sha256,
                )

    def _poll_rolling_manifest(self, rec: SessionStateRecord) -> bool:
        """读取滚动清单 manifest.json 并执行版本规则（Task 8.3）。"""
        manifest_rel = f"{rec.cloud_path}/manifest.json"
        try:
            raw = self.storage.read_bytes(
                manifest_rel, max_bytes=2 * 1024 * 1024
            )
        except (StorageAccessError, StorageLimitExceededError) as exc:
            self.logger.warning(
                "读取滚动清单失败: %s", exc, extra={"session_id": rec.session_id}
            )
            return False

        if raw is None:
            return False

        try:
            manifest = parse_manifest_bytes(
                raw,
                expected_session_id=rec.session_id,
                cloud_path=rec.cloud_path,
                is_final=False,
            )
        except (ManifestTransientError, ProtocolValidationError) as exc:
            self.logger.warning(
                "滚动清单暂不可用或解析失败: %s",
                exc,
                extra={"session_id": rec.session_id},
            )
            return False

        if manifest.revision < rec.manifest_revision:
            self.logger.warning(
                "忽略旧版本滚动清单 revision=%d < current=%d",
                manifest.revision,
                rec.manifest_revision,
                extra={"session_id": rec.session_id},
            )
            return False

        if manifest.revision == rec.manifest_revision:
            if manifest.raw_sha256 != rec.manifest_sha256:
                self.logger.warning(
                    "滚动清单同版本号 revision=%d 但内容不同，忽略并保持上次有效状态",
                    manifest.revision,
                    extra={"session_id": rec.session_id},
                )
            return False

        # revision 增大，采用新清单
        rec.manifest_revision = manifest.revision
        rec.manifest_sha256 = manifest.raw_sha256
        self._merge_manifest_chunks(rec, manifest.chunks)
        rec.updated_at = _utc_now_iso()
        self.logger.info(
            "采用滚动清单 revision=%d chunks=%d",
            manifest.revision,
            len(manifest.chunks),
            extra={"session_id": rec.session_id},
        )
        return True

    def _poll_final_manifest(self, rec: SessionStateRecord) -> bool:
        """读取最终清单 final.json 并区分暂时性不可用与内容自相矛盾（Task 8.4）。"""
        final_rel = f"{rec.cloud_path}/final.json"
        try:
            raw = self.storage.read_bytes(final_rel, max_bytes=2 * 1024 * 1024)
        except (StorageAccessError, StorageLimitExceededError) as exc:
            self.logger.warning(
                "读取最终清单失败: %s", exc, extra={"session_id": rec.session_id}
            )
            return False

        if raw is None:
            return False

        raw_sha = sha256_hex(raw)
        if rec.final_manifest_adopted:
            if raw_sha != rec.final_manifest_sha256:
                self.logger.warning(
                    "最终清单已采用过，忽略被改写的新内容",
                    extra={"session_id": rec.session_id},
                )
            return False

        try:
            final_manifest = parse_manifest_bytes(
                raw,
                expected_session_id=rec.session_id,
                cloud_path=rec.cloud_path,
                is_final=True,
            )
        except ManifestTransientError as exc:
            # 半写、无法解析或字段缺失：保持等待，不置为失败
            self.logger.warning(
                "最终清单尚未就绪（继续等待）: %s",
                exc,
                extra={"session_id": rec.session_id},
            )
            return False
        except ManifestSemanticError as exc:
            # 内容自相矛盾：置为 failed，保留已下载音频与原稿
            rec.transition_to("failed", error=exc.error)
            self.logger.error(
                "最终清单内容自相矛盾: %s",
                exc,
                extra={"session_id": rec.session_id},
            )
            return True

        rec.final_manifest_adopted = True
        rec.final_manifest_sha256 = final_manifest.raw_sha256
        rec.final_chunk_count = final_manifest.chunk_count
        rec.final_total_samples = final_manifest.total_samples
        if final_manifest.revision > rec.manifest_revision:
            rec.manifest_revision = final_manifest.revision
            rec.manifest_sha256 = final_manifest.raw_sha256
        self._merge_manifest_chunks(rec, final_manifest.chunks)
        rec.updated_at = _utc_now_iso()
        self.logger.info(
            "采用最终清单 revision=%d chunk_count=%s total_samples=%s",
            final_manifest.revision,
            final_manifest.chunk_count,
            final_manifest.total_samples,
            extra={"session_id": rec.session_id},
        )
        return True

    def _process_single_chunk(
        self, rec: SessionStateRecord, cp: ChunkProgress
    ) -> bool:
        """处理单个音频块：路径检查 -> 流式下载与六步校验 -> 串行 ASR -> 原子写本地原稿 -> 上传云盘原稿。"""
        expected_audio_rel = expected_audio_chunk_path(rec.cloud_path, cp.sequence)
        if cp.path != expected_audio_rel:
            cp.status = "failed"
            cp.error = ProtocolErrorObj(
                code="invalid_path",
                message=f"chunk {cp.sequence} path '{cp.path}' does not match '{expected_audio_rel}'",
                retryable=False,
            )
            self._advance_to_receiving_if_waiting(rec)
            return True

        local_audio = (
            self.session_audio_dir(rec.session_id) / f"{cp.sequence:06d}.wav"
        )
        local_transcript = (
            self.session_transcripts_dir(rec.session_id)
            / f"{cp.sequence:06d}.md"
        )
        cloud_transcript_rel = expected_transcript_chunk_path(
            rec.cloud_path, cp.sequence
        )

        # 步骤 A：下载并六步校验音频（若重试时已通过校验且本地文件仍在则复用）
        if not (cp.audio_verified and local_audio.is_file()):
            dl_err = self.storage.download_and_validate_audio_chunk(
                rel_path=cp.path,
                dest_file=local_audio,
                expected_sha256=cp.audio_sha256,
                expected_sample_count=cp.sample_count,
                max_chunk_bytes=self.config.max_audio_chunk_bytes,
            )
            if dl_err is not None:
                cp.status = "failed"
                cp.error = dl_err
                cp.audio_verified = False
                self._advance_to_receiving_if_waiting(rec)
                self.logger.warning(
                    "块 %d 下载/校验失败: code=%s",
                    cp.sequence,
                    dl_err.code,
                    extra={"session_id": rec.session_id},
                )
                return True
            cp.audio_verified = True

        # 步骤 B：执行识别并原子写本地原稿（若此前仅上传失败且本地原稿仍在，免重跑识别）
        if not (cp.asr_completed and local_transcript.is_file()):
            with self._asr_lock:
                self.logger.info(
                    "开始识别音频块 seq=%d samples=%d",
                    cp.sequence,
                    cp.sample_count,
                    extra={"session_id": rec.session_id},
                )
                t0 = time.monotonic()
                try:
                    text = self.transcribe_fn(local_audio, self.config)
                except Exception as exc:  # noqa: BLE001
                    elapsed = time.monotonic() - t0
                    code = (
                        "audio_decode_failed"
                        if isinstance(exc, ValueError)
                        and "decode" in str(exc).lower()
                        else "asr_failed"
                    )
                    retryable = code == "asr_failed"
                    cp.status = "failed"
                    cp.error = ProtocolErrorObj(
                        code=code,
                        message=f"ASR failed on chunk {cp.sequence}: {exc.__class__.__name__}",
                        retryable=retryable,
                    )
                    self._advance_to_receiving_if_waiting(rec)
                    self.logger.error(
                        "识别音频块失败 seq=%d elapsed=%.3fs code=%s",
                        cp.sequence,
                        elapsed,
                        code,
                        extra={"session_id": rec.session_id},
                    )
                    return True
                elapsed = time.monotonic() - t0
                self.logger.info(
                    "完成识别音频块 seq=%d elapsed=%.3fs chars=%d",
                    cp.sequence,
                    elapsed,
                    len(text),
                    extra={"session_id": rec.session_id},
                )

            try:
                raw_md, md_sha = write_local_transcript_atomic(
                    local_transcript, text
                )
            except StorageLimitExceededError as exc:
                cp.status = "failed"
                cp.error = ProtocolErrorObj(
                    code="transcript_upload_failed",
                    message=str(exc),
                    retryable=False,
                )
                self._advance_to_receiving_if_waiting(rec)
                return True
            cp.asr_completed = True
            cp.transcript_sha256 = md_sha
            cp.transcript_path = cloud_transcript_rel
        else:
            raw_md = local_transcript.read_bytes()
            cp.transcript_sha256 = sha256_hex(raw_md)
            cp.transcript_path = cloud_transcript_rel

        # 步骤 C：上传原稿到云盘 .transcripts/{sequence:06d}.md（先原稿后索引）
        try:
            self.storage.write_bytes(cloud_transcript_rel, raw_md)
        except StorageAccessError as exc:
            cp.status = "failed"
            cp.transcript_uploaded = False
            cp.error = ProtocolErrorObj(
                code="transcript_upload_failed",
                message=f"failed to upload transcript for chunk {cp.sequence}: {exc}",
                retryable=True,
            )
            self._advance_to_receiving_if_waiting(rec)
            self.logger.warning(
                "块 %d 原稿上传云盘失败: %s",
                cp.sequence,
                exc,
                extra={"session_id": rec.session_id},
            )
            return True

        cp.transcript_uploaded = True
        cp.status = "completed"
        cp.error = None
        self._advance_to_receiving_if_waiting(rec)
        return True

    def _advance_to_receiving_if_waiting(self, rec: SessionStateRecord) -> None:
        if rec.state == "waiting_source":
            target = "draining" if rec.end_received else "receiving"
            rec.transition_to(target, error=rec.error)
        else:
            rec.updated_at = _utc_now_iso()

    def _build_index_entries(
        self, rec: SessionStateRecord
    ) -> tuple[IndexChunkEntry, ...]:
        """结果索引只收录已经尝试完毕（completed 或 failed）的块（Task 8.7）。"""
        entries: list[IndexChunkEntry] = []
        for seq in sorted(rec.chunks.keys()):
            cp = rec.chunks[seq]
            if cp.status == "completed":
                entries.append(
                    IndexChunkEntry(
                        sequence=cp.sequence,
                        audio_sha256=cp.audio_sha256,
                        status="completed",
                        path=cp.transcript_path,
                        sha256=cp.transcript_sha256,
                        error=None,
                    )
                )
            elif cp.status == "failed":
                entries.append(
                    IndexChunkEntry(
                        sequence=cp.sequence,
                        audio_sha256=cp.audio_sha256,
                        status="failed",
                        path=None,
                        sha256=None,
                        error=cp.error,
                    )
                )
        return tuple(entries)

    def _publish_index_if_changed(
        self, rec: SessionStateRecord, *, force_even_if_empty: bool = False
    ) -> bool:
        """发布 `.transcripts/index.json`（内容无变化时不重复写入也不递增版本号）。"""
        entries = self._build_index_entries(rec)
        if not entries and not force_even_if_empty:
            return False

        digest_source = json.dumps(
            [e.to_dict() for e in entries], ensure_ascii=False, sort_keys=True
        ).encode("utf-8")
        digest = sha256_hex(digest_source)
        if digest == rec.last_published_index_digest and rec.index_revision > 0:
            return False

        next_rev = rec.index_revision + 1
        idx_obj = TranscriptIndex(
            protocol_version=PROTOCOL_VERSION,
            session_id=rec.session_id,
            revision=next_rev,
            chunks=entries,
        )
        raw_bytes = json.dumps(
            idx_obj.to_dict(), ensure_ascii=False, indent=2
        ).encode("utf-8")
        if len(raw_bytes) > 2 * 1024 * 1024:
            raise StorageLimitExceededError("index.json exceeds 2 MiB limit")

        index_rel = f"{rec.cloud_path}/.transcripts/index.json"
        try:
            self.storage.write_bytes(index_rel, raw_bytes)
        except StorageAccessError as exc:
            self.logger.warning(
                "发布 index.json 失败: %s", exc, extra={"session_id": rec.session_id}
            )
            return False

        rec.index_revision = next_rev
        rec.last_published_index_digest = digest
        rec.updated_at = _utc_now_iso()
        self.logger.info(
            "已发布 index.json revision=%d entries=%d",
            next_rev,
            len(entries),
            extra={"session_id": rec.session_id},
        )
        return True

    def _try_finalize_session(self, rec: SessionStateRecord) -> bool:
        """当最终清单全部块已完成时：逐份回读原稿校验 -> 先写最终索引 -> 后写完成标记 -> completed（Task 8.8）。"""
        if not rec.final_manifest_adopted or rec.final_chunk_count is None:
            return False

        expected_count = rec.final_chunk_count
        for seq in range(1, expected_count + 1):
            cp = rec.chunks.get(seq)
            if cp is None or cp.status != "completed":
                return False

        # 逐份从云盘回读原稿并校验 sha256
        complete_entries: list[CompleteChunkEntry] = []
        for seq in range(1, expected_count + 1):
            cp = rec.chunks[seq]
            assert cp.transcript_path is not None
            assert cp.transcript_sha256 is not None
            try:
                readback = self.storage.read_bytes(
                    cp.transcript_path, max_bytes=128 * 1024
                )
            except (StorageAccessError, StorageLimitExceededError) as exc:
                self.logger.warning(
                    "回读原稿 seq=%d 失败: %s",
                    seq,
                    exc,
                    extra={"session_id": rec.session_id},
                )
                cp.status = "pending"
                cp.transcript_uploaded = False
                return False

            if readback is None or sha256_hex(readback) != cp.transcript_sha256:
                self.logger.warning(
                    "回读原稿 seq=%d 校验不符，重新排队上传",
                    seq,
                    extra={"session_id": rec.session_id},
                )
                cp.status = "pending"
                cp.transcript_uploaded = False
                return False

            complete_entries.append(
                CompleteChunkEntry(
                    sequence=seq,
                    audio_sha256=cp.audio_sha256,
                    path=cp.transcript_path,
                    sha256=cp.transcript_sha256,
                )
            )

        # 先确保最终结果索引已发布（包括零块录音）
        self._publish_index_if_changed(rec, force_even_if_empty=True)
        if rec.index_revision == 0:
            # 索引发布失败则不发布完成标记
            return False

        marker = CompleteMarker(
            protocol_version=PROTOCOL_VERSION,
            session_id=rec.session_id,
            final_manifest_sha256=rec.final_manifest_sha256,
            chunks=tuple(complete_entries),
        )
        complete_rel = f"{rec.cloud_path}/.transcripts/complete.json"
        raw_complete = json.dumps(
            marker.to_dict(), ensure_ascii=False, indent=2
        ).encode("utf-8")
        try:
            self.storage.write_bytes(complete_rel, raw_complete)
        except StorageAccessError as exc:
            self.logger.warning(
                "发布 complete.json 失败: %s",
                exc,
                extra={"session_id": rec.session_id},
            )
            return False

        if rec.state == "waiting_source":
            rec.transition_to("draining")
        rec.transition_to("completed", error=None)
        self.logger.info(
            "任务已完成并发布 complete.json chunks=%d",
            expected_count,
            extra={"session_id": rec.session_id},
        )
        return True
