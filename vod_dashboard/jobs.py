"""Thread-safe, process-local job state for the dashboard."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import threading
import time
from typing import Any, Callable, Dict, MutableMapping, Optional


Job = Dict[str, Any]
LogCallback = Callable[[str], None]
CounterGetter = Callable[[], int]
CounterSetter = Callable[[int], None]

UPLOAD_SPEED_EMA_ALPHA = 0.3
MIN_UPLOAD_ETA_SPEED_BPS = 1024.0
RECORDING_GRACEFUL_STOP_TIMEOUT_SECONDS = 30.0
RECORDING_TERMINATE_TIMEOUT_SECONDS = 15.0
RECORDING_STOP_RESULT_GRACEFUL = "graceful"
RECORDING_STOP_RESULT_TERMINATED = "terminated"
RECORDING_STOP_RESULT_KILLED = "killed"
RECORDING_STOP_RESULT_ALREADY_EXITED = "already_exited"
RECORDING_STOP_RESULT_FAILED = "failed"
RECORDING_ORIGINS = frozenset({"manual", "auto"})
MAX_RECORDING_ATTEMPT = 1000
ITEM_STATES = {
    "queued",
    "running",
    "stopping",
    "cancelling",
    "completed",
    "failed",
    "cancelled",
    "interrupted",
}
TERMINAL_ITEM_STATES = {"completed", "failed", "cancelled", "interrupted"}
LEGACY_TO_ITEM_STATE = {
    "wartet": "queued",
    "läuft": "running",
    "fertig": "completed",
    "fehler": "failed",
}
ITEM_STATE_TO_LEGACY = {
    "queued": "wartet",
    "running": "läuft",
    "stopping": "läuft",
    "cancelling": "läuft",
    "completed": "fertig",
    "failed": "fehler",
    "cancelled": "fertig",
    "interrupted": "fehler",
}
DOWNLOAD_DURATION_MARKER = "VOD-DASHBOARD-DURATION="
_FFMPEG_TIME_RE = re.compile(
    r"(?:^|\s)time=\s*(-?\d+:\d{2}:\d{2}(?:\.\d+)?)",
    re.IGNORECASE,
)
_FFMPEG_SPEED_RE = re.compile(r"(?:^|\s)speed=\s*([^\s]+)", re.IGNORECASE)
_CLASSIC_DOWNLOAD_RE = re.compile(
    r"\[download\]\s+(\d+(?:\.\d+)?)%.*?\bat\s+([^\s]+)\/s"
    r"(?:.*?\bETA\s+([^\s]+))?",
    re.IGNORECASE,
)


class RecordingConflictError(RuntimeError):
    """Raised when the exclusive recording lane is already reserved."""


class RecordingJobMetadataError(ValueError):
    """Raised for invalid internal recording origin/attempt metadata."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def validate_recording_job_metadata(
    origin: Any, attempt: Any
) -> tuple[str, int]:
    normalized_origin = str(origin or "").strip()
    if normalized_origin not in RECORDING_ORIGINS:
        raise RecordingJobMetadataError("invalid_origin")
    if (
        isinstance(attempt, bool)
        or not isinstance(attempt, int)
        or attempt < 1
        or attempt > MAX_RECORDING_ATTEMPT
    ):
        raise RecordingJobMetadataError("invalid_attempt")
    return normalized_origin, attempt


def _clock_value_seconds(value: Any) -> Optional[float]:
    parts = str(value or "").strip().split(":")
    if len(parts) != 3:
        return None
    try:
        hours, minutes, seconds = (
            int(parts[0]),
            int(parts[1]),
            float(parts[2]),
        )
    except (TypeError, ValueError, OverflowError):
        return None
    if hours < 0 or not 0 <= minutes < 60 or not 0 <= seconds < 60:
        return None
    total = hours * 3600 + minutes * 60 + seconds
    return total if math.isfinite(total) else None


def parse_ffmpeg_time_seconds(text: Any) -> Optional[float]:
    """Return ffmpeg's processed media timestamp from one progress line."""
    match = _FFMPEG_TIME_RE.search(str(text or ""))
    return _clock_value_seconds(match.group(1)) if match else None


def parse_ffmpeg_speed_multiplier(text: Any) -> Optional[float]:
    """Return ffmpeg's wall-clock speed multiplier, including truthful zero."""
    match = _FFMPEG_SPEED_RE.search(str(text or ""))
    if not match:
        return None
    raw = match.group(1)
    if not raw.lower().endswith("x"):
        return None
    try:
        speed = float(raw[:-1])
    except (TypeError, ValueError, OverflowError):
        return None
    return speed if math.isfinite(speed) and speed >= 0 else None


def ffmpeg_download_metrics(
    text: Any, total_duration_seconds: Any = None
) -> Dict[str, Optional[float]]:
    """Calculate truthful ffmpeg/HLS progress without estimating transfer rate."""
    processed = parse_ffmpeg_time_seconds(text)
    speed = parse_ffmpeg_speed_multiplier(text)
    progress: Optional[float] = None
    eta: Optional[float] = None
    try:
        duration = float(total_duration_seconds)
    except (TypeError, ValueError, OverflowError):
        duration = 0.0
    if processed is not None and math.isfinite(duration) and duration > 0:
        progress = min(100.0, max(0.0, processed * 100.0 / duration))
        remaining = max(0.0, duration - processed)
        if speed is not None and speed > 0 and remaining > 0:
            eta = remaining / speed
    return {
        "processed_seconds": processed,
        "speed_multiplier": speed,
        "progress": progress,
        "eta_seconds": eta,
    }


def download_process_group_options(platform_name: Optional[str] = None) -> Dict[str, Any]:
    """Return portable ownership options for the yt-dlp/ffmpeg process tree."""
    platform = os.name if platform_name is None else platform_name
    if platform == "posix":
        return {"start_new_session": True}
    creation_flag = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return {"creationflags": creation_flag} if creation_flag else {}


def terminate_download_process_tree(
    process: Any,
    *,
    platform_name: Optional[str] = None,
    graceful_timeout: float = 8.0,
    terminate_timeout: float = 3.0,
) -> None:
    """Gracefully stop and reap an owned yt-dlp tree, escalating if needed."""
    if process is None or process.poll() is not None:
        if process is not None:
            process.wait()
        return
    platform = os.name if platform_name is None else platform_name
    if platform == "posix":
        process_group = os.getpgid(process.pid)
        os.killpg(process_group, signal.SIGINT)
        try:
            process.wait(timeout=graceful_timeout)
            return
        except subprocess.TimeoutExpired:
            os.killpg(process_group, signal.SIGTERM)
        try:
            process.wait(timeout=terminate_timeout)
            return
        except subprocess.TimeoutExpired:
            os.killpg(process_group, getattr(signal, "SIGKILL", 9))
            process.wait()
            return

    control_break = getattr(signal, "CTRL_BREAK_EVENT", None)
    if control_break is not None:
        try:
            process.send_signal(control_break)
            process.wait(timeout=graceful_timeout)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T"],
            capture_output=True,
            timeout=terminate_timeout,
            check=False,
        )
        process.wait(timeout=terminate_timeout)
        return
    except (OSError, subprocess.TimeoutExpired):
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            timeout=terminate_timeout,
            check=False,
        )
        process.wait()


