"""回归测试：验证现有批处理 `v2n START-END [TITLE]` 与 `v2n config` 行为不因 `rec-daemon` 改动而变化。"""

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
    rc = cli.main(["config"])
    assert rc == 0
    out = capsys.readouterr().out
    expected = tmp_path / CONFIG_FILE_NAME
    assert expected.is_file()
    assert str(expected.resolve()) in out
    assert not (tmp_path / "rec-daemon.yaml").exists()


def test_rec_daemon_help_subcommands(capsys: pytest.CaptureFixture[str]) -> None:
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
