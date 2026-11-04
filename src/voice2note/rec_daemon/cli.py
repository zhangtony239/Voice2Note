"""`v2n rec-daemon` 命令行入口与子命令解析。"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path


def build_init_config_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="v2n rec-daemon init-config",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "从内置模板创建远程识别服务配置文件 rec-daemon.yaml（保留注释）并打印其绝对路径。\n"
            "若目标文件已存在，则只打印绝对路径而不覆盖现有文件。"
        ),
        epilog=(
            "示例：\n"
            "  v2n rec-daemon init-config\n"
            "  v2n rec-daemon init-config --config C:\\work\\rec-daemon.yaml"
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        metavar="PATH",
        help="配置文件路径（默认当前工作目录下的 rec-daemon.yaml）",
    )
    return parser


def build_token_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        prog="v2n rec-daemon token",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "生成新的 256 位连接 token（CONNECTION_TOKEN）、其 SHA-256 摘要（SERVER_AUTH_TOKEN_HASH）\n"
            "以及会话派生密钥（SESSION_TOKEN_SECRET）。只输出到终端，不修改任何文件。"
        ),
        epilog=(
            "用法说明：\n"
            "  1. 将输出的 CONNECTION_TOKEN 填入手机端 RecNote 远程连接设置；\n"
            "  2. 将输出的 SERVER_AUTH_TOKEN_HASH 与 SESSION_TOKEN_SECRET 填入电脑端 rec-daemon.yaml。"
        ),
    )


def build_rec_daemon_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="v2n rec-daemon",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Voice2Note 面向手机端 RecNote 的远程语音识别常驻服务。\n"
            "不带子命令时加载 rec-daemon.yaml，恢复未完成任务并在前台运行 HTTP 控制面与云盘轮询。\n"
            "本服务仅执行 ASR 识别并将原稿写回云盘，不调用大模型、不生成笔记。"
        ),
        epilog=(
            "快速上手流程：\n"
            "  1. 生成服务配置模板：  v2n rec-daemon init-config --config rec-daemon.yaml\n"
            "  2. 生成鉴权凭据与密钥：v2n rec-daemon token\n"
            "  3. 编辑 rec-daemon.yaml 填入凭据与本机 OpenList WebDAV 地址及账号密码\n"
            "  4. 在前台启动常驻服务：v2n rec-daemon --config rec-daemon.yaml\n"
            "\n"
            "命令行覆盖示例：\n"
            "  v2n rec-daemon --config C:\\work\\rec-daemon.yaml --host 127.0.0.1 --port 8765 --log-level DEBUG"
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        metavar="PATH",
        help="rec-daemon.yaml 配置文件路径（默认当前工作目录下的 rec-daemon.yaml）",
    )
    parser.add_argument(
        "--host",
        type=str,
        default=None,
        metavar="HOST",
        help="覆盖配置中的 SERVER_HOST（例如 127.0.0.1）",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        metavar="PORT",
        help="覆盖配置中的 SERVER_PORT（1-65535，例如 8765）",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default=None,
        metavar="LEVEL",
        help="覆盖配置中的 LOG_LEVEL（可选值：DEBUG | INFO | WARNING | ERROR）",
    )
    subparsers = parser.add_subparsers(
        title="subcommands",
        dest="subcommand",
        metavar="{init-config,token}",
    )
    init_p = subparsers.add_parser(
        "init-config",
        help="从模板创建 rec-daemon.yaml 并打印其绝对路径",
        description="从模板创建 rec-daemon.yaml 并打印其绝对路径。",
    )
    init_p.add_argument(
        "--config",
        type=Path,
        default=None,
        metavar="PATH",
        help="配置文件路径（默认当前工作目录下的 rec-daemon.yaml）",
    )
    subparsers.add_parser(
        "token",
        help="生成新的连接 token、其 SHA-256 摘要与会话密钥（只打印，不改文件）",
        description="生成新的连接 token、其 SHA-256 摘要与会话密钥（只打印，不修改文件）。",
    )
    return parser


def _cmd_init_config(config_path: Path | None) -> int:
    from voice2note.rec_daemon.config import (
        DaemonConfigError,
        create_daemon_config,
        daemon_config_path,
    )

    try:
        target = daemon_config_path(config_path)
        if not target.exists():
            create_daemon_config(config_path)
        print(f"当前服务配置文件路径：{target.resolve()}")
        return 0
    except (DaemonConfigError, OSError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


def _cmd_token() -> int:
    from voice2note.rec_daemon.auth import (
        format_generated_credentials,
        generate_credentials,
    )

    creds = generate_credentials()
    print(format_generated_credentials(creds))
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    from voice2note.rec_daemon.config import DaemonConfigError
    from voice2note.rec_daemon.server import run_daemon_server

    try:
        return run_daemon_server(
            config_file=args.config,
            host_override=args.host,
            port_override=args.port,
            log_level_override=args.log_level,
        )
    except (DaemonConfigError, OSError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


def run_rec_daemon_cli(argv: Sequence[str]) -> int:
    args_list = list(argv)
    if args_list and args_list[0] == "init-config":
        init_args = build_init_config_parser().parse_args(args_list[1:])
        return _cmd_init_config(init_args.config)
    if args_list and args_list[0] == "token":
        build_token_parser().parse_args(args_list[1:])
        return _cmd_token()

    parser = build_rec_daemon_parser()
    args = parser.parse_args(args_list)
    if args.subcommand == "init-config":
        return _cmd_init_config(args.config)
    if args.subcommand == "token":
        return _cmd_token()
    return _cmd_serve(args)
