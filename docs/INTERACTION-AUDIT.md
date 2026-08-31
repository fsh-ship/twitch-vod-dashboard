# Interaction audit

This inventory records the current frontend behavior, not a change in product semantics. Local/form state remains inline; transient action results can use notifications; persistent operational state remains in its workspace; destructive or security-sensitive decisions require confirmation.

| Existing pattern and representative location | Category | Future treatment | Target |
| --- | --- | --- | --- |
| Settings save/status text (`markSettingsSaved`, `markSettingsScopeDirty`) | C, G | Keep inline: it describes unsaved/saving state for that form. | 11d |
| Search result errors, single-VOD status, Local VOD error (`renderResults`, `setSingleVodStatus`, `loadLocalVideos`) | C | Keep contextual inline validation/error surfaces. | 11b review |
| Dashboard/Live/Queue health and persistence warnings (`renderDashboard*`, `renderQueuePersistenceStatus`) | D | Keep persistent operational warnings in their workspaces. | No toast migration |
| Existing `showToast` Queue, Local VOD, Streamer actions | A, B | Shared toast foundation now supports these transient outcomes. Harmonize wording/variants later. | 11b |
| `alert()` success/info: playlist load, settings saves, template resets, streamer-file checks | A | Slice 11b1 migrates playlist-load success plus queue-start and template-reset acknowledgements. Settings saves and streamer-file checks remain inline/diagnostic. | 11b1/11d |
| `alert()` non-blocking failures: search/local refresh/download/maintenance catch handlers | B | Move simple failures to error toasts only after preserving useful inline detail. | 11b |
| `confirm()` for download selection, Auto YouTube release/playlist, manual upload completion, local deletion | E | Replace with an accessible confirmation dialog, retaining exact questions and guards. | 11c |
| YouTube connect/OAuth alerts (`youtubeConnect`) | F | Keep until the security/OAuth flow is reviewed; do not reduce important connection detail. | 11c |
| Queue retry/release, cleanup, recording action buttons | E, G | Preserve disabled/pending state and recovery context; add consistent progress feedback. | 11c/11d |
| Live refresh, playlist refresh, save buttons, Queue lane controls | G | Standardize button-level loading/saving feedback without changing polling or lifecycle behavior. | 11d |

Audit baseline found 31 `alert()` call sites and 4 `confirm()` call sites. Slice 11a migrated playlist-load success; Slice 11b1 migrated the queue-start acknowledgement and two harmless template-reset acknowledgements. The current baseline is 27 `alert()` call sites and 4 `confirm()` call sites. Remaining alerts are intentionally retained where they carry validation, diagnostics, persistent form state, operational failure, OAuth/security detail, or destructive-flow context.
