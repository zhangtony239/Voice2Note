"""测试任务生命周期（Tasks 7.1–7.9）：状态转移、原子落盘、幂等/冲突、结束、重试、取消、重启恢复、并发上限与全局锁。"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from tests.audio_factory import make_short_wav
from tests.mock_webdav import MockWebDAVServer
from tests.test_http_control_plane import SID_1, SID_2, SID_3, make_test_env
from voice2note.rec_daemon.protocol import ProtocolValidationError
from voice2note.rec_daemon.session_manager import (
    InvalidStateTransitionError,
    SessionManager,
    SessionStateRecord,
    load_session_record,
    save_session_record_atomic,
    validate_state_transition,
)


def _seed_manifest(
    dav: MockWebDAVServer,
    session_id: str,
    revision: int,
    chunks_info: list[tuple[int, int, int, str]],
    *,
    is_final: bool = False,
) -> bytes:
    cloud_path = f"RecNoteRemote/{session_id}"
    payload: dict = {
        "protocol_version": 1,
        "session_id": session_id,
        "revision": revision,
        "sample_rate": 16000,
        "channels": 1,
        "encoding": "pcm_s16le",
    }
    if is_final:
        payload["total_samples"] = sum(c[2] for c in chunks_info)
        payload["chunk_count"] = len(chunks_info)
    payload["chunks"] = [
        {
            "sequence": seq,
            "path": f"{cloud_path}/audio/{seq:06d}.wav",
            "start_sample": start_s,
            "sample_count": count_s,
            "sha256": sha,
        }
        for (seq, start_s, count_s, sha) in chunks_info
    ]
    raw = json.dumps(payload, indent=2).encode("utf-8")
    name = "final.json" if is_final else "manifest.json"
    dav.seed_file(f"{cloud_path}/{name}", raw)
    return raw


def test_six_state_transitions_legal_and_illegal() -> None:
    """Task 7.1: 六个状态的合法转移与非法转移各有用例。"""
    rec = SessionStateRecord(
        session_id=SID_1, cloud_path=f"RecNoteRemote/{SID_1}"
    )
    assert rec.state == "waiting_source"

    # 合法转移链：waiting_source -> receiving -> draining -> failed -> draining -> completed -> cancelled
    rec.transition_to("receiving")
    assert rec.state == "receiving"
    rec.transition_to("draining")
    assert rec.state == "draining"
    rec.transition_to("failed")
    assert rec.state == "failed"
    rec.transition_to("draining")
    assert rec.state == "draining"
    rec.transition_to("completed")
    assert rec.state == "completed"
    rec.transition_to("cancelled")
    assert rec.state == "cancelled"

    # 非法转移
    with pytest.raises(InvalidStateTransitionError):
        validate_state_transition("waiting_source", "completed")
    with pytest.raises(InvalidStateTransitionError):
        validate_state_transition("receiving", "completed")
    with pytest.raises(InvalidStateTransitionError):
        validate_state_transition("completed", "receiving")
    with pytest.raises(InvalidStateTransitionError):
        validate_state_transition("cancelled", "waiting_source")
    with pytest.raises(InvalidStateTransitionError):
        validate_state_transition("unknown_state", "receiving")


def test_atomic_state_file_write_interrupt_preserves_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 7.2: 状态文件写入过程中被中断不会破坏已有状态文件。"""
    rec = SessionStateRecord(
        session_id=SID_1,
        cloud_path=f"RecNoteRemote/{SID_1}",
        state="receiving",
    )
    target = save_session_record_atomic(tmp_path, rec)
    loaded1 = load_session_record(target)
    assert loaded1.state == "receiving"

    # 模拟在 os.replace 时进程被中断抛异常，验证旧状态文件完好无损
    def crash_on_replace(_src, _dst):
        raise OSError("simulated power loss before atomic replace")

    monkeypatch.setattr(os, "replace", crash_on_replace)
    rec.state = "draining"
    with pytest.raises(OSError):
        save_session_record_atomic(tmp_path, rec)

    loaded_after_crash = load_session_record(target)
    assert loaded_after_crash.state == "receiving"


