# Interaction audit

This inventory records the current frontend behavior, not a change in product semantics. Local/form state remains inline; transient action results can use notifications; persistent operational state remains in its workspace; destructive or security-sensitive decisions require confirmation.

| Existing pattern and representative location | Category | Future treatment | Target |
| --- | --- | --- | --- |
| Settings save/status text (`markSettingsSaved`, `markSettingsScopeDirty`) | C, G | Keep inline: it describes unsaved/saving state for that form. | 11d |
| Search result errors, single-VOD status, Local VOD error (`renderResults`, `setSingleVodStatus`, `loadLocalVideos`) | C | Keep contextual inline validation/error surfaces. | 11b review |
| Dashboard/Live/Queue health and persistence warnings (`renderDashboard*`, `renderQueuePersistenceStatus`) | D | Keep persistent operational warnings in their workspaces. | No toast migration |
| Existing `showToast` Queue, Local VOD, Streamer actions | A, B | Shared toast foundation now supports these transient outcomes. Harmonize wording/variants later. | 11b |
| `alert()` success/info: playlist load, settings saves, template resets, streamer-file checks | A | Slice 11b1 migrates playlist-load success plus queue-start and template-reset acknowledgements. Settings saves and streamer-file checks remain inline/diagnostic. | 11b1/11d |
| `alert()` non-blocking failures: playlist refresh and streamer-file status check | B | Slice 11b2 migrates only these simple action failures to error toasts. Search and Local VOD failures remain inline; upload/download and repair/recovery failures retain operational context. | 11b2 |
| Shared confirmation dialog for download selection, Auto YouTube release/playlist, manual upload completion, local deletion | E | Slice 11c uses one Promise-based dialog. Confirm continues the existing guarded action; Cancel, Escape, and backdrop click cancel it. | 11c |
| YouTube connect/OAuth alerts (`youtubeConnect`) | F | Keep until the security/OAuth flow is reviewed; do not reduce important connection detail. | 11c |
| Queue retry/release, cleanup, recording action buttons | E, G | Preserve disabled/pending state and recovery context; add consistent progress feedback. | 11c/11d |
| Live refresh, playlist refresh, save buttons, Queue lane controls | G | Standardize button-level loading/saving feedback without changing polling or lifecycle behavior. | 11d |

Audit baseline found 31 `alert()` call sites and 4 `confirm()` call sites. Slice 11a migrated playlist-load success; Slice 11b1 migrated the queue-start acknowledgement and two harmless template-reset acknowledgements; Slice 11b2 migrated playlist-refresh and read-only streamer-file-check failures; Slice 11c replaced all four native confirmations with the shared dialog. The current baseline is 25 `alert()` call sites and 0 native `confirm()` call sites. Remaining alerts are intentionally retained where they carry validation, diagnostics, persistent form state, operational failure, OAuth/security detail, or destructive-flow context.
