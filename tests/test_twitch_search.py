import unittest
from datetime import datetime
from unittest import mock

from vod_dashboard import twitch


RESULT_KEYS = {
    "streamer",
    "title",
    "date",
    "url",
    "id",
    "already_downloaded",
    "date_enriched",
    "outside_range",
}
DEBUG_KEYS = {
    "streamer",
    "source",
    "found_raw",
    "deduped",
    "kept",
    "unknown_dates",
    "date_metadata_enriched",
    "date_enrichment_failed",
    "skipped_by_date",
    "skipped_live",
    "skipped_nonvod",
}


class TwitchSearchOrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.settings = {"enrich_vod_dates": False}
        self.start = datetime(2026, 8, 1)
        self.end = datetime(2026, 8, 31)

    @staticmethod
    def entry(vod_id, title="VOD", upload_date="20260810", **updates):
        value = {
            "id": str(vod_id),
            "title": title,
            "upload_date": upload_date,
            "url": f"https://www.twitch.tv/videos/{vod_id}",
        }
        value.update(updates)
        return value

    @staticmethod
    def playlist(source, entries, returncode=0, stderr=""):
        return {
            "_source_url": source,
            "_returncode": returncode,
            "_stderr": stderr,
            "entries": entries,
        }

    def search(
        self,
        playlists,
        *,
        streamers=None,
        settings=None,
        known_vod_ids=None,
        start=None,
        end=None,
        include_unknown=True,
        strict_date_filter=False,
        exclude_live=True,
        only_real_vods=True,
        detail_runner=None,
        log_callback=None,
    ):
        source_runner = (
            playlists
            if callable(playlists)
            else mock.Mock(return_value=playlists)
        )
        return twitch.search_vods(
            streamers or ["example"],
            settings or self.settings,
            known_vod_ids or set(),
            self.start if start is None else start,
            self.end if end is None else end,
            25,
            include_unknown,
            strict_date_filter,
            exclude_live,
            only_real_vods,
            source_runner=source_runner,
            detail_runner=detail_runner,
            log_callback=log_callback,
        )

    def test_one_streamer_exact_result_error_and_debug_schema(self):
        payload = self.search(
            [self.playlist("source-one", [self.entry("1234567890")])]
        )

        self.assertEqual(set(payload), {"results", "errors", "debug"})
        self.assertEqual(payload["errors"], [])
        self.assertEqual(len(payload["results"]), 1)
        self.assertEqual(set(payload["results"][0]), RESULT_KEYS)
        self.assertNotIn("duration", payload["results"][0])
        self.assertEqual(set(payload["debug"][0]), DEBUG_KEYS)
        self.assertEqual(
            payload["debug"][0],
            {
                "streamer": "example",
                "source": "source-one",
                "found_raw": 1,
                "deduped": 1,
                "kept": 1,
                "unknown_dates": 0,
                "date_metadata_enriched": 0,
                "date_enrichment_failed": 0,
                "skipped_by_date": 0,
                "skipped_live": 0,
                "skipped_nonvod": 0,
            },
        )

    def test_multiple_streamers_keep_processing_order_but_sort_results(self):
        calls = []

        def source_runner(streamer, limit, settings):
            calls.append((streamer, limit, settings))
            entries = {
                "alpha": [self.entry("1234567890", "Older", "20260809")],
                "beta": [self.entry("2345678901", "Newer", "20260811")],
            }
            return [self.playlist(f"source-{streamer}", entries[streamer])]

        payload = self.search(
            source_runner, streamers=["alpha", "beta"]
        )

        self.assertEqual([call[0] for call in calls], ["alpha", "beta"])
        self.assertTrue(all(call[1] == 25 for call in calls))
        self.assertTrue(all(call[2] is self.settings for call in calls))
        self.assertEqual(
            [item["streamer"] for item in payload["results"]],
            ["beta", "alpha"],
        )
        self.assertEqual(
            [item["streamer"] for item in payload["debug"]],
            ["alpha", "beta"],
        )

    def test_multiple_sources_cross_source_duplicates_keep_first_entry(self):
        duplicate_id = "1234567890"
        payload = self.search(
            [
                self.playlist(
                    "archives",
                    [self.entry(duplicate_id, "First title")],
                ),
                self.playlist(
                    "all",
                    [
                        self.entry(duplicate_id, "Later duplicate"),
                        self.entry("2345678901", "Second VOD"),
                    ],
                ),
            ]
        )

        self.assertEqual(len(payload["results"]), 2)
        by_id = {item["id"]: item for item in payload["results"]}
        self.assertEqual(by_id[duplicate_id]["title"], "First title")
        self.assertEqual(payload["debug"][0]["source"], "archives, all")
        self.assertEqual(payload["debug"][0]["found_raw"], 3)
        self.assertEqual(payload["debug"][0]["deduped"], 2)

    def test_sorting_uses_date_then_streamer_then_title_descending(self):
        entries = [
            self.entry("1234567890", "Alpha", "20260810"),
            self.entry("2345678901", "Zulu", "20260810"),
            self.entry("3456789012", "Newest", "20260811"),
        ]
        payload = self.search([self.playlist("source", entries)])
        self.assertEqual(
            [item["title"] for item in payload["results"]],
            ["Newest", "Zulu", "Alpha"],
        )

    def test_date_boundaries_are_inclusive_under_strict_filtering(self):
        entries = [
            self.entry("1234567890", "Start", "20260801"),
            self.entry("2345678901", "End", "20260831"),
            self.entry("3456789012", "Before", "20260731"),
            self.entry("4567890123", "After", "20260901"),
        ]
        payload = self.search(
            [self.playlist("source", entries)], strict_date_filter=True
        )
        self.assertEqual(
            {item["title"] for item in payload["results"]},
            {"Start", "End"},
        )
        self.assertEqual(payload["debug"][0]["skipped_by_date"], 2)

    def test_unknown_dates_can_be_included_or_excluded(self):
        unknown = self.entry("1234567890", upload_date="")
        included = self.search(
            [self.playlist("source", [unknown])],
            include_unknown=True,
            strict_date_filter=True,
        )
        excluded = self.search(
            [self.playlist("source", [unknown])],
            include_unknown=False,
            strict_date_filter=True,
        )

        self.assertEqual(included["results"][0]["date"], "unknown")
        self.assertFalse(included["results"][0]["outside_range"])
        self.assertEqual(excluded["results"], [])
        self.assertEqual(excluded["debug"][0]["unknown_dates"], 1)
        self.assertEqual(excluded["debug"][0]["skipped_by_date"], 1)

    def test_include_unknown_off_filters_unknown_without_strict_range_filter(self):
        payload = self.search(
            [self.playlist("source", [self.entry("1234567890", upload_date="")])],
            include_unknown=False,
            strict_date_filter=False,
        )
        self.assertEqual(payload["results"], [])
        self.assertEqual(payload["debug"][0]["skipped_by_date"], 1)

    def test_live_upcoming_and_non_vod_entries_are_excluded(self):
        entries = [
            self.entry("1234567890", "Live", is_live=True),
            self.entry("2345678901", "Upcoming", live_status="is_upcoming"),
            {"title": "Channel page", "url": "https://www.twitch.tv/example"},
            self.entry("3456789012", "Completed", live_status="was_live"),
        ]
        payload = self.search([self.playlist("source", entries)])

        self.assertEqual(
            [item["title"] for item in payload["results"]], ["Completed"]
        )
        self.assertEqual(payload["debug"][0]["skipped_live"], 2)
        self.assertEqual(payload["debug"][0]["kept"], 1)

    def test_archive_marking_uses_the_supplied_id_set(self):
        entries = [
            self.entry("1234567890", "Archived"),
            self.entry("2345678901", "New"),
        ]
        payload = self.search(
            [self.playlist("source", entries)],
            known_vod_ids={"1234567890"},
        )
        by_id = {item["id"]: item for item in payload["results"]}
        self.assertTrue(by_id["1234567890"]["already_downloaded"])
        self.assertFalse(by_id["2345678901"]["already_downloaded"])

    def test_missing_date_is_enriched_and_missing_title_is_filled(self):
        settings = {"enrich_vod_dates": True}
        detail_runner = mock.Mock(
            return_value={"upload_date": "20260812", "title": "Detail title"}
        )
        entry = self.entry("1234567890", title="", upload_date="")
        payload = self.search(
            [self.playlist("source", [entry])],
            settings=settings,
            detail_runner=detail_runner,
        )

        detail_runner.assert_called_once_with(
            "https://www.twitch.tv/videos/1234567890", settings
        )
        result = payload["results"][0]
        self.assertEqual(result["date"], "2026-08-12")
        self.assertEqual(result["title"], "Detail title")
        self.assertTrue(result["date_enriched"])
        self.assertEqual(payload["debug"][0]["date_metadata_enriched"], 1)
        self.assertEqual(payload["debug"][0]["date_enrichment_failed"], 0)

    def test_existing_flat_date_never_triggers_detail_extraction(self):
        settings = {"enrich_vod_dates": True}
        detail_runner = mock.Mock(
            side_effect=AssertionError("dated entries must remain flat-only")
        )
        payload = self.search(
            [self.playlist("source", [self.entry("1234567890")])],
            settings=settings,
            detail_runner=detail_runner,
        )

        detail_runner.assert_not_called()
        self.assertEqual(payload["results"][0]["date"], "2026-08-10")
        self.assertEqual(payload["debug"][0]["date_metadata_enriched"], 0)

    def test_prefixed_flat_id_is_enriched_through_canonical_url_before_filter(self):
        settings = {"enrich_vod_dates": True}
        detail_runner = mock.Mock(
            return_value={"upload_date": "20260823", "title": "Subscriber VOD"}
        )
        payload = self.search(
            [
                self.playlist(
                    "source",
                    [{"id": "v2854443252", "title": "", "upload_date": ""}],
                )
            ],
            settings=settings,
            include_unknown=False,
            strict_date_filter=True,
            detail_runner=detail_runner,
        )

        detail_runner.assert_called_once_with(
            "https://www.twitch.tv/videos/2854443252", settings
        )
        self.assertEqual(len(payload["results"]), 1)
        self.assertEqual(payload["results"][0]["id"], "2854443252")
        self.assertEqual(payload["results"][0]["date"], "2026-08-23")

    def test_timestamp_enrichment_and_recovered_out_of_range_filtering(self):
        settings = {"enrich_vod_dates": True}
        inside = self.search(
            [
                self.playlist(
                    "source", [self.entry("1234567890", upload_date="")]
                )
            ],
            settings=settings,
            include_unknown=False,
            strict_date_filter=True,
            detail_runner=mock.Mock(return_value={"timestamp": 1_787_507_539}),
        )
        outside = self.search(
            [
                self.playlist(
                    "source", [self.entry("1234567890", upload_date="")]
                )
            ],
            settings=settings,
            start=datetime(2026, 8, 24),
            end=datetime(2026, 8, 31),
            include_unknown=False,
            strict_date_filter=True,
            detail_runner=mock.Mock(return_value={"timestamp": 1_787_507_539}),
        )

        self.assertEqual(inside["results"][0]["date"], "2026-08-23")
        self.assertEqual(outside["results"], [])
        self.assertEqual(outside["debug"][0]["skipped_by_date"], 1)

    def test_one_enrichment_failure_is_isolated_and_unknown_setting_still_applies(self):
        settings = {"enrich_vod_dates": True}
        entries = [
            self.entry("1234567890", "Recovered", upload_date=""),
            self.entry("2345678901", "Unavailable", upload_date=""),
        ]

        def detail(url, configured):
            self.assertIs(configured, settings)
            if url.endswith("2345678901"):
                raise RuntimeError("private signed URL and cookie detail")
            return {"release_timestamp": 1_787_507_539}

        safe_log = mock.Mock()
        excluded = self.search(
            [self.playlist("source", entries)],
            settings=settings,
            include_unknown=False,
            strict_date_filter=False,
            detail_runner=detail,
            log_callback=safe_log,
        )
        included = self.search(
            [self.playlist("source", entries)],
            settings=settings,
            include_unknown=True,
            strict_date_filter=True,
            detail_runner=detail,
        )

        self.assertEqual([item["title"] for item in excluded["results"]], ["Recovered"])
        self.assertEqual(len(included["results"]), 2)
        unavailable = next(
            item for item in included["results"] if item["title"] == "Unavailable"
        )
        self.assertEqual(unavailable["date"], "unknown")
        self.assertNotIn("private signed", str(excluded))
        safe_log.assert_called_once_with(
            "Date metadata enrichment failed for Twitch VOD 2345678901."
        )

    def test_missing_dates_use_two_workers_and_request_cache_deduplicates_ids(self):
        settings = {"enrich_vod_dates": True}
        entries = [
            self.entry("1234567890", upload_date=""),
            self.entry("2345678901", upload_date=""),
            self.entry("3456789012", upload_date=""),
        ]
        detail_runner = mock.Mock(return_value={"upload_date": "20260810"})
        with mock.patch(
            "vod_dashboard.twitch.ThreadPoolExecutor",
            wraps=twitch.ThreadPoolExecutor,
        ) as executor:
            payload = self.search(
                [self.playlist("source", entries)],
                settings=settings,
                detail_runner=detail_runner,
            )
        executor.assert_called_once_with(max_workers=2)
        self.assertEqual(detail_runner.call_count, 3)
        self.assertEqual(len(payload["results"]), 3)

        repeated = mock.Mock(return_value={"upload_date": "20260810"})

        def sources(streamer, limit, configured):
            return [
                self.playlist(
                    streamer,
                    [self.entry("4567890123", streamer, upload_date="")],
                )
            ]

        duplicate_payload = self.search(
            sources,
            streamers=["alpha", "beta"],
            settings=settings,
            detail_runner=repeated,
        )
        repeated.assert_called_once_with(
            "https://www.twitch.tv/videos/4567890123", settings
        )
        self.assertEqual(len(duplicate_payload["results"]), 2)

    def test_malformed_unknown_id_is_never_sent_to_detail_extractor(self):
        detail_runner = mock.Mock()
        payload = self.search(
            [
                self.playlist(
                    "source",
                    [{"id": "v12-not-valid", "title": "Malformed"}],
                )
            ],
            settings={"enrich_vod_dates": True},
            detail_runner=detail_runner,
        )
        detail_runner.assert_not_called()
        self.assertEqual(payload["results"], [])

    def test_enrichment_failure_is_logged_and_kept_as_unknown(self):
        settings = {"enrich_vod_dates": True}
        detail_runner = mock.Mock(side_effect=RuntimeError("detail failed"))
        log_callback = mock.Mock()
        payload = self.search(
            [
                self.playlist(
                    "source", [self.entry("1234567890", upload_date="")]
                )
            ],
            settings=settings,
            detail_runner=detail_runner,
            log_callback=log_callback,
        )

        self.assertEqual(payload["results"][0]["date"], "unknown")
        self.assertFalse(payload["results"][0]["date_enriched"])
        log_callback.assert_called_once_with(
            "Date metadata enrichment failed for Twitch VOD 1234567890."
        )
        self.assertNotIn("detail failed", str(payload))
        self.assertEqual(payload["debug"][0]["date_enrichment_failed"], 1)

    def test_partial_source_failure_keeps_successful_fallback(self):
        payload = self.search(
            [
                self.playlist("archives", [], 1, "first failed"),
                self.playlist(
                    "all", [self.entry("1234567890", "Fallback VOD")]
                ),
            ]
        )
        self.assertEqual([item["title"] for item in payload["results"]], ["Fallback VOD"])
        self.assertEqual(payload["errors"], [])
        self.assertEqual(payload["debug"][0]["source"], "archives, all")

    def test_all_sources_failing_remains_debug_only(self):
        payload = self.search(
            [
                self.playlist("archives", [], 1, "failed"),
                self.playlist("all", [], -999, "malformed"),
                self.playlist("videos", [], 1, "failed"),
            ]
        )
        self.assertEqual(payload["results"], [])
        self.assertEqual(payload["errors"], [])
        self.assertEqual(len(payload["debug"]), 1)
        self.assertEqual(
            payload["debug"][0]["source"], "archives, all, videos"
        )

    def test_per_streamer_exceptions_are_aggregated_with_partial_success(self):
        def source_runner(streamer, limit, settings):
            if streamer == "missing-module":
                raise FileNotFoundError("ignored detail")
            if streamer == "broken":
                raise RuntimeError("search failed")
            return [
                self.playlist(
                    "working", [self.entry("1234567890", "Working VOD")]
                )
            ]

        payload = self.search(
            source_runner,
            streamers=["missing-module", "working", "broken"],
        )
        self.assertEqual([item["streamer"] for item in payload["results"]], ["working"])
        self.assertEqual(
            payload["errors"],
            [
                {
                    "streamer": "missing-module",
                    "error": "Enter a valid Twitch streamer login.",
                },
                {
                    "streamer": "broken",
                    "error": "Twitch VOD discovery failed.",
                },
            ],
        )
        self.assertEqual([item["streamer"] for item in payload["debug"]], ["working"])

    def test_malformed_entries_are_counted_but_not_returned(self):
        payload = self.search(
            [
                self.playlist(
                    "source",
                    [
                        "not-a-dictionary",
                        {"title": "No URL"},
                        {"id": "12345", "title": "Short invalid ID"},
                        self.entry("1234567890", "Valid"),
                    ],
                )
            ]
        )
        self.assertEqual([item["title"] for item in payload["results"]], ["Valid"])
        self.assertEqual(payload["debug"][0]["found_raw"], 4)
        self.assertEqual(payload["debug"][0]["deduped"], 3)
        self.assertEqual(payload["debug"][0]["kept"], 1)

    def test_empty_playlist_results_have_the_exact_top_level_shape(self):
        payload = self.search([])
        self.assertEqual(
            payload,
            {
                "results": [],
                "errors": [],
                "debug": [
                    {
                        "streamer": "example",
                        "source": "",
                        "found_raw": 0,
                        "deduped": 0,
                        "kept": 0,
                        "unknown_dates": 0,
                        "date_metadata_enriched": 0,
                        "date_enrichment_failed": 0,
                        "skipped_by_date": 0,
                        "skipped_live": 0,
                        "skipped_nonvod": 0,
                    }
                ],
            },
        )


