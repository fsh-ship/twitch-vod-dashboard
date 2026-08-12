#!/usr/bin/env python3

import argparse
import os
import time
from pathlib import Path

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".m4v"}

SIDECAR_SUFFIXES = (
    ".youtube-beschreibung.txt",
    ".youtube.json",
    ".info.json",
)

TEMP_SUFFIXES = (
    ".part",
    ".ytdl",
    ".tmp",
)

ORPHAN_DAYS = 7
TEMP_DAYS = 2


def human_size(size):
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} PB"


def age_days(path):
    return (time.time() - path.stat().st_mtime) / 86400


def sidecar_base(path):
    name = path.name
    for suffix in SIDECAR_SUFFIXES:
        if name.endswith(suffix):
            return name[:-len(suffix)]
    return None


def matching_video_exists(path, base):
    try:
        for candidate in path.parent.iterdir():
            if (
                candidate.is_file()
                and candidate.stem == base
                and candidate.suffix.lower() in VIDEO_EXTENSIONS
            ):
                return True
    except OSError:
        pass

    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--media-root",
        default=os.environ.get("VOD_DASHBOARD_MEDIA_ROOT")
        or str(Path.home() / "Documents" / "Twitch VODs"),
        help="Media root to inspect (alternatively set VOD_DASHBOARD_MEDIA_ROOT)",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="Permanently delete the files found",
    )
    args = parser.parse_args()
    root = Path(args.media_root).expanduser().resolve()
    if not root.is_dir():
        parser.error(f"Media root does not exist or is not a directory: {root}")

    candidates = []

    for path in root.rglob("*"):
        if not path.is_file():
            continue

        # Dashboard-Log wird separat von logrotate verwaltet.
        if ".dashboard" in path.parts:
            continue

        base = sidecar_base(path)

        if base is not None:
            if (
                not matching_video_exists(path, base)
                and age_days(path) >= ORPHAN_DAYS
            ):
                candidates.append(("orphaned sidecar", path))
            continue

        if path.suffix.lower() in TEMP_SUFFIXES:
            if age_days(path) >= TEMP_DAYS:
                candidates.append(("stale partial download", path))

    total_size = sum(
        path.stat().st_size
        for _, path in candidates
        if path.exists()
    )

    print()
    print("Twitch VOD Cleanup")
    print("==================")
    print(f"Path:              {root}")
    print(f"Files found:       {len(candidates)}")
    print(f"Recoverable space: {human_size(total_size)}")
    print()

    if not candidates:
        print("Nothing to clean up.")
        return

    for reason, path in candidates:
        try:
            size = human_size(path.stat().st_size)
        except OSError:
            size = "?"
        print(f"[{reason}] {size:>10}  {path}")

    print()

    if not args.delete:
        print("DRY RUN - nothing was deleted.")
        print("To delete permanently: cleanup-vods.py --delete")
        return

    deleted = 0
    deleted_size = 0

    for reason, path in candidates:
        try:
            size = path.stat().st_size
            path.unlink()
            deleted += 1
            deleted_size += size
        except OSError as exc:
            print(f"ERROR for {path}: {exc}")

    print(
        f"Deleted: {deleted} files, "
        f"{human_size(deleted_size)} recovered."
    )


if __name__ == "__main__":
    main()