def test_start_idempotency_and_conflict_branches(tmp_path: Path) -> None:
    """Task 7.3: 新任务、同路径重复、不同路径冲突、已取消冲突且任务不复活。"""
    _, _, _, _, mgr, _ = make_test_env(tmp_path)
    cloud_path = f"RecNoteRemote/{SID_1}"

    # 1. 新任务
    rec1, tok1 = mgr.start_session(
        session_id=SID_1,
        cloud_path=cloud_path,
        title="首次建立",
        source_type="microphone",
    )
    assert rec1.state == "waiting_source"

    # 2. 同路径重复请求（幂等返回原 token 与当前状态）
    rec1.transition_to("receiving")
    rec_dup, tok_dup = mgr.start_session(
        session_id=SID_1,
        cloud_path=cloud_path,
        title="不同标题不影响幂等",
        source_type="import",
    )
    assert tok_dup == tok1
    assert rec_dup.state == "receiving"
    assert mgr.total_session_count() == 1

    # 3. 不同路径冲突
    rec1.cloud_path = "RecNoteRemote/different"
    with pytest.raises(ProtocolValidationError) as exc_conflict:
        mgr.start_session(
            session_id=SID_1,
            cloud_path=cloud_path,
            title="冲突",
            source_type="microphone",
        )
    assert exc_conflict.value.error.code == "session_id_conflict"
    rec1.cloud_path = cloud_path

    # 4. 已取消后再次 start 返回 409 session_terminal 且不复活
    mgr.cancel_session(tok1)
    with pytest.raises(ProtocolValidationError) as exc_term:
        mgr.start_session(
            session_id=SID_1,
            cloud_path=cloud_path,
            title="复活",
            source_type="microphone",
        )
    assert exc_term.value.error.code == "session_terminal"
    assert mgr.get_session(SID_1).state == "cancelled"  # type: ignore[union-attr]


def test_end_idempotency_and_late_chunks_preserved(tmp_path: Path) -> None:
    """Task 7.4: 重复结束返回成功且不丢弃尚未到达的块。"""
    _, _, dav, _, mgr, _ = make_test_env(tmp_path)
    cloud_path = f"RecNoteRemote/{SID_1}"
    _, tok = mgr.start_session(
        session_id=SID_1,
        cloud_path=cloud_path,
        title="结束先到",
        source_type="microphone",
    )

    # 结束通知先到，此时云盘尚无任何块
    r1 = mgr.end_session(tok)
    r2 = mgr.end_session(tok)
    assert r1.state == "draining"
    assert r2.state == "draining"
    assert r2.end_received is True

    # 稍后手机上传音频块与 final.json
    wav1, cnt1, sha1 = make_short_wav(8000)
    dav.seed_file(f"{cloud_path}/audio/000001.wav", wav1)
    _seed_manifest(dav, SID_1, 1, [(1, 0, cnt1, sha1)], is_final=True)

    mgr.step_session(SID_1)
    rec = mgr.get_session(SID_1)
    assert rec is not None
    assert rec.state == "completed"
    assert rec.chunks_completed == 1


