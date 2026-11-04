"""测试基础设施自检：本地模拟网盘服务、8 种故障注入、合成音频生成器、protocol_v1 样例校验。"""

from __future__ import annotations

import json
import wave
from pathlib import Path

import httpx
import pytest

from tests.audio_factory import (
    make_short_wav,
    make_silent_wav,
    make_standard_30s_wav,
    make_tampered_header_wav,
    make_zero_sample_wav,
    parse_wav_with_stdlib,
)
from tests.mock_webdav import FaultRule, MockWebDAVServer
from voice2note.rec_daemon.protocol import (
    parse_complete_bytes,
    parse_index_bytes,
    parse_manifest_bytes,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "protocol_v1"
SAMPLE_SESSION_ID = "3f2b8c1e-9a4d-4f7b-8e21-5c6d7a9b0e13"
SAMPLE_CLOUD_PATH = f"RecNoteRemote/{SAMPLE_SESSION_ID}"


def test_mock_webdav_crud() -> None:
    """Task 3.1: 创建目录、写入、读取、删除、探测存在。"""
    srv = MockWebDAVServer()
    client = httpx.Client(transport=srv.transport, base_url="http://localhost/dav")

    # 创建目录
    assert client.request("MKCOL", "/dav/RecNoteRemote").status_code == 201
    assert client.request("MKCOL", "/dav/RecNoteRemote/sub").status_code == 201
    # 已存在返回 405
    assert client.request("MKCOL", "/dav/RecNoteRemote/sub").status_code == 405

    # 写入与读取
    r_put = client.put("/dav/RecNoteRemote/sub/hello.txt", content=b"world")
    assert r_put.status_code == 201
    r_get = client.get("/dav/RecNoteRemote/sub/hello.txt")
    assert r_get.status_code == 200
    assert r_get.content == b"world"

    # 探测存在（HEAD / PROPFIND）
    assert client.head("/dav/RecNoteRemote/sub/hello.txt").status_code == 200
    assert client.request("PROPFIND", "/dav/RecNoteRemote/sub/hello.txt").status_code == 207
    assert client.head("/dav/RecNoteRemote/sub/missing.txt").status_code == 404

    # 删除
    assert client.delete("/dav/RecNoteRemote/sub/hello.txt").status_code == 204
    assert client.get("/dav/RecNoteRemote/sub/hello.txt").status_code == 404


def test_mock_webdav_fault_injections() -> None:
    """Task 3.2: 八种故障注入各自触发并验证预期行为。"""
    srv = MockWebDAVServer()
    client = httpx.Client(
        transport=srv.transport, base_url="http://localhost/dav", follow_redirects=False
    )
    srv.seed_file("RecNoteRemote/data.json", b'{"protocol_version": 1, "ok": true}')

    # 1. 文件尚未可见
    srv.set_fault("RecNoteRemote/data.json", FaultRule(kind="not_visible"))
    assert client.get("/dav/RecNoteRemote/data.json").status_code == 404

    # 2. 半写 JSON
    srv.set_fault(
        "RecNoteRemote/data.json", FaultRule(kind="partial_json", partial_cutoff=8)
    )
    r_partial = client.get("/dav/RecNoteRemote/data.json")
    assert r_partial.status_code == 200
    with pytest.raises(json.JSONDecodeError):
        json.loads(r_partial.content)

    # 3. 延迟可见
    srv.set_fault(
        "RecNoteRemote/data.json",
        FaultRule(kind="delayed_visible", remaining_404s=2),
    )
    assert client.get("/dav/RecNoteRemote/data.json").status_code == 404
    assert client.get("/dav/RecNoteRemote/data.json").status_code == 404
    r_visible = client.get("/dav/RecNoteRemote/data.json")
    assert r_visible.status_code == 200
    assert json.loads(r_visible.content)["ok"] is True

    # 4. 内容被篡改
    srv.set_fault(
        "RecNoteRemote/data.json",
        FaultRule(kind="tampered_content", tampered_bytes=b"corrupted-bytes"),
    )
    assert client.get("/dav/RecNoteRemote/data.json").content == b"corrupted-bytes"

    # 5. 拒绝 0 字节写入
    srv.set_fault("RecNoteRemote/empty.md", FaultRule(kind="reject_zero_byte_put"))
    assert client.put("/dav/RecNoteRemote/empty.md", content=b"").status_code == 400
    assert client.put("/dav/RecNoteRemote/empty.md", content=b"\n").status_code == 201

    # 6. 慢响应（超时）
    srv.set_fault(
        "RecNoteRemote/slow.json",
        FaultRule(kind="slow_response", raise_timeout=True),
    )
    with pytest.raises(httpx.ReadTimeout):
        client.get("/dav/RecNoteRemote/slow.json")

    # 7. 重定向
    srv.set_fault(
        "RecNoteRemote/redir.json",
        FaultRule(kind="redirect", redirect_location="https://evil.example/payload"),
    )
    r_redir = client.get("/dav/RecNoteRemote/redir.json")
    assert r_redir.status_code == 302
    assert r_redir.headers["Location"] == "https://evil.example/payload"

    # 8. 写入返回成功但不落盘
    srv.set_fault("RecNoteRemote/ghost.md", FaultRule(kind="silent_put_drop"))
    r_ghost_put = client.put("/dav/RecNoteRemote/ghost.md", content=b"hello\n")
    assert r_ghost_put.status_code == 201
    assert client.get("/dav/RecNoteRemote/ghost.md").status_code == 404


def test_audio_factory_variants() -> None:
    """Task 3.3: 标准 30 秒块、短块、纯静音块、样本数为零块、头部被篡改块。"""
    # 标准 30 秒块
    wav_30s, count_30s, sha_30s = make_standard_30s_wav()
    assert count_30s == 480000
    assert len(sha_30s) == 64
    info_30s = parse_wav_with_stdlib(wav_30s)
    assert info_30s == {
        "channels": 1,
        "sample_width": 2,
        "sample_rate": 16000,
        "nframes": 480000,
    }

    # 短块
    wav_short, count_short, _ = make_short_wav(8000)
    assert count_short == 8000
    assert parse_wav_with_stdlib(wav_short)["nframes"] == 8000

    # 纯静音块
    wav_silent, count_silent, _ = make_silent_wav(16000)
    assert count_silent == 16000
    assert parse_wav_with_stdlib(wav_silent)["nframes"] == 16000

    # 样本数为 0 的非法块
    wav_zero, count_zero, _ = make_zero_sample_wav()
    assert count_zero == 0
    assert parse_wav_with_stdlib(wav_zero)["nframes"] == 0

    # 头部字段被篡改的块（采样率 8000、双声道、8位）
    wav_bad_sr, _, _ = make_tampered_header_wav(sample_rate=8000)
    assert parse_wav_with_stdlib(wav_bad_sr)["sample_rate"] == 8000

    wav_bad_ch, _, _ = make_tampered_header_wav(channels=2)
    assert parse_wav_with_stdlib(wav_bad_ch)["channels"] == 2

    wav_bad_bits, _, _ = make_tampered_header_wav(bits_per_sample=8)
    assert parse_wav_with_stdlib(wav_bad_bits)["sample_width"] == 1

    # 声明帧数与实际数据长度不一致
    wav_bad_len, _, _ = make_tampered_header_wav(
        sample_count=100, declared_data_bytes=400
    )
    with wave.open(
        __import__("io").BytesIO(wav_bad_len), "rb"
    ) as wf:
        assert wf.getnframes() == 200
        assert len(wf.readframes(200)) == 200  # 实际只有 100 帧（200 字节）


def test_protocol_v1_fixtures_and_field_sets() -> None:
    """Task 3.4: 校验 tests/fixtures/protocol_v1/ 下四份样例的解析与字段集合。"""
    manifest_raw = (FIXTURE_DIR / "manifest.json").read_bytes()
    final_raw = (FIXTURE_DIR / "final.json").read_bytes()
    index_raw = (FIXTURE_DIR / "index.json").read_bytes()
    complete_raw = (FIXTURE_DIR / "complete.json").read_bytes()

    manifest_json = json.loads(manifest_raw)
    assert set(manifest_json.keys()) == {
        "protocol_version",
        "session_id",
        "revision",
        "sample_rate",
        "channels",
        "encoding",
        "chunks",
    }
    assert set(manifest_json["chunks"][0].keys()) == {
        "sequence",
        "path",
        "start_sample",
        "sample_count",
        "sha256",
    }
    parsed_manifest = parse_manifest_bytes(
        manifest_raw,
        expected_session_id=SAMPLE_SESSION_ID,
        cloud_path=SAMPLE_CLOUD_PATH,
        is_final=False,
    )
    assert parsed_manifest.revision == 3
    assert len(parsed_manifest.chunks) == 1

    final_json = json.loads(final_raw)
    assert set(final_json.keys()) == {
        "protocol_version",
        "session_id",
        "revision",
        "sample_rate",
        "channels",
        "encoding",
        "total_samples",
        "chunk_count",
        "chunks",
    }
    parsed_final = parse_manifest_bytes(
        final_raw,
        expected_session_id=SAMPLE_SESSION_ID,
        cloud_path=SAMPLE_CLOUD_PATH,
        is_final=True,
    )
    assert parsed_final.total_samples == 480000
    assert parsed_final.chunk_count == 1

    index_json = json.loads(index_raw)
    assert set(index_json.keys()) == {
        "protocol_version",
        "session_id",
        "revision",
        "chunks",
    }
    assert set(index_json["chunks"][0].keys()) == {
        "sequence",
        "audio_sha256",
        "status",
        "path",
        "sha256",
        "error",
    }
    parsed_index = parse_index_bytes(index_raw, cloud_path=SAMPLE_CLOUD_PATH)
    assert parsed_index.revision == 7
    assert parsed_index.chunks[0].status == "completed"

    complete_json = json.loads(complete_raw)
    assert set(complete_json.keys()) == {
        "protocol_version",
        "session_id",
        "final_manifest_sha256",
        "chunks",
    }
    assert set(complete_json["chunks"][0].keys()) == {
        "sequence",
        "audio_sha256",
        "path",
        "sha256",
    }
    parsed_complete = parse_complete_bytes(complete_raw, cloud_path=SAMPLE_CLOUD_PATH)
    assert len(parsed_complete.chunks) == 1
