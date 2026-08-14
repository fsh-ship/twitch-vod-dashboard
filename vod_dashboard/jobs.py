"""Thread-safe, process-local job state for the dashboard."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import subprocess
import threading
import time
from typing import Any, Callable, Dict, MutableMapping, Optional


Job = Dict[str, Any]
LogCallback = Callable[[str], None]
CounterGetter = Callable[[], int]
CounterSetter = Callable[[int], None]


@dataclass(frozen=True)
class DownloadWorkerDependencies:
    load_settings: Callable[[], Dict[str, Any]]
    clean_postprocess_mode: Callable[[Any], str]
    clean_rate_limit: Callable[[Any], str]
    append_log: Callable[[str, str], None]
    snapshot_video_files: Callable[[Dict[str, Any]], Dict[str, float]]
    new_video_files: Callable[[Dict[str, float], Dict[str, float]], list[Path]]
    recently_changed_video_files: Callable[..., list[Path]]
    prepare_manual_upload: Callable[..., Path]
    get_youtube_service: Callable[..., Any]
    upload_to_youtube: Callable[..., Optional[str]]
    build_download_command: Callable[[list[str], Dict[str, Any]], tuple[list[str], Path]]
    download_directory: Callable[[Dict[str, Any]], Path]
    popen: Callable[..., Any] = subprocess.Popen
    clock: Callable[[], float] = time.time


@dataclass(frozen=True)
class UploadWorkerDependencies:
    load_settings: Callable[[], Dict[str, Any]]
    append_log: Callable[[str, str], None]
    get_youtube_service: Callable[..., Any]
    safe_local_video_path: Callable[..., Path]
    upload_to_youtube: Callable[..., Optional[str]]


class JobManager:
    """Own the dashboard's in-memory job registry and bounded job logs."""

    MAX_LOG_ENTRIES = 500

    def __init__(
        self,
        registry: Optional[MutableMapping[str, Job]] = None,
        lock: Optional[threading.Lock] = None,
        counter: int = 0,
        now: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.jobs = registry if registry is not None else {}
        self.lock = lock if lock is not None else threading.Lock()
        self.counter = counter
        self._now = now or datetime.now

    @classmethod
    def compatible_with(
        cls,
        default: "JobManager",
        registry: MutableMapping[str, Job],
        lock: threading.Lock,
        counter: int,
    ) -> "JobManager":
        """Honor app-level aliases that legacy callers may replace."""
        if registry is default.jobs and lock is default.lock:
            return default
        return cls(registry=registry, lock=lock, counter=counter)

    def _next_job_id(
        self,
        counter_getter: Optional[CounterGetter] = None,
        counter_setter: Optional[CounterSetter] = None,
    ) -> str:
        if counter_getter is not None:
            self.counter = counter_getter()
        self.counter += 1
        if counter_setter is not None:
            counter_setter(self.counter)
        return str(self.counter)

    def _created_at(self) -> str:
        return self._now().strftime("%Y-%m-%d %H:%M:%S")

    def create_download_job(
        self,
        urls: list[str],
        label: str,
        *,
        counter_getter: Optional[CounterGetter] = None,
        counter_setter: Optional[CounterSetter] = None,
    ) -> str:
        """Create download-job state without starting its worker."""
        with self.lock:
            job_id = self._next_job_id(counter_getter, counter_setter)
            self.jobs[job_id] = {
                "id": job_id,
                "label": label,
                "status": "wartet",
                "created": self._created_at(),
                "urls": urls,
                "total_urls": len(urls),
                "item_statuses": ["wartet" for _ in urls],
                "log": [],
                "returncode": None,
            }
        return job_id

    def create_upload_job(
        self,
        paths: list[str],
        label: str,
        *,
        counter_getter: Optional[CounterGetter] = None,
        counter_setter: Optional[CounterSetter] = None,
    ) -> str:
        """Create YouTube-upload job state without starting its worker."""
        with self.lock:
            job_id = self._next_job_id(counter_getter, counter_setter)
            self.jobs[job_id] = {
                "id": job_id,
                "label": label,
                "status": "wartet",
                "created": self._created_at(),
                "urls": paths,
                "log": [],
                "returncode": None,
                "type": "youtube_upload",
            }
        return job_id

    def append_job_log(
        self,
        job_id: str,
        text: str,
        log_callback: Optional[LogCallback] = None,
    ) -> bool:
        """Append one entry, retaining exactly the newest 500 entries."""
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                return False
            job["log"].append(text.rstrip())
            job["log"] = job["log"][-self.MAX_LOG_ENTRIES :]
        if log_callback is not None:
            log_callback(f"Job {job_id}: {text.rstrip()}")
        return True

    def update_job(self, job_id: str, **changes: Any) -> bool:
        """Apply a generic state transition under the registry lock."""
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                return False
            job.update(changes)
        return True

    def get_job(self, job_id: str) -> Optional[Job]:
        """Return a detached snapshot of one job."""
        with self.lock:
            job = self.jobs.get(job_id)
            return deepcopy(job) if job is not None else None

    def snapshot_jobs(self, reverse: bool = False) -> list[Job]:
        """Return detached jobs in creation order, or newest first."""
        with self.lock:
            result = [deepcopy(job) for job in self.jobs.values()]
        if reverse:
            result.reverse()
        return result

    def start_job(self, job_id: str) -> list[str]:
        """Transition an existing job to running and return its queued items."""
        with self.lock:
            job = self.jobs[job_id]
            job["status"] = "läuft"
            return list(job["urls"])

    def set_returncode(self, job_id: str, returncode: int) -> None:
        with self.lock:
            self.jobs[job_id]["returncode"] = returncode

    def set_download_item_status(
        self, job_id: str, item_number: int, status: str
    ) -> bool:
        """Update one one-based download item without relying on bounded logs."""
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return False
            statuses = job.get("item_statuses")
            index = item_number - 1
            if not isinstance(statuses, list) or index < 0 or index >= len(statuses):
                return False
            statuses[index] = status
        return True

    def fail_unfinished_download_items(self, job_id: str) -> None:
        """Mark active and queued items failed after an unexpected worker exit."""
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return
            statuses = job.get("item_statuses")
            if not isinstance(statuses, list):
                return
            for index, status in enumerate(statuses):
                if status in {"wartet", "läuft"}:
                    statuses[index] = "fehler"

    def finish_job(self, job_id: str, returncode: int, status: str) -> None:
        with self.lock:
            self.jobs[job_id]["returncode"] = returncode
            self.jobs[job_id]["status"] = status

    def start_worker(
        self,
        target: Callable[[str], None],
        job_id: str,
        thread_factory: Optional[Callable[..., threading.Thread]] = None,
    ) -> threading.Thread:
        """Start one daemon worker for an already-created job."""
        factory = thread_factory or threading.Thread
        thread = factory(target=target, args=(job_id,), daemon=True)
        thread.start()
        return thread


def run_download_job(
    job_id: str,
    manager: JobManager,
    dependencies: DownloadWorkerDependencies,
) -> None:
    """Run the existing sequential Twitch download and post-processing lifecycle."""
    settings = dependencies.load_settings()
    postprocess_mode = dependencies.clean_postprocess_mode(
        settings.get("batch_postprocess_mode")
    )
    urls = manager.start_job(job_id)

    total = len(urls)
    failed = 0
    succeeded = 0
    deferred_items: list[Dict[str, Any]] = []

    dependencies.append_log(job_id, f"Batch started: {total} VOD(s)")
    rate_limit = dependencies.clean_rate_limit(settings.get("twitch_rate_limit"))
    dependencies.append_log(
        job_id, f"Twitch Download Rate Limit: {rate_limit or 'unlimited'}"
    )
    mode_label = (
        "download all, then post-process"
        if postprocess_mode == "after_all"
        else "post-process after each VOD"
    )
    dependencies.append_log(job_id, f"Batch Post-Processing: {mode_label}")
    dependencies.append_log(
        job_id,
        "YouTube Settings: "
        f"enabled={bool(settings.get('youtube_enabled'))}, "
        f"auto_upload={bool(settings.get('youtube_auto_upload'))}, "
        f"privacy={settings.get('youtube_privacy_status')}",
    )

    def detect_candidates(
        started_at: float, before_files: Dict[str, float]
    ) -> list[Path]:
        after_files = dependencies.snapshot_video_files(settings)
        candidates = dependencies.new_video_files(before_files, after_files)
        if not candidates:
            candidates = dependencies.recently_changed_video_files(
                settings, started_at, minutes_buffer=180
            )
        return candidates

    def prepare_manual_candidates(candidates: list[Path]) -> list[Path]:
        prepared_candidates = []
        for candidate in candidates:
            prepared_candidates.append(
                dependencies.prepare_manual_upload(
                    candidate, settings, job_id=job_id
                )
            )
        return prepared_candidates

    def handle_finished_download(
        url: str,
        started_at: float,
        before_files: Dict[str, float],
        known_candidates: Optional[list[Path]] = None,
    ) -> None:
        candidates = (
            known_candidates
            if known_candidates is not None
            else detect_candidates(started_at, before_files)
        )

        if not settings.get("youtube_enabled"):
            dependencies.append_log(
                job_id,
                "YouTube Auto-Upload skipped: YouTube uploads are disabled in Settings.",
            )
            if candidates:
                dependencies.append_log(
                    job_id,
                    f"Prepare for YouTube: preparing {len(candidates)} completed VOD file(s).",
                )
                prepare_manual_candidates(candidates)
            else:
                dependencies.append_log(
                    job_id,
                    "Prepare for YouTube: no new completed VOD file found to rename or describe.",
                )
            return

        if not settings.get("youtube_auto_upload"):
            dependencies.append_log(
                job_id,
                "YouTube Auto-Upload skipped: automatic upload after download is disabled.",
            )
            if candidates:
                dependencies.append_log(
                    job_id,
                    f"Prepare for YouTube: preparing {len(candidates)} completed VOD file(s).",
                )
                prepare_manual_candidates(candidates)
            else:
                dependencies.append_log(
                    job_id,
                    "Prepare for YouTube: no new completed VOD file found to prepare.",
                )
            return

        try:
            dependencies.get_youtube_service(settings, interactive=False)
            if candidates:
                dependencies.append_log(
                    job_id,
                    f"YouTube Auto-Upload: found {len(candidates)} completed VOD file(s).",
                )
            else:
                dependencies.append_log(
                    job_id,
                    "YouTube Auto-Upload: no matching completed VOD file found.",
                )
                return

            dependencies.append_log(
                job_id,
                "Prepare for YouTube: preparing completed VOD file(s) with YouTube filenames and descriptions.",
            )
            candidates = prepare_manual_candidates(candidates)
            if len(candidates) > 1:
                candidates = candidates[:1]
                dependencies.append_log(
                    job_id,
                    "YouTube Auto-Upload: only the newest matching file is uploaded for each VOD.",
                )

            for video_path in candidates:
                try:
                    dependencies.append_log(
                        job_id, f"YouTube Auto-Upload File: {video_path}"
                    )
                    dependencies.upload_to_youtube(
                        video_path, settings, job_id=job_id
                    )
                except Exception as exc:
                    dependencies.append_log(
                        job_id,
                        f"YouTube Upload failed for {video_path.name}: {exc}",
                    )
        except Exception as exc:
            dependencies.append_log(
                job_id, f"YouTube Auto-Upload did not start: {exc}"
            )

    try:
        if not urls:
            raise RuntimeError("The job contains no URLs.")

        for idx, url in enumerate(urls, start=1):
            manager.set_download_item_status(job_id, idx, "läuft")
            dependencies.append_log(job_id, "")
            dependencies.append_log(job_id, f"--- VOD {idx}/{total} ---")
            dependencies.append_log(job_id, f"URL: {url}")

            started_at = dependencies.clock()
            before_files = dependencies.snapshot_video_files(settings)
            cmd, list_path = dependencies.build_download_command([url], settings)

            dependencies.append_log(
                job_id,
                "Starting download with the Python module: python -m yt_dlp",
            )
            dependencies.append_log(job_id, "URLs in this step: 1")
            try:
                proc = dependencies.popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    cwd=str(dependencies.download_directory(settings)),
                )
                assert proc.stdout is not None
                for line in proc.stdout:
                    dependencies.append_log(job_id, line)
                rc = proc.wait()
                manager.set_returncode(job_id, rc)

                if rc == 0:
                    succeeded += 1
                    manager.set_download_item_status(job_id, idx, "fertig")
                    dependencies.append_log(
                        job_id, f"VOD {idx}/{total} download completed."
                    )
                    candidates = detect_candidates(started_at, before_files)
                    if postprocess_mode == "after_all":
                        deferred_items.append(
                            {
                                "url": url,
                                "idx": idx,
                                "started_at": started_at,
                                "before_files": before_files,
                                "candidates": candidates,
                            }
                        )
                        dependencies.append_log(
                            job_id,
                            f"Post-processing deferred: queued {len(candidates)} file(s).",
                        )
                    else:
                        handle_finished_download(
                            url,
                            started_at,
                            before_files,
                            known_candidates=candidates,
                        )
                else:
                    failed += 1
                    manager.set_download_item_status(job_id, idx, "fehler")
                    dependencies.append_log(
                        job_id,
                        f"VOD {idx}/{total} ended with error code {rc}. Continuing with the next VOD.",
                    )
            finally:
                try:
                    list_path.unlink(missing_ok=True)
                except Exception:
                    pass

        if postprocess_mode == "after_all" and deferred_items:
            dependencies.append_log(job_id, "")
            dependencies.append_log(
                job_id,
                f"--- Post-processing after all downloads: {len(deferred_items)} VOD(s) ---",
            )
            for item_no, item in enumerate(deferred_items, start=1):
                dependencies.append_log(
                    job_id,
                    f"Post-processing {item_no}/{len(deferred_items)} for VOD {item.get('idx')}/{total}",
                )
                handle_finished_download(
                    item["url"],
                    item["started_at"],
                    item["before_files"],
                    known_candidates=item.get("candidates") or [],
                )

        status = "fertig" if failed == 0 else "fehler"
        manager.finish_job(job_id, 0 if failed == 0 else 1, status)
        if failed == 0:
            dependencies.append_log(
                job_id, f"Batch completed: {succeeded}/{total} VOD(s) successful."
            )
        else:
            dependencies.append_log(
                job_id,
                f"Batch completed: {succeeded}/{total} successful, {failed} failed.",
            )
    except FileNotFoundError:
        manager.fail_unfinished_download_items(job_id)
        manager.finish_job(job_id, -1, "fehler")
        dependencies.append_log(
            job_id,
            "The yt-dlp Python module was not found. Install the dependencies from requirements.txt.",
        )
    except Exception as exc:
        manager.fail_unfinished_download_items(job_id)
        manager.finish_job(job_id, -2, "fehler")
        dependencies.append_log(job_id, f"Error: {exc}")