def test_retry_six_actions_and_index_revision_monotonic(tmp_path: Path) -> None:
    """Task 7.5: 重试六项动作——已完成块不重下重识别、失败块被重置、索引版本不回退。"""
    asr_calls: list[str] = []

    def tracking_asr(audio_path: Path, _cfg) -> str:
        asr_calls.append(audio_path.name)
        if audio_path.name == "000002.wav" and asr_calls.count("000002.wav") == 1:
            raise RuntimeError("transient GPU OOM on chunk 2")
        return f"转写_{audio_path.stem}"

    cfg, _, dav, storage, _, _ = make_test_env(tmp_path)
    mgr = SessionManager(
        cfg,
        storage,
        transcribe_fn=tracking_asr,
        enable_background_workers=False,
    )
    cloud_path = f"RecNoteRemote/{SID_1}"
    _, tok = mgr.start_session(
        session_id=SID_1,
        cloud_path=cloud_path,
        title="重试测试",
        source_type="microphone",
    )

    wav1, cnt1, sha1 = make_short_wav(8000)
    wav2, cnt2, sha2 = make_short_wav(8000)
    dav.seed_file(f"{cloud_path}/audio/000001.wav", wav1)
    dav.seed_file(f"{cloud_path}/audio/000002.wav", wav2)
    _seed_manifest(
        dav,
        SID_1,
        1,
        [(1, 0, cnt1, sha1), (2, cnt1, cnt2, sha2)],
        is_final=False,
    )

    # 第一轮：块 1 成功，块 2 失败
    mgr.step_session(SID_1)
    rec = mgr.get_session(SID_1)
    assert rec is not None
    assert rec.chunks_completed == 1
    assert rec.chunks_failed == 1
    assert rec.state == "receiving"  # Task 7.9: 单块失败不把任务置为 failed
    rev_before_retry = rec.index_revision
    assert rev_before_retry >= 1
    assert asr_calls == ["000001.wav", "000002.wav"]

    # 触发重试：只重置块 2，块 1 不重下也不重跑 ASR
    _, reset_seqs = mgr.retry_session(tok)
    assert reset_seqs == [2]

    mgr.step_session(SID_1)
    assert asr_calls == ["000001.wav", "000002.wav", "000002.wav"]  # 000001.wav 未重复识别
    assert rec.chunks_completed == 2
    assert rec.chunks_failed == 0
    assert rec.index_revision > rev_before_retry


def test_cancel_cleanup_order_and_cloud_preserved(tmp_path: Path) -> None:
    """Task 7.6: 取消先写占位记录再删本地目录、不删云盘、重复取消仍返回成功、已完成任务取消同样清理本地。"""
    cfg, _, dav, _, mgr, _ = make_test_env(tmp_path)
    cloud_path = f"RecNoteRemote/{SID_1}"
    _, tok = mgr.start_session(
        session_id=SID_1,
        cloud_path=cloud_path,
        title="取消清理",
        source_type="microphone",
    )
    wav1, cnt1, sha1 = make_short_wav(8000)
    dav.seed_file(f"{cloud_path}/audio/000001.wav", wav1)
    _seed_manifest(dav, SID_1, 1, [(1, 0, cnt1, sha1)], is_final=True)
    mgr.end_session(tok)
    mgr.step_session(SID_1)

    rec = mgr.get_session(SID_1)
    assert rec is not None and rec.state == "completed"
    work_dir = mgr.session_work_dir(SID_1)
    assert work_dir.is_dir()

    # 对已完成任务执行取消 -> 本地目录被删除，状态变 cancelled，占位记录存在，云盘文件仍在
    mgr.cancel_session(tok)
    assert not work_dir.exists()
    state_file = cfg.session_state_path / f"{SID_1}.json"
    assert state_file.is_file()
    assert load_session_record(state_file).state == "cancelled"
    assert dav.read_seeded_file(f"{cloud_path}/audio/000001.wav") == wav1
    assert dav.read_seeded_file(f"{cloud_path}/.transcripts/000001.md") is not None

    # 重复取消仍返回 cancelled
    rec_repeat = mgr.cancel_session(tok)
    assert rec_repeat.state == "cancelled"


