# Automation product-model compatibility

This document records the pre-Slice-8b settings semantics that the product
model must preserve. The compatibility helpers live in
`vod_dashboard/automation_policy.py`; they are pure translations and are not
called by coordinators, workers, settings persistence, or API routes.

## Current persisted settings

Settings remain stored in `dashboard-settings.json`. A load merges stored data
over the current defaults and normalizes the result in memory. A save accepts
only known default-setting keys, normalizes the merged settings, and atomically
writes the existing schema. The explicitly configured legacy settings file is
read only when the current settings file does not exist; it is not rewritten
until a later ordinary save.

The current global automation fields are:

| Field | Default | Current behavior |
|---|---:|---|
| `auto_vod_enabled` | `false` | Strict JSON-boolean operational gate for Auto VOD monitoring. It does not define streamer policy. |
| `auto_vod_poll_minutes` | `60` | Monitoring cadence; only `60` and `120` survive normalization. |
| `auto_youtube_enabled` | `false` | Strict JSON-boolean admission gate for the Auto VOD-to-Auto YouTube handoff. It does not define streamer policy. |
| `auto_recorder_enabled` | `false` | Global operational gate for automatic live recording. |
| `auto_youtube_cleanup_delay_hours` | `0` | Default frozen into a newly admitted Auto YouTube intent. Supported automatic delays are 1, 3, 6, 12, 24, and 48 hours; `0` means no automatic cleanup. |
| `youtube_enabled` | `false` | Legacy/manual-download auto-upload gate. It is only advisory for an explicit local upload and is not consulted by the Auto YouTube ownership path. |
| `youtube_auto_upload` | `false` | Requests the legacy upload-after-manual-download workflow, subject to `youtube_enabled`. |
| `move_uploaded_vods` | `true` | Legacy archive-after-success behavior for the older upload path. It is not Auto YouTube retention. |

`youtube_enabled`, `youtube_auto_upload`, `auto_recorder_enabled`, and other
older booleans use the legacy permissive boolean normalizer. The newer
`auto_vod_enabled` and `auto_youtube_enabled` fields only enable on the literal
JSON boolean `true`.

Per-streamer settings remain a compact mapping under `streamer_profiles`, keyed
by canonical lowercase Twitch login. Its allowlisted fields are:

- `auto_vod_download: true`
- `auto_youtube_upload: true`
- `auto_record: true`
- non-empty `youtube_playlist_id`

False flags are represented by absence. Normalization keeps each true flag
independently, including the historical inconsistent combination containing
only `auto_youtube_upload: true`. It does not repair that combination.

`POST /api/settings` continues to accept and return the current settings fields.
`POST /api/streamers` continues to accept `streamers` and, when supplied,
`streamer_profiles`; it only includes `streamer_profiles` in its response when
the caller supplied them. No product-level field is persisted or added to these
contracts in Slice 8a.

## Operational semantics

Auto VOD runs only while `auto_vod_enabled` is true and selects configured
streamers whose profile has `auto_vod_download is True`. Global pause and
streamer policy are independent. Settings changes can wake the monitor, but
loading or translating settings does not create a job.

Auto YouTube admission requires:

1. a durably completed, single-item source job owned by Auto VOD;
2. `auto_youtube_enabled is True` at completion admission; and
3. `auto_youtube_upload is True` for the source streamer.

The handoff does not separately re-check `auto_vod_download`; Auto VOD source
ownership is the workflow boundary. Admission freezes execution policy,
cleanup delay, and the profile playlist into durable ownership state. The
compatibility model does not modify this logic.

Auto Recorder runs only while `auto_recorder_enabled` is true and selects
configured streamers whose profile has `auto_record is True`. It remains a
separate product dimension from completed-VOD handling.

## Product mapping

Completed-VOD handling maps to the compact current fields as follows:

| Product mode | `auto_vod_download` | `auto_youtube_upload` |
|---|---:|---:|
| `manual` | false/absent | false/absent |
| `auto_download` | true | false/absent |
| `download_and_youtube` | true | true |
| `needs_review` | false/absent | true |

`needs_review` is derived state, not an applicable product choice. Deriving it
does not repair or rewrite the stored flags. A later explicit choice of one of
the three valid modes is the resolution boundary.

Global operational controls are intentionally not inputs to this mapping. A
streamer remains `download_and_youtube` when Auto VOD monitoring or Automatic
YouTube processing is globally paused.

Live Recording maps independently:

| Product mode | `auto_record` |
|---|---:|
| `manual` | false/absent |
| `automatic` | true |

## Playlist behavior

A playlist is not required for `download_and_youtube`. At Auto YouTube
admission, the per-streamer playlist is frozen; blank means no playlist was
requested. That handoff deliberately does not fall back to the global default
playlist. If a playlist was requested, playlist processing remains a separate
post-upload lifecycle action and failures do not roll back a confirmed video
upload.

The older explicit/manual upload path retains its existing resolution order:
an explicit per-item choice, then the streamer playlist, then the global
playlist. Slice 8a does not unify these different contexts or add a fallback.

The validation helper therefore treats a blank playlist as valid. It reports a
configured playlist as `unavailable_dependency` only when a caller has
independently verified that it is unavailable.

## Retention and legacy archiving

The product representation of the current Auto YouTube admission default is:

- `keep_local` -> persisted cleanup delay `0`;
- `cleanup_after_delay` -> one of 1, 3, 6, 12, 24, or 48 hours.

The value is a global default today, not a per-streamer persisted setting. In
the compatibility payload, `automatic_cleanup_configured` is false for delay
`0` and true for a supported nonzero delay. The product mode name `keep_local`
describes the intended default outcome only; it does **not** mean that a
durable item-level `keep_local` override exists.

The global default is frozen into a durable Auto YouTube record at admission.
Cleanup is scheduled only through the existing confirmed lifecycle and
ownership rules. An individual durable record can separately carry its own
`keep_local` override. That item-specific override remains distinct from the
global delay-`0` admission default even though both prevent automatic removal.

The legacy `move_uploaded_vods` option archives media after a successful older
upload workflow. It is technically separate and is never translated into the
Auto YouTube retention model.

## Legacy manual-download upload and `youtube_enabled`

`youtube_auto_upload` is confirmed to belong to manual downloads. Auto VOD jobs
are created in download-only post-processing mode and cannot enter this legacy
path. The setting is therefore intentionally excluded from VOD Handling.

For that legacy path, both `youtube_enabled` and `youtube_auto_upload` must be
true before upload is attempted. When either gate prevents upload, completed
media can still be prepared for later upload.

For an explicit local upload, `youtube_enabled == false` only produces a note;
the worker still attempts the user-requested upload if YouTube is connected.
The Auto YouTube handoff and executor do not consult `youtube_enabled`; they use
their own admission policy and connection checks. Consequently, the existing
“Enable YouTube Uploads” label is not technically a global upload kill switch.
Slice 8b should present it as a legacy manual-workflow gate or retire its visual
prominence without silently changing the field.

## Validation states

The compatibility layer exposes three states:

- `valid`: policy is representable under current semantics;
- `needs_review`: persisted intent is internally inconsistent or a retention
  delay cannot be represented safely;
- `unavailable_dependency`: policy is valid, but a caller has verified that a
  required YouTube connection or configured playlist is unavailable.

A temporary global pause is never a validation error. Dependency availability
is optional input because settings alone cannot prove live connection state.
