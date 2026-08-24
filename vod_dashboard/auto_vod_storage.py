"""Flask-free storage policy shared by Auto VOD scheduling and workers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
from typing import Callable, Literal


GIB = 1024 ** 3
MINIMUM_FREE_BYTES = 50 * GIB
RESERVE_PERCENT = 15

StorageState = Literal["sufficient", "insufficient", "unavailable"]
DiskUsage = Callable[[Path], object]


@dataclass(frozen=True)
class AutoVodStorageStatus:
    """Safe raw-byte result; deliberately contains neither paths nor errors."""

    state: StorageState
    free_bytes: int | None
    total_bytes: int | None
    required_free_bytes: int | None

    @property
    def allows_start(self) -> bool:
        return self.state == "sufficient"


def required_free_bytes(total_bytes: int) -> int:
    """Return the hard Auto VOD reserve for a measured filesystem capacity."""
    return max(MINIMUM_FREE_BYTES, (int(total_bytes) * RESERVE_PERCENT) // 100)


def _existing_filesystem_target(root: Path) -> Path:
    """Use an existing ancestor so a not-yet-created download folder is supported."""
    target = Path(root).expanduser().resolve(strict=False)
    while not target.exists() and target != target.parent:
        target = target.parent
    return target


def assess_auto_vod_storage(
    media_root: Path,
    *,
    disk_usage: DiskUsage = shutil.disk_usage,
) -> AutoVodStorageStatus:
    """Measure the media filesystem and fail closed without exposing diagnostics."""
    try:
        usage = disk_usage(_existing_filesystem_target(Path(media_root)))
        free_bytes = int(getattr(usage, "free"))
        total_bytes = int(getattr(usage, "total"))
        if free_bytes < 0 or total_bytes <= 0:
            raise ValueError("invalid disk usage")
        reserve = required_free_bytes(total_bytes)
    except Exception:
        return AutoVodStorageStatus("unavailable", None, None, None)
    return AutoVodStorageStatus(
        "sufficient" if free_bytes >= reserve else "insufficient",
        free_bytes,
        total_bytes,
        reserve,
    )
