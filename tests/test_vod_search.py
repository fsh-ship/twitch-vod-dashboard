import unittest
from datetime import datetime
from unittest import mock

from vod_dashboard import vod_search


class VodSearchPayloadTests(unittest.TestCase):
    def setUp(self):
        self.settings = {
            "playlist_end": 25,
            "include_unknown_dates": True,
            "strict_date_filter": False,
            "exclude_live_streams": True,
            "only_real_vod_urls": True,
        }
        self.start = datetime(2026, 8, 1)
        self.end = datetime(2026, 8, 31)

    def run_search(self, data=None, default_streamers=None, result=None):
        date_parser = mock.Mock(
            side_effect=lambda value: {
                "2026-08-01": self.start,
                "2026-08-31": self.end,
            }.get(value)
        )
        integer_parser = mock.Mock(
            side_effect=lambda value, default: int(value)
        )
        search_service = mock.Mock(
            return_value=result
            or {"results": [], "errors": [], "debug": []}
        )
        source_runner = mock.Mock()
        detail_runner = mock.Mock()
        log_callback = mock.Mock()
        payload = vod_search.search_vods_from_payload(
            data or {},
            self.settings,
            [] if default_streamers is None else default_streamers,
            {"known-id"},
            date_parser=date_parser,
            integer_parser=integer_parser,
            search_service=search_service,
            source_runner=source_runner,
            detail_runner=detail_runner,
            log_callback=log_callback,
        )
        return payload, search_service, date_parser, integer_parser

    def test_no_selected_or_stored_streamers_passes_an_empty_list(self):
        payload, service, _, _ = self.run_search()

        self.assertEqual(payload, {"results": [], "errors": [], "debug": []})
        self.assertEqual(service.call_args.args[0], [])

    def test_one_payload_streamer_is_trimmed_and_at_prefix_removed(self):
        _, service, _, _ = self.run_search(
            {"streamers": "  @ExampleStreamer  "}, ["stored"]
        )

        self.assertEqual(service.call_args.args[0], ["ExampleStreamer"])

    def test_multiple_streamers_preserve_input_order_and_current_partial_values(self):
        _, service, _, _ = self.run_search(
            {"streamers": [" @Beta ", "", "Alpha", None]}
        )

        self.assertEqual(
            service.call_args.args[0], ["Beta", "Alpha", "None"]
        )

    def test_falsey_payload_streamer_selection_falls_back_to_stored_order(self):
        _, service, _, _ = self.run_search(
            {"streamers": []}, ["StoredTwo", "StoredOne"]
        )

        self.assertEqual(
            service.call_args.args[0], ["StoredTwo", "StoredOne"]
        )

    def test_dates_limit_flags_and_dependencies_are_forwarded_exactly(self):
        data = {
            "streamers": ["example"],
            "from": "2026-08-01",
            "to": "2026-08-31",
            "limit": "7",
            "include_unknown_dates": False,
            "strict_date_filter": True,
            "exclude_live_streams": False,
            "only_real_vod_urls": False,
        }
        _, service, date_parser, integer_parser = self.run_search(data)

        args = service.call_args.args
        kwargs = service.call_args.kwargs
        self.assertEqual(args[0], ["example"])
        self.assertIs(args[1], self.settings)
        self.assertEqual(args[2], {"known-id"})
        self.assertEqual(args[3:6], (self.start, self.end, 7))
        self.assertEqual(args[6:10], (False, True, False, False))
        self.assertIsNotNone(kwargs["source_runner"])
        self.assertIsNotNone(kwargs["detail_runner"])
        self.assertIsNotNone(kwargs["log_callback"])
        self.assertEqual(
            date_parser.call_args_list,
            [mock.call("2026-08-01"), mock.call("2026-08-31")],
        )
        integer_parser.assert_called_once_with("7", 25)

    def test_settings_defaults_and_minimum_limit_are_preserved(self):
        self.settings.update(
            playlist_end=0,
            include_unknown_dates=False,
            strict_date_filter=True,
            exclude_live_streams=False,
            only_real_vod_urls=False,
        )
        _, service, _, integer_parser = self.run_search(
            {"streamers": ["example"]}
        )

        self.assertEqual(service.call_args.args[5:10], (1, False, True, False, False))
        integer_parser.assert_called_once_with(0, 0)

    def test_existing_bool_coercion_keeps_nonempty_string_values_truthy(self):
        _, service, _, _ = self.run_search(
            {
                "streamers": ["example"],
                "include_unknown_dates": "false",
                "exclude_live_streams": "0",
            }
        )

        self.assertTrue(service.call_args.args[6])
        self.assertTrue(service.call_args.args[8])

    def test_search_result_payload_is_returned_without_transformation(self):
        expected = {
            "results": [{"id": "2"}, {"id": "1"}],
            "errors": [{"streamer": "broken", "error": "preserved"}],
            "debug": [{"streamer": "working", "kept": 2}],
        }

        payload, _, _, _ = self.run_search(
            {"streamers": ["working", "broken"]}, result=expected
        )

        self.assertIs(payload, expected)

    def test_search_service_failures_remain_visible_to_the_flask_adapter(self):
        with self.assertRaisesRegex(RuntimeError, "search failed"):
            vod_search.search_vods_from_payload(
                {"streamers": ["example"]},
                self.settings,
                [],
                set(),
                date_parser=lambda value: None,
                integer_parser=lambda value, default: 25,
                search_service=mock.Mock(
                    side_effect=RuntimeError("search failed")
                ),
                source_runner=mock.Mock(),
                detail_runner=mock.Mock(),
                log_callback=mock.Mock(),
            )


