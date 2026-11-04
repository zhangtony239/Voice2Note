"""端到端流程与无 LLM 请求/无笔记产出验收测试（Tasks 10.1, 10.2, 10.3, 10.5）。"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx

from tests.audio_factory import make_short_wav
from tests.mock_webdav import FaultRule
from tests.test_http_control_plane import (
    SID_1,
    SID_2,
    SyncASGITransport,
    make_test_env,
)
from tests.test_session_lifecycle import _seed_manifest
from voice2note.rec_daemon.protocol import (
    parse_complete_bytes,
    parse_index_bytes,
    sha256_hex,
)
from voice2note.rec_daemon.session_manager import SessionManager


def test_e2e_three_chunks_flow_and_no_llm_activity(tmp_path: Path) -> None:
    """Task 10.1 & 10.5:
    - 建立任务、上传三块、识别、回写、结束、完成标记，任务状态依次推进 waiting_source -> receiving -> draining -> completed
    - 云盘文件齐全（000001.md, 000002.md, 000003.md, index.json, complete.json）
    - 全程未导入 voice2note.llm、未发出任何 LLM 请求、未生成任何笔记文件、未写笔记输出目录
    """
    sys.modules.pop("voice2note.llm", None)

    _cfg, conn_token, dav, _storage, mgr, app = make_test_env(tmp_path)
    cloud_path = f"RecNoteRemote/{SID_1}"

    with httpx.Client(
        transport=SyncASGITransport(app), base_url="http://testserver"
    ) as client:
        # 1. 建立任务 -> waiting_source
        r_start = client.post(
            "/api/v1/sessions/start",
            json={
                "protocol_version": 1,
                "token": conn_token,
                "session_id": SID_1,
                "cloud_path": cloud_path,
                "title": "三块端到端课程",
                "source_type": "microphone",
            },
        )
        assert r_start.status_code == 200
        tok = r_start.json()["session_token"]
        assert r_start.json()["state"] == "waiting_source"

        # 2. 上传第 1、2 块并写 manifest.json -> 状态推进到 receiving
        w1, c1, s1 = make_short_wav(8000)
        w2, c2, s2 = make_short_wav(8000)
        w3, c3, s3 = make_short_wav(8000)
        dav.seed_file(f"{cloud_path}/audio/000001.wav", w1)
        dav.seed_file(f"{cloud_path}/audio/000002.wav", w2)
        _seed_manifest(
            dav,
            SID_1,
            1,
            [(1, 0, c1, s1), (2, c1, c2, s2)],
            is_final=False,
        )

        mgr.step_session(SID_1)
        r_q1 = client.get(
            f"/api/v1/sessions/{SID_1}",
            headers={"Authorization": f"Bearer {tok}"},
        )
        assert r_q1.json()["state"] == "receiving"
        assert r_q1.json()["chunks_completed"] == 2

        # 3. 收到结束通知 -> 状态推进到 draining
        r_end = client.post("/api/v1/sessions/end", json={"token": tok})
        assert r_end.json()["state"] == "draining"

        # 4. 上传第 3 块与 final.json -> 状态推进到 completed，云盘文件齐全
        dav.seed_file(f"{cloud_path}/audio/000003.wav", w3)
        final_bytes = _seed_manifest(
            dav,
            SID_1,
            2,
            [(1, 0, c1, s1), (2, c1, c2, s2), (3, c1 + c2, c3, s3)],
            is_final=True,
        )
        mgr.step_session(SID_1)

        r_q2 = client.get(
            f"/api/v1/sessions/{SID_1}",
            headers={"Authorization": f"Bearer {tok}"},
        )
        assert r_q2.json()["state"] == "completed"
        assert r_q2.json()["chunks_total"] == 3
        assert r_q2.json()["chunks_completed"] == 3
        assert r_q2.json()["chunks_failed"] == 0

    # 验证云盘文件齐全且校验一致
    for seq in (1, 2, 3):
        md = dav.read_seeded_file(f"{cloud_path}/.transcripts/{seq:06d}.md")
        assert md == "固定转写文本\n".encode()

    idx = parse_index_bytes(
        dav.read_seeded_file(f"{cloud_path}/.transcripts/index.json") or b"",
        cloud_path=cloud_path,
    )
    comp = parse_complete_bytes(
        dav.read_seeded_file(f"{cloud_path}/.transcripts/complete.json") or b"",
        cloud_path=cloud_path,
    )
    assert len(idx.chunks) == 3
    assert len(comp.chunks) == 3
    assert comp.final_manifest_sha256 == sha256_hex(final_bytes)

    # Task 10.5: 确认没有任何大模型请求、未导入 voice2note.llm、未生成笔记文件或 outputs 目录
    assert "voice2note.llm" not in sys.modules
    assert not (tmp_path / "outputs").exists()
    for method, rel in dav.operation_log:
        assert rel in {"", "RecNoteRemote"} or rel.startswith("RecNoteRemote/")
        assert method in {"MKCOL", "PUT", "GET", "HEAD", "PROPFIND"}


def test_e2e_abnormal_flows_and_service_restart(tmp_path: Path) -> None:
    """Task 10.2 & 10.3:
    模拟网盘服务跑异常流程（结束先到、尾块迟到、回写失败后重试、服务停止后重启恢复接上原任务），验证每种情况均恢复并最终完成。
    """
    cfg, conn_token, dav, storage, mgr1, app1 = make_test_env(tmp_path)
    cloud_path = f"RecNoteRemote/{SID_2}"

    with httpx.Client(
        transport=SyncASGITransport(app1), base_url="http://testserver"
    ) as client1:
        r_start = client1.post(
            "/api/v1/sessions/start",
            json={
                "protocol_version": 1,
                "token": conn_token,
                "session_id": SID_2,
                "cloud_path": cloud_path,
                "title": "异常恢复课程",
                "source_type": "microphone",
            },
        )
        tok = r_start.json()["session_token"]

        # 1. 结束通知先到，此时云盘尚无任何清单或音频块
        client1.post("/api/v1/sessions/end", json={"token": tok})
        mgr1.step_session(SID_2)
        assert mgr1.get_session(SID_2).state == "draining"  # type: ignore[union-attr]

        # 2. 块 1 到达，尾块 2 迟到（设置 delayed_visible=1），块 1 原稿上传设置一次失败
        w1, c1, s1 = make_short_wav(8000)
        w2, c2, s2 = make_short_wav(8000)
        dav.seed_file(f"{cloud_path}/audio/000001.wav", w1)
        dav.seed_file(f"{cloud_path}/audio/000002.wav", w2)
        dav.set_fault(
            f"{cloud_path}/audio/000002.wav",
            FaultRule(kind="delayed_visible", remaining_404s=1),
        )
        dav.set_fault(
            f"{cloud_path}/.transcripts/000001.md",
            FaultRule(kind="redirect"),
        )
        _seed_manifest(
            dav,
            SID_2,
            1,
            [(1, 0, c1, s1), (2, c1, c2, s2)],
            is_final=True,
        )

        mgr1.step_session(SID_2)
        rec1 = mgr1.get_session(SID_2)
        assert rec1 is not None
        assert rec1.state == "draining"
        assert rec1.chunks_failed == 2  # 块1上传失败，块2迟到404

    # 3. 模拟 NSSM 服务停止并重启：新建 SessionManager 并恢复状态
    dav.clear_fault(f"{cloud_path}/.transcripts/000001.md")
    mgr2 = SessionManager(
        cfg,
        storage,
        transcribe_fn=lambda _p, _c: "恢复后转写",
        enable_background_workers=False,
    )
    assert mgr2.recover_sessions() == 1

    # 4. 触发 retry 并再次执行轮询，验证最终完成
    mgr2.retry_session(tok)
    mgr2.step_session(SID_2)
    rec2 = mgr2.get_session(SID_2)
    assert rec2 is not None
    assert rec2.state == "completed"
    assert rec2.chunks_completed == 2
    assert rec2.chunks_failed == 0
    assert (
        dav.read_seeded_file(f"{cloud_path}/.transcripts/complete.json")
        is not None
    )