class TwitchVodDiscoveryTests(unittest.TestCase):
    @staticmethod
    def entry(vod_id, title="VOD", upload_date="20260810", **updates):
        entry = {
            "id": str(vod_id),
            "title": title,
            "upload_date": upload_date,
            "url": f"https://www.twitch.tv/videos/{vod_id}",
        }
        entry.update(updates)
        return entry

    @staticmethod
    def source(entries, label="archives"):
        return [{"_source_url": label, "entries": entries}]

    def discover(self, entries, **kwargs):
        return twitch.discover_streamer_vods(
            "Example_Streamer",
            kwargs.pop("settings", {"enrich_vod_dates": True}),
            source_runner=kwargs.pop("source_runner", mock.Mock(return_value=self.source(entries))),
            **kwargs,
        )

    def test_public_discovery_defaults_to_ten_canonical_newest_first_vods(self):
        entries = [self.entry(2_854_443_252 - index) for index in range(11)]
        runner = mock.Mock(return_value=self.source(entries))

        payload = self.discover(entries, source_runner=runner)

        self.assertEqual(payload["streamer"], "example_streamer")
        self.assertIsNone(payload["error"])
        self.assertEqual(len(payload["vods"]), 10)
        self.assertEqual(
            [item["twitch_vod_id"] for item in payload["vods"]],
            [str(2_854_443_252 - index) for index in range(10)],
        )
        runner.assert_called_once_with(
            "example_streamer", 10, {"enrich_vod_dates": True}
        )

    def test_custom_limit_is_bounded_and_ids_are_canonicalized(self):
        entries = [
            self.entry("v2854443252", "Prefixed"),
            self.entry("v2854443252", "Duplicate"),
            {"id": "v12-not-valid", "title": "Malformed"},
            self.entry("2854443251", "Second"),
        ]
        runner = mock.Mock(return_value=self.source(entries))

        payload = self.discover(entries, limit=999, source_runner=runner)

        runner.assert_called_once_with(
            "example_streamer", twitch.VOD_DISCOVERY_MAX_LIMIT,
            {"enrich_vod_dates": True},
        )
        self.assertEqual(
            [item["twitch_vod_id"] for item in payload["vods"]],
            ["2854443252", "2854443251"],
        )
        self.assertEqual(
            payload["vods"][0]["canonical_url"],
            "https://www.twitch.tv/videos/2854443252",
        )
        self.assertEqual(payload["diagnostics"]["deduped"], 3)

        small = self.discover(
            entries,
            limit=1,
            source_runner=mock.Mock(return_value=self.source(entries)),
        )
        self.assertEqual(
            [item["twitch_vod_id"] for item in small["vods"]],
            ["2854443252"],
        )

    def test_discovery_reuses_targeted_date_enrichment_and_keeps_failures(self):
        dated = self.entry("2854443252", upload_date="20260823")
        unknown = self.entry("2854443251", title="", upload_date="")
        failed = self.entry("2854443250", upload_date="")
        calls = []

        def detail(url, settings):
            calls.append((url, settings))
            if url.endswith("2854443250"):
                raise RuntimeError("private COOKIE=secret signed manifest")
            return {"release_timestamp": 1_787_507_539, "title": "Recovered"}

        payload = self.discover(
            [dated, unknown, failed],
            settings={"enrich_vod_dates": True, "cookie_file": "cookie.txt"},
            detail_runner=detail,
        )

        self.assertEqual(len(calls), 2)
        self.assertTrue(all(call[1]["cookie_file"] == "cookie.txt" for call in calls))
        self.assertEqual(payload["vods"][0]["upload_date"], "2026-08-23")
        self.assertEqual(payload["vods"][1]["upload_date"], "2026-08-23")
        self.assertEqual(payload["vods"][1]["title"], "Recovered")
        self.assertIsNone(payload["vods"][2]["upload_date"])
        self.assertEqual(payload["diagnostics"]["date_metadata_enriched"], 1)
        self.assertEqual(payload["diagnostics"]["date_enrichment_failed"], 1)
        self.assertNotIn("COOKIE=secret", str(payload))
        self.assertNotIn("manifest", str(payload))

    def test_discovery_filters_live_and_non_vod_without_archive_or_date_policy(self):
        payload = self.discover(
            [
                self.entry("2854443252", "Known", upload_date=""),
                self.entry("2854443251", "Live", is_live=True),
                {"title": "Channel", "url": "https://www.twitch.tv/example_streamer"},
                self.entry("2854443250", "Completed", upload_date=""),
            ],
            settings={"enrich_vod_dates": False},
        )

        self.assertEqual(
            [item["twitch_vod_id"] for item in payload["vods"]],
            ["2854443252", "2854443250"],
        )
        self.assertTrue(all(item["upload_date"] is None for item in payload["vods"]))
        self.assertEqual(payload["diagnostics"]["skipped_live"], 1)
        self.assertEqual(payload["diagnostics"]["skipped_by_date"], 0)
        self.assertNotIn("already_downloaded", str(payload))

    def test_discovery_returns_only_safe_error_codes_and_has_no_flask_or_job_dependency(self):
        payload = twitch.discover_streamer_vods(
            "example_streamer",
            {"cookie_file": "COOKIE-SENTINEL"},
            source_runner=mock.Mock(
                side_effect=RuntimeError(
                    "Authorization: SECRET signed-url manifest.m3u8 command"
                )
            ),
        )
        invalid = twitch.discover_streamer_vods("not-valid!", {})

        self.assertEqual(payload["error"], {"code": "yt_dlp_failed"})
        self.assertEqual(invalid["error"], {"code": "invalid_streamer"})
        self.assertNotIn("COOKIE-SENTINEL", str(payload))
        self.assertNotIn("SECRET", str(payload))
        self.assertNotIn("manifest.m3u8", str(payload))
        self.assertNotIn("JobManager", twitch.discover_streamer_vods.__code__.co_names)
        self.assertNotIn("Flask", twitch.discover_streamer_vods.__code__.co_names)


if __name__ == "__main__":
    unittest.main()