class DownloadSelectionTests(unittest.TestCase):
    @staticmethod
    def validator(value):
        text = str(value)
        if text.startswith("invalid"):
            return {"ok": False, "error": f"Invalid: {text}"}
        vod_id = text.rsplit("/", 1)[-1]
        return {
            "ok": True,
            "url": f"https://www.twitch.tv/videos/{vod_id}",
            "vod_id": vod_id,
        }

    def test_single_url_uses_canonical_value_and_existing_default_label(self):
        selection = vod_search.prepare_download_selection(
            {"url": "123"}, validator=self.validator
        )

        self.assertEqual(selection.urls, ["https://www.twitch.tv/videos/123"])
        self.assertEqual(selection.label, "Single VOD 123")
        self.assertIsNone(selection.error)

    def test_vod_url_alias_and_custom_label_are_preserved(self):
        selection = vod_search.prepare_download_selection(
            {"vod_url": "456", "label": "Chosen VOD"},
            validator=self.validator,
        )

        self.assertEqual(selection.urls, ["https://www.twitch.tv/videos/456"])
        self.assertEqual(selection.label, "Chosen VOD")

    def test_invalid_single_url_returns_validator_payload_unchanged(self):
        error = {"ok": False, "error": "preserved", "input": "invalid"}
        selection = vod_search.prepare_download_selection(
            {"url": "invalid"}, validator=mock.Mock(return_value=error)
        )

        self.assertIs(selection.error, error)
        self.assertEqual(selection.urls, [])

    def test_batch_skips_blanks_deduplicates_and_preserves_first_order(self):
        selection = vod_search.prepare_download_selection(
            {"urls": [" 2 ", "", None, "1", "2"]},
            validator=self.validator,
        )

        self.assertEqual(
            selection.urls,
            [
                "https://www.twitch.tv/videos/2",
                "https://www.twitch.tv/videos/1",
            ],
        )
        self.assertEqual(selection.label, "2 VOD(s)")

    def test_string_batch_value_remains_one_url(self):
        selection = vod_search.prepare_download_selection(
            {"urls": "789"}, validator=self.validator
        )

        self.assertEqual(selection.urls, ["https://www.twitch.tv/videos/789"])
        self.assertEqual(selection.label, "1 VOD(s)")

    def test_batch_stops_at_first_invalid_url(self):
        validator = mock.Mock(side_effect=self.validator)
        selection = vod_search.prepare_download_selection(
            {"urls": ["1", "invalid-2", "3"]}, validator=validator
        )

        self.assertEqual(
            selection.error, {"ok": False, "error": "Invalid: invalid-2"}
        )
        self.assertEqual(
            validator.call_args_list, [mock.call("1"), mock.call("invalid-2")]
        )

    def test_empty_selection_preserves_current_error_payload(self):
        selection = vod_search.prepare_download_selection(
            {"urls": ["", None]}, validator=self.validator
        )

        self.assertEqual(
            selection.error,
            {"ok": False, "error": "No valid VOD URLs were provided."},
        )
        self.assertEqual(selection.label, "0 VOD(s)")


if __name__ == "__main__":
    unittest.main()
