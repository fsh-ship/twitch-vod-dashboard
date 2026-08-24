"""Small crash-safe writers for critical dashboard runtime files."""
from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path
from typing import Optional


def _fsync_parent_directory(directory: Path) -> None:
    """Best-effort metadata durability; directory fsync is unavailable on Windows."""
    if os.name != "posix":
        return
    descriptor: Optional[int] = None
    try:
        descriptor = os.open(directory, os.O_RDONLY)
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def atomic_write_bytes(
    path: Path,
    content: bytes,
    *,
    mode: Optional[int] = None,
    create_parents: bool = True,
) -> None:
    """Replace *path* only after a same-directory temporary file is durable."""
    target = Path(path)
    temporary: Optional[Path] = None
    if create_parents:
        target.parent.mkdir(parents=True, exist_ok=True)
    requested_mode = mode
    if requested_mode is None:
        try:
            requested_mode = stat.S_IMODE(target.stat().st_mode)
        except OSError:
            requested_mode = None
    try:
        descriptor, raw_temporary = tempfile.mkstemp(
            dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
        )
        temporary = Path(raw_temporary)
        if requested_mode is not None:
            os.chmod(temporary, requested_mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        temporary = None
        _fsync_parent_directory(target.parent)
    except Exception:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def atomic_write_text(
    path: Path,
    content: str,
    *,
    encoding: str = "utf-8",
    mode: Optional[int] = None,
    create_parents: bool = True,
) -> None:
    """UTF-8-friendly wrapper that prepares all content before touching the target."""
    atomic_write_bytes(
        path,
        content.encode(encoding),
        mode=mode,
        create_parents=create_parents,
    )