def terminate_recording_process_tree(
    process: Any,
    *,
    platform_name: Optional[str] = None,
    graceful_timeout: float = RECORDING_GRACEFUL_STOP_TIMEOUT_SECONDS,
    terminate_timeout: float = RECORDING_TERMINATE_TIMEOUT_SECONDS,
    log_callback: Optional[LogCallback] = None,
) -> str:
    """Stop an owned live-recording tree with recording-specific timeouts."""
    if process is None or process.poll() is not None:
        if process is not None:
            process.wait()
        return RECORDING_STOP_RESULT_ALREADY_EXITED

    def log(message: str) -> None:
        if log_callback is not None:
            log_callback(message)

    platform = os.name if platform_name is None else platform_name
    if platform == "posix":
        process_group = os.getpgid(process.pid)
        os.killpg(process_group, signal.SIGINT)
        try:
            process.wait(timeout=graceful_timeout)
            return RECORDING_STOP_RESULT_GRACEFUL
        except subprocess.TimeoutExpired:
            log("Recording stop escalated to SIGTERM.")
            os.killpg(process_group, signal.SIGTERM)
        try:
            process.wait(timeout=terminate_timeout)
            return RECORDING_STOP_RESULT_TERMINATED
        except subprocess.TimeoutExpired:
            log("Recording stop escalated to SIGKILL.")
            os.killpg(process_group, getattr(signal, "SIGKILL", 9))
            process.wait(timeout=terminate_timeout)
            return RECORDING_STOP_RESULT_KILLED

    control_break = getattr(signal, "CTRL_BREAK_EVENT", None)
    if control_break is not None:
        try:
            process.send_signal(control_break)
            process.wait(timeout=graceful_timeout)
            return RECORDING_STOP_RESULT_GRACEFUL
        except (OSError, subprocess.TimeoutExpired):
            log("Recording stop escalated to taskkill /T.")

    try:
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T"],
            capture_output=True,
            timeout=terminate_timeout,
            check=False,
        )
        process.wait(timeout=terminate_timeout)
        return RECORDING_STOP_RESULT_TERMINATED
    except (OSError, subprocess.TimeoutExpired):
        log("Recording stop escalated to taskkill /T /F.")
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            timeout=terminate_timeout,
            check=False,
        )
        process.wait(timeout=terminate_timeout)
        return RECORDING_STOP_RESULT_KILLED


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
    enqueue_upload_job: Optional[Callable[[list[str], str], str]] = None


