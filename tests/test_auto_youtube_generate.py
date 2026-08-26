from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

from vod_dashboard import auto_youtube_generate as generation
from vod_dashboard.auto_youtube_multipart import MediaProbeError, MediaProbeResult, StreamDescriptor
from vod_dashboard.media import MediaPathPolicy
from vod_dashboard.youtube_upload_state import YouTubeUploadStatePersistenceError, YouTubeUploadStateStore


VOD_ID = "2855270041"


class FakeProcess:
    def __init__(self, returncode=0, timeouts=0):
        self.returncode = returncode; self.timeouts = timeouts; self.terminated = False; self.killed = False
    def wait(self, timeout=None):
        if self.timeouts:
            self.timeouts -= 1
            raise generation.subprocess.TimeoutExpired("ffmpeg", timeout)
        return self.returncode
    def terminate(self): self.terminated = True
    def kill(self): self.killed = True


class AutoYouTubeGenerationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.media_root = self.root / "media"; self.media_root.mkdir()
        self.source = self.media_root / "bearlychen" / "vod.mkv"; self.source.parent.mkdir()
        self.source.write_bytes(b"original")
        self.source.with_suffix(".info.json").write_text(json.dumps({"id": VOD_ID}), encoding="utf-8")
        self.store = YouTubeUploadStateStore(self.root / "youtube-upload-state.json")
        record, _ = self.store.create_intent_if_absent("bearlychen", VOD_ID, source_download_job_id="12", source_download_item_id="12-item-1", media_path="bearlychen/vod.mkv", size_bytes=self.source.stat().st_size, playlist_id="PL1", plan_inputs={"title_template": "{title}", "description_template": "", "description_fallback": "", "privacy_status": "private", "category_id": "20", "tags": []})
        self.store.set_upload_plan("bearlychen", VOD_ID, {"title": "Frozen", "description": "Frozen", "privacy_status": "private", "category_id": "20", "tags": []})
        self.record = self.store.set_preparation("bearlychen", VOD_ID, source_duration_seconds=43200, state="parts_preparing", split={"mode": "stream_copy", "generation_id": "m3v1-" + "a" * 64, "target_duration_seconds": 42300, "target_size_bytes": 250000000000, "split_points_seconds": [21600]}, parts=[])
        self.streams = (StreamDescriptor("video", "h264", 1920, 1080), StreamDescriptor("audio", "aac", sample_rate=48000, channels=2))
        self.source_probe = MediaProbeResult(43200, self.streams, ())
        self.part_probe = MediaProbeResult(21600, self.streams, ())
        self.commands = []

    def tearDown(self): self.temp.cleanup()
    def probe(self, path): return self.source_probe if Path(path) == self.source else self.part_probe
    @staticmethod
    def storage(free=10**15, total=10**15, state="sufficient"):
        return mock.Mock(return_value=SimpleNamespace(state=state, free_bytes=None if state == "unavailable" else free, total_bytes=None if state == "unavailable" else total))
    def popen(self, command, **kwargs):
        self.commands.append((command, kwargs))
        pattern = Path(command[-1])
        for index in (1, 2):
            Path(str(pattern).replace("%03d", f"{index:03d}")).write_bytes(b"part")
        return FakeProcess()
    def service(self, **changes):
        values = {"state_store": self.store, "media_policy": MediaPathPolicy(self.media_root), "probe": self.probe, "popen": self.popen, "storage_assessor": self.storage()}
        values.update(changes); return generation.AutoYouTubeGenerationService(**values)

    def test_command_is_lossless_deterministic_and_manifest_is_persisted_last(self):
        self.assertEqual(self.service().generate_record(self.record), "ready")
        command, kwargs = self.commands[0]
        self.assertEqual(command[0], "ffmpeg"); self.assertIn("-nostdin", command); self.assertIn("-map", command)
        self.assertEqual(command[command.index("-map") + 1], "0"); self.assertEqual(command[command.index("-c") + 1], "copy")
        self.assertEqual(command[command.index("-f") + 1], "segment"); self.assertEqual(command[command.index("-segment_times") + 1], "21600")
        self.assertFalse(kwargs["shell"]); self.assertNotIn("libx264", command)
        self.assertIs(kwargs["stdin"], generation.subprocess.DEVNULL)
        self.assertIs(kwargs["stdout"], generation.subprocess.DEVNULL)
        self.assertIs(kwargs["stderr"], generation.subprocess.DEVNULL)
        saved = self.store.get("bearlychen", VOD_ID)
        self.assertEqual(saved["state"], "parts_ready"); self.assertEqual(len(saved["parts"]), 2)
        self.assertTrue(all("/parts/" in item["media_path"] and item["source_kind"] == "generated" for item in saved["parts"]))
        self.assertTrue(self.source.exists())

    def test_worker_storage_gate_blocks_before_ffmpeg_and_recovers_same_generation(self):
        for state, reason in (("insufficient", "multipart_storage_insufficient"), ("unavailable", "multipart_storage_unavailable")):
            with self.subTest(state=state):
                if state == "unavailable": self.tearDown(); self.setUp()
                service = self.service(storage_assessor=self.storage(free=0, state=state), popen=mock.Mock(side_effect=AssertionError("ffmpeg")))
                self.assertEqual(service.generate_record(self.record), "blocked")
                saved = self.store.get("bearlychen", VOD_ID); self.assertEqual(saved["reason"], reason); self.assertEqual(saved["parts"], [])
                generation_id = saved["split"]["generation_id"]
                self.assertEqual(self.service().generate_record(saved), "ready")
                recovered = self.store.get("bearlychen", VOD_ID)
                self.assertEqual(recovered["split"]["generation_id"], generation_id)
                self.assertTrue(all(part["attempts"] == 0 for part in recovered["parts"]))

    def test_ffmpeg_start_and_exit_failures_use_safe_reason_codes(self):
        with self.assertRaisesRegex(generation.MultipartGenerationError, "ffmpeg_unavailable"):
            generation.run_ffmpeg_segmentation(
                ["ffmpeg"],
                media_root=self.media_root,
                popen=mock.Mock(side_effect=FileNotFoundError("host detail")),
                storage_assessor=self.storage(),
            )
        with self.assertRaisesRegex(generation.MultipartGenerationError, "ffmpeg_failed"):
            generation.run_ffmpeg_segmentation(
                ["ffmpeg"],
                media_root=self.media_root,
                popen=mock.Mock(return_value=FakeProcess(returncode=1)),
                storage_assessor=self.storage(),
            )

    def test_generation_namespace_rejects_traversal(self):
        for changed in (
            {**self.record, "streamer": ".."},
            {**self.record, "split": {**self.record["split"], "generation_id": "../other"}},
        ):
            with self.subTest(changed=changed):
                with self.assertRaises((ValueError, generation.MultipartGenerationError, RuntimeError)):
                    generation.generation_paths(changed, MediaPathPolicy(self.media_root))

    def test_runtime_guard_terminates_before_reserve_breach(self):
        process = FakeProcess(timeouts=1)
        statuses = [SimpleNamespace(state="sufficient", free_bytes=10**15, total_bytes=10**15), SimpleNamespace(state="sufficient", free_bytes=1, total_bytes=10**15)]
        with self.assertRaisesRegex(generation.MultipartGenerationError, "multipart_storage_insufficient"):
            generation.run_ffmpeg_segmentation(["ffmpeg"], media_root=self.media_root, popen=lambda *a, **k: process, storage_assessor=lambda root: statuses.pop(0), poll_seconds=0.01)
        self.assertTrue(process.terminated)

    def test_complete_staging_and_final_are_reused_without_ffmpeg(self):
        paths = generation.generation_paths(self.record, MediaPathPolicy(self.media_root))
        paths.staging.mkdir(parents=True)
        for name in generation.expected_part_names(2, ".mkv"): (paths.staging / name).write_bytes(b"part")
        service = self.service(popen=mock.Mock(side_effect=AssertionError("ffmpeg")))
        self.assertEqual(service.generate_record(self.record), "ready")
        self.assertTrue(paths.final.exists()); self.assertFalse(paths.staging.exists())

    def test_stale_staging_cleanup_is_scoped_to_authorized_generation(self):
        paths = generation.generation_paths(self.record, MediaPathPolicy(self.media_root))
        paths.staging.mkdir(parents=True); (paths.staging / "partial.mkv").write_bytes(b"partial")
        unrelated = self.media_root / ".auto-youtube" / "other" / "other-generation" / "keep.mkv"
        unrelated.parent.mkdir(parents=True); unrelated.write_bytes(b"keep")
        normal = self.media_root / "normal.mkv"; normal.write_bytes(b"keep")
        self.assertEqual(self.service().generate_record(self.record), "ready")
        self.assertTrue(unrelated.exists()); self.assertTrue(normal.exists()); self.assertTrue(self.source.exists())

    def test_persistence_failure_leaves_final_generation_for_restart_revalidation(self):
        with mock.patch.object(self.store, "finalize_generated_parts", side_effect=YouTubeUploadStatePersistenceError("full")):
            self.assertEqual(self.service().generate_record(self.record), "pending")
        paths = generation.generation_paths(self.record, MediaPathPolicy(self.media_root))
        self.assertTrue(paths.final.exists()); self.assertEqual(self.store.get("bearlychen", VOD_ID)["state"], "parts_preparing")
        self.assertEqual(self.service(popen=mock.Mock(side_effect=AssertionError("ffmpeg"))).generate_record(self.store.get("bearlychen", VOD_ID)), "ready")

    def test_parts_ready_prevents_rerun_and_missing_part_fails_closed(self):
        self.assertEqual(self.service().generate_record(self.record), "ready")
        saved = self.store.get("bearlychen", VOD_ID)
        no_run = self.service(popen=mock.Mock(side_effect=AssertionError("ffmpeg")))
        self.assertEqual(no_run.generate_record(saved), "ready")
        Path(self.media_root / saved["parts"][0]["media_path"]).unlink()
        self.assertEqual(no_run.generate_record(saved), "attention")


class GenerationValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name); self.policy = MediaPathPolicy(self.root)
        self.directory = self.root / "generation"; self.directory.mkdir()
        self.streams = (StreamDescriptor("video", "h264", 1920, 1080),)
        self.record = {"media_path": "source.mkv", "source_duration_seconds": 43200, "playlist_id": None, "split": {"split_points_seconds": [21600]}}
        self.source_probe = MediaProbeResult(43200, self.streams, ())
    def tearDown(self): self.temp.cleanup()
    def files(self):
        for name in generation.expected_part_names(2, ".mkv"): (self.directory / name).write_bytes(b"part")
    def validate(self, probe): return generation.validate_generation(self.directory, record=self.record, source_probe=self.source_probe, media_policy=self.policy, probe=probe)

    def test_count_zero_probe_limits_streams_and_aggregate_are_rejected(self):
        cases = []
        cases.append((lambda: None, lambda p: MediaProbeResult(21600, self.streams, ()), "multipart_generation_incomplete"))
        def missing():
            (self.directory / generation.expected_part_names(2, ".mkv")[0]).write_bytes(b"part")
        cases.append((missing, lambda p: MediaProbeResult(21600, self.streams, ()), "multipart_generation_incomplete"))
        def extra(): self.files(); (self.directory / "extra.mkv").write_bytes(b"x")
        cases.append((extra, lambda p: MediaProbeResult(21600, self.streams, ()), "multipart_generation_incomplete"))
        def zero(): self.files(); next(iter(self.directory.iterdir())).write_bytes(b"")
        cases.append((zero, lambda p: MediaProbeResult(21600, self.streams, ()), "multipart_validation_failed"))
        cases.append((self.files, mock.Mock(side_effect=MediaProbeError("bad")), "multipart_validation_failed"))
        cases.append((self.files, lambda p: MediaProbeResult(42901, self.streams, ()), "multipart_replan_required"))
        cases.append((self.files, lambda p: MediaProbeResult(43200, self.streams, ()), "multipart_replan_required"))
        cases.append((self.files, lambda p: MediaProbeResult(21600, (StreamDescriptor("video", "hevc", 1920, 1080),), ()), "multipart_validation_failed"))
        cases.append((self.files, lambda p: MediaProbeResult(20000, self.streams, ()), "multipart_validation_failed"))
        for setup, probe, code in cases:
            with self.subTest(code=code):
                for path in list(self.directory.iterdir()): path.unlink()
                setup()
                with self.assertRaisesRegex(generation.MultipartGenerationError, code): self.validate(probe)

    def test_keyframe_shift_within_documented_tolerance_is_accepted(self):
        self.files(); values = iter((21604.0, 21599.0))
        parts = self.validate(lambda p: MediaProbeResult(next(values), self.streams, ()))
        self.assertEqual(len(parts), 2)
