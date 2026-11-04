"""`v2n rec-daemon` 命令行入口与子命令解析。"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path


def build_init_config_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="v2n rec-daemon init-config",
        description="从模板创建 rec-daemon.yaml 并打印其绝对路径。",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        metavar="PATH",
        help="配置文件路径（默认当前目录下的 rec-daemon.yaml）",
    )
    return parser


def build_token_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        prog="v2n rec-daemon token",
        description="生成新的连接 token、其 SHA-256 摘要与会话密钥（只打印，不修改文件）。",
    )


def build_rec_daemon_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="v2n rec-daemon",
        description=(
            "Voice2Note 远程识别常驻服务。子命令：init-config、token；"
            "不带子命令时在前台启动 HTTP 控制面与任务轮询。"
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        metavar="PATH",
        help="rec-daemon.yaml 配置文件路径（默认当前目录下的 rec-daemon.yaml）",
    )
    parser.add_argument(
        "--host",
        type=str,
        default=None,
        metavar="HOST",
        help="覆盖配置中的 SERVER_HOST",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        metavar="PORT",
        help="覆盖配置中的 SERVER_PORT",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default=None,
        metavar="LEVEL",
        help="覆盖配置中的 LOG_LEVEL（DEBUG/INFO/WARNING/ERROR）",
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
        help="配置文件路径（默认当前目录下的 rec-daemon.yaml）",
    )
    subparsers.add_parser(
        "token",
        help="生成新的连接 token、其摘要与会话密钥",
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