def test_startup_recovery_preserves_token_and_progress(tmp_path: Path) -> None:
    """Task 7.7: 启动恢复后任务 token 可继续使用、已完成块保留、索引版本不回退、继续等待尾块。"""
    asr_calls: list[str] = []

    def tracking_asr(audio_path: Path, _cfg) -> str:
        asr_calls.append(audio_path.name)
        return f"转写_{audio_path.stem}"

    cfg, _, dav, storage, _, _ = make_test_env(tmp_path)
    mgr1 = SessionManager(
        cfg,
        storage,
        transcribe_fn=tracking_asr,
        enable_background_workers=False,
    )
    cloud_path = f"RecNoteRemote/{SID_1}"
    _, tok_before = mgr1.start_session(
        session_id=SID_1,
        cloud_path=cloud_path,
        title="重启恢复",
        source_type="microphone",
    )

    wav1, cnt1, sha1 = make_short_wav(8000)
    wav2, cnt2, sha2 = make_short_wav(8000)
    dav.seed_file(f"{cloud_path}/audio/000001.wav", wav1)
    _seed_manifest(dav, SID_1, 1, [(1, 0, cnt1, sha1)], is_final=False)
    mgr1.step_session(SID_1)
    mgr1.end_session(tok_before)

    rec_before = mgr1.get_session(SID_1)
    assert rec_before is not None
    rev_before = rec_before.index_revision
    assert rec_before.chunks_completed == 1

    # 模拟进程重启：新建 SessionManager 并调用 recover_sessions()
    mgr2 = SessionManager(
        cfg,
        storage,
        transcribe_fn=tracking_asr,
        enable_background_workers=False,
    )
    recovered_count = mgr2.recover_sessions()
    assert recovered_count == 1

    # 原 token 仍可定位该任务
    rec_after = mgr2.lookup_session_by_token(tok_before)
    assert rec_after is not None
    assert rec_after.session_id == SID_1
    assert rec_after.state == "draining"
    assert rec_after.chunks_completed == 1
    assert rec_after.index_revision == rev_before

    # 尾块 2 与 final.json 到达后继续完成，且块 1 不重复识别
    dav.seed_file(f"{cloud_path}/audio/000002.wav", wav2)
    _seed_manifest(
        dav,
        SID_1,
        2,
        [(1, 0, cnt1, sha1), (2, cnt1, cnt2, sha2)],
        is_final=True,
    )
    mgr2.step_session(SID_1)
    assert rec_after.state == "completed"
    assert rec_after.index_revision > rev_before
    assert asr_calls == ["000001.wav", "000002.wav"]


def test_active_session_limit_and_global_asr_lock_serialization(
    tmp_path: Path,
) -> None:
    """Task 7.8 & 7.9: 活动任务上限拒绝新请求、两个任务同时识别时被全局锁串行、长时间无新块不自动失败。"""
    in_asr = 0
    max_concurrent_asr = 0
    lock = threading.Lock()

    def concurrent_probe_asr(audio_path: Path, _cfg) -> str:
        nonlocal in_asr, max_concurrent_asr
        with lock:
            in_asr += 1
            max_concurrent_asr = max(max_concurrent_asr, in_asr)
        time.sleep(0.05)
        with lock:
            in_asr -= 1
        return f"结果_{audio_path.stem}"

    cfg, _, dav, storage, _, _ = make_test_env(
        tmp_path, max_active_sessions=2
    )
    mgr = SessionManager(
        cfg,
        storage,
        transcribe_fn=concurrent_probe_asr,
        enable_background_workers=False,
    )

    mgr.start_session(
        session_id=SID_1,
        cloud_path=f"RecNoteRemote/{SID_1}",
        title="S1",
        source_type="microphone",
    )
    mgr.start_session(
        session_id=SID_2,
        cloud_path=f"RecNoteRemote/{SID_2}",
        title="S2",
        source_type="microphone",
    )
    with pytest.raises(ProtocolValidationError) as exc_info:
        mgr.start_session(
            session_id=SID_3,
            cloud_path=f"RecNoteRemote/{SID_3}",
            title="S3",
            source_type="microphone",
        )
    assert exc_info.value.error.code == "too_many_requests"
    assert exc_info.value.error.retryable is True

    # Task 7.9: 多轮空轮询（长时间无新块）不会自动把任务置为失败
    for _ in range(5):
        assert mgr.step_session(SID_1) is False
    assert mgr.get_session(SID_1).state == "waiting_source"  # type: ignore[union-attr]

    # 两个任务同时有待识别块，并发启动两个线程执行 step_session，验证识别严格串行
    wav1, cnt1, sha1 = make_short_wav(8000)
    for sid in (SID_1, SID_2):
        cp = f"RecNoteRemote/{sid}"
        dav.seed_file(f"{cp}/audio/000001.wav", wav1)
        _seed_manifest(dav, sid, 1, [(1, 0, cnt1, sha1)], is_final=False)

    t1 = threading.Thread(target=mgr.step_session, args=(SID_1,))
    t2 = threading.Thread(target=mgr.step_session, args=(SID_2,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert max_concurrent_asr == 1
    assert mgr.get_session(SID_1).chunks_completed == 1  # type: ignore[union-attr]
    assert mgr.get_session(SID_2).chunks_completed == 1  # type: ignore[union-attr]
