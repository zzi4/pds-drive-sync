"""Core PDS CLI, inventory, snapshot, and download operations."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import secrets
import shutil
import subprocess
import tarfile
import tempfile
import time
from typing import Any, Callable, Iterable, Sequence
import urllib.request

from .config import Settings


USER_AGENT_PREFIX = (
    "AlibabaCloud-Agent-Skills/alibabacloud-pds-intelligent-workspace"
)
CLI_DOWNLOADS = {
    "x86_64": "https://aliyuncli.alicdn.com/aliyun-cli-linux-latest-amd64.tgz",
    "amd64": "https://aliyuncli.alicdn.com/aliyun-cli-linux-latest-amd64.tgz",
    "aarch64": "https://aliyuncli.alicdn.com/aliyun-cli-linux-latest-arm64.tgz",
    "arm64": "https://aliyuncli.alicdn.com/aliyun-cli-linux-latest-arm64.tgz",
}
MIN_CLI_VERSION = (3, 3, 16)
MIN_PDS_VERSION = (0, 7, 7)
ITEM_PROJECTION = (
    "{items:items[].{drive_id:drive_id,file_id:file_id,"
    "parent_file_id:parent_file_id,name:name,type:type,size:size,"
    "category:category,file_extension:file_extension,created_at:created_at,"
    "updated_at:updated_at,content_hash:content_hash,"
    "content_hash_name:content_hash_name,revision_id:revision_id},"
    "next_marker:next_marker,total_count:total_count}"
)
DRIVE_PROJECTION = (
    "{drives:drives[].{drive_id:drive_id,drive_name:drive_name,"
    "space_type:space_type,owner_type:owner_type,owner:owner,"
    "total_size:total_size,used_size:used_size},count:count}"
)


class PdsError(RuntimeError):
    """A sanitized local, CLI, or PDS failure."""

    def __init__(self, message: str, *, exit_code: int | None = None):
        super().__init__(message)
        self.exit_code = exit_code

    @property
    def is_forbidden(self) -> bool:
        text = str(self).lower()
        return any(
            marker in text
            for marker in ("403", "forbidden", "no permission", "not authorized")
        )


def redact(text: str, secret_values: Iterable[str] = ()) -> str:
    """Remove known secret values from diagnostic text."""

    result = text
    for value in secret_values:
        if value:
            result = result.replace(value, "[REDACTED]")
    return result


def build_pds_argv(binary: Path, args: Sequence[str], session_id: str) -> list[str]:
    """Build one auditable PDS command without a shell."""

    if not re.fullmatch(r"[0-9a-f]{32}", session_id):
        raise ValueError("session_id must be 32 lowercase hexadecimal characters")
    return [
        str(binary),
        "pds",
        *args,
        "--user-agent",
        f"{USER_AGENT_PREFIX}/{session_id}",
    ]


def safe_destination(
    download_root: Path,
    space_type: str,
    drive_id: str,
    cloud_path: str,
) -> Path:
    """Map an absolute cloud path below a drive-specific local root."""

    if "\x00" in cloud_path or not cloud_path.startswith("/"):
        raise ValueError("cloud path must be an absolute path without NUL")
    cloud = PurePosixPath(cloud_path)
    if any(part in {"", ".", ".."} for part in cloud.parts[1:]):
        raise ValueError("cloud path contains an unsafe component")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", space_type):
        raise ValueError("space type is unsafe")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", str(drive_id)):
        raise ValueError("drive id is unsafe")
    root = Path(download_root).resolve()
    target = root.joinpath(f"{space_type}-{drive_id}", *cloud.parts[1:]).resolve()
    if not target.is_relative_to(root):
        raise ValueError("cloud path escapes download directory")
    return target


def parse_version(text: str) -> tuple[int, int, int]:
    match = re.search(r"(?<!\d)(\d+)\.(\d+)\.(\d+)(?!\d)", text)
    if not match:
        raise PdsError(f"cannot parse version from output: {text.strip()!r}")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


class PdsCli:
    """Small subprocess boundary around the official Aliyun PDS plugin."""

    def __init__(
        self,
        binary: Path,
        *,
        timeout_seconds: int = 60,
        read_retries: int = 2,
        session_id: str | None = None,
        executor: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ):
        self.binary = Path(binary)
        self.timeout_seconds = timeout_seconds
        self.read_retries = read_retries
        self.session_id = session_id or secrets.token_hex(16)
        self._executor = executor

    def _execute(
        self,
        argv: Sequence[str],
        *,
        read_only: bool,
        secret_values: Sequence[str] = (),
    ) -> subprocess.CompletedProcess[str]:
        attempts = self.read_retries + 1 if read_only else 1
        last_error = ""
        for attempt in range(attempts):
            try:
                result = self._executor(
                    list(argv),
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                last_error = f"Aliyun CLI timed out after {self.timeout_seconds}s"
                if attempt + 1 < attempts:
                    time.sleep(min(attempt + 1, 2))
                    continue
                raise PdsError(last_error) from None
            except OSError as exc:
                raise PdsError(f"cannot run Aliyun CLI: {exc}") from exc

            if result.returncode == 0:
                return result
            output = "\n".join(
                part.strip() for part in (result.stderr, result.stdout) if part.strip()
            )
            output = redact(output, secret_values) or "Aliyun CLI returned no details"
            retryable = any(
                marker in output.lower()
                for marker in ("429", "timeout", "timed out", "throttl", "temporar")
            )
            if read_only and retryable and attempt + 1 < attempts:
                time.sleep(min(attempt + 1, 2))
                continue
            raise PdsError(output, exit_code=result.returncode)
        raise PdsError(last_error or "Aliyun CLI call failed")

    def run_text(
        self,
        args: Sequence[str],
        *,
        read_only: bool = True,
        secret_values: Sequence[str] = (),
    ) -> str:
        result = self._execute(
            [str(self.binary), *args],
            read_only=read_only,
            secret_values=secret_values,
        )
        return result.stdout.strip()

    def pds(
        self,
        args: Sequence[str],
        *,
        read_only: bool = True,
        secret_values: Sequence[str] = (),
        expect_json: bool = True,
    ) -> Any:
        argv = build_pds_argv(self.binary, args, self.session_id)
        result = self._execute(
            argv,
            read_only=read_only,
            secret_values=secret_values,
        )
        text = result.stdout.strip()
        if not expect_json:
            return text
        try:
            return json.loads(text or "{}")
        except json.JSONDecodeError as exc:
            safe = redact(text[:500], secret_values)
            raise PdsError(f"Aliyun CLI did not return valid JSON: {safe!r}") from exc

    def configure_api_key(self, domain_id: str, api_key: str) -> dict[str, Any]:
        self.pds(
            [
                "config",
                "--domain-id",
                domain_id,
                "--authentication-type",
                "api_key",
                "--api-key",
                api_key,
            ],
            read_only=False,
            secret_values=(api_key,),
        )
        result = self.pds(["get-user"])
        if not isinstance(result, dict):
            raise PdsError("get-user returned an unexpected response")
        return result


def resolve_aliyun_cli(settings: Settings) -> Path | None:
    candidates: list[Path] = []
    if settings.aliyun_cli_path:
        candidates.append(settings.aliyun_cli_path)
    candidates.append(settings.project_root / ".tools" / "aliyun")
    system = shutil.which("aliyun")
    if system:
        candidates.append(Path(system))
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def install_aliyun_cli(project_root: Path) -> Path:
    """Download the official CLI binary into this project."""

    if platform.system() != "Linux":
        raise PdsError("automatic CLI installation currently supports Linux only")
    machine = platform.machine().lower()
    url = CLI_DOWNLOADS.get(machine)
    if not url:
        raise PdsError(f"unsupported CPU architecture: {machine}")
    destination = project_root / ".tools" / "aliyun"
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pds-cli-") as temp_dir:
        archive = Path(temp_dir) / "aliyun-cli.tgz"
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "pds-sync/0.1"})
            with urllib.request.urlopen(request, timeout=600) as response:
                with archive.open("wb") as handle:
                    shutil.copyfileobj(response, handle)
        except OSError as exc:
            raise PdsError(f"cannot download Aliyun CLI: {exc}") from exc
        try:
            with tarfile.open(archive, "r:gz") as bundle:
                candidates = [
                    member
                    for member in bundle.getmembers()
                    if PurePosixPath(member.name).name == "aliyun"
                ]
                if len(candidates) != 1 or not candidates[0].isfile():
                    raise PdsError("Aliyun CLI archive has no safe regular binary")
                member = candidates[0]
                parts = PurePosixPath(member.name).parts
                if member.name.startswith("/") or ".." in parts:
                    raise PdsError("Aliyun CLI archive contains an unsafe path")
                source = bundle.extractfile(member)
                if source is None:
                    raise PdsError("cannot read Aliyun CLI binary from archive")
                temporary = destination.with_suffix(".part")
                with temporary.open("wb") as handle:
                    shutil.copyfileobj(source, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                temporary.chmod(0o755)
                os.replace(temporary, destination)
        except (OSError, tarfile.TarError) as exc:
            raise PdsError(f"cannot install Aliyun CLI: {exc}") from exc
    return destination


def verify_versions(cli: PdsCli) -> tuple[str, str]:
    cli_text = cli.run_text(["version"])
    if parse_version(cli_text) < MIN_CLI_VERSION:
        raise PdsError("Aliyun CLI must be at least version 3.3.16")
    try:
        pds_text = cli.pds(["version"], expect_json=False)
    except PdsError:
        cli.run_text(["plugin", "install", "--names", "pds"], read_only=False)
        pds_text = cli.pds(["version"], expect_json=False)
    if parse_version(pds_text) < MIN_PDS_VERSION:
        cli.run_text(["plugin", "update"], read_only=False)
        pds_text = cli.pds(["version"], expect_json=False)
        if parse_version(pds_text) < MIN_PDS_VERSION:
            raise PdsError("PDS plugin must be at least version 0.7.7")
    return cli_text, pds_text


def safe_user(cli: PdsCli) -> dict[str, Any]:
    data = cli.pds(["get-user"])
    if not isinstance(data, dict):
        raise PdsError("get-user returned an unexpected response")
    return {key: data.get(key) for key in ("domain_id", "user_id", "nick_name")}


def _page_items(data: Any) -> tuple[list[dict[str, Any]], str]:
    if not isinstance(data, dict):
        raise PdsError("PDS list response is not an object")
    items = data.get("items", [])
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise PdsError("PDS list response has invalid items")
    marker = data.get("next_marker") or ""
    if not isinstance(marker, str):
        raise PdsError("PDS list response has an invalid next_marker")
    return items, marker


def _paginate(cli: PdsCli, base_args: list[str]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    marker = ""
    seen_markers: set[str] = set()
    while True:
        args = [*base_args]
        if marker:
            args.extend(["--marker", marker])
        page = cli.pds(args)
        items, next_marker = _page_items(page)
        output.extend(items)
        if not next_marker:
            return output
        if next_marker in seen_markers:
            raise PdsError("PDS pagination returned a repeated marker")
        seen_markers.add(next_marker)
        marker = next_marker


def list_drives(cli: PdsCli, spaces: Sequence[str]) -> list[dict[str, Any]]:
    try:
        data = cli.pds(
            ["list-all-drives", "--cli-query", DRIVE_PROJECTION]
        )
        if not isinstance(data, dict) or not isinstance(data.get("drives", []), list):
            raise PdsError("list-all-drives returned an unexpected response")
        drives = [item for item in data.get("drives", []) if isinstance(item, dict)]
    except PdsError as exc:
        if not exc.is_forbidden:
            raise
        personal = _paginate(
            cli,
            ["list-my-drives", "--limit", "100"],
        )
        for drive in personal:
            drive["space_type"] = "personal"
        group_data = cli.pds(["list-my-group-drive", "--limit", "100", "--marker", ""])
        if not isinstance(group_data, dict):
            raise PdsError("list-my-group-drive returned an unexpected response")
        group_items = group_data.get("items", [])
        if not isinstance(group_items, list):
            raise PdsError("list-my-group-drive returned invalid items")
        groups = [item for item in group_items if isinstance(item, dict)]
        for drive in groups:
            drive["space_type"] = "team"
        root_drive = group_data.get("root_group_drive")
        if isinstance(root_drive, dict) and root_drive.get("drive_id"):
            root_drive["space_type"] = "enterprise"
            groups.append(root_drive)
        drives = [*personal, *groups]

    allowed = set(spaces)
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for drive in drives:
        drive_id = str(drive.get("drive_id") or "")
        if not drive_id or drive_id in seen or drive.get("space_type") not in allowed:
            continue
        seen.add(drive_id)
        result.append(
            {
                key: drive.get(key)
                for key in (
                    "drive_id",
                    "drive_name",
                    "space_type",
                    "owner_type",
                    "owner",
                    "total_size",
                    "used_size",
                )
            }
        )
    return result


def inventory_drive(
    cli: PdsCli,
    drive: dict[str, Any],
    page_size: int,
) -> list[dict[str, Any]]:
    drive_id = str(drive.get("drive_id") or "")
    if not drive_id:
        raise PdsError("cannot inventory a drive without drive_id")
    base = [
        "search-file",
        "--drive-id",
        drive_id,
        "--limit",
        str(page_size),
        "--recursive",
        "true",
        "--return-total-count",
        "true",
        "--cli-query",
        ITEM_PROJECTION,
    ]
    items = _paginate(cli, base)
    normalized: list[dict[str, Any]] = []
    for item in items:
        file_id = str(item.get("file_id") or "")
        if not file_id:
            raise PdsError(f"drive {drive_id} returned an item without file_id")
        item["drive_id"] = str(item.get("drive_id") or drive_id)
        item["space_type"] = drive.get("space_type")
        item["drive_name"] = drive.get("drive_name")
        normalized.append(item)
    return normalized


def inventory_all(
    cli: PdsCli,
    drives: Sequence[dict[str, Any]],
    page_size: int,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    items: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for drive in drives:
        try:
            items.extend(inventory_drive(cli, drive, page_size))
        except PdsError as exc:
            errors.append(
                {
                    "drive_id": str(drive.get("drive_id") or ""),
                    "error": str(exc),
                }
            )
    return items, errors


def resolve_cloud_path(cli: PdsCli, item: dict[str, Any]) -> str:
    drive_id = str(item.get("drive_id") or "")
    file_id = str(item.get("file_id") or "")
    if not drive_id or not file_id:
        raise PdsError("cannot resolve a file without drive_id and file_id")
    data = cli.pds(
        [
            "resolve-path",
            "--drive-id",
            drive_id,
            "--file-id",
            file_id,
            "--cli-query",
            "{path:path}",
        ]
    )
    path = data.get("path") if isinstance(data, dict) else None
    if not isinstance(path, str) or not path.startswith("/"):
        raise PdsError(f"cannot resolve cloud path for file {file_id}")
    return path


def _file_matches(item: dict[str, Any], path: Path) -> bool:
    algorithm = str(item.get("content_hash_name") or "").lower()
    expected = str(item.get("content_hash") or "").lower()
    if algorithm not in {"sha1", "sha256"} or not expected:
        return False
    digest = hashlib.new(algorithm)
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().lower() == expected


def _conflict_path(target: Path, file_id: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return target.with_name(f"{target.name}.{stamp}-{file_id[:8]}.remote")


def download_item(
    cli: PdsCli,
    item: dict[str, Any],
    download_root: Path,
) -> dict[str, Any]:
    cloud_path = resolve_cloud_path(cli, item)
    drive_id = str(item.get("drive_id") or "")
    file_id = str(item.get("file_id") or "")
    space_type = str(item.get("space_type") or "unknown")
    target = safe_destination(download_root, space_type, drive_id, cloud_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    status = "downloaded"
    if target.exists():
        if target.is_file() and _file_matches(item, target):
            return {
                "drive_id": drive_id,
                "file_id": file_id,
                "cloud_path": cloud_path,
                "local_path": str(target),
                "status": "skipped",
            }
        target = _conflict_path(target, file_id)
        status = "conflicted"
    temporary = target.with_name(f".{target.name}.{secrets.token_hex(6)}.part")
    try:
        result = cli.pds(
            [
                "download-to-local",
                "--drive-id",
                drive_id,
                "--file-id",
                file_id,
                "--save-to",
                str(temporary),
            ]
        )
        if not temporary.is_file():
            raise PdsError(f"download did not create a local file for {file_id}")
        local_size = temporary.stat().st_size
        expected_sizes = {
            int(value)
            for value in (item.get("size"), result.get("size") if isinstance(result, dict) else None)
            if isinstance(value, int) and value >= 0
        }
        if expected_sizes and (len(expected_sizes) != 1 or local_size not in expected_sizes):
            raise PdsError(f"download size verification failed for {file_id}")
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        return {
            "drive_id": drive_id,
            "file_id": file_id,
            "cloud_path": cloud_path,
            "local_path": str(target),
            "size": local_size,
            "status": status,
        }
    finally:
        if temporary.exists():
            temporary.unlink()


def build_summary(
    drives: Sequence[dict[str, Any]],
    items: Sequence[dict[str, Any]],
    errors: Sequence[dict[str, str]],
) -> dict[str, Any]:
    files = [item for item in items if item.get("type") == "file"]
    folders = [item for item in items if item.get("type") == "folder"]
    by_space = Counter(str(item.get("space_type") or "unknown") for item in files)
    by_category = Counter(str(item.get("category") or "others") for item in files)
    bytes_by_space: defaultdict[str, int] = defaultdict(int)
    for item in files:
        size = item.get("size")
        if isinstance(size, int) and size >= 0:
            bytes_by_space[str(item.get("space_type") or "unknown")] += size
    return {
        "complete": not errors,
        "drive_count": len(drives),
        "file_count": len(files),
        "folder_count": len(folders),
        "total_file_bytes": sum(bytes_by_space.values()),
        "files_by_space": dict(sorted(by_space.items())),
        "bytes_by_space": dict(sorted(bytes_by_space.items())),
        "files_by_category": dict(sorted(by_category.items())),
        "errors": list(errors),
    }


def _atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.part")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def write_json(path: Path, value: Any) -> None:
    _atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def write_snapshot(
    settings: Settings,
    user: dict[str, Any],
    drives: Sequence[dict[str, Any]],
    items: Sequence[dict[str, Any]],
    errors: Sequence[dict[str, str]],
) -> tuple[Path, dict[str, Any]]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = settings.snapshot_dir / stamp
    suffix = 1
    while directory.exists():
        directory = settings.snapshot_dir / f"{stamp}-{suffix}"
        suffix += 1
    directory.mkdir(parents=True)
    summary = build_summary(drives, items, errors)
    write_json(directory / "user.json", user)
    write_json(directory / "drives.json", list(drives))
    ndjson = "".join(
        json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in items
    )
    _atomic_write(directory / "files.ndjson", ndjson)
    write_json(directory / "summary.json", summary)
    if summary["complete"]:
        write_json(
            settings.snapshot_dir / "latest.json",
            {"snapshot": directory.name, "created_at": stamp},
        )
    return directory, summary
