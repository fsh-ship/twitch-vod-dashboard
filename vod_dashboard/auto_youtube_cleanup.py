"""Crash-safe execution of the frozen Auto YouTube local-cleanup policy."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping, Optional

from vod_dashboard.media import (
    MediaPathPolicy,
    is_complete_video_file,
    local_video_marker_path,
)
from vod_dashboard.youtube_upload_state import YouTubeUploadStateStore


class AutoYouTubeCleanupError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def cleanup_status(record: Mapping[str, Any], *, media_policy: MediaPathPolicy, now: Optional[datetime] = None) -> dict[str, Any]:
    """Derive a bounded UI status without mutating the ledger or filesystem."""
    cleanup = record["local_cleanup"]
    execution_state = cleanup.get("state", "pending")
    try:
        source = media_policy.resolve_media_path(record["media_path"], must_exist=False)
        local_exists = source.is_file() and source.stat().st_size == record["size_bytes"]
    except Exception:
        local_exists = False
    if execution_state == "completed":
        state = "removed"
    elif execution_state == "needs_attention":
        state = "needs_attention"
    elif execution_state in {"started", "canonical_done", "artifacts_done"}:
        state = "cleaning"
    elif not local_exists:
        state = "local_copy_missing"
    elif cleanup["policy"] == "manual":
        state = "disabled"
    elif record["state"] != "completed":
        state = "waiting_for_upload"
    elif cleanup["keep_local"]:
        state = "keep_local"
    else:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None or current.utcoffset() is None:
            raise AutoYouTubeCleanupError("invalid_clock")
        state = "due" if _utc(cleanup["cleanup_due_at"]) <= current.astimezone(timezone.utc) else "scheduled"
    return {
        "state": state, "policy": cleanup["policy"], "delay_hours": cleanup["delay_hours"],
        "cleanup_due_at": cleanup["cleanup_due_at"], "keep_local": cleanup["keep_local"],
        "cleaned_at": cleanup["cleaned_at"], "reason": cleanup.get("reason"),
        "can_keep_local": state in {"scheduled", "due"}, "can_resume_cleanup": state == "keep_local",
    }


class AutoYouTubeCleanupService:
    def __init__(self, *, state_store: YouTubeUploadStateStore, media_policy: MediaPathPolicy,
                 active_paths_provider: Optional[Callable[[], Iterable[Any]]] = None,
                 clock: Optional[Callable[[], datetime]] = None,
                 unlink: Optional[Callable[[Path], None]] = None,
                 should_stop: Optional[Callable[[], bool]] = None) -> None:
        self._state_store = state_store
        self._media_policy = media_policy
        self._active_paths_provider = active_paths_provider or (lambda: ())
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._unlink = unlink or (lambda path: path.unlink())
        self._should_stop = should_stop or (lambda: False)

    def set_keep_local(self, streamer: Any, twitch_vod_id: Any, *, media_path: Any, keep_local: bool) -> Mapping[str, Any]:
        record = self._state_store.get(streamer, twitch_vod_id)
        if record is None: raise AutoYouTubeCleanupError("ownership_not_found")
        try:
            requested = self._media_policy.resolve_media_path(media_path, must_exist=True, require_file=True)
            owned = self._media_policy.resolve_media_path(record["media_path"], must_exist=True, require_file=True)
            if requested != owned or requested.stat().st_size != record["size_bytes"]: raise AutoYouTubeCleanupError("ownership_mismatch")
        except AutoYouTubeCleanupError: raise
        except Exception as exc: raise AutoYouTubeCleanupError("local_media_invalid") from exc
        try: return self._state_store.set_keep_local(streamer, twitch_vod_id, keep_local=keep_local)
        except Exception as exc:
            code = str(exc)
            if code in {"keep_local_not_allowed", "upload_not_found"}: raise AutoYouTubeCleanupError(code) from exc
            raise AutoYouTubeCleanupError("cleanup_persistence_failed") from exc

    def status_for_jobs(self, jobs: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
        wanted = {str(job.get("id") or "") for job in jobs if job.get("origin") == "auto_youtube"}
        result: dict[str, dict[str, Any]] = {}
        if not wanted: return result
        for record in self._state_store.list_records().values():
            job_id = str(record.get("upload_job_id") or "")
            if job_id in wanted: result[job_id] = cleanup_status(record, media_policy=self._media_policy, now=self._now())
        return result

    def reconcile(self) -> dict[str, int]:
        result = {"cleaned": 0, "resumed": 0, "attention": 0, "pending": 0, "ignored": 0, "errors": 0}
        for record in self._state_store.list_records().values():
            if self._should_stop(): break
            before = record["local_cleanup"].get("state", "pending")
            try: outcome = self._reconcile_record(record)
            except Exception:
                result["errors"] += 1
                continue
            result[outcome] += 1
            if before in {"started", "canonical_done", "artifacts_done"} and outcome == "cleaned": result["resumed"] += 1
        return result

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None: raise AutoYouTubeCleanupError("invalid_clock")
        return value.astimezone(timezone.utc)

    def _reconcile_record(self, record: Mapping[str, Any]) -> str:
        cleanup = record["local_cleanup"]; state = cleanup.get("state", "pending")
        if state == "completed": return "ignored"
        if state == "needs_attention": return "attention"
        if state == "pending":
            if cleanup["policy"] != "automatic" or record["state"] != "completed" or cleanup["keep_local"]: return "ignored"
            if not cleanup.get("cleanup_due_at") or _utc(cleanup["cleanup_due_at"]) > self._now(): return "pending"
            record = self._preflight_and_begin(record)
            if record is None: return "attention"
        return self._resume(record)

    def _relative(self, path: Path) -> str: return path.relative_to(self._media_policy.media_root).as_posix()

    def _manifest_entry(self, path: Path) -> dict[str, Any]:
        stat = path.stat()
        return {"path": self._relative(path), "size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns}

    def _active_paths(self) -> set[Path]:
        result: set[Path] = set()
        for raw in self._active_paths_provider():
            try: result.add(self._media_policy.resolve_media_path(raw, must_exist=False))
            except Exception: continue
        return result

    def _mark_attention(self, record: Mapping[str, Any], reason: str, component: str) -> None:
        self._state_store.mark_local_cleanup_attention(record["streamer"], record["twitch_vod_id"], reason=reason, component=component)

    def _canonical_source(self, record: Mapping[str, Any]) -> Path:
        raw = self._media_policy.media_root.joinpath(*PurePosixPath(record["media_path"]).parts)
        if raw.is_symlink(): raise AutoYouTubeCleanupError("canonical_path_invalid")
        try: source = self._media_policy.resolve_media_path(raw, must_exist=True, require_file=True)
        except Exception as exc: raise AutoYouTubeCleanupError("canonical_missing_before_start") from exc
        internal = self._media_policy.media_root / ".auto-youtube"
        if source == internal or internal in source.parents: raise AutoYouTubeCleanupError("canonical_path_invalid")
        if not is_complete_video_file(source): raise AutoYouTubeCleanupError("canonical_path_invalid")
        if source.stat().st_size != record["size_bytes"]: raise AutoYouTubeCleanupError("canonical_identity_changed")
        return source

    def _generated_paths(self, record: Mapping[str, Any]) -> list[Path]:
        generated = [part for part in record["parts"] if part["source_kind"] == "generated"]
        if generated and len(generated) != len(record["parts"]): raise AutoYouTubeCleanupError("artifact_path_invalid")
        split = record.get("split") or {}
        prefix = (".auto-youtube", record["streamer"], record["twitch_vod_id"], str(split.get("generation_id") or ""))
        paths: list[Path] = []
        for part in generated:
            rel = PurePosixPath(part["media_path"])
            if tuple(rel.parts[:4]) != prefix or len(rel.parts) < 6 or rel.parts[4] != "parts": raise AutoYouTubeCleanupError("artifact_path_invalid")
            raw = self._media_policy.media_root.joinpath(*rel.parts)
            if raw.is_symlink(): raise AutoYouTubeCleanupError("artifact_path_invalid")
            try: path = self._media_policy.resolve_media_path(raw, must_exist=True, require_file=True)
            except Exception as exc: raise AutoYouTubeCleanupError("artifact_missing_before_start") from exc
            if path.stat().st_size != part["size_bytes"]: raise AutoYouTubeCleanupError("artifact_identity_changed")
            paths.append(path)
        return paths

    def _canonical_sidecars(self, source: Path) -> list[Path]:
        """Resolve only the four exact bundle sidecar conventions."""
        candidates = [
            source.with_suffix(".info.json"),
            source.with_suffix(".youtube.json"),
            source.with_suffix(".youtube-beschreibung.txt"),
            local_video_marker_path(source),
        ]
        found: list[Path] = []
        for raw in candidates:
            if raw.is_symlink():
                raise AutoYouTubeCleanupError("canonical_path_invalid")
            if not raw.exists():
                continue
            try:
                path = self._media_policy.resolve_media_path(raw, must_exist=True, require_file=True)
            except Exception as exc:
                raise AutoYouTubeCleanupError("canonical_path_invalid") from exc
            if path != raw.resolve():
                raise AutoYouTubeCleanupError("canonical_path_invalid")
            found.append(path)
        return found

    def _preflight_and_begin(self, record: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
        component = "canonical"
        try:
            source = self._canonical_source(record)
            if source in self._active_paths(): raise AutoYouTubeCleanupError("canonical_in_use")
            canonical = [self._manifest_entry(source)]
            canonical.extend(self._manifest_entry(path) for path in self._canonical_sidecars(source))
            component = "artifacts"; generated_paths = self._generated_paths(record); active = self._active_paths()
            if any(path in active for path in generated_paths): raise AutoYouTubeCleanupError("artifact_in_use")
            generated = [self._manifest_entry(path) for path in generated_paths]
            return self._state_store.begin_local_cleanup(record["streamer"], record["twitch_vod_id"], canonical_files=canonical, generated_files=generated)
        except AutoYouTubeCleanupError as exc:
            self._mark_attention(record, exc.code, component); return None
        except Exception:
            self._mark_attention(record, "filesystem_error", component); return None

    def _entry_path(self, entry: Mapping[str, Any], *, generated: bool, source: bool = False) -> Optional[Path]:
        rel = PurePosixPath(entry["path"]); raw = self._media_policy.media_root.joinpath(*rel.parts)
        if raw.is_symlink(): raise AutoYouTubeCleanupError("artifact_path_invalid" if generated else "canonical_path_invalid")
        if not raw.exists(): return None
        path = self._media_policy.resolve_media_path(raw, must_exist=True, require_file=True)
        if generated:
            if not rel.parts or rel.parts[0] != ".auto-youtube": raise AutoYouTubeCleanupError("artifact_path_invalid")
        elif rel.parts and rel.parts[0] == ".auto-youtube": raise AutoYouTubeCleanupError("canonical_path_invalid")
        if source and not is_complete_video_file(path): raise AutoYouTubeCleanupError("canonical_path_invalid")
        stat = path.stat()
        if stat.st_size != entry["size_bytes"] or stat.st_mtime_ns != entry["mtime_ns"]: raise AutoYouTubeCleanupError("artifact_identity_changed" if generated else "canonical_identity_changed")
        if path in self._active_paths(): raise AutoYouTubeCleanupError("artifact_in_use" if generated else "canonical_in_use")
        return path

    def _account_files(self, record: Mapping[str, Any], *, component: str) -> Mapping[str, Any]:
        generated = component == "artifacts"; entries = record["local_cleanup"]["generated_files" if generated else "canonical_files"]
        try:
            for index, entry in enumerate(entries):
                path = self._entry_path(entry, generated=generated, source=not generated and index == 0)
                if path is not None: self._unlink(path)
        except AutoYouTubeCleanupError as exc:
            self._mark_attention(record, exc.code, component); raise
        except Exception as exc:
            self._mark_attention(record, "filesystem_error", component); raise AutoYouTubeCleanupError("filesystem_error") from exc
        return self._state_store.mark_local_cleanup_component(record["streamer"], record["twitch_vod_id"], component=component)

    def _remove_empty_generated_directories(self, record: Mapping[str, Any]) -> None:
        internal = self._media_policy.media_root / ".auto-youtube"; parents = []
        for entry in record["local_cleanup"]["generated_files"]:
            parents.extend(self._media_policy.media_root.joinpath(*PurePosixPath(entry["path"]).parts).parents)
        for directory in sorted(set(parents), key=lambda item: len(item.parts), reverse=True):
            if directory == internal or internal not in directory.parents: continue
            try: directory.rmdir()
            except (FileNotFoundError, OSError): pass

    def _resume(self, record: Mapping[str, Any]) -> str:
        state = record["local_cleanup"]["state"]
        if state == "started": record = self._account_files(record, component="canonical"); state = record["local_cleanup"]["state"]
        if state == "canonical_done":
            record = self._account_files(record, component="artifacts"); self._remove_empty_generated_directories(record); state = record["local_cleanup"]["state"]
        if state == "artifacts_done":
            self._state_store.complete_local_cleanup(record["streamer"], record["twitch_vod_id"]); return "cleaned"
        return "attention"


class AutoYouTubeCleanupPeriodicCoordinator:
    """Add cleanup to the existing Auto VOD periodic loop without another thread."""
    def __init__(self, primary: Any, cleanup: AutoYouTubeCleanupService) -> None: self._primary = primary; self._cleanup = cleanup
    def run_once(self) -> Mapping[str, Any]:
        try:
            result = dict(self._primary.run_once())
        except Exception:
            self._cleanup.reconcile()
            raise
        result["auto_youtube_cleanup"] = self._cleanup.reconcile()
        return result