def run_upload_job(
    job_id: str,
    manager: JobManager,
    dependencies: UploadWorkerDependencies,
) -> None:
    """Run the existing sequential local YouTube upload lifecycle."""
    settings = dependencies.load_settings()
    paths = manager.start_job(job_id)
    dependencies.append_log(
        job_id, f"Starting local YouTube upload: {len(paths)} file(s)"
    )
    dependencies.append_log(
        job_id,
        "YouTube Settings: "
        f"enabled={bool(settings.get('youtube_enabled'))}, "
        f"privacy={settings.get('youtube_privacy_status')}, "
        f"playlist={settings.get('youtube_playlist_id') or 'none'}",
    )
    if not settings.get("youtube_enabled"):
        dependencies.append_log(
            job_id,
            "Note: YouTube uploads are disabled. A local upload will still be attempted if YouTube is connected.",
        )
    failed = 0
    uploaded = 0
    try:
        dependencies.get_youtube_service(settings, interactive=False)
        for raw in paths:
            try:
                path = dependencies.safe_local_video_path(raw, settings)
                dependencies.append_log(job_id, f"Uploading local VOD file: {path}")
                video_id = dependencies.upload_to_youtube(
                    path, settings, job_id=job_id
                )
                if video_id:
                    uploaded += 1
                else:
                    failed += 1
                    dependencies.append_log(
                        job_id, f"Upload completed without a video ID: {path.name}"
                    )
            except Exception as exc:
                failed += 1
                dependencies.append_log(
                    job_id, f"YouTube Upload failed for {Path(raw).name}: {exc}"
                )
        status = "fertig" if failed == 0 else "fehler"
        manager.finish_job(job_id, 0 if failed == 0 else 1, status)
        dependencies.append_log(
            job_id,
            f"Local upload completed: {uploaded} successful, {failed} failed.",
        )
    except Exception as exc:
        manager.finish_job(job_id, -2, "fehler")
        dependencies.append_log(
            job_id, f"Local YouTube upload did not start: {exc}"
        )
