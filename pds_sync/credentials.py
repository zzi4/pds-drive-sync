"""Store a PDS API Key outside the project with private permissions."""

from __future__ import annotations

import getpass
import os
from pathlib import Path
import stat
from typing import Callable


DEFAULT_KEY_FILE = Path.home() / ".config" / "pds-sync" / "api_key"


class CredentialError(ValueError):
    """Raised for a missing or insecure local API Key file."""


def _read_private_file(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CredentialError(f"cannot read private credential file: {exc}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise CredentialError("credential path must be a regular file")
        if metadata.st_uid != os.getuid():
            raise CredentialError("credential file must be owned by the current user")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise CredentialError("credential file permissions must be 600")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            value = handle.read().strip()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not value:
        raise CredentialError("credential file is empty")
    return value


def _write_private_file(path: Path, value: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        return
    except OSError as exc:
        raise CredentialError(f"cannot create private credential file: {exc}") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(value)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        path.chmod(0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def load_or_create_api_key(
    path: Path = DEFAULT_KEY_FILE,
    *,
    prompt: Callable[[str], str] = getpass.getpass,
) -> str:
    """Read a private Key, or prompt once and create the private file."""

    key_path = Path(path)
    if key_path.exists() or key_path.is_symlink():
        return _read_private_file(key_path)
    value = prompt("PDS API Key（输入不会显示，将保存到私密文件）: ").strip()
    if not value:
        raise CredentialError("API Key cannot be empty")
    _write_private_file(key_path, value)
    return _read_private_file(key_path)
