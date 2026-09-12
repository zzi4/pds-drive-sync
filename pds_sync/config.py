"""Load and validate non-sensitive project configuration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib
from typing import Any


DEFAULT_DOMAIN_ID = "bj39311"
KNOWN_SPACES = frozenset({"personal", "team", "enterprise"})
SUPPORTED_KEYS = frozenset(
    {
        "domain_id",
        "pds_endpoint",
        "spaces",
        "page_size",
        "snapshot_dir",
        "download_dir",
        "aliyun_cli_path",
        "timeout_seconds",
        "read_retries",
    }
)


class ConfigError(ValueError):
    """Raised when the local non-secret configuration is invalid."""


@dataclass(frozen=True, slots=True)
class Settings:
    project_root: Path
    domain_id: str
    pds_endpoint: str
    spaces: tuple[str, ...]
    page_size: int
    snapshot_dir: Path
    download_dir: Path
    aliyun_cli_path: Path | None
    timeout_seconds: int
    read_retries: int


def _project_path(project_root: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else project_root / path


def _integer(
    data: dict[str, Any],
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    value = data.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{name} must be an integer")
    if value < minimum or (maximum is not None and value > maximum):
        bounds = f"{minimum}..{maximum}" if maximum is not None else f">= {minimum}"
        raise ConfigError(f"{name} must be {bounds}")
    return value


def load_settings(
    project_root: Path,
    config_path: Path | None = None,
) -> Settings:
    """Return validated settings rooted at *project_root*.

    Credentials are intentionally not part of this schema.
    """

    root = Path(project_root)
    source = Path(config_path) if config_path is not None else root / "config.toml"
    data: dict[str, Any] = {}
    if source.exists():
        try:
            with source.open("rb") as handle:
                parsed = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"cannot read {source}: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ConfigError("configuration must be a TOML table")
        data = parsed

    unknown = sorted(set(data) - SUPPORTED_KEYS)
    if unknown:
        raise ConfigError(f"unsupported configuration field: {', '.join(unknown)}")

    domain_id = data.get("domain_id", DEFAULT_DOMAIN_ID)
    if not isinstance(domain_id, str) or not domain_id.strip():
        raise ConfigError("domain_id must be a non-empty string")
    domain_id = domain_id.strip()

    endpoint_value = data.get(
        "pds_endpoint",
        f"https://{domain_id}.api.aliyunfile.com",
    )
    if not isinstance(endpoint_value, str) or not endpoint_value.strip():
        raise ConfigError("pds_endpoint must be a non-empty string")

    spaces_value = data.get("spaces", ["personal", "team", "enterprise"])
    if not isinstance(spaces_value, list) or not spaces_value:
        raise ConfigError("spaces must be a non-empty list")
    if not all(isinstance(item, str) for item in spaces_value):
        raise ConfigError("spaces entries must be strings")
    unknown_spaces = sorted(set(spaces_value) - KNOWN_SPACES)
    if unknown_spaces:
        raise ConfigError(f"unknown space type: {', '.join(unknown_spaces)}")

    snapshot_value = data.get("snapshot_dir", "snapshots")
    download_value = data.get("download_dir", "downloads")
    cli_value = data.get("aliyun_cli_path")
    if not isinstance(snapshot_value, str) or not snapshot_value:
        raise ConfigError("snapshot_dir must be a non-empty string")
    if not isinstance(download_value, str) or not download_value:
        raise ConfigError("download_dir must be a non-empty string")
    if cli_value is not None and (not isinstance(cli_value, str) or not cli_value):
        raise ConfigError("aliyun_cli_path must be a non-empty string")

    return Settings(
        project_root=root,
        domain_id=domain_id,
        pds_endpoint=endpoint_value.strip().rstrip("/"),
        spaces=tuple(dict.fromkeys(spaces_value)),
        page_size=_integer(data, "page_size", 100, minimum=1, maximum=100),
        snapshot_dir=_project_path(root, snapshot_value),
        download_dir=_project_path(root, download_value),
        aliyun_cli_path=_project_path(root, cli_value) if cli_value else None,
        timeout_seconds=_integer(data, "timeout_seconds", 60, minimum=1),
        read_retries=_integer(data, "read_retries", 2, minimum=0),
    )
