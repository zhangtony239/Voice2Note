"""测试云盘数据面（Tasks 8.1–8.9）与识别接入（Tasks 9.1–9.3）。"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from tests.audio_factory import (
    make_short_wav,
    make_silent_wav,
    make_tampered_header_wav,
    make_zero_sample_wav,
)
from tests.mock_webdav import FaultRule
from tests.test_http_control_plane import SID_1, make_test_env
from tests.test_session_lifecycle import _seed_manifest
from voice2note import asr
from voice2note.rec_daemon.logging_setup import configure_daemon_logger
from voice2note.rec_daemon.protocol import (
    MAX_INDEX_BYTES,
    MAX_MANIFEST_BYTES,
    ProtocolValidationError,
    parse_complete_bytes,
    parse_index_bytes,
    parse_manifest_bytes,
    validate_relative_cloud_path,
)
from voice2note.rec_daemon.session_manager import SessionManager
from voice2note.rec_daemon.storage import (
    StorageAccessError,
    StorageLimitExceededError,
    write_local_transcript_atomic,
)


def test_webdav_client_operations_and_redirect_rejected(tmp_path: Path) -> None:
    """Task 8.1: 云盘客户端读取、写入、创建目录、关闭重定向与超时。"""
    _, _, dav, storage, _, _ = make_test_env(tmp_path)

    storage.ensure_dir("RecNoteRemote/sub/dir")
    storage.write_bytes("RecNoteRemote/sub/dir/test.txt", b"hello")
    assert (
        storage.read_bytes("RecNoteRemote/sub/dir/test.txt", max_bytes=1024)
        == b"hello"
    )

    # 指向其他地址的 302 重定向不被跟随，抛出 StorageAccessError
    dav.set_fault(
        "RecNoteRemote/sub/dir/test.txt",
        FaultRule(kind="redirect", redirect_location="https://evil.example/x"),
    )
    with pytest.raises(StorageAccessError):
        storage.read_bytes("RecNoteRemote/sub/dir/test.txt", max_bytes=1024)


@pytest.mark.parametrize(
    "bad_path",
    [
        "",
        "/RecNoteRemote/file.wav",
        "RecNoteRemote/file.wav/",
        "RecNoteRemote/../evil.wav",
        "RecNoteRemote/./file.wav",
        "RecNoteRemote//file.wav",
        "RecNoteRemote\\audio\\000001.wav",
        "https://evil.example/000001.wav",
        "C:/Windows/System32/cmd.exe",
    ],
)
def test_relative_path_validation_rejects_bad_paths(bad_path: str) -> None:
    """Task 8.2: 越界路径、绝对地址、含上级目录、含反斜杠的路径全部被拒绝。"""
    with pytest.raises(ProtocolValidationError) as exc_info:
        validate_relative_cloud_path(bad_path)
    assert exc_info.value.error.code == "invalid_path"


def test_rolling_manifest_four_revision_dispositions(tmp_path: Path) -> None:
    """Task 8.3: 滚动清单版本处置（增大采用、相同同内容无操作、相同异内容忽略、更小忽略），均不把任务置为失败。"""
    _, _, dav, _, mgr, _ = make_test_env(tmp_path)
    cloud_path = f"RecNoteRemote/{SID_1}"
    mgr.start_session(
        session_id=SID_1,
        cloud_path=cloud_path,
        title="清单版本",
        source_type="microphone",
    )

    wav1, cnt1, sha1 = make_short_wav(8000)
    wav2, cnt2, sha2 = make_short_wav(8000)
    dav.seed_file(f"{cloud_path}/audio/000001.wav", wav1)
    dav.seed_file(f"{cloud_path}/audio/000002.wav", wav2)

    # 1. 版本增大（0 -> 3）：采用
    _seed_manifest(dav, SID_1, 3, [(1, 0, cnt1, sha1)])
    assert mgr.step_session(SID_1) is True
    rec = mgr.get_session(SID_1)
    assert rec is not None
    assert rec.manifest_revision == 3
    assert rec.chunks_completed == 1
    assert rec.state == "receiving"

    # 2. 相同版本且内容相同（revision=3）：无操作
    assert mgr.step_session(SID_1) is False
    assert rec.state == "receiving"

    # 3. 相同版本但内容不同（revision=3 含 2 个块）：忽略并保持上次状态，不失败
    _seed_manifest(
        dav, SID_1, 3, [(1, 0, cnt1, sha1), (2, cnt1, cnt2, sha2)]
    )
    assert mgr.step_session(SID_1) is False
    assert len(rec.chunks) == 1
    assert rec.state == "receiving"

    # 4. 版本更小（revision=2）：忽略并保持上次状态，不失败
    _seed_manifest(dav, SID_1, 2, [(1, 0, cnt1, sha1)])
    assert mgr.step_session(SID_1) is False
    assert rec.manifest_revision == 3
    assert rec.state == "receiving"


def test_final_manifest_transient_vs_semantic_and_immutable_once_adopted(
    tmp_path: Path,
) -> None:
    """Task 8.4: 最终清单解析失败继续等待、内容矛盾转为 failed 并保留已下载音频与原稿、已采用版本被改写时忽略。"""
    _, _, dav, _, mgr, _ = make_test_env(tmp_path)
    cloud_path = f"RecNoteRemote/{SID_1}"
    _, tok = mgr.start_session(
        session_id=SID_1,
        cloud_path=cloud_path,
        title="最终清单",
        source_type="microphone",
    )

    wav1, cnt1, sha1 = make_short_wav(8000)
    wav2, cnt2, sha2 = make_short_wav(8000)
    dav.seed_file(f"{cloud_path}/audio/000001.wav", wav1)
    _seed_manifest(dav, SID_1, 1, [(1, 0, cnt1, sha1)])
    mgr.step_session(SID_1)
    mgr.end_session(tok)

    rec = mgr.get_session(SID_1)
    assert rec is not None and rec.state == "draining"

    # 1. 半写 JSON（解析失败）：继续保持 draining 等待，不转为 failed
    dav.seed_file(f"{cloud_path}/final.json", b'{"protocol_version": 1, "trunc')
    mgr.step_session(SID_1)
    assert rec.state == "draining"

    # 2. 内容自相矛盾（块范围重叠/空洞）：转为 failed (manifest_invalid)，保留已下载音频与本地原稿
    _seed_manifest(
        dav,
        SID_1,
        2,
        [(1, 0, cnt1, sha1), (2, cnt1 + 500, cnt2, sha2)],  # start_sample 有 500 帧空洞
        is_final=True,
    )
    mgr.step_session(SID_1)
    assert rec.state == "failed"
    assert rec.error is not None and rec.error.code == "manifest_invalid"
    assert (mgr.session_audio_dir(SID_1) / "000001.wav").is_file()
    assert (mgr.session_transcripts_dir(SID_1) / "000001.md").is_file()

    # 3. 修复 final.json 并 retry -> 采用首个合法 final.json；之后再改写 final.json 则被忽略
    dav.seed_file(f"{cloud_path}/audio/000002.wav", wav2)
    valid_final = _seed_manifest(
        dav,
        SID_1,
        3,
        [(1, 0, cnt1, sha1), (2, cnt1, cnt2, sha2)],
        is_final=True,
    )
    mgr.retry_session(tok)
    mgr.step_session(SID_1)
    assert rec.final_manifest_adopted is True
    first_sha = rec.final_manifest_sha256
    assert first_sha != ""

    # 尝试改写 final.json，验证保持首个已采用版本
    dav.seed_file(f"{cloud_path}/final.json", valid_final + b"   \n")
    mgr._poll_final_manifest(rec)
    assert rec.final_manifest_sha256 == first_sha


def test_audio_chunk_six_step_validation_errors(tmp_path: Path) -> None:
    """Task 8.5: 流式下载与六步校验——体积超限、读不到、校验不符、头部字段不符、样本数为零各自返回对应错误码。"""
    _, _, dav, storage, _, _ = make_test_env(tmp_path)
    wav_ok, cnt_ok, sha_ok = make_short_wav(8000)

    # 1. 体积超限 -> audio_too_large (retryable=False)，且临时文件已清理
    dav.seed_file("RecNoteRemote/s/audio/000001.wav", wav_ok)
    dest1 = tmp_path / "dl" / "000001.wav"
    err1 = storage.download_and_validate_audio_chunk(
        rel_path="RecNoteRemote/s/audio/000001.wav",
        dest_file=dest1,
        expected_sha256=sha_ok,
        expected_sample_count=cnt_ok,
        max_chunk_bytes=100,  # 小于文件大小
    )
    assert err1 is not None and err1.code == "audio_too_large" and err1.retryable is False
    assert not dest1.exists()

    # 2. 读不到 -> audio_missing (retryable=True)
    err2 = storage.download_and_validate_audio_chunk(
        rel_path="RecNoteRemote/s/audio/999999.wav",
        dest_file=dest1,
        expected_sha256=sha_ok,
        expected_sample_count=cnt_ok,
        max_chunk_bytes=1000000,
    )
    assert err2 is not None and err2.code == "audio_missing" and err2.retryable is True

    # 3. 校验不符 -> audio_sha256_mismatch (retryable=True)
    err3 = storage.download_and_validate_audio_chunk(
        rel_path="RecNoteRemote/s/audio/000001.wav",
        dest_file=dest1,
        expected_sha256="0" * 64,
        expected_sample_count=cnt_ok,
        max_chunk_bytes=1000000,
    )
    assert (
        err3 is not None
        and err3.code == "audio_sha256_mismatch"
        and err3.retryable is True
    )
    assert not dest1.exists()

    # 4. 头部字段不符（例如采样率 8000 Hz）-> audio_format_invalid (retryable=False)
    wav_bad_sr, cnt_bad, sha_bad = make_tampered_header_wav(sample_rate=8000)
    dav.seed_file("RecNoteRemote/s/audio/000002.wav", wav_bad_sr)
    err4 = storage.download_and_validate_audio_chunk(
        rel_path="RecNoteRemote/s/audio/000002.wav",
        dest_file=dest1,
        expected_sha256=sha_bad,
        expected_sample_count=cnt_bad,
        max_chunk_bytes=1000000,
    )
    assert (
        err4 is not None
        and err4.code == "audio_format_invalid"
        and err4.retryable is False
    )
    assert not dest1.exists()

    # 5. 样本数为 0 -> audio_format_invalid (retryable=False)
    wav_zero, cnt_zero, sha_zero = make_zero_sample_wav()
    dav.seed_file("RecNoteRemote/s/audio/000003.wav", wav_zero)
    err5 = storage.download_and_validate_audio_chunk(
        rel_path="RecNoteRemote/s/audio/000003.wav",
        dest_file=dest1,
        expected_sha256=sha_zero,
        expected_sample_count=cnt_zero,
        max_chunk_bytes=1000000,
    )
    assert (
        err5 is not None
        and err5.code == "audio_format_invalid"
        and err5.retryable is False
    )


def test_transcript_generation_index_order_and_complete_marker(
    tmp_path: Path,
) -> None:
    """Task 8.6, 8.7, 8.8:
    - 空块生成 1 字节 `\\n` 文件（拒绝 0 字节写入的网盘也通过）
    - 写回失败记为可重试失败 transcript_upload_failed
    - 先写原稿后写索引、无变化不递增版本、失败块带可读原因
    - 完成标记发布前逐份回读原稿校验（回读失败不发 complete.json）
    - 零块任务发布空索引与完成标记
    """
    cfg, _, dav, storage, _, _ = make_test_env(tmp_path)
    dav.reject_zero_byte_put = True  # 验证空块 1 字节换行不会触发 0 字节拒绝

    # 空块写原稿 -> 1 字节 b"\n"
    mgr = SessionManager(
        cfg,
        storage,
        transcribe_fn=lambda _p, _c: "",  # 静音返回空串
        enable_background_workers=False,
    )
    cloud_path = f"RecNoteRemote/{SID_1}"
    _, tok = mgr.start_session(
        session_id=SID_1,
        cloud_path=cloud_path,
        title="原稿与索引",
        source_type="microphone",
    )

    wav_sil, cnt_sil, sha_sil = make_silent_wav(8000)
    dav.seed_file(f"{cloud_path}/audio/000001.wav", wav_sil)
    _seed_manifest(dav, SID_1, 1, [(1, 0, cnt_sil, sha_sil)], is_final=False)

    # 注入原稿上传失败（redirect -> StorageAccessError）
    md_rel = f"{cloud_path}/.transcripts/000001.md"
    dav.set_fault(md_rel, FaultRule(kind="redirect"))
    mgr.step_session(SID_1)
    rec = mgr.get_session(SID_1)
    assert rec is not None
    assert rec.chunks[1].status == "failed"
    assert rec.chunks[1].error is not None
    assert rec.chunks[1].error.code == "transcript_upload_failed"
    assert rec.chunks[1].error.retryable is True

    # 检查云盘 index.json 已记录该失败块及可读原因，且后续无变化轮询不重复写 index.json
    idx_raw_1 = dav.read_seeded_file(f"{cloud_path}/.transcripts/index.json")
    assert idx_raw_1 is not None
    idx_1 = parse_index_bytes(idx_raw_1)
    assert idx_1.revision == 1
    assert idx_1.chunks[0].status == "failed"
    assert idx_1.chunks[0].error is not None
    mgr.step_session(SID_1)
    assert rec.index_revision == 1  # 无变化不递增

    # 清除上传故障并 retry，但注入 silent_put_drop 验证回读校验失败时不发布 complete.json
    dav.set_fault(md_rel, FaultRule(kind="silent_put_drop"))
    _seed_manifest(dav, SID_1, 2, [(1, 0, cnt_sil, sha_sil)], is_final=True)
    mgr.end_session(tok)
    mgr.retry_session(tok)
    mgr.step_session(SID_1)
    # silent_put_drop 导致回读原稿得到 404，因此不发布 complete.json，状态保持 draining
    assert dav.read_seeded_file(f"{cloud_path}/.transcripts/complete.json") is None
    assert rec.state == "draining"

    # 清除故障，再次 step_session：先写 000001.md（1 字节 b"\n"），再写 index.json，最后写 complete.json
    dav.clear_fault(md_rel)
    dav.operation_log.clear()
    mgr.step_session(SID_1)
    assert rec.state == "completed"
    md_bytes = dav.read_seeded_file(md_rel)
    assert md_bytes == b"\n" and len(md_bytes) == 1

    put_paths = [path for (method, path) in dav.operation_log if method == "PUT"]
    assert md_rel in put_paths
    assert f"{cloud_path}/.transcripts/complete.json" in put_paths
    assert put_paths.index(md_rel) < put_paths.index(
        f"{cloud_path}/.transcripts/complete.json"
    )

    # 验证零块录音任务：发布空索引与空完成标记
    sid_zero = "11111111-2222-4333-8444-555555555555"
    cp_zero = f"RecNoteRemote/{sid_zero}"
    _, tok_z = mgr.start_session(
        session_id=sid_zero,
        cloud_path=cp_zero,
        title="零块任务",
        source_type="microphone",
    )
    _seed_manifest(dav, sid_zero, 1, [], is_final=True)
    mgr.end_session(tok_z)
    mgr.step_session(sid_zero)
    rec_z = mgr.get_session(sid_zero)
    assert rec_z is not None and rec_z.state == "completed"
    idx_z = parse_index_bytes(
        dav.read_seeded_file(f"{cp_zero}/.transcripts/index.json") or b""
    )
    comp_z = parse_complete_bytes(
        dav.read_seeded_file(f"{cp_zero}/.transcripts/complete.json") or b""
    )
    assert len(idx_z.chunks) == 0
    assert len(comp_z.chunks) == 0


def test_data_plane_size_limits_2mib_and_128kib(tmp_path: Path) -> None:
    """Task 8.9: 清单与索引超过 2 MiB、单份原稿超过 128 KiB 时返回明确错误并停止读入。"""
    _, _, dav, storage, _, _ = make_test_env(tmp_path)

    big_manifest = b"x" * (MAX_MANIFEST_BYTES + 10)
    with pytest.raises(ProtocolValidationError) as exc_m:
        parse_manifest_bytes(
            big_manifest,
            expected_session_id=SID_1,
            cloud_path=f"RecNoteRemote/{SID_1}",
        )
    assert exc_m.value.error.code == "payload_too_large"

    big_index = b"x" * (MAX_INDEX_BYTES + 10)
    with pytest.raises(ProtocolValidationError) as exc_i:
        parse_index_bytes(big_index)
    assert exc_i.value.error.code == "payload_too_large"

    # 流式读取超过 2 MiB 时立即中止并抛 StorageLimitExceededError
    dav.seed_file("RecNoteRemote/huge.json", big_manifest)
    with pytest.raises(StorageLimitExceededError):
        storage.read_bytes("RecNoteRemote/huge.json", max_bytes=MAX_MANIFEST_BYTES)

    # 单份原稿超过 128 KiB 抛 StorageLimitExceededError
    with pytest.raises(StorageLimitExceededError):
        write_local_transcript_atomic(
            tmp_path / "too_big.md", "中" * 70000
        )


def test_asr_settings_protocol_and_logging_without_transcript_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 9.1, 9.2, 9.3:
    - asr.transcribe 接受只含三个字段的设置对象，模型单例只加载一次
    - 识别前后各写一条日志，且日志中不含原稿正文
    """
    load_count = 0

    class DummyProcessor:
        def apply_transcription_request(self, **_kwargs):
            class _Inputs:
                class _Ids:
                    shape = (1, 4)

                input_ids = _Ids()

                def to(self, _backend):
                    return self

                def keys(self):
                    return ["input_ids"]

                def __getitem__(self, k):
                    return getattr(self, k)

            return _Inputs()

        def batch_decode(self, _tokens, skip_special_tokens=True):
            assert skip_special_tokens is True
            return ["机密原稿正文_绝不能出现在日志中_XYZ987"]

    class DummyModel:
        def generate(self, **_kwargs):
            import numpy as np

            return np.zeros((1, 8), dtype="int64")

    monkeypatch.setattr(asr, "_model", None)
    monkeypatch.setattr(asr, "_processor", None)

    def fake_load(settings: asr.AsrSettings):
        nonlocal load_count
        if asr._model is None:
            load_count += 1
            asr._processor = DummyProcessor()
            asr._model = DummyModel()
        assert settings.torch_backend in {"xpu", "cuda"}
        assert isinstance(settings.disable_mmap, bool)
        assert isinstance(settings.asr_language, str)
        return asr._processor, asr._model

    monkeypatch.setattr(asr, "_load", fake_load)

    cfg, _, dav, storage, _, _ = make_test_env(tmp_path)
    log_buf = io.StringIO()
    logger, _ = configure_daemon_logger("DEBUG", stream=log_buf)

    mgr = SessionManager(
        cfg,
        storage,
        transcribe_fn=lambda p, c: asr.transcribe(p, c),
        logger=logger,
        enable_background_workers=False,
    )
    cloud_path = f"RecNoteRemote/{SID_1}"
    _, tok = mgr.start_session(
        session_id=SID_1,
        cloud_path=cloud_path,
        title="ASR接入测试",
        source_type="microphone",
    )

    wav1, cnt1, sha1 = make_short_wav(16000)
    wav2, cnt2, sha2 = make_short_wav(16000)
    dav.seed_file(f"{cloud_path}/audio/000001.wav", wav1)
    dav.seed_file(f"{cloud_path}/audio/000002.wav", wav2)
    _seed_manifest(
        dav,
        SID_1,
        1,
        [(1, 0, cnt1, sha1), (2, cnt1, cnt2, sha2)],
        is_final=True,
    )
    mgr.end_session(tok)
    mgr.step_session(SID_1)

    rec = mgr.get_session(SID_1)
    assert rec is not None and rec.state == "completed"
    # 模型单例只加载一次
    assert load_count == 1
    assert asr.is_model_loaded() is True

    log_text = log_buf.getvalue()
    assert "开始识别音频块 seq=1" in log_text
    assert "完成识别音频块 seq=1" in log_text
    assert "机密原稿正文_绝不能出现在日志中_XYZ987" not in log_text
