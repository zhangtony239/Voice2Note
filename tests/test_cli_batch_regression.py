"""回归测试：验证现有批处理 `v2n START-END [TITLE]`、`v2n config` 与各级 `--help` 行为。"""

from __future__ import annotations

from pathlib import Path

import pytest

from voice2note import cli
from voice2note.config import CONFIG_FILE_NAME, parse_range


def test_batch_range_parsing_and_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[tuple[str, str]] = []

    def fake_cmd_run(args) -> int:
        captured.append((args.range, args.title))
        return 0

    monkeypatch.setattr(cli, "_cmd_run", fake_cmd_run)
    rc = cli.main(["141-142", "标题"])
    assert rc == 0
    assert captured == [("141-142", "标题")]
    assert parse_range("141-142") == (141.0, 142.0)


def test_batch_range_without_title(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[tuple[str, str]] = []

    def fake_cmd_run(args) -> int:
        captured.append((args.range, args.title))
        return 0

    monkeypatch.setattr(cli, "_cmd_run", fake_cmd_run)
    rc = cli.main(["1.2-2.4"])
    assert rc == 0
    assert captured == [("1.2-2.4", "")]


def test_v2n_config_only_touches_config_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    # `v2n config --help` 只打印帮助，不创建文件
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["config", "--help"])
    assert exc_info.value.code == 0
    help_out = capsys.readouterr().out
    assert "config.yaml" in help_out
    assert not (tmp_path / CONFIG_FILE_NAME).exists()

    # `v2n config` 创建并打印 config.yaml，不触碰 rec-daemon.yaml
    rc = cli.main(["config"])
    assert rc == 0
    out = capsys.readouterr().out
    expected = tmp_path / CONFIG_FILE_NAME
    assert expected.is_file()
    assert str(expected.resolve()) in out
    assert not (tmp_path / "rec-daemon.yaml").exists()


def test_all_cli_help_outputs(capsys: pytest.CaptureFixture[str]) -> None:
    # 顶层 `v2n --help` 包含批处理、config 与 rec-daemon 的完整引导
    with pytest.raises(SystemExit) as exc_top:
        cli.main(["--help"])
    assert exc_top.value.code == 0
    top_out = capsys.readouterr().out
    assert "START-END" in top_out
    assert "v2n config" in top_out
    assert "v2n rec-daemon" in top_out
    assert "init-config" in top_out
    assert "token" in top_out

    for argv in (
        ["rec-daemon", "--help"],
        ["rec-daemon", "init-config", "--help"],
        ["rec-daemon", "token", "--help"],
    ):
        with pytest.raises(SystemExit) as exc_info:
            cli.main(argv)
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert "usage:" in out
