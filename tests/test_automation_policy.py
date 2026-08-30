import unittest

from vod_dashboard import automation_policy, settings


class AutomationPolicyCompatibilityTests(unittest.TestCase):
    def test_vod_handling_maps_every_persisted_flag_combination(self):
        cases = (
            (False, False, automation_policy.VOD_MANUAL, automation_policy.VALID),
            (
                True,
                False,
                automation_policy.VOD_AUTO_DOWNLOAD,
                automation_policy.VALID,
            ),
            (
                True,
                True,
                automation_policy.VOD_DOWNLOAD_AND_YOUTUBE,
                automation_policy.VALID,
            ),
            (
                False,
                True,
                automation_policy.VOD_NEEDS_REVIEW,
                automation_policy.NEEDS_REVIEW,
            ),
        )
        for auto_vod, auto_youtube, expected_mode, expected_validation in cases:
            with self.subTest(auto_vod=auto_vod, auto_youtube=auto_youtube):
                policy = automation_policy.derive_vod_handling(
                    auto_vod, auto_youtube
                )
                self.assertEqual(policy.mode, expected_mode)
                self.assertEqual(policy.validation.state, expected_validation)

    def test_only_literal_true_enables_a_persisted_policy_flag(self):
        policy = automation_policy.derive_vod_handling("true", 1)
        self.assertEqual(policy.mode, automation_policy.VOD_MANUAL)

    def test_each_valid_vod_mode_round_trips_to_compact_persisted_flags(self):
        expected = {
            automation_policy.VOD_MANUAL: {},
            automation_policy.VOD_AUTO_DOWNLOAD: {
                "auto_vod_download": True,
            },
            automation_policy.VOD_DOWNLOAD_AND_YOUTUBE: {
                "auto_vod_download": True,
                "auto_youtube_upload": True,
            },
        }
        for mode, expected_flags in expected.items():
            with self.subTest(mode=mode):
                applied = automation_policy.apply_vod_handling({}, mode)
                self.assertEqual(applied, expected_flags)
                self.assertEqual(
                    automation_policy.vod_handling_from_profile(applied).mode,
                    mode,
                )

    def test_apply_vod_handling_preserves_unrelated_profile_dimensions(self):
        existing = {
            "youtube_playlist_id": "PLAYLIST",
            "auto_record": True,
            "future_field": "preserve",
            "auto_youtube_upload": True,
        }
        applied = automation_policy.apply_vod_handling(
            existing, automation_policy.VOD_AUTO_DOWNLOAD
        )
        self.assertEqual(
            applied,
            {
                "youtube_playlist_id": "PLAYLIST",
                "auto_record": True,
                "future_field": "preserve",
                "auto_vod_download": True,
            },
        )
        self.assertEqual(existing["auto_youtube_upload"], True)

    def test_legacy_invalid_profile_is_preserved_until_explicit_resolution(self):
        raw = {"auto_youtube_upload": True}
        normalized = settings.normalize_streamer_profiles({"Example": raw})
        derived = automation_policy.vod_handling_from_profile(
            normalized["example"]
        )

        self.assertEqual(
            normalized,
            {"example": {"auto_youtube_upload": True}},
        )
        self.assertEqual(derived.mode, automation_policy.VOD_NEEDS_REVIEW)
        self.assertEqual(
            derived.validation.issues,
            ("auto_youtube_requires_auto_vod",),
        )

        resolved = automation_policy.apply_vod_handling(
            normalized["example"], automation_policy.VOD_MANUAL
        )
        self.assertEqual(resolved, {})
        self.assertEqual(
            automation_policy.vod_handling_from_profile(resolved).mode,
            automation_policy.VOD_MANUAL,
        )

    def test_needs_review_is_not_an_applicable_product_choice(self):
        with self.assertRaisesRegex(ValueError, "unsupported_vod_handling"):
            automation_policy.apply_vod_handling(
                {"auto_youtube_upload": True},
                automation_policy.VOD_NEEDS_REVIEW,
            )

    def test_global_pauses_do_not_change_streamer_policy(self):
        profile = {
            "auto_vod_download": True,
            "auto_youtube_upload": True,
        }
        for global_auto_vod, global_auto_youtube in (
            (False, False),
            (True, False),
            (False, True),
            (True, True),
        ):
            with self.subTest(
                global_auto_vod=global_auto_vod,
                global_auto_youtube=global_auto_youtube,
            ):
                current_settings = {
                    "auto_vod_enabled": global_auto_vod,
                    "auto_youtube_enabled": global_auto_youtube,
                    "streamer_profiles": {"example": dict(profile)},
                }
                policy = automation_policy.vod_handling_from_profile(
                    current_settings["streamer_profiles"]["example"]
                )
                self.assertEqual(
                    policy.mode,
                    automation_policy.VOD_DOWNLOAD_AND_YOUTUBE,
                )
                self.assertEqual(
                    current_settings["streamer_profiles"]["example"], profile
                )

    def test_live_recording_is_an_independent_strict_dimension(self):
        self.assertEqual(
            automation_policy.derive_live_recording(False),
            automation_policy.LIVE_MANUAL,
        )
        self.assertEqual(
            automation_policy.derive_live_recording(True),
            automation_policy.LIVE_AUTOMATIC,
        )
        self.assertEqual(
            automation_policy.derive_live_recording("true"),
            automation_policy.LIVE_MANUAL,
        )

        profile = {
            "auto_vod_download": True,
            "auto_youtube_upload": True,
        }
        automatic = automation_policy.apply_live_recording(
            profile, automation_policy.LIVE_AUTOMATIC
        )
        self.assertIs(automatic["auto_record"], True)
        self.assertEqual(
            automation_policy.vod_handling_from_profile(automatic).mode,
            automation_policy.VOD_DOWNLOAD_AND_YOUTUBE,
        )
        manual = automation_policy.apply_live_recording(
            automatic, automation_policy.LIVE_MANUAL
        )
        self.assertNotIn("auto_record", manual)

    def test_blank_playlist_is_valid_for_download_and_youtube(self):
        policy = automation_policy.validate_streamer_automation(
            {
                "auto_vod_download": True,
                "auto_youtube_upload": True,
            },
            youtube_dependency_available=True,
            playlist_dependency_available=False,
        )
        self.assertEqual(policy.playlist_id, "")
        self.assertEqual(policy.validation.state, automation_policy.VALID)

    def test_configured_unavailable_playlist_is_a_dependency_state(self):
        policy = automation_policy.validate_streamer_automation(
            {
                "auto_vod_download": True,
                "auto_youtube_upload": True,
                "youtube_playlist_id": " PLAYLIST ",
            },
            youtube_dependency_available=True,
            playlist_dependency_available=False,
        )
        self.assertEqual(policy.playlist_id, "PLAYLIST")
        self.assertEqual(
            policy.validation.state,
            automation_policy.UNAVAILABLE_DEPENDENCY,
        )
        self.assertEqual(
            policy.validation.issues, ("playlist_unavailable",)
        )

    def test_youtube_connection_is_a_dependency_not_invalid_policy(self):
        policy = automation_policy.validate_streamer_automation(
            {
                "auto_vod_download": True,
                "auto_youtube_upload": True,
            },
            youtube_dependency_available=False,
        )
        self.assertEqual(
            policy.vod_handling.mode,
            automation_policy.VOD_DOWNLOAD_AND_YOUTUBE,
        )
        self.assertEqual(
            policy.validation.state,
            automation_policy.UNAVAILABLE_DEPENDENCY,
        )
        self.assertEqual(policy.validation.issues, ("youtube_unavailable",))

    def test_invalid_flag_relationship_takes_priority_over_dependencies(self):
        policy = automation_policy.validate_streamer_automation(
            {"auto_youtube_upload": True},
            youtube_dependency_available=False,
        )
        self.assertEqual(
            policy.validation.state, automation_policy.NEEDS_REVIEW
        )
        self.assertEqual(
            policy.validation.issues,
            ("auto_youtube_requires_auto_vod",),
        )

    def test_retention_maps_supported_current_settings_exactly(self):
        keep = automation_policy.derive_retention(0)
        self.assertEqual(keep.mode, automation_policy.RETENTION_KEEP_LOCAL)
        self.assertIsNone(keep.delay_hours)
        self.assertFalse(keep.automatic_cleanup_configured)
        self.assertEqual(keep.validation.state, automation_policy.VALID)

        for delay in sorted(settings.AUTO_YOUTUBE_CLEANUP_DELAY_HOURS):
            with self.subTest(delay=delay):
                policy = automation_policy.derive_retention(delay)
                self.assertEqual(
                    policy.mode,
                    automation_policy.RETENTION_CLEANUP_AFTER_DELAY,
                )
                self.assertEqual(policy.delay_hours, delay)
                self.assertTrue(policy.automatic_cleanup_configured)
                self.assertEqual(
                    automation_policy.retention_delay_for_mode(
                        policy.mode, policy.delay_hours
                    ),
                    delay,
                )

    def test_unrepresentable_retention_requires_review(self):
        for value in (2, "6", True, None):
            with self.subTest(value=value):
                policy = automation_policy.derive_retention(value)
                self.assertEqual(policy.mode, automation_policy.NEEDS_REVIEW)
                self.assertEqual(
                    policy.validation.state, automation_policy.NEEDS_REVIEW
                )
        with self.assertRaisesRegex(ValueError, "unsupported_retention"):
            automation_policy.retention_delay_for_mode(
                automation_policy.RETENTION_CLEANUP_AFTER_DELAY, 2
            )

    def test_keep_local_maps_to_zero_without_touching_legacy_archive_setting(self):
        current_settings = {
            "auto_youtube_cleanup_delay_hours": 24,
            "move_uploaded_vods": True,
        }
        current_settings["auto_youtube_cleanup_delay_hours"] = (
            automation_policy.retention_delay_for_mode(
                automation_policy.RETENTION_KEEP_LOCAL
            )
        )
        self.assertEqual(current_settings["auto_youtube_cleanup_delay_hours"], 0)
        self.assertIs(current_settings["move_uploaded_vods"], True)

    def test_legacy_auto_upload_stays_a_distinct_manual_workflow(self):
        disabled = automation_policy.derive_manual_download_workflow(
            True, False
        )
        blocked = automation_policy.derive_manual_download_workflow(
            False, True
        )
        enabled = automation_policy.derive_manual_download_workflow(True, True)

        self.assertEqual(disabled.status, "disabled")
        self.assertEqual(blocked.status, "blocked_by_legacy_youtube_gate")
        self.assertFalse(blocked.enabled)
        self.assertEqual(enabled.status, "enabled")
        self.assertTrue(enabled.enabled)
        self.assertEqual(
            automation_policy.derive_vod_handling(False, False).mode,
            automation_policy.VOD_MANUAL,
        )

    def test_summary_counts_manual_automatic_and_legacy_invalid_profiles(self):
        counts = automation_policy.summarize_vod_handling(
            ["Manual", "Download", "YouTube", "Legacy", "@MANUAL", "bad-name"],
            {
                "download": {"auto_vod_download": True},
                "youtube": {
                    "auto_vod_download": True,
                    "auto_youtube_upload": True,
                },
                "legacy": {"auto_youtube_upload": True},
            },
        )
        self.assertEqual(
            counts,
            {
                automation_policy.VOD_MANUAL: 1,
                automation_policy.VOD_AUTO_DOWNLOAD: 1,
                automation_policy.VOD_DOWNLOAD_AND_YOUTUBE: 1,
                automation_policy.VOD_NEEDS_REVIEW: 1,
            },
        )

    def test_helpers_do_not_add_product_fields_to_persisted_profiles(self):
        applied = automation_policy.apply_vod_handling(
            {"youtube_playlist_id": "PLAYLIST"},
            automation_policy.VOD_DOWNLOAD_AND_YOUTUBE,
        )
        self.assertEqual(
            set(applied),
            {
                "youtube_playlist_id",
                "auto_vod_download",
                "auto_youtube_upload",
            },
        )
        self.assertNotIn("vod_handling", applied)

    def test_additive_product_payload_is_json_safe_and_globally_independent(self):
        configured = ["Manual", "Download", "YouTube", "Legacy"]
        current = {
            "auto_vod_enabled": False,
            "auto_youtube_enabled": False,
            "auto_recorder_enabled": False,
            "auto_youtube_cleanup_delay_hours": 6,
            "youtube_enabled": False,
            "youtube_auto_upload": True,
            "streamer_profiles": {
                "download": {"auto_vod_download": True},
                "youtube": {
                    "auto_vod_download": True,
                    "auto_youtube_upload": True,
                    "auto_record": True,
                },
                "legacy": {"auto_youtube_upload": True},
            },
        }

        payload = automation_policy.automation_product_payload(
            current, configured
        )

        self.assertEqual(
            payload["streamer_policies"]["youtube"],
            {
                "vod_handling": "download_and_youtube",
                "live_recording": "automatic",
                "youtube_playlist_id": "",
                "validation": {"state": "valid", "issues": []},
            },
        )
        self.assertEqual(
            payload["streamer_policies"]["legacy"]["validation"],
            {
                "state": "needs_review",
                "issues": ["auto_youtube_requires_auto_vod"],
            },
        )
        self.assertEqual(
            payload["summary"],
            {
                "manual": 1,
                "auto_download": 1,
                "download_and_youtube": 1,
                "needs_review": 1,
            },
        )
        self.assertEqual(
            payload["automated_upload_retention"],
            {
                "mode": "cleanup_after_delay",
                "delay_hours": 6,
                "automatic_cleanup_configured": True,
                "validation": {"state": "valid", "issues": []},
            },
        )
        self.assertEqual(
            payload["manual_download_workflow"]["status"],
            "blocked_by_legacy_youtube_gate",
        )


if __name__ == "__main__":
    unittest.main()
