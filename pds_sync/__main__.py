"""Command-line entry point for the standalone PDS utility."""

from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path
import sys
from typing import Any, Sequence

from .config import ConfigError, Settings, load_settings
from .core import (
    PdsCli,
    PdsError,
    download_item,
    install_aliyun_cli,
    inventory_all,
    list_drives,
    resolve_aliyun_cli,
    safe_user,
    verify_versions,
    write_json,
    write_snapshot,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _settings(config_path: str | None) -> Settings:
    return load_settings(
        PROJECT_ROOT,
        Path(config_path) if config_path else None,
    )


def _cli(settings: Settings) -> PdsCli:
    binary = resolve_aliyun_cli(settings)
    if binary is None:
        raise ConfigError("Aliyun CLI not found; run './pds-sync setup' first")
    return PdsCli(
        binary,
        timeout_seconds=settings.timeout_seconds,
        read_retries=settings.read_retries,
    )


def _human_bytes(value: Any) -> str:
    if not isinstance(value, int) or value < 0:
        return "-"
    amount = float(value)
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value} B"


def _print_identity(user: dict[str, Any]) -> None:
    print(
        "认证用户: "
        f"{user.get('nick_name') or '-'}  "
        f"domain={user.get('domain_id') or '-'}  "
        f"user={user.get('user_id') or '-'}"
    )


def _print_drives(drives: Sequence[dict[str, Any]]) -> None:
    print(f"可访问空间: {len(drives)}")
    print(f"{'类型':<12} {'Drive ID':<16} {'名称':<28} {'已用 / 总量'}")
    for drive in drives:
        print(
            f"{str(drive.get('space_type') or '-'):<12} "
            f"{str(drive.get('drive_id') or '-'):<16} "
            f"{str(drive.get('drive_name') or '-')[:26]:<28} "
            f"{_human_bytes(drive.get('used_size'))} / "
            f"{_human_bytes(drive.get('total_size'))}"
        )


def _write_domain_config(domain_id: str) -> None:
    target = PROJECT_ROOT / "config.toml"
    if target.exists():
        return
    content = "\n".join(
        [
            f'domain_id = "{domain_id}"',
            'spaces = ["personal", "team", "enterprise"]',
            "page_size = 100",
            'snapshot_dir = "snapshots"',
            'download_dir = "downloads"',
            "timeout_seconds = 60",
            "read_retries = 2",
            "",
        ]
    )
    target.write_text(content, encoding="utf-8")
    target.chmod(0o600)


def command_setup(args: argparse.Namespace) -> int:
    settings = _settings(args.config)
    domain_id = args.domain_id or settings.domain_id
    binary = resolve_aliyun_cli(settings)
    if binary is None:
        if args.no_install:
            raise ConfigError("Aliyun CLI not found and --no-install was specified")
        print("正在安装项目私有 Aliyun CLI...")
        binary = install_aliyun_cli(PROJECT_ROOT)
    cli = PdsCli(
        binary,
        timeout_seconds=settings.timeout_seconds,
        read_retries=settings.read_retries,
    )
    cli_version, pds_version = verify_versions(cli)
    print(f"Aliyun CLI: {cli_version}")
    print(f"PDS 插件: {pds_version}")
    api_key = os.environ.pop("PDS_API_KEY", None)
    if not api_key:
        api_key = getpass.getpass("PDS API Key（输入不会显示）: ").strip()
    if not api_key:
        raise ConfigError("API Key cannot be empty")
    try:
        user_data = cli.configure_api_key(domain_id, api_key)
    finally:
        api_key = ""
    user = {key: user_data.get(key) for key in ("domain_id", "user_id", "nick_name")}
    _write_domain_config(domain_id)
    _print_identity(user)
    print("初始化完成。后续运行不需要 Agent。")
    return 0


def command_status(args: argparse.Namespace) -> int:
    settings = _settings(args.config)
    cli = _cli(settings)
    user = safe_user(cli)
    drives = list_drives(cli, settings.spaces)
    _print_identity(user)
    _print_drives(drives)
    return 0


def _inventory(settings: Settings, cli: PdsCli):
    user = safe_user(cli)
    drives = list_drives(cli, settings.spaces)
    items, errors = inventory_all(cli, drives, settings.page_size)
    directory, summary = write_snapshot(settings, user, drives, items, errors)
    return user, drives, items, errors, directory, summary


def command_inventory(args: argparse.Namespace) -> int:
    settings = _settings(args.config)
    cli = _cli(settings)
    user, drives, _items, errors, directory, summary = _inventory(settings, cli)
    _print_identity(user)
    _print_drives(drives)
    print(
        f"文件: {summary['file_count']}  文件夹: {summary['folder_count']}  "
        f"总大小: {_human_bytes(summary['total_file_bytes'])}"
    )
    print(f"快照: {directory}")
    if errors:
        print(f"警告: {len(errors)} 个空间盘点失败", file=sys.stderr)
        return 5
    return 0


def command_fetch(args: argparse.Namespace) -> int:
    settings = _settings(args.config)
    cli = _cli(settings)
    user, drives, items, inventory_errors, directory, summary = _inventory(settings, cli)
    _print_identity(user)
    _print_drives(drives)
    results: list[dict[str, Any]] = []
    for item in items:
        if item.get("type") != "file":
            continue
        try:
            result = download_item(cli, item, settings.download_dir)
            results.append(result)
            print(f"{result['status']}: {result['cloud_path']} -> {result['local_path']}")
        except (PdsError, OSError, ValueError) as exc:
            results.append(
                {
                    "drive_id": item.get("drive_id"),
                    "file_id": item.get("file_id"),
                    "name": item.get("name"),
                    "status": "failed",
                    "error": str(exc),
                }
            )
            print(f"下载失败: {item.get('name') or item.get('file_id')}: {exc}", file=sys.stderr)
    write_json(directory / "downloads.json", results)
    counts: dict[str, int] = {}
    for result in results:
        status = str(result.get("status") or "failed")
        counts[status] = counts.get(status, 0) + 1
    print(
        f"盘点文件: {summary['file_count']}  "
        f"已下载: {counts.get('downloaded', 0)}  "
        f"已跳过: {counts.get('skipped', 0)}  "
        f"冲突副本: {counts.get('conflicted', 0)}  "
        f"失败: {counts.get('failed', 0)}"
    )
    print(f"快照: {directory}")
    print(f"下载目录: {settings.download_dir}")
    return 5 if inventory_errors or counts.get("failed", 0) else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pds-sync",
        description="独立运行的阿里云 PDS 只读盘点与下载工具（不调用 Agent）",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    setup = subparsers.add_parser("setup", help="安装并配置 API Key")
    setup.add_argument("--config")
    setup.add_argument("--domain-id")
    setup.add_argument("--no-install", action="store_true")
    setup.set_defaults(handler=command_setup)
    for name, help_text, handler in (
        ("status", "验证身份并查看全部空间", command_status),
        ("inventory", "保存全部空间的文件元数据快照", command_inventory),
        ("fetch", "盘点并下载当前全部文件", command_fetch),
    ):
        command = subparsers.add_parser(name, help=help_text)
        command.add_argument("--config")
        command.set_defaults(handler=handler)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except ConfigError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2
    except PdsError as exc:
        message = str(exc)
        if exc.is_forbidden or "authentication" in message.lower() or "api key" in message.lower():
            code = 3
        else:
            code = 4
        print(f"PDS 错误: {message}", file=sys.stderr)
        return code
    except KeyboardInterrupt:
        print("已取消。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
