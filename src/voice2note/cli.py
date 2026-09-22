"""v2n 命令行入口。

``v2n START-END ["TITLE"]`` 运行完整流水线：按文件名首个数字段的数值
范围选取音频文件，全部 ASR 完成后将各份 STT 原稿一次性发送给 LLM 生成
一篇笔记；``v2n config`` 管理配置文件。
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence

from voice2note.config import ConfigError, config_path, create_config


def _cmd_run(args: argparse.Namespace) -> int:
    """``v2n START-END ["TITLE"]``：两阶段批处理流水线。

    阶段一：对范围内每个音频文件 ASR 转写并保存 STT 原稿；
    阶段二：全部转写完成后，将各份原稿一次性发送给 LLM 生成一篇笔记。
    """
    # 延迟导入，避免轻量命令加载 torch/transformers
    from tqdm import tqdm

    from voice2note.asr import save_transcript, transcribe
    from voice2note.config import load_config, parse_range, select_voice_files
    from voice2note.llm import generate_note

    config = load_config()
    start, end = parse_range(args.range)
    audio_files = select_voice_files(config, start, end)
    if not audio_files:
        print(
            f"编号范围 [{start}, {end}] 内未发现匹配的音频文件"
            f"（VOICE_PATH={config.voice_path}, "
            f"VOICE_FILE_KEYWORD={config.voice_file_keyword.pattern!r}）"
        )
        return 0

    transcripts: list[tuple[str, str]] = []
    for audio in tqdm(audio_files, desc="转写音频", unit="file"):
        tqdm.write(f"转写: {audio}")

        audio_name = audio.name

        def seg_progress(total: int, _name: str = audio_name):
            return tqdm(
                total=total, desc=f"  语句转写 {_name}", unit="seg", leave=False
            )

        try:
            transcript = transcribe(audio, config, progress_factory=seg_progress)
            transcript_path = save_transcript(transcript, audio, config)
        except Exception as exc:  # noqa: BLE001 - 任一文件失败即中止，不调用 LLM
            print(f"错误: 转写 {audio} 失败: {exc}", file=sys.stderr)
            return 1
        tqdm.write(f"原稿: {transcript_path}")
        transcripts.append((transcript_path.name, transcript))

    tqdm.write("生成笔记...")
    note_path = generate_note(transcripts, config, user_prompt=args.title)
    tqdm.write(f"笔记: {note_path}")
    return 0


def _cmd_config(_args: argparse.Namespace) -> int:
    """``v2n config``：输出 config.yaml 绝对路径，必要时从模板创建。"""
    target = config_path()
    if not target.exists():
        create_config()
    print(f"当前配置文件路径：{target.resolve()}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="v2n",
        description=(
            "Voice2Note：从原始录音到笔记 markdown 的工作流。"
            '用法：v2n START-END ["TITLE"]。'
        ),
    )
    parser.add_argument(
        "range",
        metavar="START-END",
        help=(
            '音频文件编号范围（如 "141-142"、"1.2-2.4"）：'
            "按文件名首个数字段的数值选取闭区间内的文件"
        ),
    )
    parser.add_argument(
        "title",
        nargs="?",
        default="",
        help=(
            "笔记标题：决定笔记文件名，非空时随原稿发送给 LLM；"
            "为空时文件名取笔记正文首行"
        ),
    )
    return parser


def _dispatch(
    func: Callable[[argparse.Namespace], int], args: argparse.Namespace
) -> int:
    try:
        return func(args)
    except (ConfigError, OSError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


def main(argv: Sequence[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)

    # `v2n config` 子命令优先于 START-END 位置参数（手动分发，避免 argparse
    # 将 "config" 匹配到必填的 START-END 位置参数）
    if args_list and args_list[0] == "config":
        return _dispatch(_cmd_config, argparse.Namespace())

    parser = build_parser()
    args = parser.parse_args(args_list)
    return _dispatch(_cmd_run, args)


if __name__ == "__main__":
    raise SystemExit(main())
