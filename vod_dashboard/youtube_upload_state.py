"""Durable Auto YouTube ownership ledger (schema v2)."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import math
from pathlib import Path, PurePosixPath
import re
import threading
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from vod_dashboard.runtime_files import atomic_write_text
from vod_dashboard.settings import canonical_streamer_login

YOUTUBE_UPLOAD_STATE_FILE_NAME = "youtube-upload-state.json"
YOUTUBE_UPLOAD_STATE_VERSION = 2
LEGACY_YOUTUBE_UPLOAD_STATE_VERSION = 1
MAX_UPLOAD_RECORDS = 100_000
MAX_VOD_ID_LENGTH = 32
MAX_PATH_LENGTH = 1024
MAX_IDENTIFIER_LENGTH = 128
MAX_REASON_LENGTH = 64
MAX_TIMESTAMP_LENGTH = 40
MAX_SIZE_BYTES = 2**63 - 1
MAX_TITLE_LENGTH = 95
MAX_DESCRIPTION_LENGTH = 5_000
MAX_TEMPLATE_LENGTH = 8_000
MAX_TAGS = 100
MAX_TAG_LENGTH = 100
MAX_PARTS = 10_000

BUNDLE_STATES = frozenset({"intent_pending", "plan_ready", "parts_preparing", "parts_ready", "upload_queued", "video_confirmed", "playlist_pending", "completed", "blocked_youtube", "needs_attention", "cancelled"})
UPLOAD_STATES = BUNDLE_STATES
PART_UPLOAD_STATES = frozenset({"ready", "queued", "transfer_started", "video_confirmed", "completed", "failed_known", "uncertain", "cancelled"})
PLAYLIST_STATES = frozenset({"not_requested", "pending", "inserting", "confirmed", "failed", "uncertain"})
SOURCE_KINDS = frozenset({"original", "generated"})
SPLIT_MODES = frozenset({"stream_copy"})
PART_PLAN_VERSION = 1
REASON_CODES = frozenset({"youtube_not_connected", "token_refresh_failed", "api_unavailable", "local_preparation_failed", "upload_outcome_uncertain", "playlist_failed", "playlist_uncertain", "plan_media_missing", "plan_source_invalid", "plan_preparation_failed", "plan_inputs_missing", "materialization_media_missing", "materialization_source_invalid", "materialization_consistency_error", "multipart_preparation_required", "parts_preparation_failed", "parts_manifest_invalid", "insufficient_storage", "storage_unavailable", "ffmpeg_unavailable", "ffmpeg_failed", "multipart_storage_insufficient", "multipart_storage_unavailable", "multipart_generation_incomplete", "multipart_validation_failed", "multipart_replan_required"})

_VOD_ID_RE = re.compile(rf"\d{{6,{MAX_VOD_ID_LENGTH}}}")
_IDENTIFIER_RE = re.compile(rf"[A-Za-z0-9][A-Za-z0-9_.-]{{0,{MAX_IDENTIFIER_LENGTH - 1}}}")
_YOUTUBE_ID_RE = re.compile(rf"[A-Za-z0-9_-]{{1,{MAX_IDENTIFIER_LENGTH}}}")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_TRANSITIONS = {
    "intent_pending": {"plan_ready", "blocked_youtube", "needs_attention", "cancelled"},
    "plan_ready": {"parts_preparing", "upload_queued", "blocked_youtube", "needs_attention", "cancelled"},
    "parts_preparing": {"parts_ready", "needs_attention", "cancelled"},
    "parts_ready": {"upload_queued", "needs_attention", "cancelled"},
    "upload_queued": {"video_confirmed", "playlist_pending", "completed", "blocked_youtube", "needs_attention", "cancelled"},
    "video_confirmed": {"playlist_pending", "completed", "needs_attention"}, "playlist_pending": {"completed", "needs_attention"},
    "blocked_youtube": {"parts_ready", "upload_queued", "cancelled", "needs_attention"},
    "needs_attention": {"parts_preparing", "parts_ready", "upload_queued", "cancelled"}, "cancelled": {"parts_preparing", "parts_ready", "upload_queued"}, "completed": set(),
}

State = Dict[str, Any]
UploadRecord = Dict[str, Any]
Clock = Callable[[], datetime]

class YouTubeUploadStateError(RuntimeError): pass
class YouTubeUploadStateValidationError(YouTubeUploadStateError, ValueError): pass
class YouTubeUploadStateLoadError(YouTubeUploadStateError):
    def __init__(self, reason: str) -> None: super().__init__(reason); self.reason = reason
class YouTubeUploadStatePersistenceError(YouTubeUploadStateError): pass

def empty_youtube_upload_state() -> State: return {"version": YOUTUBE_UPLOAD_STATE_VERSION, "uploads": {}}
def youtube_upload_state_path(dashboard_dir: Path) -> Path: return Path(dashboard_dir) / YOUTUBE_UPLOAD_STATE_FILE_NAME
def _streamer(v: Any) -> str:
    result = canonical_streamer_login(v)
    if not result: raise YouTubeUploadStateValidationError("invalid_streamer")
    return result
def _vod(v: Any) -> str:
    result = str(v).strip() if not isinstance(v, bool) and isinstance(v, (str, int)) else ""
    if not _VOD_ID_RE.fullmatch(result): raise YouTubeUploadStateValidationError("invalid_twitch_vod_id")
    return result
def canonical_upload_key(streamer: Any, twitch_vod_id: Any) -> str: return f"{_streamer(streamer)}:{_vod(twitch_vod_id)}"
def _identifier(v: Any, code: str) -> str:
    result = v.strip() if isinstance(v, str) else ""
    if not _IDENTIFIER_RE.fullmatch(result): raise YouTubeUploadStateValidationError(code)
    return result
def _youtube_id(v: Any, code: str) -> Optional[str]:
    if v is None: return None
    result = v.strip() if isinstance(v, str) else ""
    if not _YOUTUBE_ID_RE.fullmatch(result): raise YouTubeUploadStateValidationError(code)
    return result
def _path(v: Any, code: str = "invalid_media_path") -> str:
    result = v.strip().replace("\\", "/") if isinstance(v, str) else ""
    if not result or len(result) > MAX_PATH_LENGTH or _CONTROL_RE.search(result) or "://" in result or result.startswith("//") or re.match(r"^[A-Za-z]:", result): raise YouTubeUploadStateValidationError(code)
    parsed = PurePosixPath(result)
    if parsed.is_absolute() or not parsed.parts or any(part in {"", ".", ".."} for part in parsed.parts): raise YouTubeUploadStateValidationError(code)
    return parsed.as_posix()
def _size(v: Any, code: str = "invalid_size_bytes", *, positive: bool = False) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or v < (1 if positive else 0) or v > MAX_SIZE_BYTES: raise YouTubeUploadStateValidationError(code)
    return v
def _duration(v: Any, code: str) -> Optional[float]:
    if v is None: return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)) or float(v) <= 0: raise YouTubeUploadStateValidationError(code)
    return float(v)
def _attempts(v: Any) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 1000: raise YouTubeUploadStateValidationError("invalid_attempts")
    return v
def _reason(v: Any) -> Optional[str]:
    if v is None: return None
    result = v.strip() if isinstance(v, str) else ""
    if len(result) > MAX_REASON_LENGTH or result not in REASON_CODES: raise YouTubeUploadStateValidationError("invalid_reason")
    return result
def _timestamp(v: Any) -> str:
    if not isinstance(v, str) or len(v) > MAX_TIMESTAMP_LENGTH: raise YouTubeUploadStateValidationError("invalid_timestamp")
    try: parsed = datetime.fromisoformat(v.strip()[:-1] + "+00:00" if v.strip().endswith("Z") else v.strip())
    except ValueError as exc: raise YouTubeUploadStateValidationError("invalid_timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None: raise YouTubeUploadStateValidationError("invalid_timestamp")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
def _text(v: Any, code: str, maximum: int, newlines: bool) -> str:
    result = v.replace("\r\n", "\n").replace("\r", "\n") if isinstance(v, str) else None
    if result is None or len(result) > maximum or _CONTROL_RE.search(result.replace("\n", "")) or (not newlines and "\n" in result): raise YouTubeUploadStateValidationError(code)
    return result
def _plan_inputs(v: Any) -> Optional[Dict[str, Any]]:
    if v is None: return None
    if not isinstance(v, Mapping) or set(v) != {"title_template", "description_template", "description_fallback", "privacy_status", "category_id", "tags"}: raise YouTubeUploadStateValidationError("invalid_plan_inputs")
    if v.get("privacy_status") not in {"private", "unlisted", "public"} or _youtube_id(v.get("category_id"), "invalid_plan_inputs") is None or not isinstance(v.get("tags"), list) or len(v["tags"]) > MAX_TAGS: raise YouTubeUploadStateValidationError("invalid_plan_inputs")
    tags = [_text(item, "invalid_plan_inputs", MAX_TAG_LENGTH, False).strip() for item in v["tags"]]
    if any(not tag for tag in tags): raise YouTubeUploadStateValidationError("invalid_plan_inputs")
    return {"title_template": _text(v.get("title_template"), "invalid_plan_inputs", MAX_TEMPLATE_LENGTH, True), "description_template": _text(v.get("description_template"), "invalid_plan_inputs", MAX_TEMPLATE_LENGTH, True), "description_fallback": _text(v.get("description_fallback"), "invalid_plan_inputs", MAX_TEMPLATE_LENGTH, True), "privacy_status": v["privacy_status"], "category_id": _youtube_id(v["category_id"], "invalid_plan_inputs"), "tags": tags}
def _upload_plan(v: Any) -> Optional[Dict[str, Any]]:
    if v is None: return None
    if not isinstance(v, Mapping) or set(v) != {"title", "description", "privacy_status", "category_id", "tags"}: raise YouTubeUploadStateValidationError("invalid_upload_plan")
    title = _text(v.get("title"), "invalid_upload_plan", MAX_TITLE_LENGTH, False); description = _text(v.get("description"), "invalid_upload_plan", MAX_DESCRIPTION_LENGTH, True)
    if not title or "<" in title + description or ">" in title + description or v.get("privacy_status") not in {"private", "unlisted", "public"} or _youtube_id(v.get("category_id"), "invalid_upload_plan") is None or not isinstance(v.get("tags"), list) or len(v["tags"]) > MAX_TAGS: raise YouTubeUploadStateValidationError("invalid_upload_plan")
    tags = [_text(item, "invalid_upload_plan", MAX_TAG_LENGTH, False).strip() for item in v["tags"]]
    if any(not tag for tag in tags): raise YouTubeUploadStateValidationError("invalid_upload_plan")
    return {"title": title, "description": description, "privacy_status": v["privacy_status"], "category_id": _youtube_id(v["category_id"], "invalid_upload_plan"), "tags": tags}
def validate_upload_plan(v: Any) -> Dict[str, Any]:
    result = _upload_plan(v)
    if result is None: raise YouTubeUploadStateValidationError("invalid_upload_plan")
    return result
def _split(v: Any) -> Optional[Dict[str, Any]]:
    if v is None: return None
    if not isinstance(v, Mapping) or set(v) != {"mode", "generation_id", "target_duration_seconds", "target_size_bytes", "split_points_seconds"} or v.get("mode") not in SPLIT_MODES or not isinstance(v.get("split_points_seconds"), list): raise YouTubeUploadStateValidationError("invalid_split")
    points = [_duration(point, "invalid_split") for point in v["split_points_seconds"]]
    if any(a is None or b is None or a >= b for a, b in zip(points, points[1:])): raise YouTubeUploadStateValidationError("invalid_split")
    return {"mode": v["mode"], "generation_id": _identifier(v.get("generation_id"), "invalid_split"), "target_duration_seconds": _duration(v.get("target_duration_seconds"), "invalid_split"), "target_size_bytes": _size(v.get("target_size_bytes"), "invalid_split", positive=True), "split_points_seconds": points}
def _part(v: Any, index: int) -> Dict[str, Any]:
    fields = {"index", "media_path", "size_bytes", "duration_seconds", "source_kind", "upload_item_id", "upload_state", "attempts", "youtube_video_id", "playlist_state", "reason"}
    if not isinstance(v, Mapping) or set(v) != fields or v.get("index") != index or v.get("source_kind") not in SOURCE_KINDS or v.get("upload_state") not in PART_UPLOAD_STATES or v.get("playlist_state") not in PLAYLIST_STATES: raise YouTubeUploadStateValidationError("invalid_part")
    video_id = _youtube_id(v.get("youtube_video_id"), "invalid_part")
    if v["upload_state"] in {"video_confirmed", "completed"} and video_id is None: raise YouTubeUploadStateValidationError("invalid_part")
    return {"index": index, "media_path": _path(v.get("media_path"), "invalid_part"), "size_bytes": _size(v.get("size_bytes"), "invalid_part", positive=True), "duration_seconds": _duration(v.get("duration_seconds"), "invalid_part"), "source_kind": v["source_kind"], "upload_item_id": None if v.get("upload_item_id") is None else _identifier(v.get("upload_item_id"), "invalid_part"), "upload_state": v["upload_state"], "attempts": _attempts(v.get("attempts")), "youtube_video_id": video_id, "playlist_state": v["playlist_state"], "reason": _reason(v.get("reason"))}

V2_FIELDS = {"streamer", "twitch_vod_id", "source_download_job_id", "source_download_item_id", "media_path", "size_bytes", "source_duration_seconds", "state", "upload_job_id", "playlist_id", "plan_inputs", "upload_plan", "part_plan_version", "split", "parts", "reason", "created_at", "updated_at"}
V1_FIELDS = {"streamer", "twitch_vod_id", "source_download_job_id", "source_download_item_id", "media_path", "size_bytes", "state", "upload_job_id", "attempts", "youtube_video_id", "playlist_id", "playlist_state", "reason", "created_at", "updated_at"}
def _record_v2(v: Any, key: str) -> UploadRecord:
    if not isinstance(v, Mapping) or set(v) != V2_FIELDS: raise YouTubeUploadStateLoadError("invalid_record")
    try:
        streamer = _streamer(v.get("streamer")); vod_id = _vod(v.get("twitch_vod_id"))
        if key != f"{streamer}:{vod_id}" or v.get("state") not in BUNDLE_STATES: raise YouTubeUploadStateValidationError("invalid_record")
        parts_value = v.get("parts")
        if not isinstance(parts_value, list) or len(parts_value) > MAX_PARTS: raise YouTubeUploadStateValidationError("invalid_record")
        parts = [_part(part, index) for index, part in enumerate(parts_value, 1)]
        plan_version = v.get("part_plan_version")
        if plan_version is not None and (isinstance(plan_version, bool) or plan_version != PART_PLAN_VERSION): raise YouTubeUploadStateValidationError("invalid_record")
        split = _split(v.get("split"))
        if parts and plan_version is None: raise YouTubeUploadStateValidationError("invalid_record")
        if not parts and (plan_version is None) != (split is None): raise YouTubeUploadStateValidationError("invalid_record")
        if not parts and split is not None and v.get("state") not in {"parts_preparing", "needs_attention"}: raise YouTubeUploadStateValidationError("invalid_record")
        inputs = _plan_inputs(v.get("plan_inputs")); plan = _upload_plan(v.get("upload_plan"))
        if (plan is not None and inputs is None) or (v["state"] == "plan_ready" and plan is None): raise YouTubeUploadStateValidationError("invalid_record")
        return {"streamer": streamer, "twitch_vod_id": vod_id, "source_download_job_id": _identifier(v.get("source_download_job_id"), "invalid_source_download_job_id"), "source_download_item_id": _identifier(v.get("source_download_item_id"), "invalid_source_download_item_id"), "media_path": _path(v.get("media_path")), "size_bytes": _size(v.get("size_bytes")), "source_duration_seconds": _duration(v.get("source_duration_seconds"), "invalid_source_duration_seconds"), "state": v["state"], "upload_job_id": None if v.get("upload_job_id") is None else _identifier(v.get("upload_job_id"), "invalid_upload_job_id"), "playlist_id": _youtube_id(v.get("playlist_id"), "invalid_playlist_id"), "plan_inputs": inputs, "upload_plan": plan, "part_plan_version": plan_version, "split": split, "parts": parts, "reason": _reason(v.get("reason")), "created_at": _timestamp(v.get("created_at")), "updated_at": _timestamp(v.get("updated_at"))}
    except YouTubeUploadStateValidationError as exc: raise YouTubeUploadStateLoadError("invalid_record") from exc
def _record_v1(v: Any, key: str) -> UploadRecord:
    if not isinstance(v, Mapping) or not (set(v) == V1_FIELDS or set(v) == V1_FIELDS | {"plan_inputs"} or set(v) == V1_FIELDS | {"plan_inputs", "upload_plan"}): raise YouTubeUploadStateLoadError("invalid_record")
    try:
        streamer = _streamer(v.get("streamer")); vod_id = _vod(v.get("twitch_vod_id")); state = v.get("state"); playlist_state = v.get("playlist_state")
        if key != f"{streamer}:{vod_id}" or state not in {"intent_pending", "plan_ready", "upload_queued", "transfer_started", "video_confirmed", "playlist_pending", "completed", "blocked_youtube", "needs_attention", "cancelled"} or playlist_state not in PLAYLIST_STATES: raise YouTubeUploadStateValidationError("invalid_record")
        playlist_id = _youtube_id(v.get("playlist_id"), "invalid_playlist_id"); video_id = _youtube_id(v.get("youtube_video_id"), "invalid_youtube_video_id"); job = None if v.get("upload_job_id") is None else _identifier(v.get("upload_job_id"), "invalid_upload_job_id")
        if (playlist_state == "not_requested") != (playlist_id is None) or state in {"upload_queued", "transfer_started"} and job is None or state in {"video_confirmed", "playlist_pending", "completed"} and video_id is None: raise YouTubeUploadStateValidationError("invalid_record")
        inputs = _plan_inputs(v.get("plan_inputs")); plan = _upload_plan(v.get("upload_plan"))
        if (plan is not None and inputs is None) or (state == "plan_ready" and plan is None): raise YouTubeUploadStateValidationError("invalid_record")
        return {"streamer": streamer, "twitch_vod_id": vod_id, "source_download_job_id": _identifier(v.get("source_download_job_id"), "invalid_source_download_job_id"), "source_download_item_id": _identifier(v.get("source_download_item_id"), "invalid_source_download_item_id"), "media_path": _path(v.get("media_path")), "size_bytes": _size(v.get("size_bytes")), "state": state, "upload_job_id": job, "attempts": _attempts(v.get("attempts")), "youtube_video_id": video_id, "playlist_id": playlist_id, "playlist_state": playlist_state, "reason": _reason(v.get("reason")), "created_at": _timestamp(v.get("created_at")), "updated_at": _timestamp(v.get("updated_at")), **({"plan_inputs": inputs} if inputs is not None else {}), **({"upload_plan": plan} if plan is not None else {})}
    except YouTubeUploadStateValidationError as exc: raise YouTubeUploadStateLoadError("invalid_record") from exc
def normalize_legacy_youtube_upload_state(v: Any) -> State:
    if not isinstance(v, Mapping) or set(v) != {"version", "uploads"} or isinstance(v.get("version"), bool) or v.get("version") != 1 or not isinstance(v.get("uploads"), Mapping) or len(v["uploads"]) > MAX_UPLOAD_RECORDS: raise YouTubeUploadStateLoadError("invalid_structure")
    return {"version": 1, "uploads": {key: _record_v1(record, key) if isinstance(key, str) else (_ for _ in ()).throw(YouTubeUploadStateLoadError("invalid_record")) for key, record in v["uploads"].items()}}
def normalize_youtube_upload_state(v: Any) -> State:
    if not isinstance(v, Mapping) or set(v) != {"version", "uploads"}: raise YouTubeUploadStateLoadError("invalid_structure")
    if v.get("version") == 1: raise YouTubeUploadStateLoadError("migration_required")
    if isinstance(v.get("version"), bool) or v.get("version") != 2: raise YouTubeUploadStateLoadError("unsupported_version")
    if not isinstance(v.get("uploads"), Mapping) or len(v["uploads"]) > MAX_UPLOAD_RECORDS: raise YouTubeUploadStateLoadError("invalid_structure")
    return {"version": 2, "uploads": {key: _record_v2(record, key) if isinstance(key, str) else (_ for _ in ()).throw(YouTubeUploadStateLoadError("invalid_record")) for key, record in v["uploads"].items()}}
def _now(clock: Clock) -> str:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None: raise YouTubeUploadStateValidationError("invalid_clock")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

class YouTubeUploadStateStore:
    def __init__(self, path: Path, *, clock: Optional[Clock] = None) -> None: self.path = Path(path); self._clock = clock or (lambda: datetime.now(timezone.utc)); self._lock = threading.RLock()
    @classmethod
    def from_dashboard_dir(cls, dashboard_dir: Path, *, clock: Optional[Clock] = None) -> "YouTubeUploadStateStore": return cls(youtube_upload_state_path(dashboard_dir), clock=clock)
    def _load_locked(self) -> State:
        try: raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError: return empty_youtube_upload_state()
        except Exception as exc: raise YouTubeUploadStateLoadError("unreadable_state") from exc
        try: return normalize_youtube_upload_state(json.loads(raw))
        except (TypeError, ValueError) as exc: raise YouTubeUploadStateLoadError("invalid_json") from exc
    def load(self) -> State:
        with self._lock: return deepcopy(self._load_locked())
    snapshot = load
    def health(self) -> Dict[str, Any]:
        try: self.load()
        except YouTubeUploadStateLoadError as exc: return {"healthy": False, "reason": exc.reason}
        return {"healthy": True, "reason": None}
    def get(self, streamer: Any, twitch_vod_id: Any) -> Optional[UploadRecord]:
        try: key = canonical_upload_key(streamer, twitch_vod_id)
        except YouTubeUploadStateValidationError: return None
        with self._lock: return deepcopy(self._load_locked()["uploads"].get(key))
    def list_records(self) -> Dict[str, UploadRecord]:
        with self._lock: return deepcopy(self._load_locked()["uploads"])
    def _write_locked(self, state: State) -> None:
        try: atomic_write_text(self.path, json.dumps(normalize_youtube_upload_state(state), indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        except YouTubeUploadStateLoadError as exc: raise YouTubeUploadStateValidationError("invalid_state") from exc
        except YouTubeUploadStateValidationError: raise
        except Exception as exc: raise YouTubeUploadStatePersistenceError("Could not persist Auto YouTube ownership state.") from exc
    def replace_state(self, state: Any) -> State:
        with self._lock: self._write_locked(state); return self.load()
    def create_intent_if_absent(self, streamer: Any, twitch_vod_id: Any, *, source_download_job_id: Any, source_download_item_id: Any, media_path: Any, size_bytes: Any, playlist_id: Any = None, plan_inputs: Any = None) -> Tuple[UploadRecord, bool]:
        streamer = _streamer(streamer); vod_id = _vod(twitch_vod_id); key = f"{streamer}:{vod_id}"
        with self._lock:
            doc = self._load_locked()
            if key in doc["uploads"]: return deepcopy(doc["uploads"][key]), False
            if len(doc["uploads"]) >= MAX_UPLOAD_RECORDS: raise YouTubeUploadStateValidationError("too_many_uploads")
            now = _now(self._clock); record = {"streamer": streamer, "twitch_vod_id": vod_id, "source_download_job_id": _identifier(source_download_job_id, "invalid_source_download_job_id"), "source_download_item_id": _identifier(source_download_item_id, "invalid_source_download_item_id"), "media_path": _path(media_path), "size_bytes": _size(size_bytes), "source_duration_seconds": None, "state": "intent_pending", "upload_job_id": None, "playlist_id": _youtube_id(playlist_id, "invalid_playlist_id"), "plan_inputs": _plan_inputs(plan_inputs), "upload_plan": None, "part_plan_version": None, "split": None, "parts": [], "reason": None, "created_at": now, "updated_at": now}
            doc["uploads"][key] = record; self._write_locked(doc); return deepcopy(record), True
    def update_record(self, streamer: Any, twitch_vod_id: Any, *, state: Optional[str] = None, upload_job_id: Any = ..., reason: Any = ...) -> UploadRecord:
        key = canonical_upload_key(streamer, twitch_vod_id)
        with self._lock:
            doc = self._load_locked(); old = doc["uploads"].get(key)
            if old is None: raise YouTubeUploadStateValidationError("upload_not_found")
            new = deepcopy(old)
            if state is not None:
                if state not in BUNDLE_STATES or state != old["state"] and state not in _TRANSITIONS[old["state"]]: raise YouTubeUploadStateValidationError("invalid_transition")
                new["state"] = state
            if upload_job_id is not ...: new["upload_job_id"] = None if upload_job_id is None else _identifier(upload_job_id, "invalid_upload_job_id")
            if reason is not ...: new["reason"] = _reason(reason)
            new["updated_at"] = _now(self._clock); doc["uploads"][key] = _record_v2(new, key); self._write_locked(doc); return deepcopy(doc["uploads"][key])
    def set_upload_plan(self, streamer: Any, twitch_vod_id: Any, plan: Any) -> UploadRecord:
        key = canonical_upload_key(streamer, twitch_vod_id); plan = validate_upload_plan(plan)
        with self._lock:
            doc = self._load_locked(); old = doc["uploads"].get(key)
            if old is None: raise YouTubeUploadStateValidationError("upload_not_found")
            if old["upload_plan"] is not None: return deepcopy(old)
            if old["state"] != "intent_pending": raise YouTubeUploadStateValidationError("invalid_plan_transition")
            new = deepcopy(old); new.update({"upload_plan": plan, "state": "plan_ready", "reason": None, "updated_at": _now(self._clock)}); doc["uploads"][key] = _record_v2(new, key); self._write_locked(doc); return deepcopy(doc["uploads"][key])
    def set_preparation(self, streamer: Any, twitch_vod_id: Any, *, source_duration_seconds: Any, state: str, split: Any, parts: Any, reason: Any = None) -> UploadRecord:
        """Atomically persist one finalized original or pending split plan."""
        key = canonical_upload_key(streamer, twitch_vod_id)
        if state not in {"parts_ready", "parts_preparing", "needs_attention"}: raise YouTubeUploadStateValidationError("invalid_preparation_state")
        with self._lock:
            doc = self._load_locked(); old = doc["uploads"].get(key)
            if old is None: raise YouTubeUploadStateValidationError("upload_not_found")
            if old["state"] not in {"plan_ready", "parts_preparing", "parts_ready", "needs_attention"}: raise YouTubeUploadStateValidationError("invalid_preparation_transition")
            new = deepcopy(old); new.update({"source_duration_seconds": source_duration_seconds, "state": state, "part_plan_version": PART_PLAN_VERSION, "split": split, "parts": parts, "reason": reason, "updated_at": _now(self._clock)})
            normalized = _record_v2(new, key)
            if old["state"] in {"parts_preparing", "parts_ready", "needs_attention"} and any(normalized[name] != old[name] for name in ("source_duration_seconds", "part_plan_version", "split", "parts")):
                raise YouTubeUploadStateValidationError("preparation_immutable")
            doc["uploads"][key] = normalized; self._write_locked(doc); return deepcopy(normalized)
    def finalize_generated_parts(self, streamer: Any, twitch_vod_id: Any, *, parts: Any) -> UploadRecord:
        """Atomically expose a complete validated generated manifest."""
        key = canonical_upload_key(streamer, twitch_vod_id)
        with self._lock:
            doc = self._load_locked(); old = doc["uploads"].get(key)
            if old is None: raise YouTubeUploadStateValidationError("upload_not_found")
            if old["state"] not in {"parts_preparing", "needs_attention"} or old["split"] is None or old["parts"]: raise YouTubeUploadStateValidationError("invalid_generation_finalization")
            new = deepcopy(old); new.update({"state": "parts_ready", "parts": parts, "reason": None, "updated_at": _now(self._clock)})
            normalized = _record_v2(new, key)
            if not normalized["parts"] or any(part["source_kind"] != "generated" or part["upload_state"] != "ready" for part in normalized["parts"]): raise YouTubeUploadStateValidationError("invalid_generation_finalization")
            doc["uploads"][key] = normalized; self._write_locked(doc); return deepcopy(normalized)