@dataclass(frozen=True)
class RecordingWorkerDependencies:
    load_settings: Callable[[], Dict[str, Any]]
    append_log: Callable[[str, str], None]
    build_recording_command: Callable[..., list[str]]
    download_directory: Callable[[Dict[str, Any]], Path]
    resolve_completed_output: Callable[[Any, Dict[str, Any]], str]
    output_marker: str
    popen: Callable[..., Any] = subprocess.Popen
    terminate_process: Callable[..., str] = terminate_recording_process_tree
    thread_factory: Callable[..., threading.Thread] = threading.Thread


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
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self.jobs = registry if registry is not None else {}
        self.lock = lock if lock is not None else threading.Lock()
        self.counter = counter
        self._now = now or datetime.now
        self._clock = clock or time.time
        self._upload_measurements: Dict[tuple[str, int], Dict[str, Any]] = {}
        self._condition = threading.Condition(self.lock)
        self._lane_paused = {"download": False, "youtube_upload": False}
        self._lane_stop_after_current = {
            "download": False,
            "youtube_upload": False,
        }
        self._lane_active: Dict[str, Optional[tuple[str, str]]] = {
            "download": None,
            "youtube_upload": None,
            "recording": None,
        }
        self._cancel_events: Dict[tuple[str, str], threading.Event] = {}
        self._download_processes: Dict[tuple[str, str], Any] = {}
        self._recording_processes: Dict[str, Any] = {}
        self._recording_stop_events: Dict[str, threading.Event] = {}
        self._recording_termination_started: set[str] = set()
        self._recording_stop_results: Dict[str, str] = {}
        self._recording_termination_done: Dict[str, threading.Event] = {}

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

    @staticmethod
    def _lane_for_job(job: Job) -> str:
        if job.get("type") == "recording":
            return "recording"
        return (
            "youtube_upload"
            if job.get("type") == "youtube_upload"
            else "download"
        )

    @staticmethod
    def _item_ids(job_id: str, count: int) -> list[str]:
        return [f"{job_id}-item-{index + 1}" for index in range(count)]

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
            item_ids = self._item_ids(job_id, len(urls))
            self.jobs[job_id] = {
                "id": job_id,
                "label": label,
                "status": "wartet",
                "state": "queued",
                "created": self._created_at(),
                "urls": urls,
                "total_urls": len(urls),
                "item_ids": item_ids,
                "item_states": ["queued" for _ in urls],
                "item_statuses": ["wartet" for _ in urls],
                "item_progress": [None for _ in urls],
                "item_processed_seconds": [None for _ in urls],
                "item_speed_multiplier": [None for _ in urls],
                "item_speed_label": ["" for _ in urls],
                "item_eta_seconds": [None for _ in urls],
                "item_updated_at": [None for _ in urls],
                "item_total_duration_seconds": [None for _ in urls],
                "item_resolved": [False for _ in urls],
                "item_failure_kinds": ["" for _ in urls],
                "item_retry_job_ids": ["" for _ in urls],
                "stop_after_current": False,
                "log": [],
                "returncode": None,
            }
        return job_id

    def create_recording_job(
        self,
        streamer: str,
        *,
        stream_id: str = "",
        title: str = "",
        live_started_at: Optional[str] = None,
        quality: str = "source/best",
        output_name: str = "",
        origin: str = "manual",
        attempt: int = 1,
        counter_getter: Optional[CounterGetter] = None,
        counter_setter: Optional[CounterSetter] = None,
    ) -> str:
        """Create one exclusive process-local recording job."""
        canonical_login = str(streamer or "").strip()
        if not canonical_login:
            raise ValueError("A Twitch streamer is required.")
        normalized_origin, normalized_attempt = (
            validate_recording_job_metadata(origin, attempt)
        )
        with self.lock:
            for existing in self.jobs.values():
                if existing.get("type") != "recording":
                    continue
                if existing.get("state") in {"queued", "running", "stopping"}:
                    raise RecordingConflictError(
                        "A Twitch recording is already queued or active."
                    )

            job_id = self._next_job_id(counter_getter, counter_setter)
            item_id = self._item_ids(job_id, 1)[0]
            created_at = self._created_at()
            self.jobs[job_id] = {
                "id": job_id,
                "label": f"Live recording: {canonical_login}",
                "type": "recording",
                "streamer": canonical_login,
                "origin": normalized_origin,
                "attempt": normalized_attempt,
                "stream_id": str(stream_id or "").strip(),
                "title": str(title or "").strip(),
                "live_started_at": live_started_at,
                "quality": str(quality or "source/best").strip(),
                "output_name": str(output_name or "").strip(),
                "output_path": None,
                "output_complete": False,
                "status": "wartet",
                "state": "queued",
                "created": created_at,
                "created_at": created_at,
                "updated_at": created_at,
                "urls": [canonical_login],
                "total_urls": 1,
                "item_ids": [item_id],
                "item_states": ["queued"],
                "item_statuses": ["wartet"],
                "item_resolved": [False],
                "item_failure_kinds": [""],
                "item_retry_job_ids": [""],
                "recorded_seconds": 0.0,
                "stop_requested": False,
                "completion_reason": "",
                "log": [],
                "returncode": None,
            }
        return job_id

    def create_upload_job(
        self,
        paths: list[str],
        label: str,
        *,
        playlist_id: Optional[str] = None,
        item_metadata: Optional[list[Dict[str, Any]]] = None,
        counter_getter: Optional[CounterGetter] = None,
        counter_setter: Optional[CounterSetter] = None,
    ) -> str:
        """Create YouTube-upload job state without starting its worker."""
        with self.lock:
            job_id = self._next_job_id(counter_getter, counter_setter)
            item_ids = self._item_ids(job_id, len(paths))
            job = {
                "id": job_id,
                "label": label,
                "status": "wartet",
                "state": "queued",
                "created": self._created_at(),
                "urls": paths,
                "item_ids": item_ids,
                "item_states": ["queued" for _ in paths],
                "item_statuses": ["wartet" for _ in paths],
                "item_progress": [None for _ in paths],
                "item_bytes_uploaded": [None for _ in paths],
                "item_total_bytes": [None for _ in paths],
                "item_bytes_per_second": [None for _ in paths],
                "item_eta_seconds": [None for _ in paths],
                "item_updated_at": [None for _ in paths],
                "item_errors": ["" for _ in paths],
                "item_resolved": [False for _ in paths],
                "item_failure_kinds": ["" for _ in paths],
                "item_retry_job_ids": ["" for _ in paths],
                "item_metadata": list(item_metadata or [{} for _ in paths]),
                "stop_after_current": False,
                "log": [],
                "returncode": None,
                "type": "youtube_upload",
            }
            if playlist_id is not None:
                job["playlist_id"] = str(playlist_id or "").strip()
            self.jobs[job_id] = job
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
            if job.get("type") == "youtube_upload":
                match = re.search(r"YouTube Upload\s+.+?:\s*(\d+)%", text, re.I)
                if match:
                    statuses = job.get("item_statuses") or []
                    progress = job.get("item_progress") or []
                    uploaded = job.get("item_bytes_uploaded") or []
                    for index, status in enumerate(statuses):
                        if status == "l\u00e4uft" and index < len(progress):
                            if index >= len(uploaded) or uploaded[index] is None:
                                progress[index] = max(
                                    0, min(100, int(match.group(1)))
                                )
                            break
            elif job.get("type") != "recording":
                self._update_download_progress_from_log(job, text)
        if log_callback is not None:
            log_callback(f"Job {job_id}: {text.rstrip()}")
        return True

    @staticmethod
    def _download_metric_lists(job: Job) -> Dict[str, list[Any]]:
        statuses = job.get("item_statuses")
        count = len(statuses) if isinstance(statuses, list) else 0
        defaults: Dict[str, Any] = {
            "item_progress": None,
            "item_processed_seconds": None,
            "item_speed_multiplier": None,
            "item_speed_label": "",
            "item_eta_seconds": None,
            "item_updated_at": None,
            "item_total_duration_seconds": None,
        }
        result: Dict[str, list[Any]] = {}
        for key, default in defaults.items():
            values = job.get(key)
            if not isinstance(values, list):
                values = [default for _ in range(count)]
                job[key] = values
            elif len(values) < count:
                values.extend(default for _ in range(count - len(values)))
            result[key] = values
        return result

    def _update_download_progress_from_log(self, job: Job, text: str) -> None:
        statuses = job.get("item_statuses")
        if not isinstance(statuses, list):
            return
        try:
            index = statuses.index("l\u00e4uft")
        except ValueError:
            return
        metrics = self._download_metric_lists(job)

        marker_match = re.search(
            rf"{re.escape(DOWNLOAD_DURATION_MARKER)}\s*([^\s]+)",
            str(text or ""),
            re.IGNORECASE,
        )
        if marker_match:
            try:
                duration = float(marker_match.group(1))
            except (TypeError, ValueError, OverflowError):
                duration = 0.0
            metrics["item_total_duration_seconds"][index] = (
                duration if math.isfinite(duration) and duration > 0 else None
            )
            return

        duration = metrics["item_total_duration_seconds"][index]
        ffmpeg = ffmpeg_download_metrics(text, duration)
        has_ffmpeg_time = ffmpeg["processed_seconds"] is not None
        has_ffmpeg_speed = "speed=" in str(text or "").lower()
        if has_ffmpeg_time or has_ffmpeg_speed:
            if has_ffmpeg_time:
                metrics["item_processed_seconds"][index] = round(
                    float(ffmpeg["processed_seconds"]), 3
                )
                metrics["item_progress"][index] = (
                    round(float(ffmpeg["progress"]), 1)
                    if ffmpeg["progress"] is not None
                    else None
                )
            if has_ffmpeg_speed:
                speed = ffmpeg["speed_multiplier"]
                metrics["item_speed_multiplier"][index] = speed
                metrics["item_speed_label"][index] = (
                    f"{speed:g}x" if speed is not None else ""
                )
            metrics["item_eta_seconds"][index] = (
                int(math.ceil(float(ffmpeg["eta_seconds"])))
                if ffmpeg["eta_seconds"] is not None
                else None
            )
            metrics["item_updated_at"][index] = float(self._clock())
            return

        classic = _CLASSIC_DOWNLOAD_RE.search(str(text or ""))
        if not classic:
            return
        metrics["item_progress"][index] = max(
            0.0, min(100.0, round(float(classic.group(1)), 1))
        )
        metrics["item_speed_label"][index] = f"{classic.group(2)}/s"
        raw_eta = classic.group(3) or ""
        eta = _clock_value_seconds(
            raw_eta if raw_eta.count(":") == 2 else f"00:{raw_eta}"
        )
        metrics["item_eta_seconds"][index] = (
            int(math.ceil(eta)) if eta is not None and eta > 0 else None
        )
        metrics["item_updated_at"][index] = float(self._clock())

    def update_job(self, job_id: str, **changes: Any) -> bool:
        """Apply a generic state transition under the registry lock."""
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                return False
            job.update(changes)
        return True

    def _ensure_control_lists_locked(self, job: Job) -> None:
        sources = job.get("urls") if isinstance(job.get("urls"), list) else []
        count = len(sources)
        job_id = str(job.get("id") or "job")
        ids = job.get("item_ids")
        if not isinstance(ids, list):
            ids = self._item_ids(job_id, count)
            job["item_ids"] = ids
        elif len(ids) < count:
            ids.extend(
                f"{job_id}-item-{index + 1}"
                for index in range(len(ids), count)
            )
        statuses = job.get("item_statuses")
        if not isinstance(statuses, list):
            statuses = ["wartet" for _ in range(count)]
            job["item_statuses"] = statuses
        elif len(statuses) < count:
            statuses.extend("wartet" for _ in range(count - len(statuses)))
        states = job.get("item_states")
        if not isinstance(states, list):
            states = [
                LEGACY_TO_ITEM_STATE.get(
                    statuses[index] if index < len(statuses) else "wartet",
                    "queued",
                )
                for index in range(count)
            ]
            job["item_states"] = states
        elif len(states) < count:
            states.extend("queued" for _ in range(count - len(states)))
        for key in ("item_failure_kinds", "item_retry_job_ids"):
            values = job.get(key)
            if not isinstance(values, list):
                job[key] = ["" for _ in range(count)]
            elif len(values) < count:
                values.extend("" for _ in range(count - len(values)))
        job.setdefault("stop_after_current", False)

    def _item_index_locked(self, job: Job, item_id: str) -> Optional[int]:
        self._ensure_control_lists_locked(job)
        try:
            return job["item_ids"].index(str(item_id))
        except ValueError:
            return None

    def _set_item_state_locked(
        self,
        job: Job,
        index: int,
        state: str,
        *,
        failure_kind: str = "",
    ) -> None:
        if state not in ITEM_STATES:
            raise ValueError(f"Unsupported Queue item state: {state}")
        self._ensure_control_lists_locked(job)
        job["item_states"][index] = state
        job["item_statuses"][index] = ITEM_STATE_TO_LEGACY[state]
        job["item_failure_kinds"][index] = (
            str(failure_kind or "") if state == "failed" else ""
        )

    def _recompute_job_state_locked(self, job: Job) -> None:
        self._ensure_control_lists_locked(job)
        states = list(job.get("item_states") or [])
        if any(state == "stopping" for state in states):
            job["state"] = "stopping"
            job["status"] = "läuft"
        elif any(state in {"running", "cancelling"} for state in states):
            job["state"] = "running"
            job["status"] = "läuft"
        elif any(state == "queued" for state in states):
            job["state"] = "queued"
            job["status"] = "wartet"
        elif any(state == "failed" for state in states):
            job["state"] = "failed"
            job["status"] = "fehler"
        elif any(state == "interrupted" for state in states):
            job["state"] = "interrupted"
            job["status"] = "fehler"
        elif states and all(state == "cancelled" for state in states):
            job["state"] = "cancelled"
            job["status"] = "fertig"
        else:
            job["state"] = "completed"
            job["status"] = "fertig"

    def _item_capabilities_locked(self, job: Job, index: int) -> Dict[str, Any]:
        state = job["item_states"][index]
        failure_kind = job["item_failure_kinds"][index]
        retry_job_id = job["item_retry_job_ids"][index]
        if job.get("type") == "recording":
            return {
                "can_cancel": False,
                "can_remove": False,
                "can_retry": False,
                "can_resolve": False,
                "can_stop_after_current": False,
                "retry_pending": False,
                "retry_job_id": "",
                "retry_block_reason": "",
            }
        uncertain = (
            job.get("type") == "youtube_upload"
            and failure_kind == "uncertain"
        )
        return {
            "can_cancel": state == "running",
            "can_remove": state == "queued",
            "can_retry": state == "failed" and not retry_job_id and not uncertain,
            "can_resolve": state == "failed",
            "can_stop_after_current": state == "running",
            "retry_pending": retry_job_id == "__pending__",
            "retry_job_id": "" if retry_job_id == "__pending__" else retry_job_id,
            "retry_block_reason": (
                "YouTube may have accepted this upload. Verify it in YouTube Studio before starting a new upload."
                if uncertain
                else ""
            ),
        }

    def queue_controls_snapshot(self) -> Dict[str, Dict[str, bool]]:
        with self.lock:
            return {
                lane: {
                    "queue_paused": bool(self._lane_paused[lane]),
                    "stop_after_current": bool(
                        self._lane_stop_after_current[lane]
                    ),
                    "has_active_item": self._lane_active[lane] is not None,
                }
                for lane in self._lane_paused
            }

    def pause_queue(self, lane: str, *, stop_after_current: bool = False) -> bool:
        if lane not in self._lane_paused:
            return False
        with self._condition:
            self._lane_paused[lane] = True
            if stop_after_current:
                self._lane_stop_after_current[lane] = True
            active = self._lane_active.get(lane)
            if active:
                job = self.jobs.get(active[0])
                if job:
                    job["stop_after_current"] = bool(stop_after_current)
            self._condition.notify_all()
        return True

    def resume_queue(self, lane: str) -> bool:
        if lane not in self._lane_paused:
            return False
        with self._condition:
            self._lane_paused[lane] = False
            self._lane_stop_after_current[lane] = False
            for job in self.jobs.values():
                if self._lane_for_job(job) == lane:
                    job["stop_after_current"] = False
            self._condition.notify_all()
        return True

    def request_stop_after_current(self, job_id: str, item_id: str) -> bool:
        with self._condition:
            job = self.jobs.get(job_id)
            if job is None:
                return False
            index = self._item_index_locked(job, item_id)
            if index is None or job["item_states"][index] != "running":
                return False
            lane = self._lane_for_job(job)
            if lane not in self._lane_paused:
                return False
            if self._lane_active.get(lane) != (str(job_id), str(item_id)):
                return False
            self._lane_paused[lane] = True
            self._lane_stop_after_current[lane] = True
            job["stop_after_current"] = True
            self._condition.notify_all()
            return True

    def _job_is_next_for_lane_locked(self, job_id: str, lane: str) -> bool:
        for candidate_id, candidate in self.jobs.items():
            if self._lane_for_job(candidate) != lane:
                continue
            self._ensure_control_lists_locked(candidate)
            if any(state == "queued" for state in candidate["item_states"]):
                return str(candidate_id) == str(job_id)
        return True

    def claim_next_item(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Atomically claim the next eligible item for one process-local lane."""
        with self._condition:
            while True:
                job = self.jobs.get(job_id)
                if job is None:
                    return None
                self._ensure_control_lists_locked(job)
                queued = [
                    index
                    for index, state in enumerate(job["item_states"])
                    if state == "queued"
                ]
                if not queued:
                    self._recompute_job_state_locked(job)
                    return None
                lane = self._lane_for_job(job)
                if (
                    self._lane_paused[lane]
                    or self._lane_active[lane] is not None
                    or not self._job_is_next_for_lane_locked(job_id, lane)
                ):
                    self._recompute_job_state_locked(job)
                    self._condition.wait()
                    continue
                index = queued[0]
                item_id = job["item_ids"][index]
                self._set_item_state_locked(job, index, "running")
                self._lane_active[lane] = (str(job_id), str(item_id))
                self._cancel_events.setdefault(
                    (str(job_id), str(item_id)), threading.Event()
                )
                self._recompute_job_state_locked(job)
                return {
                    "job_id": str(job_id),
                    "item_id": str(item_id),
                    "index": index,
                    "item_number": index + 1,
                    "value": job["urls"][index],
                    "lane": lane,
                }

    def finish_claimed_item(
        self,
        job_id: str,
        item_id: str,
        state: str,
        *,
        failure_kind: str = "",
    ) -> bool:
        with self._condition:
            job = self.jobs.get(job_id)
            if job is None:
                return False
            index = self._item_index_locked(job, item_id)
            if index is None:
                return False
            self._set_item_state_locked(
                job, index, state, failure_kind=failure_kind
            )
            if job.get("type") == "youtube_upload" and state == "completed":
                progress = job.get("item_progress")
                if isinstance(progress, list) and index < len(progress):
                    progress[index] = 100
            lane = self._lane_for_job(job)
            if self._lane_active.get(lane) == (str(job_id), str(item_id)):
                self._lane_active[lane] = None
            self._download_processes.pop((str(job_id), str(item_id)), None)
            self._recompute_job_state_locked(job)
            self._condition.notify_all()
        return True

    def remove_queued_item(self, job_id: str, item_id: str) -> bool:
        with self._condition:
            job = self.jobs.get(job_id)
            if job is None or job.get("type") == "recording":
                return False
            index = self._item_index_locked(job, item_id)
            if index is None:
                return False
            state = job["item_states"][index]
            if state == "cancelled":
                return True
            if state != "queued":
                return False
            self._set_item_state_locked(job, index, "cancelled")
            self._recompute_job_state_locked(job)
            self._condition.notify_all()
        return True

    def request_cancel_item(self, job_id: str, item_id: str) -> Optional[str]:
        with self._condition:
            job = self.jobs.get(job_id)
            if job is None or job.get("type") == "recording":
                return None
            index = self._item_index_locked(job, item_id)
            if index is None:
                return None
            state = job["item_states"][index]
            if state == "cancelled":
                return self._lane_for_job(job)
            if state not in {"running", "cancelling"}:
                return None
            self._set_item_state_locked(job, index, "cancelling")
            event = self._cancel_events.setdefault(
                (str(job_id), str(item_id)), threading.Event()
            )
            event.set()
            self._recompute_job_state_locked(job)
            self._condition.notify_all()
            return self._lane_for_job(job)

    def is_cancel_requested(self, job_id: str, item_id: str) -> bool:
        with self.lock:
            event = self._cancel_events.get((str(job_id), str(item_id)))
            return bool(event and event.is_set())

    def item_state(self, job_id: str, item_id: str) -> Optional[str]:
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return None
            index = self._item_index_locked(job, item_id)
            return job["item_states"][index] if index is not None else None

    def claim_recording_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Claim the sole item in the non-queuing recording lane."""
        with self._condition:
            job = self.jobs.get(job_id)
            if job is None or job.get("type") != "recording":
                return None
            self._ensure_control_lists_locked(job)
            item_state = job["item_states"][0]
            if item_state not in {"queued", "stopping"}:
                return None
            if self._lane_active["recording"] is not None:
                return None
            item_id = str(job["item_ids"][0])
            stop_event = self._recording_stop_events.setdefault(
                str(job_id), threading.Event()
            )
            self._set_item_state_locked(
                job, 0, "stopping" if stop_event.is_set() else "running"
            )
            self._lane_active["recording"] = (str(job_id), item_id)
            self._recompute_job_state_locked(job)
            job["updated_at"] = self._created_at()
            return {
                "job_id": str(job_id),
                "item_id": item_id,
                "index": 0,
                "item_number": 1,
                "value": job["streamer"],
                "lane": "recording",
            }

    def is_recording_active(self) -> bool:
        with self.lock:
            return self._lane_active["recording"] is not None

    def has_pending_or_active_recording(self) -> bool:
        with self.lock:
            return any(
                job.get("type") == "recording"
                and job.get("state") in {"queued", "running", "stopping"}
                for job in self.jobs.values()
            )

    def register_recording_process(
        self, job_id: str, item_id: str, process: Any
    ) -> Optional[bool]:
        """Register the process owned by one active recording job."""
        with self.lock:
            key = (str(job_id), str(item_id))
            job = self.jobs.get(str(job_id))
            if (
                job is None
                or job.get("type") != "recording"
                or self._lane_active["recording"] != key
            ):
                return None
            self._recording_processes[str(job_id)] = process
            event = self._recording_stop_events.get(str(job_id))
            return bool(event and event.is_set())

    def recording_process(self, job_id: str) -> Any:
        with self.lock:
            return self._recording_processes.get(str(job_id))

    def clear_recording_process(self, job_id: str) -> None:
        with self.lock:
            self._recording_processes.pop(str(job_id), None)

    def request_recording_stop(self, job_id: str) -> bool:
        """Record an idempotent internal stop request."""
        with self._condition:
            job = self.jobs.get(str(job_id))
            if job is None or job.get("type") != "recording":
                return False
            self._ensure_control_lists_locked(job)
            state = job["item_states"][0]
            if (
                state == "completed"
                and job.get("completion_reason") == "stopped_by_user"
            ):
                return True
            if state not in {"queued", "running", "stopping"}:
                return False
            event = self._recording_stop_events.setdefault(
                str(job_id), threading.Event()
            )
            event.set()
            job["stop_requested"] = True
            self._set_item_state_locked(job, 0, "stopping")
            self._recompute_job_state_locked(job)
            job["updated_at"] = self._created_at()
            self._condition.notify_all()
            return True

    def start_recording_termination(
        self,
        job_id: str,
        *,
        terminator: Callable[..., str] = terminate_recording_process_tree,
        thread_factory: Callable[..., threading.Thread] = threading.Thread,
    ) -> bool:
        """Start at most one asynchronous process-tree stop for a recording."""
        key = str(job_id)
        with self.lock:
            stop_event = self._recording_stop_events.get(key)
            process = self._recording_processes.get(key)
            if not stop_event or not stop_event.is_set() or process is None:
                return False
            if key in self._recording_termination_started:
                return True
            self._recording_termination_started.add(key)
            done = self._recording_termination_done.setdefault(
                key, threading.Event()
            )

        def terminate_owned_tree() -> None:
            result = RECORDING_STOP_RESULT_FAILED
            try:
                result = terminator(
                    process,
                    log_callback=lambda message: self.append_job_log(
                        key, message
                    ),
                )
            except Exception:
                self.append_job_log(
                    key, "Recording process-tree stop failed."
                )
            finally:
                with self._condition:
                    self._recording_stop_results[key] = result
                    done.set()
                    self._condition.notify_all()

        try:
            thread = thread_factory(target=terminate_owned_tree, daemon=True)
            thread.start()
        except Exception:
            with self._condition:
                self._recording_stop_results[key] = RECORDING_STOP_RESULT_FAILED
                done.set()
                self._condition.notify_all()
            return False
        return True

    def wait_recording_stop_result(
        self, job_id: str, timeout: Optional[float] = None
    ) -> Optional[str]:
        key = str(job_id)
        with self.lock:
            done = self._recording_termination_done.get(key)
        if done is not None:
            done.wait(
                timeout=(
                    None if timeout is None else max(0.0, float(timeout))
                )
            )
        with self.lock:
            return self._recording_stop_results.get(key)

    def is_recording_stop_requested(self, job_id: str) -> bool:
        with self.lock:
            event = self._recording_stop_events.get(str(job_id))
            return bool(event and event.is_set())

    def stop_recording_for_shutdown(self, timeout: float = 50.0) -> bool:
        """Reuse the recording stop lifecycle and wait within a bounded budget."""
        with self.lock:
            active_job_id = next(
                (
                    str(job_id)
                    for job_id, job in self.jobs.items()
                    if job.get("type") == "recording"
                    and job.get("state") in {"queued", "running", "stopping"}
                ),
                None,
            )
        if active_job_id is None:
            return True

        self.request_recording_stop(active_job_id)
        self.start_recording_termination(active_job_id)
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._condition:
            while True:
                job = self.jobs.get(active_job_id)
                if job is None or job.get("state") not in {
                    "queued",
                    "running",
                    "stopping",
                }:
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(timeout=remaining)

    def update_recorded_seconds(self, job_id: str, seconds: Any) -> bool:
        try:
            value = float(seconds)
        except (TypeError, ValueError, OverflowError):
            return False
        if not math.isfinite(value) or value < 0:
            return False
        with self.lock:
            job = self.jobs.get(str(job_id))
            if job is None or job.get("type") != "recording":
                return False
            self._ensure_control_lists_locked(job)
            if job["item_states"][0] not in {"running", "stopping"}:
                return False
            previous = float(job.get("recorded_seconds") or 0.0)
            job["recorded_seconds"] = round(max(previous, value), 3)
            job["updated_at"] = self._created_at()
            return True

    def finalize_recording_job(
        self,
        job_id: str,
        item_id: str,
        *,
        state: str,
        returncode: int,
        completion_reason: str,
        output_path: Optional[str] = None,
    ) -> bool:
        """Finalize one recording and always release its lane/process state."""
        if state not in {"completed", "failed"}:
            raise ValueError("A recording must finish as completed or failed.")
        with self._condition:
            job = self.jobs.get(str(job_id))
            if job is None or job.get("type") != "recording":
                return False
            index = self._item_index_locked(job, item_id)
            if index != 0:
                return False
            self._set_item_state_locked(
                job,
                index,
                state,
                failure_kind="known" if state == "failed" else "",
            )
            job["returncode"] = int(returncode)
            job["completion_reason"] = str(completion_reason or "")
            job["output_path"] = str(output_path) if output_path else None
            job["output_complete"] = bool(output_path and state == "completed")
            job["updated_at"] = self._created_at()
            if self._lane_active["recording"] == (
                str(job_id),
                str(item_id),
            ):
                self._lane_active["recording"] = None
            self._recording_processes.pop(str(job_id), None)
            self._recording_stop_events.pop(str(job_id), None)
            self._recording_termination_started.discard(str(job_id))
            self._recording_stop_results.pop(str(job_id), None)
            self._recording_termination_done.pop(str(job_id), None)
            self._recompute_job_state_locked(job)
            self._condition.notify_all()
            return True

    def register_download_process(
        self, job_id: str, item_id: str, process: Any
    ) -> bool:
        with self.lock:
            key = (str(job_id), str(item_id))
            self._download_processes[key] = process
            event = self._cancel_events.get(key)
            return bool(event and event.is_set())

    def download_process(self, job_id: str, item_id: str) -> Any:
        with self.lock:
            return self._download_processes.get((str(job_id), str(item_id)))

    def terminate_registered_download(
        self,
        job_id: str,
        item_id: str,
        *,
        terminator: Callable[..., None] = terminate_download_process_tree,
    ) -> bool:
        process = self.download_process(job_id, item_id)
        if process is None:
            return False
        terminator(process)
        return True

    def reserve_retry(
        self, job_id: str, item_id: str
    ) -> Optional[Dict[str, Any]]:
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None or job.get("type") == "recording":
                return None
            index = self._item_index_locked(job, item_id)
            if index is None or job["item_states"][index] != "failed":
                return None
            existing = job["item_retry_job_ids"][index]
            if existing:
                return {
                    "reserved": False,
                    "retry_job_id": "" if existing == "__pending__" else existing,
                    "pending": existing == "__pending__",
                }
            if (
                job.get("type") == "youtube_upload"
                and job["item_failure_kinds"][index] == "uncertain"
            ):
                return {
                    "reserved": False,
                    "blocked": True,
                    "reason": self._item_capabilities_locked(job, index)[
                        "retry_block_reason"
                    ],
                }
            job["item_retry_job_ids"][index] = "__pending__"
            return {
                "reserved": True,
                "type": job.get("type") or "download",
                "value": job["urls"][index],
                "label": job.get("label") or "Queue retry",
                "index": index,
            }

    def finalize_retry(
        self, job_id: str, item_id: str, retry_job_id: str
    ) -> bool:
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return False
            index = self._item_index_locked(job, item_id)
            if index is None:
                return False
            job["item_retry_job_ids"][index] = str(retry_job_id or "")
            return True

    def get_job(self, job_id: str) -> Optional[Job]:
        """Return a detached snapshot of one job."""
        with self.lock:
            job = self.jobs.get(job_id)
            return deepcopy(job) if job is not None else None

    def snapshot_jobs(self, reverse: bool = False) -> list[Job]:
        """Return detached jobs in creation order, or newest first."""
        with self.lock:
            result = []
            for job in self.jobs.values():
                self._ensure_control_lists_locked(job)
                lane = self._lane_for_job(job)
                snapshot = deepcopy(job)
                snapshot["lane"] = lane
                snapshot["queue_paused"] = bool(
                    self._lane_paused.get(lane, False)
                )
                snapshot["stop_after_current"] = bool(
                    self._lane_stop_after_current.get(lane, False)
                )
                snapshot["item_capabilities"] = [
                    self._item_capabilities_locked(job, index)
                    for index in range(len(job["item_states"]))
                ]
                result.append(snapshot)
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
            self._ensure_control_lists_locked(job)
            state = LEGACY_TO_ITEM_STATE.get(status, status)
            if state not in ITEM_STATES:
                return False
            self._set_item_state_locked(job, index, state)
            metrics = self._download_metric_lists(job)
            for key in (
                "item_progress",
                "item_processed_seconds",
                "item_speed_multiplier",
                "item_eta_seconds",
                "item_updated_at",
            ):
                metrics[key][index] = None
            metrics["item_speed_label"][index] = ""
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
            metrics = self._download_metric_lists(job)
            for index, status in enumerate(statuses):
                if status in {"wartet", "läuft"}:
                    self._set_item_state_locked(job, index, "failed")
                    for key in (
                        "item_progress",
                        "item_processed_seconds",
                        "item_speed_multiplier",
                        "item_eta_seconds",
                        "item_updated_at",
                    ):
                        metrics[key][index] = None
                    metrics["item_speed_label"][index] = ""

    def set_upload_item_status(
        self,
        job_id: str,
        item_number: int,
        status: str,
        *,
        progress: Optional[int] = None,
        error: str = "",
    ) -> bool:
        """Transition one one-based upload item without inferring state from logs."""
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None or job.get("type") != "youtube_upload":
                return False
            index = item_number - 1
            statuses = job.get("item_statuses")
            progresses = job.get("item_progress")
            errors = job.get("item_errors")
            if (
                not isinstance(statuses, list)
                or not isinstance(progresses, list)
                or not isinstance(errors, list)
                or index < 0
                or index >= len(statuses)
            ):
                return False
            self._ensure_control_lists_locked(job)
            state = LEGACY_TO_ITEM_STATE.get(status, status)
            if state not in ITEM_STATES:
                return False
            self._set_item_state_locked(job, index, state)
            progresses[index] = progress
            errors[index] = str(error or "")
            metric_defaults = {
                "item_bytes_uploaded": None,
                "item_total_bytes": None,
                "item_bytes_per_second": None,
                "item_eta_seconds": None,
                "item_updated_at": None,
            }
            for key, default in metric_defaults.items():
                values = job.get(key)
                if not isinstance(values, list):
                    values = [default for _ in statuses]
                    job[key] = values
                elif len(values) < len(statuses):
                    values.extend(default for _ in range(len(statuses) - len(values)))
            measurement_key = (str(job_id), index)
            if state == "running":
                for key, default in metric_defaults.items():
                    job[key][index] = default
                self._upload_measurements.pop(measurement_key, None)
            else:
                job["item_bytes_per_second"][index] = None
                job["item_eta_seconds"][index] = None
                if state == "completed":
                    total_bytes = job["item_total_bytes"][index]
                    if isinstance(total_bytes, int) and total_bytes > 0:
                        job["item_bytes_uploaded"][index] = total_bytes
                self._upload_measurements.pop(measurement_key, None)
        return True

    def update_active_upload_progress(
        self,
        job_id: str,
        bytes_uploaded: Any,
        total_bytes: Any,
        *,
        item_id: Optional[str] = None,
        observed_at: Optional[float] = None,
    ) -> bool:
        """Record exact resumable-upload bytes and a smoothed transfer ETA."""
        try:
            uploaded = max(0, int(bytes_uploaded))
            total = int(total_bytes)
            timestamp = float(
                self._clock() if observed_at is None else observed_at
            )
        except (TypeError, ValueError, OverflowError):
            return False
        if total <= 0 or not math.isfinite(timestamp):
            return False
        uploaded = min(uploaded, total)

        with self.lock:
            job = self.jobs.get(job_id)
            if job is None or job.get("type") != "youtube_upload":
                return False
            statuses = job.get("item_statuses")
            if not isinstance(statuses, list):
                return False
            self._ensure_control_lists_locked(job)
            if item_id is not None:
                index = self._item_index_locked(job, item_id)
                if index is None or job["item_states"][index] not in {
                    "running",
                    "cancelling",
                }:
                    return False
            else:
                try:
                    index = next(
                        index
                        for index, state in enumerate(job["item_states"])
                        if state in {"running", "cancelling"}
                    )
                except StopIteration:
                    return False

            metric_keys = (
                "item_progress",
                "item_bytes_uploaded",
                "item_total_bytes",
                "item_bytes_per_second",
                "item_eta_seconds",
                "item_updated_at",
            )
            for key in metric_keys:
                values = job.get(key)
                if not isinstance(values, list):
                    values = [None for _ in statuses]
                    job[key] = values
                elif len(values) < len(statuses):
                    values.extend(None for _ in range(len(statuses) - len(values)))

            progress = min(100.0, max(0.0, uploaded * 100.0 / total))
            job["item_progress"][index] = round(progress, 1)
            job["item_bytes_uploaded"][index] = uploaded
            job["item_total_bytes"][index] = total
            job["item_updated_at"][index] = timestamp

            key = (str(job_id), index)
            previous = self._upload_measurements.get(key)
            smoothed_speed: Optional[float] = None
            if previous is not None:
                elapsed = timestamp - previous["timestamp"]
                transferred = uploaded - int(previous["bytes_uploaded"])
                if elapsed > 0 and transferred >= 0:
                    instant_speed = transferred / elapsed
                    old_speed = previous.get("smoothed_speed")
                    smoothed_speed = (
                        instant_speed
                        if old_speed is None
                        else (
                            UPLOAD_SPEED_EMA_ALPHA * instant_speed
                            + (1.0 - UPLOAD_SPEED_EMA_ALPHA) * old_speed
                        )
                    )

            job["item_bytes_per_second"][index] = (
                round(smoothed_speed, 2)
                if smoothed_speed is not None
                and math.isfinite(smoothed_speed)
                and smoothed_speed >= 0
                else None
            )
            remaining = total - uploaded
            job["item_eta_seconds"][index] = (
                int(math.ceil(remaining / smoothed_speed))
                if smoothed_speed is not None
                and math.isfinite(smoothed_speed)
                and smoothed_speed >= MIN_UPLOAD_ETA_SPEED_BPS
                and remaining > 0
                else None
            )
            self._upload_measurements[key] = {
                "timestamp": timestamp,
                "bytes_uploaded": float(uploaded),
                "smoothed_speed": smoothed_speed,
            }
        return True

    def fail_unfinished_upload_items(self, job_id: str, error: str) -> None:
        """Fail upload items that never started after a job-level failure."""
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return
            statuses = job.get("item_statuses") or []
            errors = job.get("item_errors") or []
            progresses = job.get("item_progress") or []
            speeds = job.get("item_bytes_per_second") or []
            etas = job.get("item_eta_seconds") or []
            for index, status in enumerate(statuses):
                if status in {"wartet", "l\u00e4uft"}:
                    self._set_item_state_locked(job, index, "failed")
                    if index < len(errors):
                        errors[index] = str(error or "")
                    if index < len(progresses):
                        progresses[index] = None
                    if index < len(speeds):
                        speeds[index] = None
                    if index < len(etas):
                        etas[index] = None
                    self._upload_measurements.pop((str(job_id), index), None)

    def resolve_error(self, job_id: str, item_number: int) -> bool:
        """Dismiss one failed item while retaining its failure and diagnostics."""
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return False
            index = item_number - 1
            statuses = job.get("item_statuses")
            resolved = job.get("item_resolved")
            if not isinstance(resolved, list):
                resolved = [False for _ in statuses] if isinstance(statuses, list) else []
                job["item_resolved"] = resolved
            if (
                not isinstance(statuses, list)
                or not isinstance(resolved, list)
                or index < 0
                or index >= len(statuses)
                or statuses[index] != "fehler"
            ):
                return False
            resolved[index] = True
        return True

    def resolve_error_by_id(self, job_id: str, item_id: str) -> bool:
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return False
            index = self._item_index_locked(job, item_id)
        return self.resolve_error(job_id, index + 1) if index is not None else False

    def unfinished_upload_paths(self) -> set[str]:
        """Return files already represented by a queued or running upload item."""
        with self.lock:
            paths: set[str] = set()
            for job in self.jobs.values():
                if job.get("type") != "youtube_upload":
                    continue
                self._ensure_control_lists_locked(job)
                states = job.get("item_states") or []
                for index, raw in enumerate(job.get("urls") or []):
                    state = states[index] if index < len(states) else "queued"
                    if state in {"queued", "running", "cancelling"}:
                        paths.add(str(raw))
            return paths

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


def _safe_recording_log_line(text: Any) -> str:
    line = str(text or "").rstrip()
    lowered = line.lower()
    if not line or parse_ffmpeg_time_seconds(line) is not None:
        return ""
    if any(
        marker in lowered
        for marker in (
            "http://",
            "https://",
            "cookie",
            "authorization",
            "oauth",
            "access_token",
            "token=",
        )
    ):
        return ""
    return line


def run_recording_job(
    job_id: str,
    manager: JobManager,
    dependencies: RecordingWorkerDependencies,
) -> None:
    """Run one Twitch livestream in its dedicated process-local lane."""
    claimed = manager.claim_recording_job(job_id)
    if claimed is None:
        return
    item_id = str(claimed["item_id"])
    output_path: Optional[str] = None
    finalized = False

    try:
        job = manager.get_job(job_id) or {}
        streamer = str(job.get("streamer") or "")
        settings = dependencies.load_settings()
        settings["quality"] = str(
            job.get("quality") or settings.get("quality") or "source/best"
        )
        command = dependencies.build_recording_command(
            streamer, settings, attempt=int(job.get("attempt") or 1)
        )
        dependencies.append_log(job_id, f"Recording started for {streamer}.")
        process = dependencies.popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(dependencies.download_directory(settings)),
            **download_process_group_options(),
        )
        registration = manager.register_recording_process(
            job_id, item_id, process
        )
        if registration is None:
            raise RuntimeError("The recording process could not be registered.")
        if registration:
            manager.start_recording_termination(
                job_id,
                terminator=dependencies.terminate_process,
                thread_factory=dependencies.thread_factory,
            )

        assert process.stdout is not None
        for raw_line in process.stdout:
            if manager.is_recording_stop_requested(job_id):
                manager.start_recording_termination(
                    job_id,
                    terminator=dependencies.terminate_process,
                    thread_factory=dependencies.thread_factory,
                )
            line = str(raw_line or "").rstrip()
            marker_at = line.find(dependencies.output_marker)
            if marker_at >= 0:
                raw_output = line[
                    marker_at + len(dependencies.output_marker) :
                ].strip()
                try:
                    output_path = dependencies.resolve_completed_output(
                        raw_output, settings
                    )
                except Exception:
                    output_path = None
                continue

            recorded_seconds = parse_ffmpeg_time_seconds(line)
            if recorded_seconds is not None:
                manager.update_recorded_seconds(job_id, recorded_seconds)
                continue

            safe_line = _safe_recording_log_line(line)
            if safe_line:
                dependencies.append_log(job_id, safe_line)

        returncode = int(process.wait())
        stop_requested = manager.is_recording_stop_requested(job_id)
        if stop_requested:
            stop_result = manager.wait_recording_stop_result(job_id)
            if stop_result in {
                RECORDING_STOP_RESULT_KILLED,
                RECORDING_STOP_RESULT_FAILED,
            }:
                manager.finalize_recording_job(
                    job_id,
                    item_id,
                    state="failed",
                    returncode=returncode,
                    completion_reason="stop_failed",
                )
                dependencies.append_log(
                    job_id, "Recording stop did not finalize cleanly."
                )
            elif output_path:
                manager.finalize_recording_job(
                    job_id,
                    item_id,
                    state="completed",
                    returncode=returncode,
                    completion_reason="stopped_by_user",
                    output_path=output_path,
                )
                dependencies.append_log(job_id, "Recording stopped.")
            else:
                manager.finalize_recording_job(
                    job_id,
                    item_id,
                    state="failed",
                    returncode=returncode,
                    completion_reason="stop_incomplete",
                )
                dependencies.append_log(
                    job_id,
                    "Recording stopped without a confirmed final media file.",
                )
        elif returncode == 0:
            manager.finalize_recording_job(
                job_id,
                item_id,
                state="completed",
                returncode=returncode,
                completion_reason="natural_end",
                output_path=output_path,
            )
            dependencies.append_log(job_id, "Recording completed naturally.")
        else:
            manager.finalize_recording_job(
                job_id,
                item_id,
                state="failed",
                returncode=returncode,
                completion_reason="process_error",
            )
            dependencies.append_log(
                job_id, f"Recording failed with process code {returncode}."
            )
        finalized = True
    except FileNotFoundError:
        manager.finalize_recording_job(
            job_id,
            item_id,
            state="failed",
            returncode=-1,
            completion_reason="worker_error",
        )
        dependencies.append_log(
            job_id,
            "The yt-dlp Python module was not found. Install the dependencies from requirements.txt.",
        )
        finalized = True
    except Exception as exc:
        manager.finalize_recording_job(
            job_id,
            item_id,
            state="failed",
            returncode=-2,
            completion_reason="worker_error",
        )
        dependencies.append_log(
            job_id, f"Recording worker failed: {type(exc).__name__}."
        )
        finalized = True
    finally:
        if not finalized:
            manager.finalize_recording_job(
                job_id,
                item_id,
                state="failed",
                returncode=-2,
                completion_reason="worker_error",
            )
        manager.clear_recording_process(job_id)


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
    initial_job = manager.get_job(job_id) or {}
    total = len(initial_job.get("urls") or [])
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

            if dependencies.enqueue_upload_job is None:
                raise RuntimeError(
                    "The automatic upload Queue controller is unavailable."
                )
            upload_job_id = dependencies.enqueue_upload_job(
                [str(video_path) for video_path in candidates],
                "Automatic YouTube Upload",
            )
            dependencies.append_log(
                job_id,
                f"YouTube Auto-Upload queued as upload job {upload_job_id}.",
            )
        except Exception as exc:
            dependencies.append_log(
                job_id, f"YouTube Auto-Upload did not start: {exc}"
            )

    try:
        if not total:
            raise RuntimeError("The job contains no URLs.")

        while True:
            claimed = manager.claim_next_item(job_id)
            if claimed is None:
                break
            idx = int(claimed["item_number"])
            item_id = str(claimed["item_id"])
            url = str(claimed["value"])
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
                process_options = download_process_group_options()
                proc = dependencies.popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    cwd=str(dependencies.download_directory(settings)),
                    **process_options,
                )
                cancel_before_output = manager.register_download_process(
                    job_id, item_id, proc
                )
                if cancel_before_output:
                    manager.terminate_registered_download(job_id, item_id)
                assert proc.stdout is not None
                for line in proc.stdout:
                    dependencies.append_log(job_id, line)
                rc = proc.wait()
                manager.set_returncode(job_id, rc)

                if manager.is_cancel_requested(job_id, item_id):
                    manager.finish_claimed_item(
                        job_id, item_id, "cancelled"
                    )
                    dependencies.append_log(
                        job_id,
                        f"VOD {idx}/{total} download cancelled. Partial files were retained.",
                    )
                elif rc == 0:
                    succeeded += 1
                    manager.finish_claimed_item(
                        job_id, item_id, "completed"
                    )
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
                    manager.finish_claimed_item(
                        job_id, item_id, "failed", failure_kind="known"
                    )
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

        final_job = manager.get_job(job_id) or {}
        final_states = final_job.get("item_states") or []
        cancelled = sum(state == "cancelled" for state in final_states)
        status = "fertig" if failed == 0 else "fehler"
        manager.finish_job(job_id, 0 if failed == 0 else 1, status)
        if failed == 0 and cancelled == 0:
            dependencies.append_log(
                job_id, f"Batch completed: {succeeded}/{total} VOD(s) successful."
            )
        elif failed == 0:
            dependencies.append_log(
                job_id,
                f"Batch ended: {succeeded} successful, {cancelled} cancelled.",
            )
        else:
            dependencies.append_log(
                job_id,
                f"Batch completed: {succeeded} successful, {failed} failed, {cancelled} cancelled.",
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
    initial_job = manager.get_job(job_id) or {}
    settings = dict(dependencies.load_settings())
    if "playlist_id" in initial_job:
        settings["youtube_playlist_id"] = str(
            initial_job.get("playlist_id") or ""
        ).strip()
    paths = list(initial_job.get("urls") or [])
    item_metadata = initial_job.get("item_metadata")
    if not isinstance(item_metadata, list):
        item_metadata = []
    has_item_playlists = any(
        isinstance(metadata, dict)
        and "youtube_playlist_id" in metadata
        for metadata in item_metadata
    )
    playlist_summary = (
        "per-item"
        if has_item_playlists
        else settings.get("youtube_playlist_id") or "none"
    )
    dependencies.append_log(
        job_id, f"Starting local YouTube upload: {len(paths)} file(s)"
    )
    dependencies.append_log(
        job_id,
        "YouTube Settings: "
        f"enabled={bool(settings.get('youtube_enabled'))}, "
        f"privacy={settings.get('youtube_privacy_status')}, "
        f"playlist={playlist_summary}",
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
        while True:
            claimed = manager.claim_next_item(job_id)
            if claimed is None:
                break
            item_number = int(claimed["item_number"])
            item_id = str(claimed["item_id"])
            raw = str(claimed["value"])
            manager.set_upload_item_status(
                job_id, item_number, "running", progress=0
            )
            try:
                item_settings = dict(settings)
                item_index = int(claimed["index"])
                metadata = (
                    item_metadata[item_index]
                    if item_index < len(item_metadata)
                    else {}
                )
                if (
                    isinstance(metadata, dict)
                    and "youtube_playlist_id" in metadata
                ):
                    item_settings["youtube_playlist_id"] = str(
                        metadata.get("youtube_playlist_id") or ""
                    ).strip()
                path = dependencies.safe_local_video_path(raw, item_settings)
                dependencies.append_log(job_id, f"Uploading local VOD file: {path}")
                video_id = dependencies.upload_to_youtube(
                    path, item_settings, job_id=job_id, item_id=item_id
                )
                if manager.is_cancel_requested(job_id, item_id):
                    manager.finish_claimed_item(
                        job_id, item_id, "cancelled"
                    )
                    dependencies.append_log(
                        job_id,
                        f"YouTube upload cancelled after the current chunk: {path.name}",
                    )
                elif video_id:
                    uploaded += 1
                    manager.finish_claimed_item(
                        job_id, item_id, "completed"
                    )
                else:
                    failed += 1
                    error = "Upload completed without a YouTube video ID."
                    manager.set_upload_item_status(
                        job_id, item_number, "fehler", error=error
                    )
                    manager.finish_claimed_item(
                        job_id,
                        item_id,
                        "failed",
                        failure_kind="uncertain",
                    )
                    dependencies.append_log(
                        job_id, f"Upload completed without a video ID: {path.name}"
                    )
            except Exception as exc:
                if manager.is_cancel_requested(job_id, item_id):
                    manager.finish_claimed_item(
                        job_id, item_id, "cancelled"
                    )
                    dependencies.append_log(
                        job_id,
                        f"YouTube upload cancelled after the current chunk: {Path(raw).name}",
                    )
                else:
                    failed += 1
                    failure_kind = (
                        "uncertain"
                        if getattr(exc, "upload_outcome_uncertain", False)
                        else "known"
                    )
                    manager.set_upload_item_status(
                        job_id, item_number, "fehler", error=str(exc)
                    )
                    manager.finish_claimed_item(
                        job_id,
                        item_id,
                        "failed",
                        failure_kind=failure_kind,
                    )
                    dependencies.append_log(
                        job_id, f"YouTube Upload failed for {Path(raw).name}: {exc}"
                    )
        final_job = manager.get_job(job_id) or {}
        cancelled = sum(
            state == "cancelled"
            for state in final_job.get("item_states") or []
        )
        status = "fertig" if failed == 0 else "fehler"
        manager.finish_job(job_id, 0 if failed == 0 else 1, status)
        summary = f"Local upload completed: {uploaded} successful, {failed} failed."
        if cancelled:
            summary = (
                f"Local upload completed: {uploaded} successful, {failed} failed, "
                f"{cancelled} cancelled."
            )
        dependencies.append_log(job_id, summary)
    except Exception as exc:
        manager.fail_unfinished_upload_items(job_id, str(exc))
        manager.finish_job(job_id, -2, "fehler")
        dependencies.append_log(
            job_id, f"Local YouTube upload did not start: {exc}"
        )
