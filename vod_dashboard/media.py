from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional


VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".m4v"}
INCOMPLETE_VIDEO_SUFFIXES = {".temp", ".part", ".partial", ".download", ".ytdl"}


def is_complete_video_file(path: Path) -> bool:
    """Accept supported videos while rejecting known downloader work artifacts."""
    if path.suffix.lower() not in VIDEO_EXTENSIONS:
        return False
    stem_suffix = Path(path.stem).suffix.lower()
    return stem_suffix not in INCOMPLETE_VIDEO_SUFFIXES


def local_video_marker_path(path: Path) -> Path:
    return path.with_suffix(".uploaded.json")


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    base = path.with_suffix("")
    suffix = path.suffix
    for index in range(2, 1000):
        candidate = Path(f"{base} ({index}){suffix}")
        if not candidate.exists():
            return candidate
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return Path(f"{base} ({stamp}){suffix}")


@dataclass(frozen=True)
class MediaPathPolicy:
    media_root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "media_root", Path(self.media_root).resolve())

    def resolve_media_path(
        self,
        raw: Any,
        *,
        must_exist: bool = False,
        require_file: bool = False,
        allowed_extensions: Optional[set[str]] = None,
    ) -> Path:
        """Resolve a path and enforce the administrator-controlled media root."""
        text = str(raw or "").strip()
        if not text:
            raise RuntimeError("No media path provided.")

        candidate = Path(text).expanduser()
        if not candidate.is_absolute():
            candidate = self.media_root / candidate

        root = self.media_root.resolve()
        resolved = candidate.resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise RuntimeError(
                f"The path is outside the administrator-configured media root: {root}"
            ) from exc

        if must_exist and not resolved.exists():
            raise RuntimeError(f"File not found: {resolved}")
        if require_file and not resolved.is_file():
            raise RuntimeError(f"Path is not a file: {resolved}")
        if (
            allowed_extensions is not None
            and resolved.suffix.lower() not in allowed_extensions
        ):
            raise RuntimeError("Unsupported VOD file type.")
        return resolved

    def normalize_media_directory(self, raw: Any, fallback: Path) -> Path:
        """Normalize a configurable directory to a safe location below the media root."""
        try:
            return self.resolve_media_path(raw or fallback)
        except RuntimeError:
            return self.resolve_media_path(fallback)

    def download_path(self, settings: Mapping[str, Any]) -> Path:
        return self.normalize_media_directory(
            settings.get("download_path"), self.media_root
        )

    def uploaded_vods_folder(
        self,
        settings: Mapping[str, Any],
        fallback: Path,
    ) -> Path:
        raw = str(settings.get("uploaded_vods_folder") or "").strip()
        return self.normalize_media_directory(raw, fallback)

    def is_path_inside(self, path: Path, root: Path) -> bool:
        try:
            resolved = self.resolve_media_path(path)
            resolved_root = self.resolve_media_path(root)
            return resolved == resolved_root or resolved_root in resolved.parents
        except RuntimeError:
            return False

    def safe_local_video_path(
        self,
        raw: Any,
        settings: Mapping[str, Any],
        must_exist: bool = True,
    ) -> Path:
        del settings
        path = self.resolve_media_path(
            raw,
            must_exist=must_exist,
            require_file=must_exist,
            allowed_extensions=VIDEO_EXTENSIONS,
        )
        if not is_complete_video_file(path):
            raise RuntimeError("Incomplete or temporary VOD files cannot be used.")
        return path

    def local_video_sidecars(self, path: Path) -> List[Path]:
        """Alle Dateien, die eindeutig zu einer Videodatei gehören."""
        names = [
            path.with_suffix(".info.json"),
            path.with_suffix(".youtube.json"),
            path.with_suffix(".youtube-beschreibung.txt"),
            local_video_marker_path(path),
        ]
        found: List[Path] = []
        for candidate in names:
            try:
                if candidate.exists():
                    candidate = self.resolve_media_path(
                        candidate, must_exist=True, require_file=True
                    )
                    if candidate not in found:
                        found.append(candidate)
            except Exception:
                pass
        return found

    def read_local_upload_marker(
        self,
        path: Path,
        log: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, Any]:
        marker = self.resolve_media_path(local_video_marker_path(path))
        try:
            if marker.exists():
                raw = marker.read_text(encoding="utf-8-sig")
                data = json.loads(raw) if raw.strip() else {}
                return data if isinstance(data, dict) else {}
        except Exception as exc:
            if log:
                log(f"Could not read upload marker {marker}: {exc}")
        return {}

    def write_local_upload_marker(
        self,
        path: Path,
        method: str = "manual",
    ) -> Dict[str, Any]:
        path = self.resolve_media_path(
            path,
            must_exist=True,
            require_file=True,
            allowed_extensions=VIDEO_EXTENSIONS,
        )
        marker = self.resolve_media_path(local_video_marker_path(path))
        payload = {
            "uploaded": True,
            "method": method,
            "uploaded_at": datetime.now().isoformat(timespec="seconds"),
            "video_path": str(path),
            "video_name": path.name,
        }
        marker.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return payload

    def snapshot_video_files(self, settings: Mapping[str, Any]) -> Dict[str, float]:
        root = self.download_path(settings)
        found: Dict[str, float] = {}
        if not root.exists():
            return found
        for path in root.rglob("*"):
            if path.is_file() and is_complete_video_file(path):
                try:
                    path = self.safe_local_video_path(path, settings)
                    found[str(path)] = path.stat().st_mtime
                except Exception:
                    pass
        return found

    def new_video_files(
        self,
        before: Dict[str, float],
        after: Dict[str, float],
    ) -> List[Path]:
        files: List[Path] = []
        for path_value, mtime in after.items():
            if path_value not in before or mtime > before.get(path_value, 0) + 1:
                try:
                    files.append(
                        self.resolve_media_path(
                            path_value,
                            must_exist=True,
                            require_file=True,
                            allowed_extensions=VIDEO_EXTENSIONS,
                        )
                    )
                except RuntimeError:
                    continue
        files.sort(
            key=lambda path: path.stat().st_mtime if path.exists() else 0,
            reverse=True,
        )
        return files

    def recently_changed_video_files(
        self,
        settings: Mapping[str, Any],
        started_at: float,
        minutes_buffer: int = 180,
    ) -> List[Path]:
        """Fallback: Falls die vorher/nachher-Erkennung nichts findet, nimm fertige Videos,
        die seit Jobstart bzw. kurz davor geändert wurden. Das hilft bei yt-dlp Merge/Move-Eigenheiten.
        """
        root = self.download_path(settings)
        if not root.exists():
            return []
        cutoff = started_at - (minutes_buffer * 60)
        files: List[Path] = []
        uploaded = set(map(str, settings.get("youtube_uploaded_files") or []))
        for path in root.rglob("*"):
            if not path.is_file() or not is_complete_video_file(path):
                continue
            try:
                path = self.safe_local_video_path(path, settings)
                stat = path.stat()
            except Exception:
                continue
            if stat.st_size <= 0:
                continue
            if stat.st_mtime >= cutoff and str(path) not in uploaded:
                files.append(path)
        files.sort(
            key=lambda path: path.stat().st_mtime if path.exists() else 0,
            reverse=True,
        )
        return files

    def move_video_bundle_verified(
        self,
        path: Path,
        settings: Mapping[str, Any],
        uploaded_folder_fallback: Path,
        log: Optional[Callable[[str], None]] = None,
        job_log: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, Any]:
        """Verschiebt Video und Sidecars. Erfolg gilt nur, wenn Quelle weg und Ziel vorhanden ist."""
        path = self.safe_local_video_path(path, settings)
        target_root = self.resolve_media_path(
            self.uploaded_vods_folder(settings, uploaded_folder_fallback)
        )
        target_root.mkdir(parents=True, exist_ok=True)

        if self.is_path_inside(path, target_root):
            return {
                "ok": True,
                "already_in_target": True,
                "old_path": str(path),
                "new_path": str(path),
                "source_removed": True,
                "moved_sidecars": [],
            }

        root = self.download_path(settings)
        try:
            relative_parent = path.parent.resolve().relative_to(root.resolve())
            target_dir = self.resolve_media_path(target_root / relative_parent)
        except Exception:
            target_dir = target_root
        target_dir.mkdir(parents=True, exist_ok=True)

        target_video = self.resolve_media_path(unique_path(target_dir / path.name))
        sidecars = self.local_video_sidecars(path)
        old_path = path
        old_size = path.stat().st_size

        shutil.move(str(old_path), str(target_video))

        if not target_video.exists():
            raise RuntimeError(
                f"Destination file was not found after the move: {target_video}"
            )
        if old_path.exists():
            if target_video.stat().st_size == old_size:
                old_path.unlink()
            else:
                raise RuntimeError(
                    "The source still exists and the destination size does not match. Stopping to prevent data loss."
                )
        if old_path.exists():
            raise RuntimeError("The source file still exists after the move.")

        moved_sidecars = []
        for sidecar in sidecars:
            if not sidecar.exists():
                continue
            suffix_tail = sidecar.name[len(old_path.stem):]
            side_target = self.resolve_media_path(
                target_video.with_name(target_video.stem + suffix_tail)
            )
            if side_target.exists():
                side_target = self.resolve_media_path(unique_path(side_target))
            shutil.move(str(sidecar), str(side_target))
            if sidecar.exists():
                raise RuntimeError(
                    f"Sidecar file was not moved successfully: {sidecar.name}"
                )
            moved_sidecars.append(str(side_target))

        marker = local_video_marker_path(target_video)
        if marker.exists():
            try:
                marker_data = self.read_local_upload_marker(target_video, log=log)
                marker_data["video_path"] = str(target_video)
                marker_data["video_name"] = target_video.name
                marker.write_text(
                    json.dumps(marker_data, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
            except Exception:
                pass

        if job_log:
            job_log(f"VOD moved: {old_path} -> {target_video}")
            job_log("Source removed: yes")

        return {
            "ok": True,
            "already_in_target": False,
            "old_path": str(old_path),
            "new_path": str(target_video),
            "source_removed": not old_path.exists(),
            "moved_sidecars": moved_sidecars,
        }

    def delete_video_bundle_permanently(
        self,
        path: Path,
        settings: Mapping[str, Any],
    ) -> Dict[str, Any]:
        path = self.safe_local_video_path(path, settings)
        files = [path] + self.local_video_sidecars(path)
        deleted = []
        freed_bytes = 0
        errors = []
        for candidate in files:
            try:
                if candidate.exists():
                    size = candidate.stat().st_size
                    candidate.unlink()
                    freed_bytes += size
                    deleted.append(str(candidate))
            except Exception as exc:
                errors.append({"path": str(candidate), "error": str(exc)})
        return {
            "ok": not errors,
            "deleted": deleted,
            "errors": errors,
            "freed_gb": round(freed_bytes / (1024 ** 3), 2),
        }

    def disk_status(self, settings: Mapping[str, Any]) -> Dict[str, Any]:
        path = self.download_path(settings)
        try:
            target = path if path.exists() else path.parent
            usage = shutil.disk_usage(target)
            return {
                "ok": True,
                "path": str(path),
                "free_gb": round(usage.free / (1024 ** 3), 1),
                "total_gb": round(usage.total / (1024 ** 3), 1),
                "used_gb": round(usage.used / (1024 ** 3), 1),
            }
        except Exception as exc:
            return {"ok": False, "path": str(path), "error": str(exc)}
