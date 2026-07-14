"""Codex binary discovery and version gating."""

from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path

from cuckoo.codex.config import MIN_CODEX_VERSION
from cuckoo.codex.errors import CodexNotFoundError, CodexVersionError

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\S+)")


def find_codex(codex_path: str | Path | None = None) -> str:
    """Resolve the codex binary; raises :class:`CodexNotFoundError`."""
    if codex_path is not None:
        path = Path(codex_path)
        if not path.exists():
            raise CodexNotFoundError(f"codex binary not found at {path}")
        return str(path)
    resolved = shutil.which("codex")
    if resolved is None:
        raise CodexNotFoundError(
            "codex binary not found on PATH; install the Codex CLI or pass codex_path"
        )
    return resolved


async def check_codex_version(codex_path: str) -> str:
    """Run ``codex --version`` and gate on the tested version floor."""
    try:
        process = await asyncio.create_subprocess_exec(
            codex_path,
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=15)
    except (OSError, TimeoutError) as exc:
        raise CodexNotFoundError(f"could not run {codex_path} --version: {exc}") from exc
    output = stdout.decode(errors="replace").strip()
    match = _VERSION_RE.search(output)
    if match is None:
        raise CodexVersionError(
            f"could not parse codex version from {output!r}", version=output
        )
    major, minor = int(match.group(1)), int(match.group(2))
    if (major, minor) < MIN_CODEX_VERSION:
        floor = ".".join(map(str, MIN_CODEX_VERSION))
        raise CodexVersionError(
            f"codex {match.group(0)} is older than the tested floor {floor}; "
            "upgrade codex or pass check_version=False",
            version=match.group(0),
        )
    return match.group(0)
