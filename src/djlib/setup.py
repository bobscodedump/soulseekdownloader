"""First-run setup for people who do not use a terminal: fetch sockseek and store the Soulseek login.

sockseek (AGPL, github.com/fiso64/sockseek) is downloaded from its own release page, not bundled.
The login lives where sockseek itself reads it, ~/.config/sockseek/sockseek.conf, readable only by you.
"""
from __future__ import annotations

import io
import os
import platform
import re
import tarfile
import urllib.request
from pathlib import Path

from .db import db_path

VERSION = "3.0.6"
URL = "https://github.com/fiso64/sockseek/releases/download/v{v}/sockseek_{v}_{arch}.tar.gz"
CONF = Path.home() / ".config" / "sockseek" / "sockseek.conf"


def managed_path() -> Path:
    return db_path().parent / "bin" / "sockseek"


def asset_url() -> str:
    arch = "osx-arm64" if platform.machine() == "arm64" else "osx-x64"
    return URL.format(v=VERSION, arch=arch)


def download(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as r:
        return r.read()


def install_sockseek(fetch=download, dest: Path | None = None) -> Path:
    dest = dest or managed_path()
    data = fetch(asset_url())
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        member = next((m for m in tar.getmembers() if m.isfile() and Path(m.name).name == "sockseek"), None)
        if member is None:
            raise RuntimeError("the download did not contain sockseek")
        blob = tar.extractfile(member).read()
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_bytes(blob)
    tmp.chmod(0o755)
    os.replace(tmp, dest)
    return dest


def _read(conf: Path) -> list[str]:
    return conf.read_text().splitlines() if conf.exists() else []


def soulseek_user(conf: Path = CONF) -> str | None:
    for line in _read(conf):
        m = re.match(r"\s*(?:user|username)\s*=\s*(.+?)\s*$", line)
        if m:
            return m[1]
    return None


def save_login(user: str, password: str, conf: Path = CONF) -> None:
    """Replace the login lines and keep everything else in the file."""
    if not user.strip() or not password:
        raise ValueError("Enter both the Soulseek username and password.")
    if "\n" in user + password or "\r" in user + password:
        raise ValueError("The username and password cannot contain line breaks.")
    keep = [ln for ln in _read(conf) if not re.match(r"\s*(user|username|pass|password)\s*=", ln)]
    lines = [f"username = {user.strip()}", f"password = {password}"] + keep
    conf.parent.mkdir(parents=True, exist_ok=True)
    tmp = conf.with_name(conf.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, conf)
