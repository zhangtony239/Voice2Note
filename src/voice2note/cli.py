"""v2n 命令行入口。"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence

from voice2note.config import ConfigError, config_path, create_config


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
        description="Voice2Note：从原始录音到笔记 markdown 的工作流。",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

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
