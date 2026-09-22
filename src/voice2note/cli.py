"""v2n 命令行入口。

裸 ``v2n`` 运行完整流水线（音频 → ASR → STT 原稿 → LLM → 笔记）；
``v2n config`` 管理配置文件。
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence

from voice2note.config import ConfigError, config_path, create_config


def _cmd_run(_args: argparse.Namespace) -> int:
    """裸 ``v2n``：对发现的每个音频文件产出 STT 原稿与笔记。"""
    # 延迟导入，避免轻量命令加载 torch/transformers
    from tqdm import tqdm

    from voice2note.asr import save_transcript, transcribe
    from voice2note.config import discover_voice_files, load_config
    from voice2note.llm import generate_note

    config = load_config()
    audio_files = discover_voice_files(config)
    if not audio_files:
        print(
            f"未发现匹配的音频文件（VOICE_PATH={config.voice_path}, "
            f"VOICE_FILE_KEYWORD={config.voice_file_keyword.pattern!r}）"
        )
        return 0

    for audio in tqdm(audio_files, desc="处理音频", unit="file"):
        tqdm.write(f"转写: {audio}")
        transcript = transcribe(audio, config)
        transcript_path = save_transcript(transcript, audio, config)
        tqdm.write(f"原稿: {transcript_path}")

        tqdm.write("生成笔记...")
        note_path = generate_note(transcript, config)
        tqdm.write(f"笔记: {note_path}")
    return 0


def _cmd_config(_args: argparse.Namespace) -> int:
    """``v2n config``：输出 config.yaml 绝对路径，必要时从模板创建。"""
    target = config_path()
    if not target.exists():
        create_config()
    print(target.resolve())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="v2n",
        description="Voice2Note：从原始录音到笔记 markdown 的工作流。不带子命令时运行完整流水线。",
    )
    parser.set_defaults(func=_cmd_run)

    subparsers = parser.add_subparsers(dest="command", required=False)

    config_parser = subparsers.add_parser(
        "config",
        help="输出 config.yaml 的绝对路径；不存在时从模板创建",
    )
    config_parser.set_defaults(func=_cmd_config)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    func: Callable[[argparse.Namespace], int] = args.func
    try:
        return func(args)
    except (ConfigError, OSError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
