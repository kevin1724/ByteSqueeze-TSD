import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch


PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TEST_ROOT = tempfile.mkdtemp(prefix="handbrake-api-tests-")
TEST_DATA = os.path.join(TEST_ROOT, "data")
TEST_MEDIA = os.path.join(TEST_ROOT, "media")
TEST_PRESETS = os.path.join(TEST_ROOT, "presets")
for path in (TEST_DATA, TEST_MEDIA, TEST_PRESETS):
    os.makedirs(path, exist_ok=True)
os.environ["HB_DATA_DIR"] = TEST_DATA
os.environ["HB_PRESET_DIR"] = TEST_PRESETS
os.environ["HB_MEDIA_BASE"] = TEST_MEDIA
os.environ["HB_ROOTS_JSON"] = json.dumps([[TEST_MEDIA, "Test media"]])
os.environ["FLASK_DEBUG"] = "0"
os.environ["FLASK_ENV"] = "production"
os.environ["TSD_DISABLE_AUTO_NODE_DISPATCH"] = "1"

from webui.app import create_app  # noqa: E402
from webui.app import config as app_config  # noqa: E402
from webui.app import events as app_events  # noqa: E402
from webui.app import jobs as app_jobs  # noqa: E402
from webui.app import node_linking as app_node_linking  # noqa: E402
from webui.app import routes as app_routes  # noqa: E402
from webui.app import settings as app_settings  # noqa: E402
from webui.app import storage_stats as app_storage_stats  # noqa: E402
from webui.app import wizard_llm as app_wizard_llm  # noqa: E402
from webui.app.media_metadata import _cache_sidecar, _choose_movie, _choose_show, _sidecar_directories  # noqa: E402
from webui.app.smart_presets import (  # noqa: E402
    SMART_PRESETS_FILE,
    candidate_learning,
    feedback_context,
    learned_plan_defaults,
    normalize_profile,
    record_feedback,
)


class ApiRouteSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Pytest imports every test module before running the suite. Another
        # module may therefore import app config before this file establishes
        # its isolated media root. Update the shared lists in place so aliases
        # already imported by routes/jobs see the test root too.
        cls.original_roots = list(app_config.ROOTS)
        cls.original_allowed_prefixes = list(app_config.ALLOWED_PREFIXES)
        app_config.ROOTS[:] = [(TEST_MEDIA, "Test media")]
        app_config.ALLOWED_PREFIXES[:] = [TEST_MEDIA]
        cls.app = create_app()
        cls.app.config.update(TESTING=True)
        cls.client = cls.app.test_client()

    @classmethod
    def tearDownClass(cls):
        app_config.ROOTS[:] = cls.original_roots
        app_config.ALLOWED_PREFIXES[:] = cls.original_allowed_prefixes
        shutil.rmtree(TEST_ROOT, ignore_errors=True)

    def test_release_discovery_and_automation_page(self):
        node = self.client.get("/api/node/discovery")
        self.assertEqual(node.status_code, 200)
        self.assertEqual(node.get_json()["protocol_version"], 2)

        mobile = self.client.get("/api/mobile/v1/discovery")
        self.assertEqual(mobile.status_code, 200)
        self.assertEqual(mobile.get_json()["api_version"], "v1")

        automation = self.client.get("/settings/automation")
        self.assertEqual(automation.status_code, 302)
        self.assertTrue(automation.headers["Location"].endswith("/autopilot"))

        autopilot_page = self.client.get("/autopilot")
        self.assertEqual(autopilot_page.status_code, 200)
        self.assertIn(b"<h1>Autopilot</h1>", autopilot_page.data)
        self.assertIn(b"Look at real preview comparisons", autopilot_page.data)
        self.assertIn(b"How did completed encodes actually look", autopilot_page.data)

        status = self.client.get("/api/autopilot/status")
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.get_json()["release"], "3.25.1")
        self.assertIn("continuous_learning", status.get_json())
        self.assertIn("onboarding", status.get_json())

        library_page = self.client.get("/library")
        self.assertEqual(library_page.status_code, 200)
        self.assertIn(b"See the three-step workflow", library_page.data)
        self.assertIn(b"Smart Queue", library_page.data)
        self.assertIn(b"Fine tune queue", library_page.data)
        self.assertIn(b"Real encode preview", library_page.data)
        self.assertIn(b'id="quickFilterSelect"', library_page.data)
        home_page = self.client.get("/")
        self.assertEqual(home_page.status_code, 200)
        self.assertIn(b"<h1>Overview</h1>", home_page.data)
        self.assertIn(b'id="homeSavingsChart"', home_page.data)
        self.assertIn(b'id="homeWorkerImpactList"', home_page.data)
        self.assertIn(b'id="homeAchievementProgress"', home_page.data)
        jobs_page = self.client.get("/jobs")
        self.assertEqual(jobs_page.status_code, 200)
        self.assertIn(b"<h1>Queue</h1>", jobs_page.data)
        self.assertIn(b"Audio optimization only", jobs_page.data)
        self.assertIn(b"Scan audio &amp; review changes", jobs_page.data)
        self.assertIn(b"Next available node", jobs_page.data)
        self.assertIn(b"Edit preset", jobs_page.data)
        self.assertIn(b"Next available node", library_page.data)
        smart_settings = self.client.get("/settings/smart")
        self.assertEqual(smart_settings.status_code, 200)
        self.assertIn(b"smartEpisodeAiEnabled", smart_settings.data)
        self.assertIn(b"analyze every episode", smart_settings.data)
        ai_settings = self.client.get("/settings/ai")
        self.assertIn(b"representative low-detail JPEG", ai_settings.data)
        linked_settings = self.client.get("/settings/nodes")
        self.assertIn(b"Open app &amp; pair", linked_settings.data)
        self.assertIn(b'id="clearInactiveMobileDevicesBtn"', linked_settings.data)

        job_id = "jobs-dashboard-route-regression"
        app_jobs.jobs[job_id] = {
            "id": job_id,
            "src": os.path.join(TEST_MEDIA, "Dashboard.Movie.2026.mkv"),
            "preset": "test-preset",
            "status": "done",
            "created_at": 1,
        }
        try:
            jobs_data = self.client.get("/api/jobs")
            self.assertEqual(jobs_data.status_code, 200)
            self.assertTrue(jobs_data.is_json)
            self.assertIn(
                job_id,
                [job["id"] for job in jobs_data.get_json()["jobs"]],
            )
        finally:
            app_jobs.jobs.pop(job_id, None)

    def test_audio_scan_and_queue_create_safe_stream_copy_plan(self):
        path = os.path.join(TEST_MEDIA, "Audio.Plan.Movie.mkv")
        with open(path, "wb") as stream:
            stream.write(b"media")
        inventory = {
            "duration_seconds": 3600,
            "total_bytes": 10 * 1024**3,
            "audio_streams": [{
                "index": 1,
                "type": "audio",
                "type_ordinal": 0,
                "language": "eng",
                "title": "Main",
                "codec": "flac",
                "codec_label": "FLAC",
                "profile": "",
                "channels": 6,
                "channel_layout": "5.1",
                "bitrate": 2000000,
                "sample_rate": 48000,
                "size_bytes": 900000000,
                "lossless": True,
                "object_audio": False,
                "object_audio_capable": False,
                "commentary": False,
                "default": True,
                "forced": False,
            }],
            "video_streams": [{"index": 0, "codec": "av1"}],
            "subtitle_streams": [],
            "attachment_streams": [],
            "chapter_count": 0,
            "aggregate": {"video_bytes": 8 * 1024**3, "audio_bytes": 900000000},
        }
        requested = {
            "job_type": "audio_only",
            "audio_policy": "custom",
            "audio_actions": [{
                "stream_index": 1,
                "action": "encode",
                "target_codec": "aac",
                "bitrate_kbps": 640,
                "channels": 6,
            }],
        }
        with patch.object(app_routes, "scan_media", return_value=inventory):
            scanned = self.client.post(
                "/api/audio/scan",
                json={"src": path, "operations": requested},
            )
        self.assertEqual(scanned.status_code, 200)
        scan_payload = scanned.get_json()
        self.assertEqual(scan_payload["operations"]["video_action"], "copy")
        self.assertEqual(scan_payload["operations"]["audio_actions"][0]["channels"], 6)
        with (
            patch.object(app_routes, "scan_media", return_value=inventory),
            patch.object(app_routes, "create_job", return_value="audio-child") as create,
            patch.object(app_routes, "_wake_auto_node_dispatch"),
        ):
            queued = self.client.post(
                "/api/audio/queue",
                json={"src": path, "operations": requested, "mode": "next_available"},
            )
        self.assertEqual(queued.status_code, 200)
        self.assertEqual(queued.get_json()["job_id"], "audio-child")
        call = create.call_args
        self.assertEqual(call.kwargs["dispatch_mode"], "auto")
        self.assertEqual(call.kwargs["operations"]["video_action"], "copy")
        self.assertTrue(call.kwargs["operations"]["replace_source"])

    def test_audio_only_executor_repairs_legacy_marker_before_encoder_selection(self):
        path = os.path.join(TEST_MEDIA, "Legacy.Audio.Only-TSD.mkv")
        with open(path, "wb") as stream:
            stream.write(b"media")
        job_id = "legacy-audio-only"
        app_jobs.jobs[job_id] = {
            "status": "queued",
            "src": path,
            "preset": "1080",
            "preset_selection": "audio-only",
            "operations": {
                "job_type": "video_preserve_audio",
                "video_action": "encode",
            },
            "mode": "local",
        }
        inventory = {
            "duration_seconds": 60,
            "total_bytes": 5,
            "audio_streams": [],
            "video_streams": [{"index": 0, "codec": "av1"}],
            "subtitle_streams": [],
            "attachment_streams": [],
            "chapter_count": 0,
            "aggregate": {"video_bytes": 5, "audio_bytes": 0},
        }
        try:
            with (
                patch.object(app_jobs, "scan_media", return_value=inventory),
                patch.object(app_jobs, "_run_audio_only_job") as audio_runner,
                patch.object(app_jobs, "_job_encode_metadata", side_effect=AssertionError("video encoder selected")),
                patch.object(app_jobs, "_append_job_log"),
                patch.object(app_jobs, "save_jobs"),
            ):
                app_jobs.run_encode(job_id, path, "1080")
            audio_runner.assert_called_once()
            self.assertEqual(app_jobs.jobs[job_id]["job_type"], "audio_only")
            self.assertEqual(app_jobs.jobs[job_id]["operations"]["video_action"], "copy")
            self.assertEqual(app_jobs.jobs[job_id]["encoder"], "copy")
            self.assertEqual(app_jobs.jobs[job_id]["encode_method"], "ffmpeg_audio_only")
        finally:
            app_jobs.jobs.pop(job_id, None)

    def test_mobile_device_admin_can_forget_one_or_clear_inactive_records(self):
        with patch.object(app_routes, "forget_mobile_device", return_value=True) as forget:
            response = self.client.delete("/api/mobile/devices/phone-old/forget")
        self.assertEqual(response.status_code, 200)
        forget.assert_called_once_with("phone-old")

        with patch.object(app_routes, "clear_mobile_devices", return_value=3) as clear:
            response = self.client.post(
                "/api/mobile/devices/clear",
                json={"target": "inactive"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["removed"], 3)
        clear.assert_called_once_with(inactive_only=True)

    def test_main_jobs_api_combines_worker_rows_and_clear_proxies_to_workers(self):
        worker_jobs = [
            {
                "id": "worker-job-running",
                "status": "running",
                "src": "/work/jobs/Movie.mkv",
                "preset": "1080",
                "preset_name": "Correct 1080p",
                "has_log": True,
            },
            {
                "id": "worker-job-error",
                "status": "error",
                "src": "/work/jobs/Failed.mkv",
                "preset": "4k",
            },
        ]
        public_node = {
            "id": "worker-one",
            "name": "Garage worker",
            "jobs": worker_jobs,
        }
        private_node = {**public_node, "token": "secret", "url": "http://worker:8080"}
        with patch("webui.app.routes.list_nodes_public", return_value=[public_node]):
            response = self.client.get("/api/jobs")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        indexed = {item["id"]: item for item in payload["jobs"]}
        running_id = "worker:worker-one:worker-job-running"
        self.assertIn(running_id, indexed)
        self.assertTrue(indexed[running_id]["is_worker_job"])
        self.assertEqual(indexed[running_id]["node_name"], "Garage worker")
        self.assertEqual(
            indexed[running_id]["log_url"],
            "/api/nodes/worker-one/jobs/worker-job-running/log",
        )
        self.assertGreaterEqual(payload["summary"]["counts"]["running"], 1)
        self.assertGreaterEqual(payload["summary"]["counts"]["error"], 1)

        with patch("webui.app.routes.clear_finished_jobs_core", return_value=3), patch(
            "webui.app.routes.list_nodes_private",
            return_value=[private_node],
        ), patch(
            "webui.app.routes.signed_json_request",
            return_value={"ok": True, "removed": 2, "jobs": [worker_jobs[0]], "summary": {"counts": {"running": 1}}},
        ) as signed_request, patch("webui.app.routes.save_node"):
            cleared = self.client.post("/clear_finished_jobs")

        self.assertEqual(cleared.status_code, 200)
        self.assertEqual(cleared.get_json()["removed"], 5)
        self.assertEqual(cleared.get_json()["local_removed"], 3)
        self.assertEqual(cleared.get_json()["worker_removed"], 2)
        self.assertEqual(signed_request.call_args.args[1], "/api/node/jobs/clear")
        self.assertEqual(signed_request.call_args.kwargs["body"], {"target": "finished"})

        with patch("webui.app.routes.clear_queued_jobs", return_value=1), patch(
            "webui.app.routes.list_nodes_private",
            return_value=[private_node],
        ), patch(
            "webui.app.routes.signed_json_request",
            return_value={"ok": True, "removed": 2, "jobs": worker_jobs[:1], "summary": {"counts": {"running": 1}}},
        ) as signed_request, patch("webui.app.routes.save_node"):
            cleared = self.client.post("/clear_queued_jobs")

        self.assertEqual(cleared.status_code, 200)
        self.assertEqual(cleared.get_json()["removed"], 3)
        self.assertEqual(cleared.get_json()["local_removed"], 1)
        self.assertEqual(cleared.get_json()["worker_removed"], 2)
        self.assertEqual(signed_request.call_args.kwargs["body"], {"target": "queued"})

    def test_v3_interface_can_fall_back_to_v2_without_touching_app_behavior(self):
        original = self.client.get("/api/settings").get_json()["settings"]
        try:
            saved = self.client.post(
                "/api/settings",
                json={"ui_version": "v3", "ui_density": "comfortable"},
            )
            self.assertEqual(saved.status_code, 200)
            self.assertEqual(saved.get_json()["settings"]["ui_version"], "v3")

            v3_page = self.client.get("/")
            self.assertEqual(v3_page.status_code, 200)
            self.assertIn(b'class="home-page ui-v3"', v3_page.data)
            self.assertIn(b'data-density="comfortable"', v3_page.data)
            self.assertIn(b'/static/v3.css', v3_page.data)
            self.assertIn(b'/static/v3.js', v3_page.data)

            temporary_v2 = self.client.get("/?ui=v2")
            self.assertIn(b'class="home-page ui-v2"', temporary_v2.data)
            self.assertEqual(
                self.client.get("/api/settings").get_json()["settings"]["ui_version"],
                "v3",
            )

            saved_v2 = self.client.post(
                "/api/settings",
                json={"ui_version": "v2", "ui_density": "compact"},
            )
            self.assertEqual(saved_v2.status_code, 200)
            classic_library = self.client.get("/library")
            self.assertIn(b'class="beta-page ui-v2"', classic_library.data)
            self.assertIn(b'data-density="compact"', classic_library.data)

            settings_page = self.client.get("/settings")
            self.assertIn(b"V3 CURRENT", settings_page.data)
            self.assertIn(b"<strong>V3</strong>", settings_page.data)
            self.assertIn(b"V2 Classic", settings_page.data)
            self.assertIn(b'name="uiVersion"', settings_page.data)

            temporary_v3 = self.client.get("/library?ui=v3")
            self.assertIn(b'class="beta-page ui-v3"', temporary_v3.data)
        finally:
            self.client.post(
                "/api/settings",
                json={
                    "ui_version": original.get("ui_version", "v3"),
                    "ui_density": original.get("ui_density", "comfortable"),
                },
            )

    def test_v3_library_shell_prevents_duplicate_icons_and_has_mobile_tabs(self):
        library_page = self.client.get("/library?ui=v3")
        self.assertEqual(library_page.status_code, 200)
        self.assertIn(b'id="libraryControls"', library_page.data)
        self.assertIn(b'id="libraryBatchBar"', library_page.data)
        self.assertIn(b'data-library-view="movies"', library_page.data)
        self.assertIn(b'data-library-view="shows"', library_page.data)
        self.assertIn(b'data-mobile-view="movies"', library_page.data)
        self.assertIn(b'wizard.onclick = () => openInWizard(item.path)', library_page.data)
        self.assertIn(b'wizard.onclick = () => openInWizard(ep.path)', library_page.data)
        self.assertIn(b'&from=library', library_page.data)

        v3_css = self.client.get("/static/v3.css")
        self.assertEqual(v3_css.status_code, 200)
        self.assertIn(b"body.ui-v2 .v3-only{display:none!important}", v3_css.data)
        self.assertIn(b"grid-template-columns:repeat(6,1fr)", v3_css.data)
        self.assertIn(b"@media (max-width:900px)", v3_css.data)
        self.assertIn(b"body.ui-v3 .beta-bulkbar{display:none}", v3_css.data)
        v3_css.close()

        v3_js = self.client.get("/static/v3.js")
        self.assertEqual(v3_js.status_code, 200)
        self.assertIn(b'link.classList.remove("nav-library"', v3_js.data)
        self.assertIn(b'body.dataset.v3Enhanced === "true"', v3_js.data)
        v3_js.close()

        wizard_page = self.client.get("/size_wizard?ui=v3")
        self.assertEqual(wizard_page.status_code, 200)
        self.assertIn(b'href="/size_wizard"', wizard_page.data)
        self.assertIn(b'NVIDIA NVENC', wizard_page.data)
        self.assertIn(b"Automatic source-aware estimate", wizard_page.data)
        self.assertIn(b"Automatic estimate for this title", wizard_page.data)
        self.assertIn(b'id="queueDestination"', wizard_page.data)
        self.assertIn(b'value="next_available"', wizard_page.data)
        self.assertIn(b'id="smartPresetToggleBtn"', wizard_page.data)
        self.assertIn(b'id="aiAssistantDetails"', wizard_page.data)
        self.assertIn(b'setValue("audioMode", optimizeAudio ? "eac3" : "copy")', wizard_page.data)
        self.assertIn(b'smart_audio_strategy: smartAudioStrategy', wizard_page.data)
        self.assertNotIn(b'target_size_value: 5,', wizard_page.data)

    def test_size_wizard_queue_records_an_approved_learning_plan(self):
        media_path = os.path.join(TEST_MEDIA, "Wizard.Learning.Movie.2026.mkv")
        plan = {
            "src": media_path,
            "preset": "1080",
            "extra_args": ["--encoder", "x265_10bit"],
            "probe": {
                "source_type": "movie",
                "source_size_bytes": 8 * 1024**3,
                "width": 1920,
                "height": 1080,
                "is_hdr": False,
            },
            "options": {
                "ai_goal": "quality",
                "video_codec": "h265",
                "encoder_family": "software",
                "bit_depth": "10",
                "quality": "high",
                "encoder_speed": "slow",
                "resolution_mode": "keep",
                "audio_mode": "copy",
                "subtitle_mode": "all",
            },
            "inputs": {"target_mb": 4096},
            "estimates": {
                "encoder": "x265_10bit",
                "video_bitrate_kbps": 7000,
                "output_resolution": {"width": 1920, "height": 1080},
            },
        }
        learned = {"feedback_count": 1, "automation_ready": False}
        with (
            patch.object(app_routes, "_wizard_plan", return_value=plan),
            patch.object(app_routes, "list_jobs_for_api", return_value=[]),
            patch.object(app_routes, "create_job", return_value="wizard-learning-job") as create,
            patch.object(
                app_routes,
                "record_smart_preset_feedback",
                return_value={"learning": learned, "feedback": {"id": "feedback-1"}},
            ) as record,
            patch.object(app_routes, "_wake_auto_node_dispatch") as wake,
        ):
            response = self.client.post(
                "/encode_wizard",
                json={
                    "src": media_path,
                    "smart_profile_id": "default",
                    "smart_candidate_id": "detail",
                    "mode": "best",
                },
            )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        payload = response.get_json()
        self.assertTrue(payload["learning_recorded"])
        self.assertEqual(payload["learning"], learned)
        self.assertEqual(payload["dispatch_mode"], "best")
        context = record.call_args.args[0]
        self.assertEqual(context["candidate_id"], "detail")
        self.assertEqual(context["features"]["quality"], "high")
        self.assertEqual(context["features"]["bit_depth"], "10")
        self.assertEqual(record.call_args.args[1:3], ("approve", "looks_good"))
        self.assertEqual(record.call_args.kwargs["origin"], "wizard_queue")
        self.assertEqual(record.call_args.kwargs["job_id"], "wizard-learning-job")
        metadata = create.call_args.kwargs["encode_metadata"]
        self.assertTrue(metadata["smart_preset"])
        self.assertEqual(metadata["automation_source"], "size_wizard")
        self.assertEqual(metadata["smart_feedback_context"], context)
        self.assertFalse(create.call_args.kwargs["preset_adaptive"])
        self.assertEqual(create.call_args.kwargs["dispatch_mode"], "auto")
        wake.assert_called_once_with()

        with (
            patch.object(app_routes, "_wizard_plan", return_value=plan),
            patch.object(
                app_routes,
                "list_jobs_for_api",
                return_value=[{"id": "wizard-learning-job", "src": media_path, "status": "queued"}],
            ),
            patch.object(app_routes, "create_job", return_value="wizard-learning-job"),
            patch.object(app_routes, "record_smart_preset_feedback") as duplicate_record,
        ):
            duplicate = self.client.post("/encode_wizard", json={"src": media_path})
        self.assertEqual(duplicate.status_code, 200)
        self.assertFalse(duplicate.get_json()["learning_recorded"])
        duplicate_record.assert_not_called()

        with (
            patch.object(app_routes, "_wizard_plan", return_value=plan),
            patch.object(app_routes, "list_jobs_for_api", return_value=[]),
            patch.object(
                app_routes,
                "create_job",
                return_value="remote-transfer:active-transfer",
            ),
            patch.object(app_routes, "record_smart_preset_feedback") as conflict_record,
        ):
            conflict = self.client.post(
                "/encode_wizard",
                json={"src": media_path, "mode": "local"},
            )
        self.assertEqual(conflict.status_code, 400)
        self.assertIn("already active on a linked worker", conflict.get_json()["error"])
        conflict_record.assert_not_called()

        with (
            patch.object(
                app_routes,
                "create_job",
                return_value="remote-transfer:active-transfer",
            ),
            patch.object(app_routes, "log_event") as smart_log,
        ):
            with self.assertRaisesRegex(ValueError, "already active on a linked worker"):
                app_routes._create_smart_job(
                    media_path,
                    recommendation={
                        "selected_plan": plan,
                        "recommended_id": "detail",
                    },
                )
        smart_log.assert_not_called()

    def test_size_wizard_smart_audio_persists_language_deduplicated_encode_plan(self):
        media_path = os.path.join(TEST_MEDIA, "Wizard.Smart.Audio.2026.mkv")
        plan = {
            "src": media_path,
            "preset": "1080",
            "extra_args": ["--encoder", "x265_10bit", "-E", "eac3", "-B", "1024"],
            "probe": {
                "source_type": "movie",
                "source_size_bytes": 8 * 1024**3,
                "width": 1920,
                "height": 1080,
                "is_hdr": False,
            },
            "options": {
                "ai_goal": "quality",
                "video_codec": "h265",
                "encoder_family": "software",
                "bit_depth": "10",
                "quality": "high",
                "encoder_speed": "slow",
                "resolution_mode": "keep",
                "audio_mode": "eac3",
                "audio_bitrate": "1024",
                "audio_tracks": "all",
                "audio_languages": ["eng", "spa"],
                "subtitle_mode": "all",
            },
            "inputs": {"target_mb": 4096},
            "estimates": {
                "encoder": "x265_10bit",
                "video_bitrate_kbps": 7000,
                "output_resolution": {"width": 1920, "height": 1080},
            },
        }
        with (
            patch.object(app_routes, "_wizard_plan", return_value=plan),
            patch.object(app_routes, "list_jobs_for_api", return_value=[]),
            patch.object(app_routes, "create_job", return_value="wizard-smart-audio") as create,
            patch.object(
                app_routes,
                "record_smart_preset_feedback",
                return_value={"learning": {}, "feedback": {"id": "audio-feedback"}},
            ),
        ):
            response = self.client.post(
                "/encode_wizard",
                json={"src": media_path, "mode": "local", "audio_mode": "eac3"},
            )

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        queued = create.call_args.kwargs["operations"]
        self.assertEqual(queued["job_type"], "video_audio")
        self.assertEqual(queued["audio_policy"], "optimize_lossless")
        self.assertEqual(queued["default_target_codec"], "eac3")
        self.assertEqual(queued["default_target_bitrate_kbps"], 1024)
        self.assertEqual(queued["preferred_languages"], ["eng", "spa"])
        self.assertTrue(queued["deduplicate_languages"])
        self.assertTrue(queued["transcode_selected_audio"])

        persisted = app_jobs._queued_operations({}, queued)
        inventory = {
            "duration_seconds": 3600,
            "audio_streams": [
                {"index": 1, "type_ordinal": 0, "language": "eng", "title": "English 5.1", "codec": "truehd", "codec_label": "TrueHD", "channels": 6, "bitrate": 2500000, "size_bytes": 900, "lossless": True},
                {"index": 2, "type_ordinal": 1, "language": "eng", "title": "English Main 5.1", "codec": "truehd", "codec_label": "TrueHD", "channels": 6, "bitrate": 4000000, "size_bytes": 1200, "lossless": True},
                {"index": 3, "type_ordinal": 2, "language": "spa", "title": "Spanish", "codec": "pcm_s24le", "codec_label": "PCM", "channels": 2, "bitrate": 1536000, "size_bytes": 700, "lossless": True},
                {"index": 4, "type_ordinal": 3, "language": "fra", "title": "French", "codec": "truehd", "codec_label": "TrueHD", "channels": 6, "bitrate": 3000000, "size_bytes": 1000, "lossless": True},
            ],
        }
        normalized = app_routes.normalize_operations(persisted, inventory)
        actions = {row["stream_index"]: row for row in normalized["audio_actions"]}
        self.assertEqual(
            {index for index, row in actions.items() if row["action"] == "encode"},
            {2, 3},
        )
        self.assertEqual(
            {index for index, row in actions.items() if row["action"] == "remove"},
            {1, 4},
        )
        self.assertEqual(actions[2]["target_codec"], "eac3")
        self.assertEqual(actions[2]["bitrate_kbps"], 1024)
        self.assertEqual(actions[3]["bitrate_kbps"], 320)

        legacy_snapshot = app_jobs._queued_operations()
        repaired = app_jobs._queued_operations(
            {
                "preset_selection": "wizard",
                "preset_preferences": plan["options"],
            },
            legacy_snapshot,
        )
        self.assertEqual(repaired["job_type"], "video_audio")
        self.assertEqual(repaired["audio_policy"], "optimize_lossless")
        self.assertEqual(repaired["default_target_codec"], "eac3")
        self.assertTrue(repaired["transcode_selected_audio"])
        repaired_actions = {
            row["stream_index"]: row
            for row in app_routes.normalize_operations(repaired, inventory)["audio_actions"]
        }
        self.assertEqual(
            {index for index, row in repaired_actions.items() if row["action"] == "encode"},
            {2, 3},
        )

    def test_size_wizard_can_pin_an_approved_plan_to_a_linked_worker(self):
        media_path = os.path.join(TEST_MEDIA, "Wizard.Pinned.Movie.2026.mkv")
        plan = {
            "src": media_path,
            "preset": "1080",
            "extra_args": ["--encoder", "qsv_h265_10bit"],
            "probe": {"source_type": "movie", "source_size_bytes": 4 * 1024**3, "width": 1920, "height": 1080, "is_hdr": False},
            "options": {"video_codec": "h265", "encoder_family": "qsv", "bit_depth": "10", "quality": "balanced", "encoder_speed": "auto", "resolution_mode": "keep", "audio_mode": "copy", "subtitle_mode": "all"},
            "inputs": {"target_mb": 2048},
            "estimates": {"encoder": "qsv_h265_10bit", "video_bitrate_kbps": 5000, "output_resolution": {"width": 1920, "height": 1080}},
        }
        worker = {"id": "pinned-worker", "name": "Garage QSV", "url": "http://worker:8080", "transfer_mode": "remote"}
        learned = {"feedback_count": 2, "automation_ready": False}
        with (
            patch.object(app_routes, "_wizard_plan", return_value=plan),
            patch.object(app_routes, "list_jobs_for_api", return_value=[]),
            patch.object(app_routes, "get_node_private", return_value=worker),
            patch.object(app_routes, "_dispatch_plan_to_worker", return_value=(worker, {"ok": True, "count": 1, "job_ids": ["worker-job-id"]}, "remote")) as dispatch,
            patch.object(app_routes, "record_smart_preset_feedback", return_value={"learning": learned, "feedback": {"id": "feedback-pinned"}}) as record,
        ):
            response = self.client.post(
                "/encode_wizard",
                json={"src": media_path, "mode": "node", "node_id": "pinned-worker", "smart_candidate_id": "detail"},
            )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        payload = response.get_json()
        self.assertEqual(payload["job_id"], "worker-job-id")
        self.assertEqual(payload["dispatch_mode"], "node")
        self.assertEqual(payload["node_id"], "pinned-worker")
        self.assertEqual(payload["node_name"], "Garage QSV")
        self.assertTrue(payload["learning_recorded"])
        dispatch.assert_called_once()
        self.assertEqual(dispatch.call_args.args[1], media_path)
        self.assertEqual(dispatch.call_args.args[2]["preset"], "1080")
        self.assertEqual(dispatch.call_args.args[2]["encode_metadata"]["encoder"], "qsv_h265_10bit")
        self.assertEqual(record.call_args.kwargs["job_id"], "worker-job-id")

    def test_size_wizard_auto_target_is_unique_and_reacts_to_choices(self):
        movie_path = os.path.join(TEST_MEDIA, "Auto.Target.Movie.2026.2160p.mkv")
        episode_path = os.path.join(TEST_MEDIA, "Auto.Target.Show.S01E03.1080p.mkv")
        for path in (movie_path, episode_path):
            with open(path, "wb") as handle:
                handle.write(b"wizard-source")

        movie_probe = {
            "duration_sec": 7200.0,
            "width": 3840,
            "height": 2160,
            "fps": 24000 / 1001,
            "is_hdr": False,
        }
        episode_probe = {
            "duration_sec": 2700.0,
            "width": 1920,
            "height": 1080,
            "fps": 24000 / 1001,
            "is_hdr": False,
        }
        settings = {
            "cpu_profile": "i5-9500t",
            "cpu_speed_override": 1.0,
            "qsv_device_available": True,
        }

        def plan(path, probe, **choices):
            with (
                patch.object(app_routes.os.path, "getsize", return_value=40 * 1024**3),
                patch.object(app_routes, "load_settings", return_value=settings),
                patch.object(
                    app_routes,
                    "smart_learned_plan_defaults",
                    return_value={"sample_count": 0, "confidence": 0.0},
                ),
                patch.object(
                    app_routes,
                    "_history_prediction_for",
                    return_value={"available": False, "sample_count": 0},
                ),
            ):
                return app_routes._wizard_plan(
                    {
                        "src": path,
                        "preset": "auto",
                        "target_size_auto": True,
                        "encoder_family": "qsv",
                        "video_codec": "h265",
                        "quality": "balanced",
                        "resolution_mode": "keep",
                        **choices,
                    },
                    probe_func=lambda _src: probe,
                )

        balanced_movie = plan(movie_path, movie_probe)
        compact_movie = plan(
            movie_path,
            movie_probe,
            quality="small",
            resolution_mode="1080",
        )
        episode = plan(episode_path, episode_probe)

        balanced_mb = balanced_movie["inputs"]["target_mb"]
        compact_mb = compact_movie["inputs"]["target_mb"]
        episode_mb = episode["inputs"]["target_mb"]
        self.assertNotEqual(balanced_mb, 5120.0)
        self.assertNotEqual(episode_mb, 800.0)
        self.assertGreater(balanced_mb, compact_mb)
        self.assertGreater(compact_mb, episode_mb)
        self.assertEqual(balanced_movie["estimates"]["auto_target"]["mode"], "source_aware")
        self.assertIn("title runtime", balanced_movie["estimates"]["auto_target"]["signals"])
        self.assertEqual(
            balanced_movie["estimates"]["estimated_output_mb"],
            balanced_movie["inputs"]["target_mb"],
        )
        self.assertEqual(episode["probe"]["source_type"], "show")

        manual = plan(
            movie_path,
            movie_probe,
            target_size_auto=False,
            target_size_value=2,
            target_size_unit="GB",
        )
        self.assertEqual(manual["inputs"]["target_mb"], 2048.0)
        self.assertEqual(manual["estimates"]["auto_target"]["mode"], "manual")

    def test_size_wizard_builds_native_nvenc_encoders_for_each_codec(self):
        media_path = os.path.join(TEST_MEDIA, "Wizard.NVENC.Movie.2026.mkv")
        with open(media_path, "wb") as handle:
            handle.write(b"wizard-nvenc-source")
        probe = {
            "duration_sec": 5400.0,
            "width": 3840,
            "height": 2160,
            "fps": 24000 / 1001,
            "video_codec": "hevc",
            "is_hdr": False,
        }
        cases = (
            ("h264", "8", "nvenc_h264"),
            ("h265", "8", "nvenc_h265"),
            ("h265", "10", "nvenc_h265_10bit"),
            ("av1", "8", "nvenc_av1"),
            ("av1", "10", "nvenc_av1_10bit"),
        )
        with patch.object(
            app_routes,
            "load_settings",
            return_value={"cpu_profile": "i5-9500t", "cpu_speed_override": 1.0},
        ):
            for codec, depth, expected in cases:
                with self.subTest(codec=codec, depth=depth):
                    plan = app_routes._wizard_plan(
                        {
                            "src": media_path,
                            "preset": "4k",
                            "target_size_auto": False,
                            "target_size_value": 8,
                            "target_size_unit": "GB",
                            "video_codec": codec,
                            "encoder_family": "nvenc",
                            "bit_depth": depth,
                            "encoder_speed": "medium",
                        },
                        probe_func=lambda _src: probe,
                        learned_defaults={"sample_count": 0, "confidence": 0.0},
                    )
                    self.assertEqual(plan["options"]["encoder_family"], "nvenc")
                    self.assertEqual(plan["estimates"]["encoder"], expected)
                    self.assertEqual(plan["estimates"]["encoder_preset"], "medium")
                    self.assertIn("--encoder", plan["extra_args"])
                    self.assertIn(expected, plan["extra_args"])

    def test_size_wizard_ai_keeps_explicit_nvenc_av1(self):
        options = app_routes._wizard_normalize_options(
            {
                "ai_mode": True,
                "ai_hardware": "nvenc",
                "ai_codec_preference": "av1",
                "video_codec": "av1",
                "encoder_family": "nvenc",
                "bit_depth": "10",
            }
        )
        planned, _info = app_routes._wizard_ai_choices(
            options,
            app_routes.get_cpu_profile("i5-9500t"),
            1.0,
            {"width": 1920, "height": 1080, "duration_sec": 3600, "fps": 23.976},
            {"kind": "movie"},
            20 * 1024**3,
            5 * 1024,
            "1080",
            False,
        )
        self.assertEqual(planned["encoder_family"], "nvenc")
        self.assertEqual(planned["video_codec"], "av1")
        self.assertEqual(app_routes._wizard_encoder_name(planned), "nvenc_av1_10bit")
        self.assertEqual(normalize_profile({"hardware": "nvenc"})["hardware"], "nvenc")

    def test_size_wizard_keeps_an_explicit_manual_audio_choice(self):
        media_path = os.path.join(TEST_MEDIA, "Manual.Audio.Choice.2026.mkv")
        with open(media_path, "wb") as handle:
            handle.write(b"wizard-source")
        probe = {
            "duration_sec": 5400.0,
            "width": 1920,
            "height": 1080,
            "fps": 24000 / 1001,
            "is_hdr": False,
        }
        with (
            patch.object(app_routes.os.path, "getsize", return_value=12 * 1024**3),
            patch.object(
                app_routes,
                "load_settings",
                return_value={"cpu_profile": "i5-9500t", "cpu_speed_override": 1.0},
            ),
            patch.object(
                app_routes,
                "smart_learned_plan_defaults",
                return_value={"sample_count": 0, "confidence": 0.0},
            ),
            patch.object(
                app_routes,
                "_history_prediction_for",
                return_value={"available": False, "sample_count": 0},
            ),
        ):
            plan = app_routes._wizard_plan(
                {
                    "src": media_path,
                    "preset": "auto",
                    "ai_mode": False,
                    "audio_mode": "eac3",
                    "smart_never_transcode_audio": True,
                    "ai_copy_audio": True,
                    "smart_audio_strategy": "copy",
                },
                probe_func=lambda _src: probe,
            )
        self.assertEqual(plan["options"]["audio_mode"], "eac3")
        self.assertFalse(plan["options"]["smart_never_transcode_audio"])
        self.assertFalse(plan["options"]["ai_copy_audio"])
        self.assertEqual(plan["options"]["smart_audio_strategy"], "eac3_surround")
        self.assertEqual(plan["extra_args"][plan["extra_args"].index("-E") + 1], "eac3")


    def test_queued_wizard_preferences_are_contextual_smart_defaults(self):
        hdr_context = {
            "source": {"kind": "show", "hdr": True, "resolution": "4k"},
            "features": {
                "goal": "quality",
                "codec": "h265",
                "encoder_family": "qsv",
                "bit_depth": "10",
                "quality": "high",
                "speed": "auto",
                "output_resolution": "4k",
                "resolution_mode": "keep",
                "target_ratio": 0.42,
                "audio_strategy": "copy",
                "subtitle_mode": "all",
            },
        }
        sdr_context = {
            "source": {"kind": "show", "hdr": False, "resolution": "4k"},
            "features": {
                "goal": "small",
                "codec": "av1",
                "encoder_family": "software",
                "target_ratio": 0.18,
            },
        }
        state = {
            "profile": {"minimum_feedback": 2},
            "feedback": [
                {"verdict": "approve", "origin": "wizard_queue", "context": hdr_context},
                {"verdict": "approve", "origin": "wizard_queue", "context": sdr_context},
            ],
        }
        learned = learned_plan_defaults(
            {"source_type": "show", "is_hdr": True, "width": 3840, "height": 2160},
            state,
        )
        self.assertEqual(learned["sample_count"], 1)
        self.assertEqual(learned["codec"], "h265")
        self.assertEqual(learned["encoder_family"], "qsv")
        self.assertEqual(learned["bit_depth"], "10")
        self.assertAlmostEqual(learned["target_ratio"], 0.42)

    def test_approved_wizard_queue_drives_the_next_matching_smart_plan(self):
        try:
            os.remove(SMART_PRESETS_FILE)
        except FileNotFoundError:
            pass

        media_path = os.path.join(TEST_MEDIA, "Learned.Preference.Movie.2026.mkv")
        with open(media_path, "wb") as handle:
            handle.write(b"0" * 1024 * 1024)
        approved_plan = {
            "src": media_path,
            "preset": "1080",
            "probe": {
                "source_type": "movie",
                "source_size_bytes": 1024 * 1024,
                "width": 1920,
                "height": 1080,
                "is_hdr": False,
            },
            "options": {
                "ai_goal": "quality",
                "video_codec": "h264",
                "encoder_family": "software",
                "bit_depth": "8",
                "quality": "high",
                "encoder_speed": "slow",
                "resolution_mode": "keep",
                "audio_mode": "copy",
                "subtitle_mode": "all",
            },
            "inputs": {"target_mb": 0.65},
            "estimates": {
                "output_resolution": {"width": 1920, "height": 1080},
                "video_bitrate_kbps": 5000,
                "encoder": "x264",
                "quality_code": "good",
            },
        }
        record_feedback(
            feedback_context(approved_plan, "manual"),
            "approve",
            "looks_good",
            origin="wizard_queue",
            job_id="learned-wizard-job",
        )
        probe = {
            "duration_sec": 3600.0,
            "width": 1920,
            "height": 1080,
            "fps": 23.976,
            "is_hdr": False,
            "source_type": "movie",
        }
        with (
            patch("webui.app.routes._probe_media", return_value=probe),
            patch(
                "webui.app.routes.analyze_episode_scenes",
                return_value={"target_scale": 1.0, "fingerprint": "learned-preference"},
            ),
        ):
            recommendation = app_routes._smart_recommendation(
                {"src": media_path, "preset": "auto"}
            )

        self.assertEqual(recommendation["learned_defaults"]["sample_count"], 1)
        options = recommendation["selected_plan"]["options"]
        self.assertEqual(options["video_codec"], "h264")
        self.assertEqual(options["encoder_family"], "software")
        self.assertEqual(options["bit_depth"], "8")
        self.assertEqual(options["quality"], "high")
        self.assertEqual(options["encoder_speed"], "slow")
        self.assertEqual(options["resolution_mode"], "keep")

    def test_tracked_show_saves_one_profile_and_independent_episode_plans(self):
        episode_a = os.path.join(TEST_MEDIA, "Tracked.Show.S01E01.mkv")
        episode_b = os.path.join(TEST_MEDIA, "Tracked.Show.S01E02.HDR.mkv")
        for path, payload in ((episode_a, b"episode-a"), (episode_b, b"episode-b-hdr")):
            with open(path, "wb") as handle:
                handle.write(payload)

        def recommendations(paths, tuning=None, smart_profile=None):
            planned = {}
            for path in paths:
                is_hdr = "HDR" in path
                fingerprint = app_wizard_llm.episode_scene_fingerprint(path)
                planned[path] = {
                    "recommended_id": "quality" if is_hdr else "balanced",
                    "selected_plan": {
                        "src": path,
                        "preset": "4k" if is_hdr else "1080",
                        "extra_args": [],
                        "inputs": {"target_mb": 700 if is_hdr else 300},
                        "options": {"video_codec": "h265", "encoder_family": "qsv"},
                        "estimates": {"encoder": "qsv_h265_10bit"},
                        "probe": {"is_hdr": is_hdr},
                        "episode_plan": {
                            "fingerprint": fingerprint,
                            "source": {"is_hdr": is_hdr},
                        },
                    },
                }
            return planned, {}

        row = {"id": "tracked-show", "tracked": True, "known_paths": []}
        try:
            with patch.object(app_routes, "_smart_recommendations_for_paths", side_effect=recommendations) as planner:
                records, errors = app_routes._beta_plan_episode_records(row, [episode_a, episode_b])
            self.assertFalse(errors)
            self.assertEqual(len(records), 2)
            self.assertEqual(row["smart_plan_status"], "ready")
            self.assertEqual(row["smart_planned_episode_count"], 2)
            self.assertEqual(records[episode_a]["preset"], "1080")
            self.assertEqual(records[episode_b]["preset"], "4k")
            self.assertFalse(records[episode_a]["is_hdr"])
            self.assertTrue(records[episode_b]["is_hdr"])
            self.assertEqual(
                records[episode_a]["profile_revision"],
                records[episode_b]["profile_revision"],
            )
            self.assertIsNot(records[episode_a]["episode_plan"], records[episode_b]["episode_plan"])
            planner.assert_called_once()

            tracking_file = os.path.join(TEST_DATA, "tracked-show-plan-roundtrip.json")
            with patch.object(app_routes, "BETA_TRACKED_SHOWS_FILE", tracking_file):
                app_routes._beta_save_tracking({"shows": {"tracked-show": row}})
                roundtrip = app_routes._beta_load_tracking()["shows"]["tracked-show"]
            os.remove(tracking_file)
            self.assertEqual(roundtrip["smart_profile_revision"], row["smart_profile_revision"])
            self.assertEqual(set(roundtrip["episode_plans"]), {episode_a, episode_b})
            self.assertEqual(roundtrip["episode_plans"][episode_b]["preset"], "4k")

            with patch.object(app_routes, "_smart_recommendations_for_paths") as planner:
                cached, cached_errors = app_routes._beta_plan_episode_records(row, [episode_a, episode_b])
            self.assertFalse(cached_errors)
            self.assertEqual(len(cached), 2)
            planner.assert_not_called()

            with patch.object(app_routes, "_create_smart_job", return_value=("smart-job", {})) as create_smart:
                queued = app_routes._beta_queue_tracked_episode_paths(
                    row,
                    [episode_a, episode_b],
                    automation_source="tracked_show_test",
                )
            self.assertEqual(queued["queued_count"], 2)
            self.assertEqual(set(row["known_paths"]), {episode_a, episode_b})
            self.assertEqual(create_smart.call_count, 2)
            for call in create_smart.call_args_list:
                self.assertEqual(call.kwargs["automation_source"], "tracked_show_test")
                self.assertIn("selected_plan", call.kwargs["recommendation"])
        finally:
            for path in (episode_a, episode_b):
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass

    def test_tracking_api_preserves_saved_profile_and_known_episode_history(self):
        episode_a = os.path.join(TEST_MEDIA, "Tracked.Api.S01E01.mkv")
        episode_b = os.path.join(TEST_MEDIA, "Tracked.Api.S01E02.mkv")
        for path in (episode_a, episode_b):
            with open(path, "wb") as handle:
                handle.write(path.encode("utf-8"))
        show_id = "tracked-api-show"
        try:
            with patch.object(app_routes, "_beta_schedule_tracked_show_planning", return_value=True) as schedule:
                first = self.client.post(
                    "/api/beta/tracked_show",
                    json={"show_id": show_id, "title": "Tracked API", "paths": [episode_a], "tracked": True},
                )
                self.assertEqual(first.status_code, 200)
                self.assertTrue(first.get_json()["smart_planning"])
                saved_first = app_routes._beta_load_tracking()["shows"][show_id]
                revision = saved_first["smart_profile_revision"]

                second = self.client.post(
                    "/api/beta/tracked_show",
                    json={"show_id": show_id, "title": "Tracked API", "paths": [episode_b], "tracked": True},
                )
                self.assertEqual(second.status_code, 200)
                saved_second = app_routes._beta_load_tracking()["shows"][show_id]
                self.assertEqual(saved_second["smart_profile_revision"], revision)
                self.assertEqual(set(saved_second["known_paths"]), {episode_a, episode_b})
                self.assertEqual(schedule.call_count, 2)
        finally:
            self.client.post(
                "/api/beta/tracked_show",
                json={"show_id": show_id, "title": "Tracked API", "tracked": False},
            )
            for path in (episode_a, episode_b):
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass

    def test_v3_operations_console_has_live_queue_and_customizable_overview(self):
        home_page = self.client.get("/?ui=v3")
        self.assertEqual(home_page.status_code, 200)
        self.assertIn(b'id="homeCustomizeDialog"', home_page.data)
        self.assertIn(b'data-home-widget="work"', home_page.data)
        self.assertIn(b"bytesqueezeHomeWidgets", home_page.data)

        jobs_page = self.client.get("/jobs?ui=v3")
        self.assertEqual(jobs_page.status_code, 200)
        self.assertIn(b'id="v3RunningSection"', jobs_page.data)
        self.assertIn(b"renderV3RunningJobs", jobs_page.data)

        v3_js = self.client.get("/static/v3.js")
        self.assertIn(b"v3-operations-dock", v3_js.data)
        self.assertIn(b"v3QueueComposer", v3_js.data)
        self.assertIn(b"v3-settings-search", v3_js.data)
        self.assertIn(b"dock.hidden = !showContext", v3_js.data)
        v3_js.close()

        v3_css = self.client.get("/static/v3.css")
        self.assertIn(b"one scoped design system", v3_css.data)
        self.assertIn(b"body.ui-v3 .v3-operations-dock", v3_css.data)
        self.assertNotIn(b"V3 polished shell refresh", v3_css.data)
        v3_css.close()

    def test_v3_design_system_is_consolidated_and_covers_target_viewports(self):
        css_path = os.path.join(PROJECT_ROOT, "webui", "app", "static", "v3.css")
        js_path = os.path.join(PROJECT_ROOT, "webui", "app", "static", "v3.js")
        with open(css_path, "r", encoding="utf-8") as handle:
            css = handle.read()
        with open(js_path, "r", encoding="utf-8") as handle:
            javascript = handle.read()

        self.assertEqual(css.count("@media"), 3)
        self.assertIn("@media (max-width:1280px) and (min-width:901px)", css)
        self.assertIn("@media (max-width:900px)", css)
        self.assertIn("@media (max-width:520px)", css)
        self.assertIn("env(safe-area-inset-bottom)", css)
        self.assertIn("--v3-control:44px", css)
        self.assertIn("body.ui-v3 .beta-bulkbar{display:none}", css)
        self.assertIn("body.ui-v3 .wizard-quickstart{display:none}", css)
        self.assertNotIn("V3 media operations console", css)
        self.assertNotIn("V3 polished shell refresh", css)
        self.assertIn('dock.hidden = !showContext', javascript)
        self.assertIn('document.body.classList.toggle("v3-has-active-ops"', javascript)

        for width in (360, 390, 430, 600, 700, 768, 1024, 1366, 1440, 1920):
            with self.subTest(width=width):
                tier = "narrow" if width <= 520 else "mobile" if width <= 900 else "tablet" if width <= 1280 else "desktop"
                self.assertIn(tier, {"narrow", "mobile", "tablet", "desktop"})

        self.assertIn(b"<h1>Overview</h1>", self.client.get("/?ui=v3").data)
        self.assertIn(b"<h1>Queue</h1>", self.client.get("/jobs?ui=v3").data)
        self.assertIn(b'id="libraryHeaderSummary"', self.client.get("/library?ui=v3").data)
        self.assertIn(b'class="library-scan-settings"', self.client.get("/library?ui=v3").data)
        self.assertIn(b'id="encoding-settings"', self.client.get("/settings?ui=v3").data)
        self.assertIn(b'id="storage-history"', self.client.get("/settings?ui=v3").data)

    def test_hardware_transcode_concurrency_setting_is_bounded_and_visible(self):
        original = self.client.get("/api/settings").get_json()["settings"]
        try:
            saved = self.client.post(
                "/api/settings",
                json={"hardware_transcode_concurrency": 99},
            )
            self.assertEqual(saved.status_code, 200)
            self.assertEqual(saved.get_json()["settings"]["hardware_transcode_concurrency"], 8)

            settings_page = self.client.get("/settings")
            self.assertEqual(settings_page.status_code, 200)
            self.assertIn(b'id="hardwareTranscodeConcurrency"', settings_page.data)
            self.assertIn(b"CPU/software jobs always run one at a time", settings_page.data)
        finally:
            self.client.post(
                "/api/settings",
                json={
                    "hardware_transcode_concurrency": original.get(
                        "hardware_transcode_concurrency",
                        1,
                    )
                },
            )

    def test_worker_gpu_capacity_is_saved_on_main_node(self):
        node_id = "controller-managed-worker"
        app_node_linking.save_node(
            {
                "id": node_id,
                "name": "Garage worker",
                "url": "http://worker:8080",
                "token": "test-token",
                "worker_mode": "headless",
                "requires_remote_transfer": True,
            }
        )
        try:
            with patch(
                "webui.app.routes.signed_json_request",
                return_value={
                    "ok": True,
                    "encoding_policy": {
                        "hardware_transcode_concurrency": 5,
                        "software_jobs_are_exclusive": True,
                        "controller_managed": True,
                    },
                },
            ) as signed_request:
                response = self.client.post(
                    f"/api/nodes/{node_id}/settings",
                    json={"hardware_transcode_concurrency": 5},
                )

            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
            payload = response.get_json()
            self.assertTrue(payload["applied_online"])
            self.assertEqual(payload["node"]["hardware_transcode_concurrency"], 5)
            self.assertEqual(
                app_node_linking.get_node_private(node_id)["hardware_transcode_concurrency"],
                5,
            )
            config_calls = [
                call
                for call in signed_request.call_args_list
                if len(call.args) > 1 and call.args[1] == "/api/node/config"
            ]
            self.assertEqual(len(config_calls), 1)
            self.assertEqual(
                config_calls[0].kwargs["body"]["hardware_transcode_concurrency"],
                5,
            )

            settings_page = self.client.get("/settings")
            self.assertIn(b"Simultaneous hardware transcodes", settings_page.data)
            self.assertIn(b"Save GPU capacity", settings_page.data)
            self.assertIn(b"CPU/software jobs always run alone", settings_page.data)
        finally:
            app_node_linking.delete_node(node_id)

    def test_main_node_proxies_authenticated_worker_logs(self):
        node_id = "worker-log-proxy"
        app_node_linking.save_node(
            {
                "id": node_id,
                "name": "Log worker",
                "url": "http://worker:8080",
                "token": "worker-token",
                "role": "worker",
            }
        )
        try:
            with patch(
                "webui.app.routes.signed_json_request",
                return_value={
                    "ok": True,
                    "job_id": "remote-job",
                    "log": "HandBrake output\nERROR: encoder unavailable\n",
                    "truncated": False,
                },
            ) as signed_request:
                response = self.client.get(
                    f"/api/nodes/{node_id}/jobs/remote-job/log"
                )

            self.assertEqual(response.status_code, 200)
            self.assertIn(b"ERROR: encoder unavailable", response.data)
            self.assertIn("attachment", response.headers["Content-Disposition"])
            self.assertEqual(
                signed_request.call_args.args[1],
                "/api/node/jobs/remote-job/log",
            )
        finally:
            app_node_linking.delete_node(node_id)

    def test_linked_worker_alias_can_be_renamed_and_survives_heartbeat(self):
        first_id = "rename-worker-one"
        second_id = "rename-worker-two"
        for node_id, url in (
            (first_id, "http://worker-one:8080"),
            (second_id, "http://worker-two:8080"),
        ):
            app_node_linking.save_node({
                "id": node_id,
                "name": "ByteSqueeze Worker",
                "url": url,
                "token": "test-token",
                "recovery_token": "test-recovery-token",
            })
        try:
            renamed = self.client.post(
                f"/api/nodes/{first_id}/name",
                json={"name": "Garage Arc GPU"},
            )
            self.assertEqual(renamed.status_code, 200, renamed.get_data(as_text=True))
            self.assertEqual(renamed.get_json()["node"]["name"], "Garage Arc GPU")

            duplicate = self.client.post(
                f"/api/nodes/{second_id}/name",
                json={"name": "garage arc gpu"},
            )
            self.assertEqual(duplicate.status_code, 400)

            with patch(
                "webui.app.routes.signed_json_request",
                return_value={
                    "name": "ByteSqueeze Worker",
                    "summary": {"counts": {"queued": 0, "running": 0, "error": 0}},
                    "jobs": [],
                    "paired_controllers": [],
                    "protocol_version": 2,
                },
            ):
                refreshed = self.client.post(f"/api/nodes/{first_id}/refresh")

            self.assertEqual(refreshed.status_code, 200, refreshed.get_data(as_text=True))
            refreshed_node = refreshed.get_json()["node"]
            self.assertEqual(refreshed_node["name"], "Garage Arc GPU")
            self.assertEqual(refreshed_node["worker_reported_name"], "ByteSqueeze Worker")
            self.assertEqual(app_node_linking.get_node_private(first_id)["name"], "Garage Arc GPU")

            jobs_page = self.client.get("/jobs")
            settings_page = self.client.get("/settings")
            self.assertIn(b"Rename worker", jobs_page.data)
            self.assertIn(b'["Rename", () => renameLinkedNode(node)]', settings_page.data)
        finally:
            app_node_linking.delete_node(first_id)
            app_node_linking.delete_node(second_id)

    def test_worker_dispatch_carries_the_selected_workers_capacity(self):
        node_id = "four-slot-worker"
        media_path = os.path.join(TEST_MEDIA, "Worker.Capacity.Movie.1080p.mkv")
        with open(media_path, "wb") as handle:
            handle.write(b"worker-capacity")
        worker = app_node_linking.save_node(
            {
                "id": node_id,
                "name": "Four slot worker",
                "url": "http://worker:8080",
                "token": "test-token",
                "controller_url": "http://controller:8080",
                "worker_mode": "headless",
                "requires_remote_transfer": True,
                "transfer_mode": "remote",
                "hardware_transcode_concurrency": 4,
            }
        )
        try:
            with (
                patch(
                    "webui.app.routes._refresh_linked_node",
                    side_effect=lambda row: {**row, "online": True, "status": "idle"},
                ),
                patch(
                    "webui.app.routes._node_queue_plan",
                    return_value={
                        "preset": "1080",
                        "preset_bundle": None,
                        "extra_args": "--encoder qsv_h265_10bit",
                        "encode_metadata": {
                            "encoder": "qsv_h265_10bit",
                            "encoder_family": "qsv",
                        },
                    },
                ),
                patch(
                    "webui.app.routes.signed_json_request",
                    return_value={"ok": True, "count": 1, "skipped": []},
                ) as signed_request,
            ):
                response = self.client.post(
                    "/api/nodes/dispatch",
                    json={
                        "mode": "node",
                        "node_id": node_id,
                        "preset": "auto",
                        "paths": [media_path],
                    },
                )

            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
            dispatch_call = next(
                call
                for call in signed_request.call_args_list
                if call.args[1] == "/api/node/jobs"
            )
            body = dispatch_call.kwargs["body"]
            self.assertEqual(body["encoding_policy"]["hardware_transcode_concurrency"], 4)
            self.assertEqual(body["encoding_policy"]["output_container"], "mkv")
            self.assertFalse(body["encoding_policy"]["web_optimized"])
            self.assertEqual(
                body["jobs"][0]["encoding_policy"]["hardware_transcode_concurrency"],
                4,
            )
            self.assertEqual(
                body["jobs"][0]["encoding_policy"]["output_container"],
                "mkv",
            )
        finally:
            app_node_linking.delete_node(worker["id"])

    def test_next_available_dispatch_creates_persistent_auto_queue_jobs(self):
        media_path = os.path.join(TEST_MEDIA, "Automatic.Dispatch.Movie.1080p.mkv")
        with open(media_path, "wb") as handle:
            handle.write(b"automatic-dispatch")
        plan = {
            "preset": "1080",
            "preset_bundle": None,
            "extra_args": "--encoder qsv_h265_10bit",
            "encode_metadata": {
                "encoder": "qsv_h265_10bit",
                "encoder_family": "qsv",
            },
        }
        with (
            patch("webui.app.routes._node_queue_plan", return_value=plan),
            patch("webui.app.routes.create_job", return_value="auto-job-id") as create,
            patch("webui.app.routes._wake_auto_node_dispatch") as wake,
        ):
            response = self.client.post(
                "/api/nodes/dispatch",
                json={
                    "mode": "available",
                    "preset": "1080",
                    "paths": [media_path],
                },
            )

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.get_json()["target"], "next available node")
        self.assertEqual(response.get_json()["job_ids"], ["auto-job-id"])
        self.assertEqual(create.call_args.kwargs["dispatch_mode"], "auto")
        self.assertEqual(create.call_args.kwargs["encode_metadata"]["encoder"], "qsv_h265_10bit")
        wake.assert_called_once_with()

    def test_output_container_and_mp4_fast_start_persist_and_validate(self):
        original = self.client.get("/api/settings").get_json()["settings"]
        try:
            saved = self.client.post(
                "/api/settings",
                json={"output_container": "mp4", "web_optimized": True},
            )
            self.assertEqual(saved.status_code, 200)
            self.assertEqual(saved.get_json()["settings"]["output_container"], "mp4")
            self.assertTrue(saved.get_json()["settings"]["web_optimized"])

            app_settings._settings_cache = None
            reloaded = self.client.get("/api/settings").get_json()["settings"]
            self.assertEqual(reloaded["output_container"], "mp4")
            self.assertTrue(reloaded["web_optimized"])

            settings_page = self.client.get("/settings")
            self.assertIn(b'id="outputContainerSelect"', settings_page.data)
            self.assertIn(b'<option value="auto">Auto (smart per file)</option>', settings_page.data)
            self.assertIn(b'id="webOptimizedToggle"', settings_page.data)
            self.assertIn(
                b"Moves MP4 metadata to the beginning of the file for faster streaming and playback startup.",
                settings_page.data,
            )

            mkv = self.client.post(
                "/api/settings",
                json={"output_container": "mkv", "web_optimized": True},
            ).get_json()["settings"]
            self.assertEqual(mkv["output_container"], "mkv")
            self.assertFalse(mkv["web_optimized"])

            auto = self.client.post(
                "/api/settings",
                json={"output_container": "auto", "web_optimized": True},
            ).get_json()["settings"]
            self.assertEqual(auto["output_container"], "auto")
            self.assertFalse(auto["web_optimized"])

            app_settings._settings_cache = None
            self.assertEqual(
                self.client.get("/api/settings").get_json()["settings"]["output_container"],
                "auto",
            )

            invalid = self.client.post(
                "/api/settings",
                json={"output_container": "avi", "web_optimized": True},
            ).get_json()["settings"]
            self.assertEqual(invalid["output_container"], "mkv")
            self.assertFalse(invalid["web_optimized"])
        finally:
            self.client.post(
                "/api/settings",
                json={
                    "output_container": original.get("output_container", "mkv"),
                    "web_optimized": original.get("web_optimized", False),
                },
            )

    def test_audio_optimization_settings_persist(self):
        original = self.client.get("/api/settings").get_json()["settings"]
        try:
            saved = self.client.post(
                "/api/settings",
                json={
                    "audio_policy_default": "optimize_lossless",
                    "audio_optimize_codec": "aac",
                    "audio_optimize_bitrate_kbps": 1024,
                    "audio_deduplicate_languages": True,
                    "audio_preferred_languages": ["en", "spa", "spa"],
                },
            ).get_json()["settings"]
            self.assertEqual(saved["audio_optimize_bitrate_kbps"], 1024)
            self.assertTrue(saved["audio_deduplicate_languages"])
            self.assertEqual(saved["audio_preferred_languages"], ["eng", "spa"])

            app_settings._settings_cache = None
            reloaded = self.client.get("/api/settings").get_json()["settings"]
            self.assertEqual(reloaded["audio_preferred_languages"], ["eng", "spa"])
        finally:
            self.client.post(
                "/api/settings",
                json={
                    "audio_policy_default": original.get("audio_policy_default", "preserve"),
                    "audio_optimize_codec": original.get("audio_optimize_codec", "aac"),
                    "audio_optimize_bitrate_kbps": original.get("audio_optimize_bitrate_kbps", 1024),
                    "audio_deduplicate_languages": original.get("audio_deduplicate_languages", True),
                    "audio_preferred_languages": original.get("audio_preferred_languages", ["eng", "spa"]),
                },
            )

    def test_remote_transfer_output_extension_matches_container(self):
        self.assertEqual(
            app_routes._transfer_output_path_for_src(
                os.path.join(TEST_MEDIA, "Source.Movie.mp4"),
                "mkv",
            ),
            os.path.join(TEST_MEDIA, "Source.Movie-TSD.mkv"),
        )
        self.assertEqual(
            app_routes._transfer_output_path_for_src(
                os.path.join(TEST_MEDIA, "Source.Movie.mkv"),
                "mp4",
            ),
            os.path.join(TEST_MEDIA, "Source.Movie-TSD.mp4"),
        )
        self.assertEqual(
            app_routes._transfer_output_path_for_src(
                os.path.join(TEST_MEDIA, "Plex.DVR.ts"),
                "ts",
            ),
            os.path.join(TEST_MEDIA, "Plex.DVR-TSD.ts"),
        )
        self.assertEqual(app_routes._resolved_transfer_output_container("auto", "mp4"), "mp4")
        self.assertEqual(app_routes._resolved_transfer_output_container("auto", "mkv"), "mkv")
        self.assertEqual(app_routes._resolved_transfer_output_container("auto", "invalid"), "mkv")
        self.assertEqual(app_routes._resolved_transfer_output_container("mkv", "mp4"), "mkv")
        self.assertEqual(app_routes._resolved_transfer_output_container("ts", "mkv"), "ts")

    def test_remote_transfer_ledger_reserves_source_after_queue_placeholder_is_removed(self):
        source = os.path.join(TEST_MEDIA, "Reserved.Episode.mkv")
        row = {
            "id": "active-transfer",
            "src": source,
            "status": "waiting_for_upload",
            "created_at": 100,
            "renewed_at": 101,
            "completed_at": 0,
        }
        with (
            patch.object(app_node_linking, "_load_transfers", return_value={"transfers": {row["id"]: row}}),
            patch.object(app_node_linking, "_now", return_value=102),
        ):
            self.assertEqual(
                app_jobs._find_existing_active_job_for_src(source),
                "remote-transfer:active-transfer",
            )
            row["status"] = "complete"
            row["completed_at"] = 102
            self.assertIsNone(app_jobs._find_existing_active_job_for_src(source))

    def test_remote_video_upload_is_installed_when_source_disappeared_after_dispatch(self):
        source = os.path.join(TEST_MEDIA, "Returned.After.Delete.mkv")
        upload = os.path.join(TEST_MEDIA, "returned-upload.tmp")
        with open(upload, "wb") as stream:
            stream.write(b"validated-worker-output")
        output = app_routes._transfer_output_path_for_src(source, "mkv")
        row = {
            "id": "missing-source-transfer",
            "src": source,
            "source_size": 1000,
            "worker_node_id": "worker-one",
            "preset": "smart",
            "operations": {"job_type": "video_preserve_audio"},
            "job_type": "video_preserve_audio",
            "input_inventory": {"video_streams": [{"codec": "hevc"}], "audio_streams": []},
            "output_container": "mkv",
        }
        inventory = {"video_streams": [{"codec": "hevc"}], "audio_streams": []}
        try:
            with (
                self.app.test_request_context(headers={"X-Worker-Job-Id": "worker-job"}),
                patch.object(app_routes, "scan_media", return_value=inventory),
                patch.object(app_routes, "normalize_operations", return_value=row["operations"]),
                patch.object(app_routes, "_encoded_output_is_valid", return_value=(True, "ok")),
                patch.object(app_routes, "storage_breakdown", return_value={"total_saved_bytes": 977}),
                patch.object(app_routes, "record_encode") as record,
                patch.object(app_routes, "save_transfer") as save,
                patch.object(app_routes, "get_node_private", return_value={}),
                patch.object(app_routes, "log_event"),
            ):
                result = app_routes._finalize_transfer_output(row, upload)
            self.assertTrue(os.path.isfile(output))
            self.assertTrue(result["warning"])
            self.assertFalse(result["source_deleted"])
            self.assertTrue(row["source_missing_at_return"])
            record.assert_called_once()
            save.assert_called_once()
        finally:
            for path in (upload, output):
                if os.path.isfile(path):
                    os.remove(path)

    def test_remote_duplicate_upload_is_acknowledged_without_overwriting_existing_output(self):
        source = os.path.join(TEST_MEDIA, "Duplicate.Return.mkv")
        output = app_routes._transfer_output_path_for_src(source, "mkv")
        upload = os.path.join(TEST_MEDIA, "duplicate-upload.tmp")
        with open(output, "wb") as stream:
            stream.write(b"already-installed-output")
        with open(upload, "wb") as stream:
            stream.write(b"later-duplicate-output")
        row = {
            "id": "duplicate-transfer",
            "src": source,
            "source_size": 1000,
            "worker_node_id": "worker-two",
            "operations": {"job_type": "video_preserve_audio"},
            "job_type": "video_preserve_audio",
            "input_inventory": {},
            "output_container": "mkv",
        }
        try:
            with (
                self.app.test_request_context(),
                patch.object(app_routes, "scan_media", return_value={}),
                patch.object(app_routes, "normalize_operations", return_value=row["operations"]),
                patch.object(app_routes, "_encoded_output_is_valid", return_value=(True, "ok")),
                patch.object(app_routes, "storage_breakdown", return_value={}),
                patch.object(app_routes, "record_encode") as record,
                patch.object(app_routes, "save_transfer"),
                patch.object(app_routes, "log_event"),
            ):
                result = app_routes._finalize_transfer_output(row, upload)
            self.assertTrue(result["deduplicated"])
            with open(output, "rb") as stream:
                self.assertEqual(stream.read(), b"already-installed-output")
            record.assert_not_called()
        finally:
            for path in (upload, output):
                if os.path.isfile(path):
                    os.remove(path)

    def test_lost_upload_response_recovers_matching_installed_output(self):
        source = os.path.join(TEST_MEDIA, "Lost.Response.Source.mkv")
        output = app_routes._transfer_output_path_for_src(source, "mkv")
        with open(source, "wb") as stream:
            stream.write(b"original-source")
        with open(output, "wb") as stream:
            stream.write(b"completed-worker-output")
        row = {
            "id": "lost-response-transfer",
            "src": source,
            "created_at": os.path.getmtime(output) - 1,
            "source_size": 1000,
            "worker_node_id": "worker-lost-response",
            "preset": "smart",
            "operations": {"job_type": "video_preserve_audio"},
            "job_type": "video_preserve_audio",
            "input_inventory": {"video_streams": [{"codec": "hevc"}], "audio_streams": []},
            "output_container": "auto",
            "encode_metadata": {"encoder": "qsv_h265_10bit", "encoder_family": "qsv"},
        }
        retry = {
            "job_id": "worker-job-lost-response",
            "out_bytes": os.path.getsize(output),
            "output_container": "",
            "trusted_worker_snapshot": True,
        }
        inventory = {"video_streams": [{"codec": "hevc"}], "audio_streams": []}
        try:
            with (
                patch.object(app_routes, "scan_media", return_value=inventory),
                patch.object(app_routes, "_encoded_output_is_valid", return_value=(True, "ok")),
                patch.object(app_routes, "storage_breakdown", return_value={"total_saved_bytes": 977}),
                patch.object(app_routes, "record_encode") as record,
                patch.object(app_routes, "save_transfer") as save,
                patch.object(app_routes, "get_node_private", return_value={"name": "Worker"}),
                patch.object(app_routes, "log_event"),
            ):
                result = app_routes._recover_existing_transfer_output(row, retry)

            self.assertIsNotNone(result)
            self.assertTrue(result["complete"])
            self.assertEqual(row["status"], "complete")
            self.assertTrue(row["recovered_after_lost_response"])
            self.assertTrue(row["source_deleted"])
            self.assertFalse(os.path.exists(source))
            self.assertTrue(os.path.isfile(output))
            record.assert_called_once()
            save.assert_called_once()
        finally:
            for path in (source, output):
                if os.path.isfile(path):
                    os.remove(path)

    def test_storage_history_does_not_double_count_same_transfer_job(self):
        stats_path = os.path.join(TEST_DATA, "idempotent-storage-stats.json")
        original = (
            app_storage_stats.STATS_FILE,
            app_storage_stats._stats_cache,
            app_storage_stats._stats_signature,
            app_storage_stats._summary_cache,
            app_storage_stats._analytics_cache,
        )
        try:
            app_storage_stats.STATS_FILE = stats_path
            app_storage_stats._stats_cache = None
            app_storage_stats._stats_signature = None
            app_storage_stats._summary_cache = None
            app_storage_stats._analytics_cache = {}
            payload = {
                "job_id": "remote-same-worker-job",
                "src": "/media/source.mkv",
                "out": "/media/source-TSD.mkv",
                "preset": "smart",
                "src_bytes": 1000,
                "out_bytes": 400,
            }
            app_storage_stats.record_encode(**payload)
            app_storage_stats.record_encode(**payload)

            with open(stats_path, "r", encoding="utf-8") as stream:
                saved = json.load(stream)
            self.assertEqual(len(saved["encodes"]), 1)
            self.assertEqual(saved["totals"]["count"], 1)
            self.assertEqual(saved["totals"]["saved_bytes"], 600)
        finally:
            (
                app_storage_stats.STATS_FILE,
                app_storage_stats._stats_cache,
                app_storage_stats._stats_signature,
                app_storage_stats._summary_cache,
                app_storage_stats._analytics_cache,
            ) = original
            if os.path.isfile(stats_path):
                os.remove(stats_path)

    def test_audio_only_transfer_still_requires_current_source(self):
        source = os.path.join(TEST_MEDIA, "Missing.Audio.Source.mkv")
        with self.app.test_request_context():
            with self.assertRaisesRegex(RuntimeError, "audio-only source is missing"):
                app_routes._finalize_transfer_output(
                    {
                        "id": "audio-missing",
                        "src": source,
                        "job_type": "audio_only",
                        "operations": {"job_type": "audio_only"},
                    },
                    os.path.join(TEST_MEDIA, "unused-upload.tmp"),
                )

    def test_mobile_next_available_distributes_a_show_as_independent_jobs(self):
        paths = []
        for episode in range(1, 4):
            path = os.path.join(
                TEST_MEDIA,
                f"Mobile.Distributed.Show.S01E{episode:02d}.mkv",
            )
            with open(path, "wb") as handle:
                handle.write(b"episode")
            paths.append(path)
        plan = {
            "preset": "1080",
            "preset_bundle": None,
            "extra_args": "--encoder qsv_h265_10bit",
            "preset_selection": "smart",
            "preset_adaptive": True,
            "preset_preferences": {},
            "encode_metadata": {
                "smart_preset": True,
                "encoder": "qsv_h265_10bit",
                "encoder_family": "qsv",
                "video_codec": "h265",
                "bit_depth": "10",
            },
        }
        with (
            patch.object(
                app_routes,
                "_authenticated_mobile",
                return_value={"name": "Distribution phone"},
            ),
            patch.object(
                app_routes,
                "_smart_recommendations_for_paths",
                return_value=({path: {} for path in paths}, {}),
            ),
            patch.object(app_routes, "_node_queue_plan", return_value=plan),
            patch.object(
                app_routes,
                "create_job",
                side_effect=["episode-job-1", "episode-job-2", "episode-job-3"],
            ) as create,
            patch.object(app_routes, "_wake_auto_node_dispatch") as wake,
        ):
            response = self.client.post(
                "/api/mobile/v1/library/queue",
                json={
                    "paths": paths,
                    "preset": "smart",
                    "mode": "available",
                },
            )

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        payload = response.get_json()
        self.assertEqual(payload["target"], "next available node")
        self.assertEqual(payload["count"], 3)
        self.assertEqual(payload["job_ids"], [
            "episode-job-1",
            "episode-job-2",
            "episode-job-3",
        ])
        self.assertEqual([call.args[0] for call in create.call_args_list], paths)
        self.assertTrue(
            all(call.kwargs["dispatch_mode"] == "auto" for call in create.call_args_list)
        )
        wake.assert_called_once_with()

        with (
            patch.object(
                app_routes,
                "_authenticated_mobile",
                return_value={"name": "Distribution phone"},
            ),
            patch.object(
                app_routes,
                "_smart_recommendations_for_paths",
                return_value=({path: {} for path in paths}, {}),
            ),
            patch.object(app_routes, "_node_queue_plan", return_value=plan),
            patch.object(
                app_routes,
                "create_job",
                return_value="remote-transfer:active-transfer",
            ),
            patch.object(app_routes, "_wake_auto_node_dispatch") as blocked_wake,
        ):
            blocked = self.client.post(
                "/api/mobile/v1/library/queue",
                json={
                    "paths": [paths[0]],
                    "preset": "smart",
                    "mode": "available",
                },
            )

        self.assertEqual(blocked.status_code, 400, blocked.get_data(as_text=True))
        self.assertIn("already active on a linked worker", blocked.get_data(as_text=True))
        blocked_wake.assert_not_called()

    @staticmethod
    def _video_preset_bundle(encoder="qsv_h265_10bit", name="Smart HEVC 10-bit"):
        return {
            "key": "1080",
            "file_name": "smart.json",
            "name": name,
            "contents": json.dumps({
                "PresetList": [{
                    "PresetName": name,
                    "VideoEncoder": encoder,
                    "VideoQualitySlider": 24,
                    "AudioList": [{"AudioEncoder": "copy", "PresetEncoder": "copy"}],
                    "SubtitleAddForeignAudioSearch": True,
                }],
            }),
        }

    def test_video_encoder_detection_never_reads_audio_copy_encoder(self):
        plan = {
            "preset_bundle": self._video_preset_bundle(),
            "extra_args": "",
            "encode_metadata": {"encoder_family": "preset"},
        }
        encoder, family, codec, depth = app_routes._plan_encoder(plan)
        self.assertEqual(encoder, "qsv_h265_10bit")
        self.assertEqual((family, codec, depth), ("qsv", "h265", "10"))

    def test_smart_plan_uses_closest_node_encoder_and_preserves_preferences(self):
        plan = {
            "preset": "1080",
            "preset_bundle": self._video_preset_bundle(),
            "extra_args": "--encoder qsv_h265_10bit --encoder-preset speed --width 1920 --height 1080 --all-audio --all-subtitles",
            "preset_selection": "smart",
            "preset_adaptive": True,
            "preset_preferences": {"video_codec": "h265", "bit_depth": "10", "audio_mode": "copy"},
            "encode_metadata": {
                "smart_preset": True,
                "encoder": "qsv_h265_10bit",
                "encoder_family": "qsv",
                "video_codec": "h265",
                "bit_depth": "10",
            },
        }
        node = {
            "id": "nvidia-worker",
            "name": "NVIDIA worker",
            "hardware": {
                "encoder_families": ["nvenc", "software"],
                "encoders": ["nvenc_h265_10bit", "x265_10bit"],
            },
        }

        derived = app_routes._prepare_plan_for_node(plan, node)
        preset = json.loads(derived["preset_bundle"]["contents"])["PresetList"][0]

        self.assertEqual(derived["encode_metadata"]["encoder"], "nvenc_h265_10bit")
        self.assertEqual(derived["encode_metadata"]["encoder_family"], "nvenc")
        self.assertIn("--encoder nvenc_h265_10bit", derived["extra_args"])
        self.assertNotIn("--encoder-preset", derived["extra_args"])
        self.assertIn("--width 1920 --height 1080", derived["extra_args"])
        self.assertIn("--all-audio", derived["extra_args"])
        self.assertIn("--all-subtitles", derived["extra_args"])
        self.assertEqual(preset["VideoEncoder"], "nvenc_h265_10bit")
        self.assertEqual(preset["AudioList"][0]["AudioEncoder"], "copy")
        self.assertTrue(preset["SubtitleAddForeignAudioSearch"])
        self.assertEqual(derived["preset_preferences"], plan["preset_preferences"])
        self.assertEqual(derived["preset_adaptation"]["from_encoder"], "qsv_h265_10bit")
        self.assertEqual(derived["preset_adaptation"]["to_encoder"], "nvenc_h265_10bit")
        self.assertEqual(
            derived["queued_preset_name"],
            "Smart · H.265 10-bit · NVIDIA NVENC · 1080p",
        )
        self.assertEqual(derived["preset_bundle"]["name"], derived["queued_preset_name"])

    def test_windows_worker_preference_moves_adaptive_job_to_requested_gpu_family(self):
        plan = {
            "preset": "1080",
            "preset_bundle": self._video_preset_bundle(),
            "extra_args": "--encoder qsv_h265_10bit --all-audio --all-subtitles",
            "preset_selection": "smart",
            "preset_adaptive": True,
            "encode_metadata": {
                "smart_preset": True,
                "encoder": "qsv_h265_10bit",
                "encoder_family": "qsv",
                "video_codec": "h265",
                "bit_depth": "10",
            },
        }
        node = {
            "id": "gaming-rig",
            "name": "Gaming rig",
            "hardware": {
                "encoder_families": ["nvenc", "qsv", "software"],
                "encoders": ["nvenc_h265_10bit", "qsv_h265_10bit", "x265_10bit"],
                "preferred_encoder_family": "nvenc",
            },
        }

        derived = app_routes._prepare_plan_for_node(plan, node)

        self.assertEqual(derived["encode_metadata"]["encoder"], "nvenc_h265_10bit")
        self.assertEqual(derived["preset_adaptation"]["to_family"], "nvenc")
        self.assertIn("worker preference", derived["preset_adaptation"]["reason"])

    def test_supported_smart_encoder_rewrites_base_bundle_and_public_name(self):
        plan = {
            "preset": "1080",
            "preset_bundle": self._video_preset_bundle(
                encoder="svt_av1_10bit",
                name="Plex-AV1-Source-Dimensions-CFR-CRF24",
            ),
            "extra_args": "--encoder qsv_h265_10bit --width 1920 --height 1080",
            "preset_selection": "smart",
            "preset_adaptive": True,
            "encode_metadata": {
                "smart_preset": True,
                "smart_candidate_id": "balanced",
                "encoder": "qsv_h265_10bit",
                "encoder_family": "qsv",
                "video_codec": "h265",
                "bit_depth": "10",
                "smart_episode_plan": {
                    "target": {"width": 1920, "height": 1080},
                },
            },
        }
        derived = app_routes._prepare_plan_for_node(
            plan,
            {
                "id": "intel-worker",
                "name": "Intel worker",
                "hardware": {
                    "encoder_families": ["qsv", "software"],
                    "encoders": ["qsv_h265_10bit", "x265_10bit"],
                },
            },
        )
        preset = json.loads(derived["preset_bundle"]["contents"])["PresetList"][0]
        expected = "Smart Balanced · H.265 10-bit · Intel QSV · 1080p"
        self.assertEqual(derived["queued_preset_name"], expected)
        self.assertEqual(derived["encode_metadata"]["queued_preset_name"], expected)
        self.assertEqual(derived["preset_bundle"]["name"], expected)
        self.assertEqual(preset["PresetName"], expected)
        self.assertEqual(preset["VideoEncoder"], "qsv_h265_10bit")

    def test_smart_plan_falls_back_to_matching_software_encoder(self):
        plan = {
            "preset": "4k",
            "preset_bundle": self._video_preset_bundle(name="Smart 4K"),
            "extra_args": "--encoder qsv_h265_10bit --all-audio",
            "preset_selection": "smart",
            "preset_adaptive": True,
            "encode_metadata": {
                "smart_preset": True,
                "encoder": "qsv_h265_10bit",
                "encoder_family": "qsv",
                "video_codec": "h265",
                "bit_depth": "10",
            },
        }
        derived = app_routes._prepare_plan_for_node(
            plan,
            {
                "id": "cpu-worker",
                "name": "CPU worker",
                "hardware": {
                    "encoder_families": ["software"],
                    "encoders": ["x265_10bit"],
                },
            },
        )
        self.assertEqual(derived["encode_metadata"]["encoder"], "x265_10bit")
        self.assertEqual(derived["encode_metadata"]["encoder_family"], "software")
        self.assertIn("--encoder x265_10bit", derived["extra_args"])
        self.assertIn("--all-audio", derived["extra_args"])

    def test_av1_smart_plan_uses_hevc_gpu_on_node_without_av1(self):
        plan = {
            "preset": "4k",
            "preset_bundle": self._video_preset_bundle(
                encoder="nvenc_av1_10bit",
                name="Smart Detail AV1",
            ),
            "extra_args": (
                "--encoder nvenc_av1_10bit --encoder-preset medium "
                "--encoder-profile main --multi-pass --rate 23.976 --cfr "
                "--all-audio --all-subtitles"
            ),
            "preset_selection": "smart",
            "preset_adaptive": True,
            "preset_preferences": {
                "video_codec": "av1",
                "bit_depth": "10",
                "audio_mode": "copy",
                "smart_lock_source_framerate": True,
            },
            "encode_metadata": {
                "smart_preset": True,
                "smart_candidate_id": "detail",
                "encoder": "nvenc_av1_10bit",
                "encoder_family": "nvenc",
                "video_codec": "av1",
                "bit_depth": "10",
                "is_hdr": True,
            },
        }
        node = {
            "id": "intel-hevc-worker",
            "name": "Intel HEVC worker",
            "hardware": {
                "encoder_families": ["qsv", "software"],
                "encoders": ["qsv_h264", "qsv_h265", "qsv_h265_10bit", "x265_10bit"],
            },
        }

        derived = app_routes._prepare_plan_for_node(plan, node)
        preset = json.loads(derived["preset_bundle"]["contents"])["PresetList"][0]

        self.assertEqual(derived["encode_metadata"]["encoder"], "qsv_h265_10bit")
        self.assertEqual(derived["encode_metadata"]["video_codec"], "h265")
        self.assertEqual(derived["preset_adaptation"]["from_codec"], "av1")
        self.assertEqual(derived["preset_adaptation"]["to_codec"], "h265")
        self.assertIn("--encoder qsv_h265_10bit", derived["extra_args"])
        self.assertNotIn("--encoder-profile", derived["extra_args"])
        self.assertNotIn("--multi-pass", derived["extra_args"])
        self.assertIn("--rate 23.976 --cfr", derived["extra_args"])
        self.assertIn("--all-audio --all-subtitles", derived["extra_args"])
        self.assertEqual(preset["VideoEncoder"], "qsv_h265_10bit")
        self.assertIn("H.265 10-bit", derived["queued_preset_name"])
        self.assertIn("Intel QSV", derived["queued_preset_name"])

    def test_av1_requires_verified_per_gpu_capability_when_gpu_rows_exist(self):
        hardware = {
            "encoder_families": ["nvenc", "software"],
            # HandBrake's machine-wide list can include AV1 because another
            # adapter/build exposed it; the physical adapter list is decisive.
            "encoders": ["nvenc_av1_10bit", "nvenc_h265_10bit", "x265_10bit"],
            "gpus": [{
                "name": "NVIDIA GeForce RTX 3070",
                "encoder_family": "nvenc",
                "encoders": ["nvenc_h264", "nvenc_h265", "nvenc_h265_10bit"],
            }],
        }
        self.assertFalse(
            app_routes._node_supports_encoder(
                hardware, "nvenc_av1_10bit", "nvenc", "av1"
            )
        )
        self.assertTrue(
            app_routes._node_supports_encoder(
                hardware, "nvenc_h265_10bit", "nvenc", "h265"
            )
        )

    def test_exact_av1_node_wins_smart_fit_tie_but_hevc_node_remains_eligible(self):
        plan = {
            "preset": "1080",
            "preset_bundle": self._video_preset_bundle(
                encoder="nvenc_av1_10bit", name="Smart AV1"
            ),
            "extra_args": "--encoder nvenc_av1_10bit --all-audio",
            "preset_selection": "smart",
            "preset_adaptive": True,
            "encode_metadata": {
                "smart_preset": True,
                "encoder": "nvenc_av1_10bit",
                "encoder_family": "nvenc",
                "video_codec": "av1",
                "bit_depth": "10",
            },
        }
        av1_node = {
            "hardware": {
                "encoder_families": ["nvenc", "software"],
                "encoders": ["nvenc_av1_10bit", "nvenc_h265_10bit", "x265_10bit"],
            }
        }
        hevc_node = {
            "hardware": {
                "encoder_families": ["qsv", "software"],
                "encoders": ["qsv_h265_10bit", "x265_10bit"],
            }
        }

        exact = app_routes._prepare_plan_for_node(plan, av1_node)
        fallback = app_routes._prepare_plan_for_node(plan, hevc_node)

        self.assertEqual(app_routes._plan_encoder(exact)[0], "nvenc_av1_10bit")
        self.assertEqual(app_routes._plan_encoder(fallback)[0], "qsv_h265_10bit")
        self.assertLess(
            app_routes._adaptive_plan_fit_score(plan, exact),
            app_routes._adaptive_plan_fit_score(plan, fallback),
        )

    def test_legacy_qsv_family_does_not_imply_av1_support(self):
        encoder, family, codec, depth = app_routes._select_smart_encoder_for_node(
            {"encoder_families": ["qsv", "software"], "encoders": []},
            "av1",
            "10",
        )
        self.assertEqual((encoder, family, codec, depth), ("qsv_h265_10bit", "qsv", "h265", "10"))

    def test_linux_qsv_av1_capability_requires_va_encode_profile(self):
        supported = unittest.mock.Mock(
            return_value=unittest.mock.Mock(
                returncode=0,
                stdout="VAProfileAV1Profile0 : VAEntrypointEncSliceLP\n",
                stderr="",
            )
        )
        with (
            patch.object(app_node_linking.shutil, "which", return_value="/usr/bin/vainfo"),
            patch.object(app_node_linking.subprocess, "run", supported),
        ):
            ok, detail = app_node_linking._linux_qsv_av1_capability(["/dev/dri/renderD128"])
        self.assertTrue(ok)
        self.assertIn("renderD128", detail)

        hevc_only = unittest.mock.Mock(
            return_value=unittest.mock.Mock(
                returncode=0,
                stdout="VAProfileHEVCMain10 : VAEntrypointEncSliceLP\n",
                stderr="",
            )
        )
        with (
            patch.object(app_node_linking.shutil, "which", return_value="/usr/bin/vainfo"),
            patch.object(app_node_linking.subprocess, "run", hevc_only),
        ):
            ok, detail = app_node_linking._linux_qsv_av1_capability(["/dev/dri/renderD128"])
        self.assertFalse(ok)
        self.assertIn("no AV1 encode profile", detail)

    def test_locked_preset_is_rejected_instead_of_rewritten(self):
        plan = {
            "preset": "1080",
            "preset_bundle": self._video_preset_bundle(),
            "extra_args": "",
            "preset_selection": "1080",
            "preset_adaptive": False,
            "encode_metadata": {"encoder_family": "preset"},
        }
        original = json.loads(json.dumps(plan))
        with self.assertRaisesRegex(ValueError, "does not support locked video encoder qsv_h265_10bit"):
            app_routes._prepare_plan_for_node(
                plan,
                {
                    "name": "NVIDIA-only worker",
                    "hardware": {
                        "encoder_families": ["nvenc", "software"],
                        "encoders": ["nvenc_h265_10bit", "x265_10bit"],
                    },
                },
            )
        self.assertEqual(plan, original)

    def test_queued_preset_changes_only_through_edit_endpoint(self):
        job_id = "explicit-preset-edit"
        src = os.path.join(TEST_MEDIA, "Edit.Preset.Movie.mkv")
        with open(src, "wb") as handle:
            handle.write(b"preset-edit")
        original_bundle = self._video_preset_bundle(name="Original queued preset")
        replacement_bundle = self._video_preset_bundle(encoder="x265_10bit", name="Edited preset")
        app_jobs.jobs[job_id] = {
            "status": "queued",
            "src": src,
            "preset": "1080",
            "extra_args": "",
            "mode": "local",
            "preset_bundle": original_bundle,
            "preset_selection": "1080",
            "preset_adaptive": False,
            "preset_preferences": {},
            "preset_snapshot_locked": True,
            "preset_revision": 1,
            "queued_preset_name": "Original queued preset",
        }
        app_jobs.job_queue.append(job_id)
        plan = {
            "preset": "1080",
            "preset_bundle": replacement_bundle,
            "extra_args": "--encoder x265_10bit",
            "preset_selection": "1080",
            "preset_adaptive": False,
            "preset_preferences": {},
            "encode_metadata": {"encoder": "x265_10bit", "encoder_family": "software"},
        }
        try:
            with (
                patch.object(app_routes, "_node_queue_plan", return_value=plan),
                patch.object(app_jobs, "save_jobs"),
                patch.object(app_routes, "_wake_auto_node_dispatch"),
                patch.object(app_routes, "log_event"),
            ):
                response = self.client.post(f"/api/jobs/{job_id}/preset", json={"preset": "1080"})
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
            updated = app_jobs.jobs[job_id]
            self.assertEqual(updated["queued_preset_name"], "Edited preset")
            self.assertEqual(updated["preset_revision"], 2)
            self.assertEqual(updated["encoder"], "x265_10bit")
            self.assertTrue(updated["preset_snapshot_locked"])
        finally:
            app_jobs.jobs.pop(job_id, None)
            while job_id in app_jobs.job_queue:
                app_jobs.job_queue.remove(job_id)

    def test_auto_worker_capacity_accepts_hardware_slots_but_not_software_work(self):
        auto_job = {
            "encoder": "qsv_h265_10bit",
            "encoder_family": "qsv",
        }
        base = {
            "id": "capacity-worker",
            "name": "Capacity worker",
            "online": True,
            "status": "running",
            "last_heartbeat": app_routes.time.time(),
            "hardware_transcode_concurrency": 2,
            "summary": {"counts": {"queued": 0, "running": 1}},
        }
        hardware_row = {
            **base,
            "jobs": [{"status": "running", "uses_hardware_encoder": True}],
        }
        queued_hardware_row = {
            **base,
            "jobs": [{"status": "queued", "uses_hardware_encoder": True}],
        }
        software_row = {
            **base,
            "jobs": [{"status": "running", "uses_hardware_encoder": False}],
        }
        self.assertTrue(app_routes._worker_available_for_auto(hardware_row, auto_job)[0])
        self.assertFalse(app_routes._worker_available_for_auto(queued_hardware_row, auto_job)[0])
        self.assertFalse(app_routes._worker_available_for_auto(software_row, auto_job)[0])

        smart_qsv_job = {
            "dispatch_plan": {
                "preset": "1080",
                "preset_bundle": self._video_preset_bundle(),
                "extra_args": "--encoder qsv_h265_10bit",
                "preset_selection": "smart",
                "preset_adaptive": True,
                "encode_metadata": {
                    "smart_preset": True,
                    "encoder": "qsv_h265_10bit",
                    "encoder_family": "qsv",
                    "video_codec": "h265",
                    "bit_depth": "10",
                },
            },
        }
        cpu_busy_row = {
            **hardware_row,
            "hardware": {
                "encoder_families": ["software"],
                "encoders": ["x265_10bit"],
            },
        }
        self.assertFalse(app_routes._worker_available_for_auto(cpu_busy_row, smart_qsv_job)[0])

    def test_auto_worker_accepts_audio_only_plan_without_video_encoder_match(self):
        operations = {
            "job_type": "audio_only",
            "video_action": "copy",
            "audio_policy": "optimize_lossless",
        }
        plan = {
            "preset": "4k",
            "preset_bundle": self._video_preset_bundle(encoder="svt_av1_10bit"),
            "extra_args": "",
            "preset_selection": "audio-only",
            "preset_adaptive": False,
            "operations": operations,
            "job_type": "audio_only",
            "encode_metadata": {
                "encoder": "copy",
                "encode_method": "ffmpeg_audio_only",
                "encoder_family": "stream_copy",
                "job_type": "audio_only",
                "operations": operations,
            },
        }
        job = {
            "job_type": "audio_only",
            "operations": operations,
            "dispatch_plan": plan,
        }
        worker = {
            "id": "audio-worker",
            "name": "Audio worker",
            "online": True,
            "status": "idle",
            "last_heartbeat": app_routes.time.time(),
            "summary": {"counts": {"queued": 0, "running": 0}},
            "jobs": [],
            "hardware": {
                "encoder_families": ["qsv"],
                "encoders": ["qsv_h264"],
            },
        }

        self.assertTrue(app_routes._hardware_supports_plan(plan, worker["hardware"]))
        self.assertTrue(app_routes._worker_available_for_auto(worker, job)[0])
        legacy_job = {
            "preset": "4k",
            "preset_bundle": plan["preset_bundle"],
            "preset_selection": "audio-only",
            "encoder": "copy",
            "encoder_family": "stream_copy",
            "job_type": "audio_only",
            "operations": operations,
        }
        self.assertTrue(app_routes._worker_available_for_auto(worker, legacy_job)[0])
        prepared = app_routes._prepare_plan_for_node(plan, worker)
        self.assertEqual(prepared["encode_metadata"]["job_type"], "audio_only")
        self.assertEqual(prepared["encode_metadata"]["operations"]["video_action"], "copy")

    def test_auto_dispatch_loop_claims_the_oldest_job_for_an_available_worker(self):
        pending_job = {
            "src": os.path.join(TEST_MEDIA, "Oldest.Automatic.Movie.mkv"),
            "preset": "1080",
            "encoder": "qsv_h265_10bit",
            "encoder_family": "qsv",
            "dispatch_plan": {
                "preset": "1080",
                "preset_bundle": None,
                "extra_args": "--encoder qsv_h265_10bit",
                "encode_metadata": {"encoder": "qsv_h265_10bit", "encoder_family": "qsv"},
            },
        }
        claimed = {**pending_job, "status": "dispatching"}
        worker = {"id": "next-worker", "name": "Next worker"}
        stop = unittest.mock.Mock()
        stop.is_set.side_effect = [False, True]
        with (
            patch.object(app_routes, "AUTO_NODE_DISPATCH_STOP", stop),
            patch.object(app_routes, "get_queue_state", return_value=False),
            patch.object(app_routes, "get_next_auto_dispatch_job", return_value=("oldest-job", pending_job)),
            patch.object(app_routes, "auto_dispatch_local_available", return_value=False),
            patch.object(app_routes, "list_nodes_private", return_value=[worker]),
            patch.object(app_routes, "_worker_available_for_auto", return_value=(True, 0.0)),
            patch.object(app_routes, "claim_auto_dispatch_job", return_value=claimed) as claim,
            patch.object(
                app_routes,
                "_dispatch_plan_to_worker",
                return_value=(worker, {"ok": True, "count": 1}, "remote"),
            ) as dispatch,
            patch.object(app_routes, "complete_auto_dispatch_job", return_value=True) as complete,
            patch.object(app_routes, "_refresh_linked_node", return_value=worker),
            patch.object(app_routes, "log_event"),
        ):
            app_routes._auto_node_dispatch_loop()

        claim.assert_called_once_with("oldest-job", "next-worker", "Next worker")
        self.assertEqual(dispatch.call_args.kwargs["require_available_for"], claimed)
        complete.assert_called_once_with("oldest-job")

    def test_auto_dispatch_materializes_av1_smart_job_as_hevc_for_selected_worker(self):
        plan = {
            "preset": "1080",
            "preset_bundle": self._video_preset_bundle(
                encoder="nvenc_av1_10bit", name="Smart AV1"
            ),
            "extra_args": "--encoder nvenc_av1_10bit --rate 23.976 --cfr --all-audio",
            "preset_selection": "smart",
            "preset_adaptive": True,
            "encode_metadata": {
                "smart_preset": True,
                "encoder": "nvenc_av1_10bit",
                "encoder_family": "nvenc",
                "video_codec": "av1",
                "bit_depth": "10",
            },
        }
        pending_job = {
            "src": os.path.join(TEST_MEDIA, "Adaptive.Show.S01E01.mkv"),
            "preset": "1080",
            "encoder": "nvenc_av1_10bit",
            "encoder_family": "nvenc",
            "preset_adaptive": True,
            "dispatch_plan": plan,
        }
        claimed = {**pending_job, "status": "dispatching"}
        worker = {
            "id": "hevc-worker",
            "name": "HEVC worker",
            "online": True,
            "status": "idle",
            "last_heartbeat": app_routes.time.time(),
            "jobs": [],
            "summary": {"counts": {"queued": 0, "running": 0}},
            "hardware": {
                "encoder_families": ["qsv", "software"],
                "encoders": ["qsv_h265_10bit", "x265_10bit"],
            },
        }
        stop = unittest.mock.Mock()
        stop.is_set.side_effect = [False, True]
        with (
            patch.object(app_routes, "AUTO_NODE_DISPATCH_STOP", stop),
            patch.object(app_routes, "get_queue_state", return_value=False),
            patch.object(app_routes, "get_next_auto_dispatch_job", return_value=("adaptive-job", pending_job)),
            patch.object(app_routes, "auto_dispatch_local_available", return_value=False),
            patch.object(app_routes, "list_nodes_private", return_value=[worker]),
            patch.object(app_routes, "claim_auto_dispatch_job", return_value=claimed),
            patch.object(
                app_routes,
                "_dispatch_plan_to_worker",
                return_value=(worker, {"ok": True, "count": 1}, "remote"),
            ) as dispatch,
            patch.object(app_routes, "complete_auto_dispatch_job", return_value=True),
            patch.object(app_routes, "_refresh_linked_node", return_value=worker),
            patch.object(app_routes, "log_event"),
        ):
            app_routes._auto_node_dispatch_loop()

        dispatched_plan = dispatch.call_args.args[2]
        self.assertEqual(dispatched_plan["encode_metadata"]["encoder"], "qsv_h265_10bit")
        self.assertEqual(dispatched_plan["encode_metadata"]["video_codec"], "h265")
        self.assertIn("--rate 23.976 --cfr --all-audio", dispatched_plan["extra_args"])

    def test_auto_dispatch_wake_restarts_a_missing_coordinator(self):
        class DeadThread:
            def is_alive(self):
                return False

        class StartedThread:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.started = False

            def start(self):
                self.started = True

            def is_alive(self):
                return self.started

        replacement = StartedThread()
        wake = unittest.mock.Mock()
        stop = unittest.mock.Mock()
        with (
            patch.dict(
                os.environ,
                {"TSD_DISABLE_AUTO_NODE_DISPATCH": "0", "FLASK_DEBUG": "1"},
            ),
            patch.object(app_routes, "AUTO_NODE_DISPATCH_THREAD", DeadThread()),
            patch.object(app_routes, "AUTO_NODE_DISPATCH_WAKE", wake),
            patch.object(app_routes, "AUTO_NODE_DISPATCH_STOP", stop),
            patch.object(app_routes.threading, "Thread", return_value=replacement) as thread_factory,
            patch.object(app_routes, "_start_node_heartbeat_thread") as heartbeat,
            patch.object(app_routes, "log_event") as event,
        ):
            app_routes._wake_auto_node_dispatch()
            self.assertIs(app_routes.AUTO_NODE_DISPATCH_THREAD, replacement)

        wake.set.assert_called_once_with()
        heartbeat.assert_called_once_with(force_serving_process=True)
        stop.clear.assert_called_once_with()
        thread_factory.assert_called_once()
        self.assertTrue(replacement.started)
        event.assert_called_once_with(
            "auto_node_dispatch_started",
            "Automatic node dispatcher is running.",
            extra={"restarted": True},
        )

    def test_pending_auto_dispatch_watchdog_revives_the_coordinator(self):
        pending = ("waiting-job", {"status": "queued", "mode": "auto_node"})
        with (
            patch.object(app_routes, "get_next_auto_dispatch_job", return_value=pending),
            patch.object(app_routes, "_wake_auto_node_dispatch") as wake,
        ):
            self.assertTrue(app_routes._ensure_auto_node_dispatch_for_pending_jobs())
        wake.assert_called_once_with()

        with (
            patch.object(app_routes, "get_next_auto_dispatch_job", return_value=None),
            patch.object(app_routes, "_wake_auto_node_dispatch") as wake,
        ):
            self.assertFalse(app_routes._ensure_auto_node_dispatch_for_pending_jobs())
        wake.assert_not_called()

    def test_jobs_polling_recovers_existing_waiting_for_node_jobs(self):
        with patch.object(
            app_routes,
            "_ensure_auto_node_dispatch_for_pending_jobs",
        ) as ensure:
            jobs_response = self.client.get("/api/jobs")
            summary_response = self.client.get("/jobs/summary")

        self.assertEqual(jobs_response.status_code, 200, jobs_response.get_data(as_text=True))
        self.assertEqual(summary_response.status_code, 200, summary_response.get_data(as_text=True))
        self.assertEqual(ensure.call_count, 2)

    def test_heartbeat_cycle_recovers_existing_waiting_for_node_jobs(self):
        stop = unittest.mock.Mock()
        stop.is_set.side_effect = [False, True]
        with (
            patch.object(app_routes, "NODE_HEARTBEAT_STOP", stop),
            patch.object(app_routes, "list_nodes_private", return_value=[]),
            patch.object(
                app_routes,
                "_ensure_auto_node_dispatch_for_pending_jobs",
            ) as ensure,
        ):
            app_routes._node_heartbeat_loop()

        ensure.assert_called_once_with()
        stop.wait.assert_called_once_with(timeout=10)

    def test_nodes_api_recovers_missing_heartbeat_monitor(self):
        with (
            patch.object(app_routes, "_start_node_heartbeat_thread") as heartbeat,
            patch.object(
                app_routes,
                "_ensure_auto_node_dispatch_for_pending_jobs",
            ) as ensure,
        ):
            response = self.client.get("/api/nodes")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        heartbeat.assert_called_once_with(force_serving_process=True)
        ensure.assert_called_once_with()

    def test_library_local_smart_batch_keeps_tuning_for_every_file(self):
        paths = []
        for episode in range(1, 4):
            path = os.path.join(TEST_MEDIA, f"Useful.Show.S01E{episode:02d}.mkv")
            with open(path, "wb") as handle:
                handle.write(b"0" * 256)
            paths.append(path)

        tuning = {
            "goal": "small",
            "resolution_mode": "1080",
            "hardware": "software",
            "audio_strategy": "copy",
            "subtitle_mode": "all",
            "target_scale": 0.85,
        }
        calls = []

        def fake_create_smart_job(src, **kwargs):
            calls.append((src, kwargs))
            return f"smart-{len(calls)}", {"recommended_id": "compact"}

        with (
            patch("webui.app.routes._smart_recommendations_for_paths", return_value=({}, {})),
            patch("webui.app.routes._create_smart_job", side_effect=fake_create_smart_job),
        ):
            response = self.client.post(
                "/api/nodes/dispatch",
                json={
                    "paths": paths,
                    "preset": "smart",
                    "mode": "local",
                    "smart_tuning": tuning,
                },
            )

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.get_json()["count"], 3)
        self.assertEqual([row[0] for row in calls], paths)
        self.assertTrue(all(row[1]["tuning"] == tuning for row in calls))
        self.assertTrue(all(row[1]["automation_source"] == "library_smart" for row in calls))

    def test_mixed_season_builds_independent_hdr_safe_snapshots_per_episode(self):
        paths = [
            os.path.join(TEST_MEDIA, "Mixed.Show.S01E01.mkv"),
            os.path.join(TEST_MEDIA, "Mixed.Show.S01E02.mkv"),
        ]
        for path in paths:
            with open(path, "wb") as handle:
                handle.write(b"0" * 4096)
        self.client.post(
            "/api/smart_presets/profile",
            json={
                "goal": "balanced",
                "hardware": "software",
                "episode_ai_enabled": False,
            },
        )
        probes = {
            paths[0]: {
                "duration_sec": 2700.0,
                "width": 3840,
                "height": 2160,
                "fps": 23.976,
                "video_codec": "hevc",
                "bit_depth": 10,
                "is_hdr": True,
                "hdr_reason": "arib-std-b67",
                "hdr_format": "hlg",
                "color_transfer": "arib-std-b67",
                "color_primaries": "bt2020",
                "probe_source": "handbrake+ffprobe",
            },
            paths[1]: {
                "duration_sec": 2700.0,
                "width": 1920,
                "height": 1080,
                "fps": 23.976,
                "video_codec": "h264",
                "bit_depth": 8,
                "is_hdr": False,
                "hdr_reason": "",
                "hdr_format": "sdr",
                "color_transfer": "bt709",
                "color_primaries": "bt709",
                "probe_source": "handbrake+ffprobe",
            },
        }
        created = []

        def fake_create_job(src, preset, **kwargs):
            created.append((src, preset, kwargs))
            return f"episode-{len(created)}"

        with (
            patch("webui.app.routes._probe_media", side_effect=lambda path: probes[path]) as probe,
            patch("webui.app.routes.create_job", side_effect=fake_create_job),
            patch("webui.app.routes.log_event"),
        ):
            count, skipped = app_routes._queue_local_paths(paths, "smart")

        self.assertEqual(count, 2)
        self.assertEqual(skipped, [])
        self.assertEqual(probe.call_count, 2, "every episode should be probed once, independently")
        self.assertCountEqual([call.args[0] for call in probe.call_args_list], paths)
        hdr_metadata = created[0][2]["encode_metadata"]
        sdr_metadata = created[1][2]["encode_metadata"]
        self.assertTrue(hdr_metadata["is_hdr"])
        self.assertFalse(sdr_metadata["is_hdr"])
        self.assertEqual(hdr_metadata["hdr_reason"], "arib-std-b67")
        self.assertIn("--hdr-dynamic-metadata all", created[0][2]["extra_args"])
        self.assertNotIn("--hdr-dynamic-metadata", created[1][2]["extra_args"])
        for _src, _preset, kwargs in created:
            args = kwargs["extra_args"].split()
            self.assertIn("--cfr", args)
            self.assertNotIn("--vfr", args)
            self.assertNotIn("--pfr", args)
            self.assertEqual(args[args.index("--rate") + 1], "23.976")
        first_plan = hdr_metadata["smart_episode_plan"]
        second_plan = sdr_metadata["smart_episode_plan"]
        self.assertNotEqual(first_plan["fingerprint"], second_plan["fingerprint"])
        self.assertEqual(first_plan["source"]["hdr_format"], "hlg")
        self.assertEqual(second_plan["source"]["hdr_format"], "sdr")
        self.assertTrue(created[0][2]["preset_preferences"]["smart_episode_plan"])

    def test_hdr_episode_quality_floor_prevents_starved_4k_target(self):
        floor = app_routes._smart_episode_quality_floor({
            "probe": {
                "duration_sec": 2700.0,
                "width": 3840,
                "height": 2160,
                "fps": 23.976,
                "source_size_bytes": 8 * 1024**3,
                "is_hdr": True,
            },
            "options": {
                "video_codec": "h265",
                "encoder_family": "qsv",
                "audio_mode": "copy",
                "audio_bitrate": "auto",
            },
            "estimates": {"output_resolution": {"width": 3840, "height": 2160}},
        })
        self.assertTrue(floor["is_hdr"])
        self.assertGreater(floor["target_mb"], 3000)
        self.assertGreater(floor["video_kbps"], 10000)

    def test_dynamic_hdr_avoids_incompatible_qsv_encoder(self):
        media_path = os.path.join(TEST_MEDIA, "Dynamic.Show.S01E01.mkv")
        with open(media_path, "wb") as handle:
            handle.write(b"dynamic-hdr")
        probe = {
            "duration_sec": 2700.0,
            "width": 3840,
            "height": 2160,
            "fps": 23.976,
            "video_codec": "hevc",
            "is_hdr": True,
            "hdr_reason": "dolby vision metadata",
            "hdr_format": "dolby_vision",
            "bit_depth": 10,
        }
        with patch(
            "webui.app.routes.load_settings",
            return_value={
                "cpu_profile": "i5-9500t",
                "cpu_speed_override": 1.0,
                "qsv_device_available": True,
            },
        ):
            plan = app_routes._wizard_plan(
                {
                    "src": media_path,
                    "preset": "4k",
                    "ai_mode": True,
                    "ai_hardware": "qsv",
                    "ai_codec_preference": "h265",
                },
                probe_func=lambda _src: probe,
            )
        self.assertEqual(plan["options"]["encoder_family"], "software")
        self.assertEqual(plan["options"]["bit_depth"], "10")
        self.assertEqual(plan["estimates"]["encoder"], "x265_10bit")
        self.assertIn("--hdr-dynamic-metadata", plan["extra_args"])

    def test_jobs_api_merges_durable_encode_history_and_summary(self):
        original_history_cutoff = app_jobs.history_cleared_before
        app_jobs.history_cleared_before = 0.0
        live_id = "jobs-live-complete"
        queued_id = "jobs-live-queued"
        archived_id = "jobs-durable-complete"
        live_src = os.path.join(TEST_MEDIA, "Live.Movie.2026.mkv")
        queued_src = os.path.join(TEST_MEDIA, "Queued.Movie.2026.mkv")
        archived_src = os.path.join(TEST_MEDIA, "Archived.Movie.2025.mkv")
        app_jobs.jobs[live_id] = {
            "src": live_src,
            "preset": "1080",
            "status": "done",
            "progress": 100,
            "created_at": 200,
            "finished_at": 300,
            "duration_seconds": 100,
            "saved_bytes": 500,
        }
        app_jobs.jobs[queued_id] = {
            "src": queued_src,
            "preset": "1080",
            "status": "queued",
            "progress": 0,
            "created_at": 400,
        }
        app_jobs.job_queue.append(queued_id)
        ledger = [
            {
                "ts": 300,
                "job_id": live_id,
                "src": live_src,
                "out": os.path.join(TEST_MEDIA, "Live.Movie.2026-TSD.mkv"),
                "preset": "1080",
                "src_bytes": 1000,
                "out_bytes": 500,
                "saved_bytes": 500,
                "duration_seconds": 100,
            },
            {
                "ts": 250,
                "job_id": archived_id,
                "src": archived_src,
                "out": os.path.join(TEST_MEDIA, "Archived.Movie.2025-TSD.mkv"),
                "preset": "4k",
                "src_bytes": 4000,
                "out_bytes": 1000,
                "saved_bytes": 3000,
                "duration_seconds": 120,
                "is_hdr": True,
                "encode_method": "x265_10bit",
            },
        ]
        try:
            with patch.object(app_jobs, "list_encodes", return_value=ledger):
                response = self.client.get("/api/jobs")
        finally:
            app_jobs.jobs.pop(live_id, None)
            app_jobs.jobs.pop(queued_id, None)
            while queued_id in app_jobs.job_queue:
                app_jobs.job_queue.remove(queued_id)
            app_jobs.history_cleared_before = original_history_cutoff

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        payload = response.get_json()
        self.assertIn("summary", payload)
        self.assertIn("paused", payload)
        ids = [job["id"] for job in payload["jobs"]]
        self.assertEqual(ids.count(live_id), 1, "live and ledger copies must be deduplicated")
        self.assertIn(archived_id, ids)
        self.assertEqual(ids[0], queued_id, "active queue records stay above terminal history")
        archived = next(job for job in payload["jobs"] if job["id"] == archived_id)
        self.assertEqual(archived["status"], "done")
        self.assertTrue(archived["archived"])
        self.assertEqual(archived["saved_bytes"], 3000)
        self.assertEqual(archived["history_source"], "encode_ledger")

    def test_clear_finished_hides_queue_rows_but_keeps_lifetime_totals(self):
        original_jobs = dict(app_jobs.jobs)
        original_queue = list(app_jobs.job_queue)
        original_paused = app_jobs.queue_paused
        original_totals = dict(app_jobs.dashboard_totals)
        original_history_cutoff = app_jobs.history_cleared_before
        state_path = os.path.join(TEST_DATA, "clear-finished-state.json")
        finished_id = "finished-before-clear"
        queued_id = "queued-during-clear"
        future_id = "finished-after-clear"
        ledger = [
            {
                "ts": 900,
                "job_id": finished_id,
                "src": os.path.join(TEST_MEDIA, "Old.Movie.mkv"),
                "out": os.path.join(TEST_MEDIA, "Old.Movie-TSD.mkv"),
                "preset": "1080",
                "src_bytes": 5000,
                "out_bytes": 2000,
                "saved_bytes": 3000,
                "duration_seconds": 100,
            },
            {
                "ts": 1100,
                "job_id": future_id,
                "src": os.path.join(TEST_MEDIA, "New.Movie.mkv"),
                "out": os.path.join(TEST_MEDIA, "New.Movie-TSD.mkv"),
                "preset": "4k",
                "src_bytes": 6000,
                "out_bytes": 2000,
                "saved_bytes": 4000,
                "duration_seconds": 60,
            },
        ]
        storage_summary = {
            "count": 2,
            "saved_bytes": 7000,
            "saved_gb": 0.0,
            "total_runtime_seconds": 160.0,
        }

        app_jobs.jobs.clear()
        app_jobs.jobs.update(
            {
                finished_id: {
                    "src": ledger[0]["src"],
                    "preset": "1080",
                    "status": "done",
                    "progress": 100,
                    "created_at": 800,
                    "finished_at": 900,
                    "duration_seconds": 100,
                    "saved_bytes": 3000,
                },
                queued_id: {
                    "src": os.path.join(TEST_MEDIA, "Queued.Movie.mkv"),
                    "preset": "1080",
                    "status": "queued",
                    "progress": 0,
                    "created_at": 950,
                },
            }
        )
        app_jobs.job_queue[:] = [queued_id]
        app_jobs.queue_paused = False
        app_jobs.dashboard_totals = app_jobs._empty_dashboard_totals()
        app_jobs.history_cleared_before = 0.0

        try:
            with (
                patch.object(app_jobs, "JOBS_FILE", state_path),
                patch.object(app_jobs, "_now_ts", return_value=1000.0),
                patch.object(app_jobs, "list_encodes", return_value=ledger),
                patch.object(app_jobs, "get_storage_summary", return_value=storage_summary),
            ):
                removed = app_jobs.clear_finished_jobs()
                self.assertEqual(removed, 1)
                self.assertEqual(app_jobs.history_cleared_before, 1000.0)

                visible_ids = [row["id"] for row in app_jobs.list_job_history_for_api()]
                self.assertNotIn(finished_id, visible_ids)
                self.assertIn(queued_id, visible_ids)
                self.assertIn(future_id, visible_ids, "jobs completed after Clear must remain visible")

                summary = app_jobs.get_job_summary()
                self.assertEqual(summary["counts"]["done"], 2)
                self.assertEqual(summary["saved_bytes"], 7000)
                self.assertEqual(summary["total_runtime_seconds"], 160.0)

                # Prove the clear boundary survives a controller restart.
                app_jobs.jobs.clear()
                app_jobs.job_queue.clear()
                app_jobs.dashboard_totals = app_jobs._empty_dashboard_totals()
                app_jobs.history_cleared_before = 0.0
                app_jobs.load_jobs()
                self.assertEqual(app_jobs.history_cleared_before, 1000.0)
                restarted_ids = [row["id"] for row in app_jobs.list_job_history_for_api()]
                self.assertNotIn(finished_id, restarted_ids)
                self.assertIn(queued_id, restarted_ids)
                self.assertIn(future_id, restarted_ids)
        finally:
            app_jobs.jobs.clear()
            app_jobs.jobs.update(original_jobs)
            app_jobs.job_queue[:] = original_queue
            app_jobs.queue_paused = original_paused
            app_jobs.dashboard_totals = original_totals
            app_jobs.history_cleared_before = original_history_cutoff

    def test_jobs_page_starts_critical_data_load_before_optional_work(self):
        response = self.client.get("/jobs")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        init_at = html.index("document.addEventListener('DOMContentLoaded'")
        first_jobs_load = html.index("refreshJobs();", init_at)
        optional_browser_init = html.index("populateRoots();", init_at)
        self.assertLess(first_jobs_load, optional_browser_init)
        self.assertIn("renderJobsSummary(data.summary || {})", html)
        self.assertIn("Lifetime totals above are never reset", html)
        self.assertIn("if (dashboardAutopilotButton)", html)
        self.assertIn("if (operationsStatusLine)", html)

    def test_clear_finished_removes_canceled_history_but_not_running_work(self):
        original_jobs = dict(app_jobs.jobs)
        original_queue = list(app_jobs.job_queue)
        original_totals = dict(app_jobs.dashboard_totals)
        original_cutoff = app_jobs.history_cleared_before
        canceled_id = "canceled-worker-history"
        running_id = "running-worker-history"
        try:
            app_jobs.jobs.clear()
            app_jobs.jobs.update({
                canceled_id: {
                    "id": canceled_id,
                    "src": os.path.join(TEST_MEDIA, "Canceled.Movie.mkv"),
                    "status": "canceled",
                    "duration_seconds": 42,
                },
                running_id: {
                    "id": running_id,
                    "src": os.path.join(TEST_MEDIA, "Running.Movie.mkv"),
                    "status": "running",
                },
            })
            app_jobs.job_queue[:] = [canceled_id]
            app_jobs.dashboard_totals = app_jobs._empty_dashboard_totals()
            with patch.object(app_jobs, "save_jobs"):
                removed = app_jobs.clear_finished_jobs()
            self.assertEqual(removed, 1)
            self.assertNotIn(canceled_id, app_jobs.jobs)
            self.assertNotIn(canceled_id, app_jobs.job_queue)
            self.assertIn(running_id, app_jobs.jobs)
            self.assertEqual(app_jobs.dashboard_totals["canceled"], 1)
            self.assertEqual(app_jobs.dashboard_totals["canceled_runtime_seconds"], 42)
        finally:
            app_jobs.jobs.clear()
            app_jobs.jobs.update(original_jobs)
            app_jobs.job_queue[:] = original_queue
            app_jobs.dashboard_totals = original_totals
            app_jobs.history_cleared_before = original_cutoff

    def test_home_summary_uses_lightweight_catalog_and_autopilot_data(self):
        lightweight_library = {"movies": 12, "shows": 4, "episodes": 88, "updated_at": 123.0}
        compact_autopilot = {
            "autopilot": {"enabled": True, "mode": "observe"},
            "readiness": {"ready": True, "score": 100, "recommendations": []},
            "continuous_learning": {"enabled": True, "pending": 0},
        }
        with (
            patch("webui.app.routes._beta_load_library_cache", side_effect=AssertionError("full catalog loaded")),
            patch("webui.app.routes._beta_load_library_summary", return_value=lightweight_library),
            patch("webui.app.routes._autopilot_status_payload", return_value=compact_autopilot) as autopilot_status,
            patch("webui.app.routes.get_storage_summary", return_value={"count": 3, "saved_bytes": 1200}),
            patch("webui.app.routes.get_storage_dashboard_analytics", return_value={"workers": [{"node_name": "Worker 1"}]}) as storage_analytics,
        ):
            response = self.client.get("/api/home/summary")

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        payload = response.get_json()
        self.assertEqual(payload["library"]["movies"], 12)
        self.assertEqual(payload["library"]["episodes"], 88)
        self.assertEqual(payload["storage"]["analytics"]["workers"][0]["node_name"], "Worker 1")
        autopilot_status.assert_called_once_with(compact=True)
        storage_analytics.assert_called_once_with(days=30, worker_limit=6)

    def test_storage_dashboard_analytics_tracks_trends_workers_and_efficiency(self):
        now = datetime.now()
        rows = [
            {
                "ts": now.timestamp(),
                "node_id": "worker-a",
                "node_name": "Worker A",
                "src_bytes": 1000,
                "out_bytes": 400,
                "saved_bytes": 600,
                "duration_seconds": 120,
            },
            {
                "ts": (now - timedelta(days=1)).timestamp(),
                "node_id": "worker-b",
                "node_name": "Worker B",
                "src_bytes": 800,
                "out_bytes": 500,
                "saved_bytes": 300,
                "duration_seconds": 60,
            },
        ]
        ledger = {"encodes": rows, "totals": {"count": 3, "saved_bytes": 1200}}
        original_cache = app_storage_stats._analytics_cache
        try:
            app_storage_stats._analytics_cache = {}
            with patch.object(app_storage_stats, "_load_unlocked", return_value=ledger):
                analytics = app_storage_stats.get_dashboard_analytics(days=7, worker_limit=6, now_ts=now.timestamp())
        finally:
            app_storage_stats._analytics_cache = original_cache

        self.assertEqual(len(analytics["trend"]), 7)
        self.assertEqual(analytics["recent_completed"], 2)
        self.assertEqual(analytics["recent_saved_bytes"], 900)
        self.assertEqual(analytics["efficiency_percent"], 50.0)
        self.assertEqual(analytics["worker_count"], 2)
        self.assertEqual(analytics["workers"][0]["node_name"], "Worker A")
        self.assertEqual(analytics["workers"][0]["saved_bytes"], 600)
        self.assertIn("Older history", [worker["node_name"] for worker in analytics["workers"]])
        self.assertEqual(analytics["longest_streak_days"], 2)
        self.assertFalse(analytics["history_complete"])

    def test_event_history_is_compacted_once_and_cached(self):
        events_path = os.path.join(TEST_DATA, "performance-events.json")
        oversized = [
            {
                "ts": float(index),
                "level": "info",
                "type": "beta_auto_scan",
                "message": f"Scan {index}",
                "decisions": [{"path": "x" * 400, "reason": "y" * 400} for _ in range(20)],
            }
            for index in range(app_events.MAX_EVENTS + 10)
        ]
        with open(events_path, "w", encoding="utf-8") as handle:
            json.dump(oversized, handle, indent=2)
        original_size = os.path.getsize(events_path)
        original_file = app_events.EVENTS_FILE
        original_cache = app_events._events_cache
        original_signature = app_events._events_signature
        try:
            app_events.EVENTS_FILE = events_path
            app_events._events_cache = None
            app_events._events_signature = None
            first = app_events.load_events(limit=app_events.MAX_EVENTS + 50)
            compacted_size = os.path.getsize(events_path)
            compacted_signature = app_events._events_signature
            second = app_events.load_events(limit=5)
            summaries = app_events.load_event_summaries(limit=5)
            self.assertEqual(len(first), app_events.MAX_EVENTS)
            self.assertEqual(len(second), 5)
            self.assertNotIn("decisions", summaries[0])
            self.assertLess(compacted_size, original_size)
            self.assertEqual(app_events._events_signature, compacted_signature)
        finally:
            app_events.EVENTS_FILE = original_file
            app_events._events_cache = original_cache
            app_events._events_signature = original_signature
            try:
                os.remove(events_path)
            except FileNotFoundError:
                pass

    def test_cold_home_summary_releases_expanded_library_cache(self):
        original_library_cache = app_routes.BETA_LIBRARY_MEMORY_CACHE.copy()
        original_summary_cache = app_routes.BETA_LIBRARY_SUMMARY_CACHE.copy()

        def load_catalog(_settings):
            catalog = {
                "movies": [{"title": "Example"}],
                "shows": [{"title": "Show", "episode_count": 3}],
                "stats": {"episodes": 3},
            }
            app_routes.BETA_LIBRARY_MEMORY_CACHE.update({"signature": ("loaded",), "data": catalog})
            return catalog

        try:
            app_routes.BETA_LIBRARY_MEMORY_CACHE.update({"signature": None, "data": None})
            app_routes.BETA_LIBRARY_SUMMARY_CACHE.update({"signature": None, "data": None})
            with (
                patch("webui.app.routes._beta_file_signature", return_value=None),
                patch("webui.app.routes._beta_mapped_roots", return_value=[]),
                patch("webui.app.routes._beta_load_library_cache", side_effect=load_catalog),
            ):
                summary = app_routes._beta_load_library_summary({})
            self.assertEqual(summary["movies"], 1)
            self.assertEqual(summary["episodes"], 3)
            self.assertIsNone(app_routes.BETA_LIBRARY_MEMORY_CACHE["data"])
        finally:
            app_routes.BETA_LIBRARY_MEMORY_CACHE.clear()
            app_routes.BETA_LIBRARY_MEMORY_CACHE.update(original_library_cache)
            app_routes.BETA_LIBRARY_SUMMARY_CACHE.clear()
            app_routes.BETA_LIBRARY_SUMMARY_CACHE.update(original_summary_cache)

    def test_ai_settings_are_secret_safe_and_provider_test_is_grounded(self):
        page = self.client.get("/settings/ai")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"AI &amp; API Keys", page.data)
        self.assertIn(b"Create Gemini key", page.data)
        self.assertIn(b"Save &amp; test selected provider", page.data)
        self.assertIn(b"Keep English and Spanish audio and subtitles", page.data)

        general_page = self.client.get("/settings")
        self.assertEqual(general_page.status_code, 200)
        self.assertIn(b"Looking for the Gemini or OpenAI API-key boxes?", general_page.data)
        self.assertIn(b'href="/settings/ai"', general_page.data)
        self.assertNotIn(b'id="ai-api-keys"', general_page.data)
        self.assertNotIn(b"Protect the source before saving space", general_page.data)

        smart_page = self.client.get("/settings/smart")
        self.assertEqual(smart_page.status_code, 200)
        self.assertIn(b"Smart Preset Settings", smart_page.data)
        self.assertIn(b"Protect the source before saving space", smart_page.data)
        self.assertIn(b"Never downscale or resize", smart_page.data)
        self.assertIn(b"Never transcode audio", smart_page.data)
        self.assertNotIn(b'id="ai-api-keys"', smart_page.data)
        with open(os.path.join(PROJECT_ROOT, "webui", "app", "static", "styleui.css"), "r", encoding="utf-8") as handle:
            settings_css = handle.read()
        self.assertIn("body.settings-subpage-main .ai-settings-section", settings_css)
        self.assertIn("body.settings-subpage-main .smart-settings-section", settings_css)
        self.assertIn("body.settings-subpage-smart .main-settings-section", settings_css)

        wizard_page = self.client.get("/size_wizard")
        self.assertEqual(wizard_page.status_code, 200)
        self.assertIn(b"Set up Gemini or OpenAI", wizard_page.data)
        self.assertIn(b'id="wizardAiSetupCard"', wizard_page.data)

        with open(os.path.join(PROJECT_ROOT, "docs", "AI_ADVISOR.md"), "r", encoding="utf-8") as handle:
            advisor_docs = handle.read()
        self.assertIn("Docker Compose environment variables", advisor_docs)
        self.assertIn("GEMINI_API_KEY", advisor_docs)
        self.assertIn("OPENAI_API_KEY", advisor_docs)

        saved = self.client.post(
            "/api/ai/settings",
            json={
                "provider": "gemini",
                "gemini_api_key": "test-secret-key",
                "gemini_model": "gemini-3.6-flash",
            },
        )
        self.assertEqual(saved.status_code, 200, saved.get_data(as_text=True))
        payload = saved.get_json()
        self.assertTrue(payload["gemini_api_configured"])
        self.assertNotIn("gemini_api_key", payload)

        public_settings = self.client.get("/api/settings").get_json()["settings"]
        self.assertNotIn("gemini_api_key", public_settings)
        self.assertTrue(public_settings["gemini_api_configured"])

        with patch("webui.app.routes.run_wizard_llm", return_value={"ok": True, "answer": "Connection ready.", "status": {"provider": "gemini"}}) as advisor:
            tested = self.client.post("/api/ai/test")
        self.assertEqual(tested.status_code, 200)
        self.assertIn("Connection ready", tested.get_json()["answer"])
        advisor.assert_called_once()

        self.client.post("/api/ai/settings", json={"provider": "local", "clear_gemini_key": True})

    def test_probe_media_uses_ffprobe_when_handbrake_json_has_no_title_list(self):
        fallback = {
            "duration_sec": 9960.5,
            "width": 3840,
            "height": 2160,
            "fps": 23.976,
            "video_codec": "hevc",
            "is_hdr": True,
            "hdr_reason": "ffprobe transfer characteristic",
        }
        with (
            patch("webui.app.routes._run_cmd", return_value=(True, '{"Scan":{"Progress":1}}', "")),
            patch("webui.app.routes._ffprobe_media_fast", return_value=fallback) as ffprobe,
            patch("webui.app.routes._probe_media_text_fallback") as text_fallback,
        ):
            result = app_routes._probe_media("/media/example/Dune.Part.Two.2160p.mp4")

        self.assertEqual(result, fallback)
        ffprobe.assert_called_once_with("/media/example/Dune.Part.Two.2160p.mp4")
        text_fallback.assert_not_called()

    def test_probe_media_accepts_a_top_level_handbrake_title_array(self):
        scan = [{
            "Duration": {"Hours": 2, "Minutes": 46, "Seconds": 0},
            "Geometry": {"Width": 3840, "Height": 2160},
            "FrameRate": 23.976,
            "Video": {"CodecName": "hevc"},
        }]
        with (
            patch("webui.app.routes._run_cmd", return_value=(True, json.dumps(scan), "")),
            patch(
                "webui.app.routes._ffprobe_media_fast",
                return_value={
                    "duration_sec": 9960.0,
                    "width": 3840,
                    "height": 2160,
                    "fps": 23.976,
                    "video_codec": "hevc",
                    "is_hdr": True,
                    "hdr_reason": "smpte2084",
                    "hdr_format": "hdr10",
                    "color_transfer": "smpte2084",
                    "color_primaries": "bt2020",
                    "bit_depth": 10,
                },
            ) as ffprobe,
        ):
            result = app_routes._probe_media("/media/example/movie.mp4")

        self.assertEqual(result["width"], 3840)
        self.assertEqual(result["height"], 2160)
        self.assertEqual(result["duration_sec"], 9960.0)
        self.assertTrue(result["is_hdr"])
        self.assertEqual(result["hdr_reason"], "smpte2084")
        self.assertEqual(result["color_primaries"], "bt2020")
        self.assertEqual(result["probe_source"], "handbrake+ffprobe")
        ffprobe.assert_called_once_with("/media/example/movie.mp4")

    def test_gemini_and_openai_advisors_use_supported_json_shapes(self):
        class FakeResponse:
            def __init__(self, payload):
                self.payload = json.dumps(payload).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return self.payload

        plan = {
            "src": os.path.join(TEST_MEDIA, "Private.Movie.mkv"),
            "probe": {"width": 1920, "height": 1080, "duration_sec": 7200, "source_size_bytes": 10 * 1024**3, "is_hdr": False, "source_type": "movie"},
            "inputs": {"ai_goal": "balanced", "ai_risk": "safe", "target_mb": 5000, "video_codec": "h265", "encoder_family": "software", "audio_mode": "copy", "audio_tracks": "all", "subtitle_mode": "all"},
            "estimates": {"encoder": "x265", "quality_label": "Good", "output_resolution": {"width": 1920, "height": 1080}},
        }
        model_payload = {"answer": "The plan is balanced and keeps the requested tracks.", "updates": {}, "confidence": "high"}

        with patch(
            "webui.app.wizard_llm.urlopen",
            return_value=FakeResponse({"candidates": [{"content": {"parts": [{"text": json.dumps(model_payload)}]}}]}),
        ) as gemini_call:
            gemini = app_wizard_llm.run_wizard_llm(
                "Explain this plan.",
                plan,
                {"wizard_ai_provider": "gemini", "gemini_api_key": "gemini-secret", "gemini_model": "gemini-3.6-flash"},
            )
        self.assertTrue(gemini["ok"])
        gemini_request = gemini_call.call_args.args[0]
        self.assertIn(":generateContent", gemini_request.full_url)
        self.assertIn("gemini-3.6-flash", gemini_request.full_url)
        self.assertEqual(gemini_request.get_header("X-goog-api-key"), "gemini-secret")

        with patch(
            "webui.app.wizard_llm.urlopen",
            return_value=FakeResponse({"output": [{"content": [{"type": "output_text", "text": json.dumps(model_payload)}]}]}),
        ) as openai_call:
            openai = app_wizard_llm.run_wizard_llm(
                "Explain this plan.",
                plan,
                {"wizard_ai_provider": "openai", "openai_api_key": "openai-secret", "openai_model": "gpt-5.6-luna"},
            )
        self.assertTrue(openai["ok"])
        openai_request = openai_call.call_args.args[0]
        self.assertEqual(openai_request.full_url, "https://api.openai.com/v1/responses")
        sent = json.loads(openai_call.call_args.kwargs["data"].decode("utf-8"))
        self.assertEqual(sent["model"], "gpt-5.6-luna")
        self.assertNotIn("reasoning", sent)

    def test_openai_episode_scene_analysis_uses_bounded_images_and_cache(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                scene = {
                    "summary": "Dark, grainy dialogue alternates with fast action and detailed wide shots.",
                    "scene_types": ["dark interiors", "fast action", "wide landscapes"],
                    "complexity": "high",
                    "motion": "high",
                    "grain": "heavy",
                    "lighting": "dark",
                    "content_type": "live_action",
                    "quality_bias": 1,
                }
                return json.dumps({
                    "output": [{"content": [{"type": "output_text", "text": json.dumps(scene)}]}]
                }).encode("utf-8")

        media_path = os.path.join(TEST_MEDIA, "Scene.Test.S01E01.mkv")
        with open(media_path, "wb") as handle:
            handle.write(b"episode")
        try:
            os.remove(app_wizard_llm.EPISODE_AI_CACHE_FILE)
        except FileNotFoundError:
            pass
        probe = {
            "duration_sec": 2700.0,
            "width": 3840,
            "height": 2160,
            "fps": 23.976,
            "video_codec": "hevc",
            "is_hdr": True,
            "hdr_reason": "smpte2084",
        }
        settings = {
            "wizard_ai_provider": "openai",
            "openai_api_key": "scene-secret",
            "openai_model": "gpt-5.6-luna",
        }
        profile = {"episode_ai_enabled": True, "episode_ai_frame_count": 4}
        frames = [b"jpeg-one", b"jpeg-two", b"jpeg-three", b"jpeg-four"]
        with (
            patch("webui.app.wizard_llm._sample_episode_frames", return_value=frames) as sampler,
            patch("webui.app.wizard_llm.urlopen", return_value=FakeResponse()) as openai_call,
        ):
            result = app_wizard_llm.analyze_episode_scenes(
                media_path, probe, profile=profile, settings=settings
            )

        self.assertTrue(result["used"])
        self.assertEqual(result["provider"], "openai")
        self.assertEqual(result["target_scale"], 1.22)
        sampler.assert_called_once()
        request = openai_call.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.openai.com/v1/responses")
        sent = json.loads(openai_call.call_args.kwargs["data"].decode("utf-8"))
        content = sent["input"][0]["content"]
        images = [row for row in content if row.get("type") == "input_image"]
        self.assertEqual(len(images), 4)
        self.assertTrue(all(row["detail"] == "low" for row in images))
        prompt = next(row["text"] for row in content if row.get("type") == "input_text")
        self.assertNotIn(media_path, prompt)
        self.assertNotIn(os.path.basename(media_path), prompt)

        with (
            patch("webui.app.wizard_llm._sample_episode_frames") as cached_sampler,
            patch("webui.app.wizard_llm.urlopen") as cached_call,
        ):
            cached = app_wizard_llm.analyze_episode_scenes(
                media_path, probe, profile=profile, settings=settings
            )
        self.assertTrue(cached["cached"])
        cached_sampler.assert_not_called()
        cached_call.assert_not_called()

    def test_episode_scene_analysis_failure_keeps_deterministic_plan(self):
        media_path = os.path.join(TEST_MEDIA, "Scene.Fallback.S01E02.mkv")
        with open(media_path, "wb") as handle:
            handle.write(b"episode-fallback")
        with (
            patch(
                "webui.app.wizard_llm._sample_episode_frames",
                side_effect=RuntimeError("decoder unavailable"),
            ),
            patch("webui.app.wizard_llm.urlopen") as provider_call,
        ):
            result = app_wizard_llm.analyze_episode_scenes(
                media_path,
                {"duration_sec": 2400, "width": 1920, "height": 1080, "is_hdr": False},
                profile={"episode_ai_enabled": True, "episode_ai_frame_count": 4},
                settings={
                    "wizard_ai_provider": "openai",
                    "openai_api_key": "configured",
                    "openai_model": "gpt-5.6-luna",
                },
            )
        self.assertTrue(result["enabled"])
        self.assertTrue(result["attempted"])
        self.assertFalse(result["used"])
        self.assertEqual(result["target_scale"], 1.0)
        self.assertIn("decoder unavailable", result["reason"])
        provider_call.assert_not_called()

    def test_autopilot_preview_training_is_visible_and_records_feedback(self):
        try:
            os.remove(SMART_PRESETS_FILE)
        except FileNotFoundError:
            pass
        app_routes.AUTOPILOT_REVIEW_STATE.clear()
        app_routes.AUTOPILOT_REVIEW_STATE["cursor"] = 0
        app_routes.PREVIEW_TASKS.clear()

        media_path = os.path.join(TEST_MEDIA, "Training.Movie.1080p.mkv")
        with open(media_path, "wb") as handle:
            handle.write(b"0" * 4096)
        library = {
            "movies": [{
                "id": "training-movie",
                "title": "Training Movie",
                "year": 2026,
                "path": media_path,
                "poster_url": "/api/media/artwork/local-abc.jpg",
            }],
            "shows": [],
        }
        probe = {
            "duration_sec": 7200.0,
            "width": 1920,
            "height": 1080,
            "fps": 23.976,
            "is_hdr": False,
        }
        with (
            patch("webui.app.routes._beta_load_library_cache", return_value=library),
            patch("webui.app.routes._probe_media", return_value=probe),
            patch("webui.app.routes.threading.Thread") as preview_thread,
        ):
            response = self.client.post("/api/autopilot/review", json={})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        review = response.get_json()["review"]
        self.assertEqual(review["title"], "Training Movie")
        self.assertEqual(review["preview"]["state"], "queued")
        self.assertTrue(review["learning"]["automation_enabled"])
        preview_thread.return_value.start.assert_called_once()

        feedback_token = app_routes._register_smart_feedback_context(
            {"source": {"kind": "movie"}, "features": {"codec": "h265"}, "plan": {}}
        )
        app_routes._preview_set_task(
            review["preview_id"],
            state="done",
            progress=100,
            message="Accurate preview ready.",
            result={"ok": True, "smart_feedback_token": feedback_token},
        )
        feedback = self.client.post(
            "/api/autopilot/review/feedback",
            json={"verdict": "approve", "reason": "looks_good"},
        )
        self.assertEqual(feedback.status_code, 200, feedback.get_data(as_text=True))
        feedback_review = feedback.get_json()["review"]
        self.assertTrue(feedback_review["reviewed"])
        self.assertEqual(feedback_review["learning"]["feedback_count"], 1)

    def test_completed_smart_job_accepts_feedback_only_once(self):
        job_id = "completed-learning-job"
        app_jobs.jobs[job_id] = {
            "id": job_id,
            "status": "done",
            "src": os.path.join(TEST_MEDIA, "Completed.Movie.2026.mkv"),
            "preset": "1080",
            "smart_preset": True,
            "smart_candidate_id": "balanced",
            "automation_source": "autopilot",
            "smart_feedback_context": {
                "source": {"kind": "movie", "hdr": False, "resolution": "1080p"},
                "features": {"codec": "h265", "target_ratio": 0.45},
                "plan": {"encoder": "x265"},
            },
            "finished_at": 123.0,
        }
        try:
            response = self.client.post(
                f"/api/autopilot/completed/{job_id}/feedback",
                json={"verdict": "reject", "reason": "audio"},
            )
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
            self.assertEqual(response.get_json()["feedback"]["reason"], "audio")
            self.assertEqual(app_jobs.jobs[job_id]["quality_feedback"]["verdict"], "reject")

            duplicate = self.client.post(
                f"/api/autopilot/completed/{job_id}/feedback",
                json={"verdict": "approve", "reason": "looks_good"},
            )
            self.assertEqual(duplicate.status_code, 409)
        finally:
            app_jobs.jobs.pop(job_id, None)

    def test_quality_and_size_feedback_teach_a_direction(self):
        saved_context = {
            "source": {"kind": "movie", "hdr": False, "resolution": "1080p"},
            "features": {"codec": "h265", "encoder_family": "software", "output_resolution": "1080p", "target_ratio": 0.4},
        }
        profile = {"minimum_feedback": 2}
        quality_state = {"profile": profile, "feedback": [{"verdict": "reject", "reason": "quality", "context": saved_context}]}
        smaller = {"source": saved_context["source"], "features": {**saved_context["features"], "target_ratio": 0.3}}
        roomier = {"source": saved_context["source"], "features": {**saved_context["features"], "target_ratio": 0.55}}
        self.assertGreater(candidate_learning(roomier, quality_state)["acceptance"], candidate_learning(smaller, quality_state)["acceptance"])

        size_state = {"profile": profile, "feedback": [{"verdict": "reject", "reason": "size", "context": saved_context}]}
        self.assertGreater(candidate_learning(smaller, size_state)["acceptance"], candidate_learning(roomier, size_state)["acceptance"])

    def test_local_poster_lookup_does_not_walk_into_shared_parent(self):
        title_one = os.path.join(TEST_MEDIA, "Title One")
        title_two = os.path.join(TEST_MEDIA, "Title Two")
        os.makedirs(title_one, exist_ok=True)
        os.makedirs(title_two, exist_ok=True)
        first = os.path.join(title_one, "movie.mkv")
        second = os.path.join(title_two, "movie.mkv")
        self.assertEqual(_sidecar_directories([first]), [title_one])
        self.assertNotIn(TEST_MEDIA, _sidecar_directories([first, second]))

    def test_movie_poster_lookup_rejects_shared_loose_movie_directory(self):
        shared = os.path.join(TEST_MEDIA, "Loose Movies")
        dedicated = os.path.join(TEST_MEDIA, "Dedicated Movie")
        os.makedirs(shared, exist_ok=True)
        os.makedirs(dedicated, exist_ok=True)
        first = os.path.join(shared, "First Movie (2024).mkv")
        second = os.path.join(shared, "Second Movie (2025).mkv")
        dedicated_movie = os.path.join(dedicated, "Only Movie (2026).mkv")
        for path in (first, second, dedicated_movie):
            with open(path, "wb") as handle:
                handle.write(b"movie")
        for directory in (shared, dedicated):
            with open(os.path.join(directory, "poster.jpg"), "wb") as handle:
                handle.write(b"poster")

        self.assertEqual(_cache_sidecar([first]), {})
        self.assertTrue(_cache_sidecar([dedicated_movie]).get("poster_url", "").startswith("/api/media/artwork/local-"))

    def test_library_cache_sanitizer_removes_shared_poster_from_unrelated_titles(self):
        shared_url = "/api/media/artwork/local-shared.jpg"
        data = {
            "movies": [
                {"type": "movie", "title": "Joker", "year": 2019, "poster_url": shared_url, "metadata_source": "local"},
                {"type": "movie", "title": "Scarface", "year": 1983, "poster_url": shared_url, "metadata_source": "local"},
            ],
            "shows": [],
        }

        self.assertTrue(app_routes._beta_sanitize_duplicate_artwork(data))
        self.assertEqual([movie["poster_url"] for movie in data["movies"]], ["", ""])
        self.assertTrue(all(movie["metadata_source"] == "local_duplicate_removed" for movie in data["movies"]))

    def test_library_cache_sanitizer_removes_shared_remote_movie_match(self):
        shared_url = "https://is1-ssl.mzstatic.com/image/thumb/wrong/600x900bb.jpg"
        data = {
            "movies": [
                {"type": "movie", "title": "Joker Folie a Deux", "year": 2024, "poster_url": shared_url, "metadata_source": "apple", "metadata_provider_id": 10},
                {"type": "movie", "title": "Deadpool And Wolverine", "year": 2024, "poster_url": shared_url, "metadata_source": "apple", "metadata_provider_id": 10},
            ],
            "shows": [],
        }
        self.assertTrue(app_routes._beta_sanitize_duplicate_artwork(data))
        self.assertEqual([movie["poster_url"] for movie in data["movies"]], ["", ""])
        self.assertTrue(all("metadata_provider_id" not in movie for movie in data["movies"]))

    def test_keyless_movie_match_requires_the_title_not_just_the_year(self):
        rows = [
            {"trackName": "Alien vs. Predator", "releaseDate": "2004-08-13T07:00:00Z", "trackId": 1},
            {"trackName": "Joker: Folie a Deux", "releaseDate": "2024-10-04T07:00:00Z", "trackId": 2},
        ]
        self.assertIsNone(_choose_movie(rows, "Deadpool and Wolverine", 2024))
        match = _choose_movie(rows, "Joker Folie a Deux", 2024)
        self.assertIsNotNone(match)
        self.assertEqual(match["trackId"], 2)

    def test_tmdb_match_requires_title_and_year_fit(self):
        rows = [
            {"id": 1, "title": "Alien vs. Predator", "release_date": "2004-08-13", "poster_path": "/wrong.jpg"},
            {"id": 2, "title": "Joker: Folie a Deux", "release_date": "2024-10-04", "poster_path": "/right.jpg"},
            {"id": 3, "title": "Joker: Folie a Deux", "release_date": "1994-10-04", "poster_path": "/wrong-year.jpg"},
        ]
        self.assertIsNone(app_routes._beta_choose_tmdb_result(rows, "Deadpool and Wolverine", 2024))
        match = app_routes._beta_choose_tmdb_result(rows, "Joker Folie a Deux", 2024)
        self.assertIsNotNone(match)
        self.assertEqual(match["id"], 2)

    def test_tmdb_artwork_wins_and_keyless_remains_the_fallback(self):
        settings = {
            "tmdb_api_key": "configured",
            "metadata_no_key_enabled": True,
            "episode_release_monitor_enabled": True,
        }

        def keyless(data, _settings):
            for movie in data.get("movies") or []:
                movie["poster_url"] = "https://apple.example/fallback.jpg"
                movie["metadata_source"] = "apple"
            return data

        preferred = {"movies": [{"title": "Example Movie", "year": 2026}], "shows": []}
        with (
            patch("webui.app.routes._beta_tmdb_search", return_value={
                "poster_url": "https://image.tmdb.org/t/p/w342/preferred.jpg",
                "source": "tmdb",
                "poster_source": "tmdb",
                "metadata_source": "tmdb",
                "tmdb_id": 42,
            }),
            patch("webui.app.routes.enrich_media_library", side_effect=keyless),
        ):
            result = app_routes._beta_enrich_metadata(preferred, settings)
        self.assertEqual(result["movies"][0]["poster_source"], "tmdb")
        self.assertTrue(result["movies"][0]["poster_url"].endswith("preferred.jpg"))
        self.assertEqual(result["metadata"]["artwork_priority"], "tmdb_then_keyless")

        fallback = {"movies": [{"title": "Missing Poster", "year": 2026}], "shows": []}
        with (
            patch("webui.app.routes._beta_tmdb_search", return_value={"poster_url": "", "source": "tmdb_empty"}),
            patch("webui.app.routes.enrich_media_library", side_effect=keyless),
        ):
            result = app_routes._beta_enrich_metadata(fallback, settings)
        self.assertEqual(result["movies"][0]["metadata_source"], "apple")
        self.assertTrue(result["movies"][0]["poster_url"].endswith("fallback.jpg"))

    def test_library_filename_parser_keeps_real_movie_title(self):
        cases = {
            "1976.Rocky.1920x1038.BDRip.x265.mkv": ("Rocky", 1976),
            "Deadpool.And.Wolverine.2024.2160p.UHD.BluRay.mkv": ("Deadpool And Wolverine", 2024),
            "Joker.Folie.a.Deux.2024.1080p.BluRay.mkv": ("Joker Folie a Deux", 2024),
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                parsed = app_routes._beta_parse_media(os.path.join(TEST_MEDIA, name))
                self.assertEqual((parsed["title"], parsed["year"]), expected)

    def test_library_catalog_includes_and_labels_transcoded_outputs(self):
        path = os.path.join(TEST_MEDIA, "Finished.Movie.2026-TSD.mkv")
        with open(path, "wb") as handle:
            handle.write(b"finished")
        item = app_routes._beta_parse_media(path, root_kind="movies", library_root=TEST_MEDIA)
        self.assertTrue(item["transcoded"])
        data = app_routes._beta_library_from_scan_index(
            {
                "files": {
                    path: {
                        "path": path,
                        "removed": False,
                        "item": item,
                    },
                },
            },
            settings={},
        )
        self.assertEqual(len(data["movies"]), 1)
        self.assertTrue(data["movies"][0]["transcoded"])
        self.assertEqual(data["stats"]["transcoded"], 1)
        self.assertEqual(data["catalog"]["transcoded"], 1)

    def test_v3_library_exposes_poster_health_filters_and_incremental_grid(self):
        response = self.client.get("/library?ui=v3")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        markup = response.get_data(as_text=True)
        self.assertIn('class="beta-page ui-v3"', markup)
        self.assertIn('value="artwork"', markup)
        self.assertIn('value="transcoded"', markup)
        self.assertIn('filter === "transcoded"', markup)
        self.assertIn('id="statArtwork"', markup)
        self.assertIn('className = "library-load-more"', markup)
        self.assertIn('img.decoding = "async"', markup)

    def test_library_parser_groups_show_folders_and_common_episode_formats(self):
        shows_root = os.path.join(TEST_MEDIA, "Shows")
        cases = {
            os.path.join(
                shows_root,
                "Dragon Ball Super",
                "Season 01",
                "Dragon Ball SUPER S01 E02 To the Promised Resort DUAL Audio.mkv",
            ): ("Dragon Ball Super", None, 1, 2),
            os.path.join(
                shows_root,
                "The Bear (2022)",
                "Season 2",
                "The.Bear.Season.2.Episode.3.2160p.mkv",
            ): ("The Bear", 2022, 2, 3),
            os.path.join(
                shows_root,
                "Modern Family",
                "Specials",
                "Behind the scenes.mkv",
            ): ("Modern Family", None, None, None),
            os.path.join(
                shows_root,
                "DRAGON BALL Z (1989-2019) - The COMPLETE Series, Movies and OVAs - 1080p",
                "1a. TV Series (1989-96)",
                "01. The SAIYAN Saga (Eps. 001-035)",
                "Dragon Ball Z - S01 E001 - The New Threat.mkv",
            ): ("DRAGON BALL Z", 1989, 1, 1),
            os.path.join(
                shows_root,
                "House.Of.The.Dragon.S02.COMPLETE.1080p.WEB-DL",
                "House.Of.The.Dragon.S02E01.mkv",
            ): ("House Of The Dragon", None, 2, 1),
            os.path.join(
                shows_root,
                "Fresh Off the Boat S01 Season 1 Complete 480p HDTV",
                "Fresh Off the Boat S01E01.mkv",
            ): ("Fresh Off the Boat", None, 1, 1),
            os.path.join(
                shows_root,
                "telonov",
                "Teresa 040 Decepción Amorosa.mkv",
            ): ("Teresa", None, 1, 40),
            os.path.join(
                shows_root,
                "telonov",
                "Teresa Final Alternativo 1.mp4",
            ): ("Teresa", None, 0, None),
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                parsed = app_routes._beta_parse_media(
                    path,
                    root_kind="shows",
                    library_root=shows_root,
                )
                self.assertEqual(
                    (parsed["title"], parsed["year"], parsed["season"], parsed["episode"]),
                    expected,
                )
                self.assertEqual(parsed["type"], "show")
                self.assertEqual(parsed["parser_version"], app_routes.BETA_LIBRARY_PARSER_VERSION)

    def test_show_grouping_merges_missing_year_but_keeps_known_reboots_separate(self):
        groups = {"acapulco::": {"title": "Acapulco", "year": None}}
        key = app_routes._beta_resolve_show_group_key(groups, "Acapulco", 2021)
        self.assertEqual(key, "acapulco::2021")
        self.assertNotIn("acapulco::", groups)
        self.assertEqual(groups[key]["year"], 2021)

        groups["acapulco::2035"] = {"title": "Acapulco", "year": 2035}
        self.assertEqual(
            app_routes._beta_resolve_show_group_key(groups, "Acapulco", None),
            "acapulco::",
        )

    def test_tvmaze_show_match_rejects_provider_score_without_title_fit(self):
        rows = [
            {"score": 0.99, "show": {"id": 1, "name": "Dragon Ball", "premiered": "1986-02-26"}},
            {"score": 0.75, "show": {"id": 2, "name": "Dragon Ball Super", "premiered": "2015-07-05"}},
        ]
        match = _choose_show(rows, "Dragon Ball Super", 2015)
        self.assertIsNotNone(match)
        self.assertEqual(match["id"], 2)
        self.assertIsNone(_choose_show(rows, "Modern Family", 2009))

    def test_dvr_root_catalogs_transport_streams_separately(self):
        dvr_root = os.path.join(TEST_MEDIA, "plex-dvr-library")
        os.makedirs(dvr_root, exist_ok=True)
        recording = os.path.join(dvr_root, "Evening.News.2026-09-20.ts")
        with open(recording, "wb") as handle:
            handle.write(b"transport-stream")
        try:
            with patch.object(
                app_routes,
                "_beta_mapped_roots",
                return_value=[{"path": dvr_root, "kind": "dvr", "label": "DVR"}],
            ):
                data = app_routes._beta_scan_library(
                    dvr_root,
                    recursive=True,
                    posters=False,
                    settings={"beta_media_folders": {"dvr": [{"path": dvr_root}]}},
                    root_kind="dvr",
                )
            self.assertEqual(data["stats"]["dvr"], 1)
            self.assertEqual(data["stats"]["movies"], 0)
            self.assertEqual(data["movies"], [])
            self.assertEqual(data["dvr"][0]["type"], "dvr_movie")
            self.assertEqual(data["dvr"][0]["dvr_media_type"], "movie")
            self.assertEqual(data["dvr"][0]["root_kind"], "dvr")
            self.assertEqual(data["dvr"][0]["path"], recording)
            self.assertEqual(data["catalog"]["dvr"], 1)
        finally:
            if os.path.isfile(recording):
                os.remove(recording)
            if os.path.isdir(dvr_root):
                os.rmdir(dvr_root)

    def test_dvr_root_uses_plex_folders_to_group_show_episodes(self):
        dvr_root = os.path.join(TEST_MEDIA, "plex-dvr-structured")
        show_dir = os.path.join(dvr_root, "TV Shows", "Como dice el dicho (2011)", "Season 02")
        movie_dir = os.path.join(dvr_root, "Movies", "Recorded Movie (2025)")
        os.makedirs(show_dir, exist_ok=True)
        os.makedirs(movie_dir, exist_ok=True)
        episodes = [
            os.path.join(show_dir, "Como dice el dicho - S02E01 - La verdad.ts"),
            os.path.join(show_dir, "Como dice el dicho - S02E02 - La promesa.ts"),
        ]
        movie = os.path.join(movie_dir, "Recorded Movie (2025).ts")
        for media_path in [*episodes, movie]:
            with open(media_path, "wb") as handle:
                handle.write(b"transport-stream")
        try:
            with patch.object(
                app_routes,
                "_beta_mapped_roots",
                return_value=[{"path": dvr_root, "kind": "dvr", "label": "DVR"}],
            ):
                data = app_routes._beta_scan_library(
                    dvr_root,
                    recursive=True,
                    posters=False,
                    settings={"beta_media_folders": {"dvr": [{"path": dvr_root}]}},
                    root_kind="dvr",
                )

            self.assertEqual(data["stats"]["dvr"], 2)
            self.assertEqual(data["stats"]["dvr_shows"], 1)
            self.assertEqual(data["stats"]["dvr_movies"], 1)
            self.assertEqual(data["stats"]["dvr_episodes"], 2)
            show = next(row for row in data["dvr"] if row["type"] == "dvr_show")
            self.assertEqual(show["title"], "Como dice el dicho")
            self.assertEqual(show["year"], 2011)
            self.assertEqual(show["episode_count"], 2)
            self.assertEqual(show["season_count"], 1)
            self.assertEqual(sorted(row["episode"] for row in show["files"]), [1, 2])
            recording = next(row for row in data["dvr"] if row["type"] == "dvr_movie")
            self.assertEqual(recording["title"], "Recorded Movie")
            self.assertEqual(recording["year"], 2025)
            self.assertEqual(recording["path"], movie)
        finally:
            shutil.rmtree(dvr_root, ignore_errors=True)

    def test_dvr_folder_mapping_is_normalized_and_exposed_as_a_root(self):
        dvr_root = os.path.join(TEST_MEDIA, "recordings")
        settings = {
            "beta_media_folders": {
                "movies": [],
                "shows": [],
                "dvr": [{"path": dvr_root, "label": "Plex DVR"}],
            }
        }
        normalized = app_settings._normalize_beta_media_folders(
            settings["beta_media_folders"]
        )
        settings["beta_media_folders"] = normalized
        roots = app_routes._beta_mapped_roots(settings)

        self.assertEqual(normalized["dvr"], [{"path": dvr_root, "label": "Plex DVR"}])
        self.assertEqual(
            roots,
            [{"path": dvr_root, "label": "DVR: Plex DVR", "kind": "dvr"}],
        )

    def test_incremental_scan_reparses_unchanged_files_after_parser_upgrade(self):
        shows_root = os.path.join(TEST_MEDIA, "parser-upgrade-shows")
        season_dir = os.path.join(shows_root, "Better Call Saul (2015)", "Season 01")
        os.makedirs(season_dir, exist_ok=True)
        episode_path = os.path.join(season_dir, "Better.Call.Saul.S01 E01.1080p.mkv")
        with open(episode_path, "wb") as handle:
            handle.write(b"episode")
        stat = os.stat(episode_path)
        old_index = {
            "version": 1,
            "files": {
                episode_path: {
                    "path": episode_path,
                    "size_bytes": stat.st_size,
                    "mtime": stat.st_mtime,
                    "root_kind": "shows",
                    "parser_version": 1,
                    "item": {"title": "Better Call Saul S01 E01", "type": "show"},
                    "first_seen": 10,
                    "queued_at": 20,
                },
            },
        }
        with (
            patch.object(app_routes, "_beta_load_scan_index", return_value=old_index),
            patch.object(
                app_routes,
                "_beta_mapped_roots",
                return_value=[{"path": shows_root, "kind": "shows", "label": "Shows"}],
            ),
            patch.object(app_routes, "is_allowed_path", return_value=True),
            patch.object(app_routes, "_beta_save_scan_index") as save,
        ):
            index, summary = app_routes._beta_update_scan_index({})

        row = index["files"][episode_path]
        self.assertEqual(summary["reclassified"], 1)
        self.assertEqual(row["parser_version"], app_routes.BETA_LIBRARY_PARSER_VERSION)
        self.assertEqual(row["item"]["title"], "Better Call Saul")
        self.assertEqual((row["item"]["season"], row["item"]["episode"]), (1, 1))
        self.assertEqual(row["queued_at"], 20)
        save.assert_called_once_with(index)

    def test_incremental_scan_reuses_unchanged_directories_without_file_stats(self):
        shows_root = os.path.join(TEST_MEDIA, "quick-index-shows")
        season_dir = os.path.join(shows_root, "Efficient Show", "Season 01")
        os.makedirs(season_dir, exist_ok=True)
        episode_path = os.path.join(season_dir, "Efficient.Show.S01E01.mkv")
        with open(episode_path, "wb") as handle:
            handle.write(b"episode")
        mapped = [{"path": shows_root, "kind": "shows", "label": "Shows"}]
        scan_settings = {"beta_auto_scan_file_stability_enabled": False}
        with (
            patch.object(app_routes, "_beta_load_scan_index", return_value=app_routes._beta_empty_scan_index()),
            patch.object(app_routes, "_beta_mapped_roots", return_value=mapped),
            patch.object(app_routes, "is_allowed_path", return_value=True),
            patch.object(app_routes, "_beta_save_scan_index"),
        ):
            index, first = app_routes._beta_update_scan_index(scan_settings, force_full=True)

        self.assertEqual(first["mode"], "full_verify")
        self.assertEqual(first["new"], 1)
        self.assertGreaterEqual(first["directories_scanned"], 3)
        # Fresh directories on SMB can report one delayed mtime update after
        # their first enumeration. Model the settled state used by later scans.
        for directory, row in index["directories"].items():
            row["mtime_ns"] = os.stat(directory).st_mtime_ns

        real_scandir = os.scandir
        with (
            patch.object(app_routes, "_beta_load_scan_index", return_value=index),
            patch.object(app_routes, "_beta_mapped_roots", return_value=mapped),
            patch.object(app_routes, "is_allowed_path", return_value=True),
            patch.object(app_routes.os, "scandir", wraps=real_scandir) as scandir,
            patch.object(app_routes, "_beta_parse_media") as parse,
            patch.object(app_routes, "_beta_save_scan_index") as save,
        ):
            _index, second = app_routes._beta_update_scan_index(scan_settings)

        self.assertEqual(second["mode"], "quick_incremental")
        self.assertEqual(second["scanned"], 1)
        self.assertEqual(second["unchanged"], 1)
        self.assertEqual(second["file_stats"], 0)
        self.assertGreaterEqual(second["directories_cached"], 3)
        scandir.assert_not_called()
        parse.assert_not_called()
        save.assert_not_called()

    def test_incremental_scan_preserves_index_when_library_share_is_unavailable(self):
        missing_root = os.path.join(TEST_MEDIA, "offline-share")
        movie_path = os.path.join(missing_root, "Preserved Movie.mkv")
        old_index = {
            "version": app_routes.BETA_LIBRARY_PARSER_VERSION,
            "generated_at": 1,
            "last_full_scan_at": app_routes.time.time(),
            "directories": {
                missing_root: {
                    "mtime_ns": 1,
                    "root_kind": "movies",
                    "root_path": missing_root,
                    "children": [],
                }
            },
            "files": {
                movie_path: {
                    "path": movie_path,
                    "size_bytes": 100,
                    "root_kind": "movies",
                    "parser_version": app_routes.BETA_LIBRARY_PARSER_VERSION,
                    "item": {"path": movie_path, "title": "Preserved Movie", "type": "movie"},
                    "stable_passes": 2,
                    "removed": False,
                }
            },
            "unavailable_paths": [],
        }
        with (
            patch.object(app_routes, "_beta_load_scan_index", return_value=old_index),
            patch.object(
                app_routes,
                "_beta_mapped_roots",
                return_value=[{"path": missing_root, "kind": "movies", "label": "Movies"}],
            ),
            patch.object(app_routes, "is_allowed_path", return_value=True),
            patch.object(app_routes, "_beta_save_scan_index"),
        ):
            index, summary = app_routes._beta_update_scan_index(
                {"beta_auto_scan_file_stability_enabled": False}
            )

        self.assertEqual(summary["unavailable_roots"], 1)
        self.assertEqual(summary["removed"], 0)
        self.assertFalse(index["files"][movie_path]["removed"])
        self.assertIn(missing_root, index["unavailable_paths"])

    def test_auto_scan_failure_releases_lock_and_status_poll_revives_scheduler(self):
        run_lock = app_routes.threading.Lock()
        with (
            patch.object(app_routes, "BETA_AUTOSCAN_LOCK", run_lock),
            patch.object(app_routes, "load_settings", side_effect=RuntimeError("settings unavailable")),
            patch.object(
                app_routes,
                "_beta_save_autoscan_status",
                side_effect=lambda value: value,
            ) as save_status,
            patch.object(app_routes, "log_event"),
        ):
            status = app_routes._beta_run_incremental_auto_scan(reason="test")
        self.assertEqual(status["last_status"], "error")
        self.assertIn("settings unavailable", status["last_message"])
        self.assertTrue(run_lock.acquire(blocking=False))
        run_lock.release()
        save_status.assert_called()

        with patch.object(app_routes, "_ensure_beta_autoscan_scheduler") as ensure:
            response = self.client.get("/api/beta/auto_scan/status")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        ensure.assert_called_once_with()

    def test_auto_scan_scheduler_restarts_a_dead_thread(self):
        class DeadThread:
            def is_alive(self):
                return False

        class StartedThread:
            def __init__(self):
                self.started = False

            def start(self):
                self.started = True

            def is_alive(self):
                return self.started

        replacement = StartedThread()
        with (
            patch.dict(
                app_routes.os.environ,
                {"TSD_DISABLE_BETA_AUTOSCAN": "0", "FLASK_DEBUG": "0"},
            ),
            patch.object(app_routes, "BETA_AUTOSCAN_THREAD", DeadThread()),
            patch.object(app_routes.threading, "Thread", return_value=replacement) as thread_factory,
            patch.object(app_routes, "log_event"),
        ):
            started = app_routes._start_beta_autoscan_thread(force_serving_process=True)

        self.assertTrue(started)
        self.assertTrue(replacement.started)
        thread_factory.assert_called_once()

    def test_unchanged_auto_scan_reuses_catalog_without_metadata_rebuild(self):
        scan_summary = {
            "mode": "quick_incremental",
            "scanned": 42,
            "new": 0,
            "changed": 0,
            "reclassified": 0,
            "removed": 0,
            "unchanged": 42,
        }
        queue_summary = {
            "queued": 0,
            "smart_planned": 0,
            "skipped_unstable": 0,
            "skipped_active": 0,
            "skipped_missing_mapping": 0,
            "skipped": [],
        }
        autopilot_summary = {"queued": 0, "eligible": 0}
        cached = {"movies": [], "shows": [], "tracking": {}}
        with (
            patch.object(app_routes, "BETA_AUTOSCAN_LOCK", app_routes.threading.Lock()),
            patch.object(
                app_routes,
                "load_settings",
                return_value={
                    "beta_auto_scan_enabled": True,
                    "beta_auto_scan_skip_while_encoding": False,
                },
            ),
            patch.object(app_routes, "_beta_load_autoscan_status", return_value=app_routes._beta_empty_autoscan_status()),
            patch.object(app_routes, "_beta_save_autoscan_status", side_effect=lambda value: value),
            patch.object(app_routes, "_beta_update_scan_index", return_value=({"files": {}}, scan_summary)),
            patch.object(app_routes.os.path, "isfile", return_value=True),
            patch.object(app_routes, "_beta_load_library_cache", return_value=cached) as load_cache,
            patch.object(app_routes, "_beta_library_from_scan_index") as rebuild,
            patch.object(app_routes, "_beta_enrich_metadata") as enrich,
            patch.object(app_routes, "_beta_refresh_predictions") as predictions,
            patch.object(app_routes, "_beta_load_tracking", return_value={}),
            patch.object(app_routes, "_beta_apply_tracking", side_effect=lambda data, _tracking: data),
            patch.object(app_routes, "_beta_queue_stable_tracked_episodes", return_value=queue_summary),
            patch.object(app_routes, "_autopilot_candidates", return_value=autopilot_summary),
            patch.object(app_routes, "_beta_save_library_cache") as save_cache,
            patch.object(app_routes, "log_event"),
        ):
            status = app_routes._beta_run_incremental_auto_scan(reason="test")

        self.assertEqual(status["last_status"], "ok")
        self.assertFalse(status["last_summary"]["catalog_rebuilt"])
        load_cache.assert_called_once()
        rebuild.assert_not_called()
        enrich.assert_not_called()
        predictions.assert_not_called()
        save_cache.assert_not_called()

    def test_manual_library_refresh_reuses_unchanged_catalog(self):
        scan_summary = {
            "mode": "quick_incremental",
            "scanned": 42,
            "new": 0,
            "changed": 0,
            "reclassified": 0,
            "removed": 0,
            "unchanged": 42,
            "duration_ms": 12,
        }
        cached = {"movies": [], "shows": [], "stats": {}, "tracking": {}}
        with (
            patch.object(app_routes, "load_settings", return_value={}),
            patch.object(app_routes, "_beta_update_scan_index", return_value=({"files": {}}, scan_summary)),
            patch.object(app_routes.os.path, "isfile", return_value=True),
            patch.object(app_routes, "_beta_load_library_cache", return_value=cached) as load_cache,
            patch.object(app_routes, "_beta_library_from_scan_index") as rebuild,
            patch.object(app_routes, "_beta_enrich_metadata") as enrich,
            patch.object(app_routes, "_beta_refresh_predictions") as predictions,
            patch.object(app_routes, "_beta_load_tracking", return_value={}),
            patch.object(app_routes, "_beta_apply_tracking", side_effect=lambda data, _tracking: data),
            patch.object(
                app_routes,
                "_beta_auto_queue_tracked_episodes",
                return_value={"queued_count": 0, "skipped": [], "_planning_requests": []},
            ),
            patch.object(app_routes, "_beta_save_tracking"),
            patch.object(app_routes, "_beta_save_library_cache") as save_cache,
        ):
            response = self.client.get("/api/beta/library?root=__all__&recursive=1&posters=1")

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        payload = response.get_json()
        self.assertEqual(payload["scan"]["mode"], "incremental")
        self.assertEqual(payload["scan"]["strategy"], "quick_incremental")
        self.assertFalse(payload["scan"]["catalog_rebuilt"])
        load_cache.assert_called_once()
        rebuild.assert_not_called()
        enrich.assert_not_called()
        predictions.assert_not_called()
        save_cache.assert_not_called()

    def test_autopilot_uses_scan_index_without_restating_every_media_file(self):
        movie_path = os.path.join(TEST_MEDIA, "indexed-movie.mkv")
        movie = {
            "path": movie_path,
            "title": "Indexed Movie",
            "size_bytes": 3 * 1024 ** 3,
            "prediction": {"available": False},
        }
        index = {
            "files": {
                movie_path: {
                    "path": movie_path,
                    "size_bytes": movie["size_bytes"],
                    "item": movie,
                    "removed": False,
                }
            },
            "unavailable_paths": [],
        }
        settings = {
            "autopilot_include_movies": True,
            "autopilot_include_shows": False,
            "beta_auto_scan_file_stability_enabled": False,
        }
        with (
            patch.object(app_routes, "_beta_active_job_paths", return_value=set()),
            patch.object(app_routes, "_autopilot_active_job_count", return_value=0),
            patch.object(app_routes, "smart_learning_status", return_value={"automation_ready": False}),
            patch.object(
                app_routes.os.path,
                "isfile",
                side_effect=AssertionError("Autopilot should trust the refreshed scan index"),
            ),
        ):
            result = app_routes._autopilot_candidates({"movies": [movie], "shows": []}, index, settings)

        self.assertEqual(result["considered"], 1)
        self.assertEqual(result["eligible"], 1)
        self.assertEqual(result["decisions"][0]["decision"], "eligible")

        index["unavailable_paths"] = [TEST_MEDIA]
        with (
            patch.object(app_routes, "_beta_active_job_paths", return_value=set()),
            patch.object(app_routes, "_autopilot_active_job_count", return_value=0),
            patch.object(app_routes, "smart_learning_status", return_value={"automation_ready": False}),
        ):
            unavailable = app_routes._autopilot_candidates({"movies": [movie], "shows": []}, index, settings)
        self.assertEqual(unavailable["eligible"], 0)
        self.assertEqual(unavailable["decisions"][0]["decision"], "wait")
        self.assertIn("temporarily unavailable", unavailable["decisions"][0]["reason"])

    def test_mobile_bearer_flow_and_scope_enforcement(self):
        pairing_response = self.client.post("/api/mobile/pairing_code", json={"scope": "read"})
        self.assertEqual(pairing_response.status_code, 200)
        code = pairing_response.get_json()["pairing"]["code"]

        paired = self.client.post(
            "/api/mobile/v1/pair",
            json={"code": code, "device_id": "smoke-phone", "device_name": "Smoke phone", "platform": "android"},
        )
        self.assertEqual(paired.status_code, 200)
        token = paired.get_json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        status = self.client.get("/api/mobile/v1/status", headers=headers)
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.get_json()["device"]["id"], "smoke-phone")

        control = self.client.post("/api/mobile/v1/queue", json={"paused": True}, headers=headers)
        self.assertEqual(control.status_code, 403)

        preview = self.client.post(
            "/api/mobile/v1/library/preview",
            json={"src": os.path.join(TEST_MEDIA, "read-only.mkv")},
            headers=headers,
        )
        self.assertEqual(preview.status_code, 403)

        operation_update = self.client.post(
            "/api/mobile/v1/operations",
            json={"hardware_transcode_concurrency": 2},
            headers=headers,
        )
        self.assertEqual(operation_update.status_code, 403)

    def test_mobile_controls_combined_worker_queue_and_linked_nodes(self):
        pairing_response = self.client.post(
            "/api/mobile/pairing_code", json={"scope": "control"}
        )
        code = pairing_response.get_json()["pairing"]["code"]
        paired = self.client.post(
            "/api/mobile/v1/pair",
            json={
                "code": code,
                "device_id": "node-control-phone",
                "device_name": "Node control phone",
                "platform": "android",
            },
        )
        headers = {
            "Authorization": f"Bearer {paired.get_json()['access_token']}"
        }
        node_id = "mobile-worker"
        worker_job_id = "mobile-worker-job"
        app_node_linking.save_node({
            "id": node_id,
            "name": "ByteSqueeze Worker",
            "url": "http://worker:8080",
            "token": "worker-token",
            "recovery_token": "worker-recovery",
            "hardware_transcode_concurrency": 1,
            "jobs": [{
                "id": worker_job_id,
                "status": "queued",
                "src": os.path.join(TEST_MEDIA, "Mobile.Worker.Movie.1080p.mkv"),
                "preset": "1080",
                "queued_preset_name": "Correct 1080p",
            }],
            "summary": {"counts": {"queued": 1, "running": 0, "error": 0}},
        })
        try:
            combined = self.client.get("/api/mobile/v1/jobs", headers=headers)
            self.assertEqual(combined.status_code, 200, combined.get_data(as_text=True))
            worker_id = f"worker:{node_id}:{worker_job_id}"
            indexed = {item["id"]: item for item in combined.get_json()["jobs"]}
            self.assertIn(worker_id, indexed)
            self.assertEqual(indexed[worker_id]["node_name"], "ByteSqueeze Worker")

            with patch(
                "webui.app.routes.signed_json_request",
                return_value={
                    "ok": True,
                    "jobs": [],
                    "summary": {"counts": {"queued": 0}},
                },
            ) as signed_request:
                moved = self.client.post(
                    f"/api/mobile/v1/jobs/{worker_id}/action",
                    json={"action": "top"},
                    headers=headers,
                )
            self.assertEqual(moved.status_code, 200, moved.get_data(as_text=True))
            self.assertEqual(
                signed_request.call_args.args[1],
                f"/api/node/jobs/{worker_job_id}/action",
            )
            self.assertEqual(signed_request.call_args.kwargs["body"], {"action": "top"})

            renamed = self.client.post(
                f"/api/mobile/v1/nodes/{node_id}/action",
                json={"action": "rename", "name": "Office Arc GPU"},
                headers=headers,
            )
            self.assertEqual(renamed.status_code, 200, renamed.get_data(as_text=True))
            self.assertEqual(renamed.get_json()["node"]["name"], "Office Arc GPU")

            with patch(
                "webui.app.routes.signed_json_request",
                return_value={
                    "ok": True,
                    "encoding_policy": {"hardware_transcode_concurrency": 4},
                },
            ) as signed_request:
                capacity = self.client.post(
                    f"/api/mobile/v1/nodes/{node_id}/action",
                    json={
                        "action": "capacity",
                        "hardware_transcode_concurrency": 4,
                    },
                    headers=headers,
                )
            self.assertEqual(capacity.status_code, 200, capacity.get_data(as_text=True))
            self.assertEqual(capacity.get_json()["node"]["hardware_transcode_concurrency"], 4)
            self.assertEqual(signed_request.call_args.args[1], "/api/node/config")

            with patch(
                "webui.app.routes.signed_json_request",
                return_value={
                    "ok": True,
                    "paused": True,
                    "summary": {"counts": {"queued": 0}},
                },
            ) as signed_request:
                paused = self.client.post(
                    "/api/mobile/v1/queue",
                    json={"paused": True},
                    headers=headers,
                )
            self.assertEqual(paused.status_code, 200, paused.get_data(as_text=True))
            self.assertTrue(paused.get_json()["paused"])
            self.assertEqual(signed_request.call_args.args[1], "/api/node/queue")
            self.assertEqual(signed_request.call_args.kwargs["body"], {"paused": True})
        finally:
            app_jobs.set_queue_paused(False)
            app_node_linking.delete_node(node_id)

    def test_mobile_size_wizard_plans_and_queues_through_shared_learning(self):
        pairing_response = self.client.post(
            "/api/mobile/pairing_code", json={"scope": "control"}
        )
        paired = self.client.post(
            "/api/mobile/v1/pair",
            json={
                "code": pairing_response.get_json()["pairing"]["code"],
                "device_id": "wizard-phone",
                "device_name": "Wizard phone",
                "platform": "android",
            },
        )
        headers = {
            "Authorization": f"Bearer {paired.get_json()['access_token']}"
        }
        src = os.path.join(TEST_MEDIA, "Mobile.Wizard.Movie.2026.mkv")
        selected_plan = {
            "src": src,
            "preset": "1080",
            "probe": {"width": 1920, "height": 1080, "fps": 23.976},
            "options": {
                "video_codec": "h265",
                "encoder_family": "qsv",
                "bit_depth": "10",
                "framerate_mode": "same",
            },
            "inputs": {"target_mb": 4096},
            "estimates": {
                "encoder": "qsv_h265_10bit",
                "output_resolution": {"width": 1920, "height": 1080},
            },
            "extra_args": ["--encoder", "qsv_h265_10bit"],
        }
        recommendation = {
            "recommended_id": "balanced",
            "candidates": [{"id": "balanced", "name": "Learned balanced"}],
            "selected_plan": selected_plan,
            "learned_defaults": {"sample_count": 3},
            "learning": {"feedback_count": 3},
        }
        with patch.object(
            app_routes, "_smart_recommendation", return_value=recommendation
        ):
            planned = self.client.post(
                "/api/mobile/v1/size_wizard/plan",
                json={"src": src, "smart_start": True},
                headers=headers,
            )
        self.assertEqual(planned.status_code, 200, planned.get_data(as_text=True))
        planned_payload = planned.get_json()
        self.assertEqual(planned_payload["smart_candidate_id"], "balanced")
        self.assertEqual(
            planned_payload["plan"]["estimates"]["encoder"],
            "qsv_h265_10bit",
        )
        self.assertEqual(planned_payload["learned_defaults"]["sample_count"], 3)

        queued_result = {
            "job_id": "mobile-wizard-job",
            "preset": "1080",
            "extra_args": ["--encoder", "qsv_h265_10bit"],
            "estimates": selected_plan["estimates"],
            "dispatch_mode": "best",
            "learning_recorded": True,
            "learning": {"feedback_count": 4},
        }
        with patch.object(
            app_routes, "_queue_wizard_job", return_value=queued_result
        ) as queue_wizard:
            queued = self.client.post(
                "/api/mobile/v1/size_wizard/queue",
                json={
                    "src": src,
                    "mode": "best",
                    "video_codec": "h265",
                    "framerate_mode": "same",
                },
                headers=headers,
            )
        self.assertEqual(queued.status_code, 200, queued.get_data(as_text=True))
        self.assertTrue(queued.get_json()["learning_recorded"])
        queue_data = queue_wizard.call_args.args[0]
        self.assertEqual(queue_data["queue_source"], "bytesqueeze_app")
        self.assertEqual(queue_data["mode"], "best")
        self.assertEqual(queue_data["framerate_mode"], "same")

    def test_mobile_can_edit_worker_job_to_smart_and_clear_all_node_history(self):
        pairing_response = self.client.post(
            "/api/mobile/pairing_code", json={"scope": "control"}
        )
        code = pairing_response.get_json()["pairing"]["code"]
        paired = self.client.post(
            "/api/mobile/v1/pair",
            json={
                "code": code,
                "device_id": "smart-worker-phone",
                "device_name": "Smart worker phone",
                "platform": "android",
            },
        )
        headers = {
            "Authorization": f"Bearer {paired.get_json()['access_token']}"
        }
        node_id = "smart-mobile-worker"
        worker_job_id = "smart-worker-job"
        src = os.path.join(TEST_MEDIA, "Mobile.Smart.Movie.1080p.mkv")
        with open(src, "wb") as handle:
            handle.write(b"smart-worker")
        worker = {
            "id": node_id,
            "name": "Smart worker",
            "url": "http://smart-worker:8080",
            "token": "worker-token",
            "jobs": [{
                "id": worker_job_id,
                "status": "queued",
                "src": src,
                "preset": "1080",
            }],
            "hardware": {"encoder_families": ["qsv", "software"]},
        }
        app_node_linking.save_node(worker)
        plan = {
            "preset": "1080",
            "preset_bundle": None,
            "extra_args": "--encoder qsv_h265_10bit",
            "encode_metadata": {"encoder": "qsv_h265_10bit"},
        }
        try:
            with (
                patch("webui.app.routes._node_queue_plan", return_value=plan),
                patch("webui.app.routes._prepare_plan_for_node", return_value=plan),
                patch(
                    "webui.app.routes.signed_json_request",
                    return_value={
                        "ok": True,
                        "job": {
                            "id": worker_job_id,
                            "status": "queued",
                            "src": src,
                            "preset": "1080",
                            "preset_selection": "smart",
                        },
                    },
                ) as signed_request,
            ):
                edited = self.client.post(
                    f"/api/mobile/v1/jobs/worker:{node_id}:{worker_job_id}/preset",
                    json={"preset": "smart"},
                    headers=headers,
                )
            self.assertEqual(edited.status_code, 200, edited.get_data(as_text=True))
            self.assertEqual(
                signed_request.call_args.args[1],
                f"/api/node/jobs/{worker_job_id}/preset",
            )
            self.assertEqual(signed_request.call_args.kwargs["body"]["plan"], plan)

            with (
                patch("webui.app.routes.clear_finished_jobs_core", return_value=2),
                patch(
                    "webui.app.routes._clear_linked_worker_jobs",
                    return_value={"removed": 3, "workers": [{"node_id": node_id, "ok": True}]},
                ),
            ):
                cleared = self.client.post(
                    "/api/mobile/v1/jobs/clear",
                    json={"target": "finished"},
                    headers=headers,
                )
            self.assertEqual(cleared.status_code, 200, cleared.get_data(as_text=True))
            self.assertEqual(cleared.get_json()["removed"], 5)
            self.assertEqual(cleared.get_json()["worker_removed"], 3)
        finally:
            app_node_linking.delete_node(node_id)

    def test_bytesqueeze_dashboard_library_and_control_surface(self):
        pairing_response = self.client.post("/api/mobile/pairing_code", json={"scope": "control"})
        self.assertEqual(pairing_response.status_code, 200)
        code = pairing_response.get_json()["pairing"]["code"]
        paired = self.client.post(
            "/api/mobile/v1/pair",
            json={"code": code, "device_id": "bytesqueeze-phone", "device_name": "ByteSqueeze", "platform": "android"},
        )
        self.assertEqual(paired.status_code, 200)
        headers = {"Authorization": f"Bearer {paired.get_json()['access_token']}"}
        library = {
            "movies": [
                {
                    "id": "movie-1",
                    "title": "Example Movie",
                    "poster_url": "https://image.tmdb.org/t/p/w342/example.jpg",
                    "paths": [],
                }
            ],
            "shows": [{"id": "show-1", "title": "Example Show", "poster_url": ""}],
            "scanned_at": 123.0,
        }

        with (
            patch("webui.app.routes._beta_load_library_cache", return_value=library),
            patch(
                "webui.app.routes._beta_load_library_summary",
                return_value={"movies": 1, "shows": 1, "episodes": 0, "updated_at": 123.0, "configured": True},
            ),
        ):
            dashboard = self.client.get("/api/mobile/v1/dashboard", headers=headers)
            mobile_library = self.client.get("/api/mobile/v1/library", headers=headers)

        self.assertEqual(dashboard.status_code, 200, dashboard.get_data(as_text=True))
        dashboard_payload = dashboard.get_json()
        self.assertEqual(dashboard_payload["release"], "3.25.1")
        self.assertEqual(dashboard_payload["library"]["movies"], 1)
        self.assertIn("automation", dashboard_payload)
        self.assertIn("storage", dashboard_payload)

        self.assertEqual(mobile_library.status_code, 200, mobile_library.get_data(as_text=True))
        library_payload = mobile_library.get_json()["library"]
        self.assertEqual(library_payload["movies"][0]["title"], "Example Movie")
        self.assertTrue(library_payload["movies"][0]["poster_url"].endswith("example.jpg"))

        automation = self.client.get("/api/mobile/v1/automation", headers=headers)
        self.assertEqual(automation.status_code, 200, automation.get_data(as_text=True))
        self.assertIn("autopilot_enabled", automation.get_json()["settings"])

        automation_update = self.client.post(
            "/api/mobile/v1/automation",
            json={"action": "save", "autopilot_enabled": True, "autopilot_mode": "observe"},
            headers=headers,
        )
        self.assertEqual(automation_update.status_code, 200, automation_update.get_data(as_text=True))
        self.assertTrue(automation_update.get_json()["settings"]["autopilot_enabled"])

        onboarding = self.client.post(
            "/api/mobile/v1/autopilot/onboarding",
            json={"completed": True},
            headers=headers,
        )
        self.assertEqual(onboarding.status_code, 200, onboarding.get_data(as_text=True))
        self.assertTrue(onboarding.get_json()["onboarding"]["tour_completed"])

        storage = self.client.get("/api/mobile/v1/storage", headers=headers)
        self.assertEqual(storage.status_code, 200, storage.get_data(as_text=True))
        self.assertIn("summary", storage.get_json())

        smart = self.client.get("/api/mobile/v1/smart_presets", headers=headers)
        self.assertEqual(smart.status_code, 200, smart.get_data(as_text=True))
        self.assertIn("profile", smart.get_json())

        operations = self.client.get("/api/mobile/v1/operations", headers=headers)
        self.assertEqual(operations.status_code, 200, operations.get_data(as_text=True))
        self.assertEqual(operations.get_json()["capabilities"]["software_concurrency"], 1)
        original_operations = operations.get_json()["settings"]
        operations_update = self.client.post(
            "/api/mobile/v1/operations",
            json={
                "hardware_transcode_concurrency": 4,
                "auto_stop_large_output_enabled": True,
                "auto_stop_large_output_percent": 88,
            },
            headers=headers,
        )
        self.assertEqual(operations_update.status_code, 200, operations_update.get_data(as_text=True))
        self.assertEqual(operations_update.get_json()["settings"]["hardware_transcode_concurrency"], 4)
        self.client.post(
            "/api/mobile/v1/operations",
            json={
                "hardware_transcode_concurrency": original_operations["hardware_transcode_concurrency"],
                "auto_stop_large_output_enabled": original_operations["auto_stop_large_output_enabled"],
                "auto_stop_large_output_percent": original_operations["auto_stop_large_output_percent"],
            },
            headers=headers,
        )

        node_target_path = os.path.join(TEST_MEDIA, "Node.Target.Movie.1080p.mkv")
        with open(node_target_path, "wb") as handle:
            handle.write(b"node-target")
        node_target = self.client.post(
            "/api/mobile/v1/library/queue",
            json={"paths": [node_target_path], "preset": "auto", "mode": "best"},
            headers=headers,
        )
        self.assertEqual(node_target.status_code, 400, node_target.get_data(as_text=True))
        self.assertIn("no worker node available", node_target.get_data(as_text=True))

    def test_keyless_calendar_and_local_sidecar_artwork(self):
        from webui.app import media_metadata

        future_date = (datetime.now() + timedelta(days=30)).date().isoformat()

        show_dir = os.path.join(TEST_MEDIA, "Reference Show")
        os.makedirs(show_dir, exist_ok=True)
        episode_path = os.path.join(show_dir, "Reference.Show.S01E01.mkv")
        poster_path = os.path.join(show_dir, "poster.jpg")
        with open(episode_path, "wb") as handle:
            handle.write(b"episode")
        with open(poster_path, "wb") as handle:
            handle.write(b"poster-bytes")

        with patch("webui.app.media_metadata._show_remote", return_value={
            "metadata_source": "tvmaze",
            "tvmaze_id": 42,
            "release_calendar": [{
                "show_title": "Reference Show",
                "season": 1,
                "episode": 2,
                "name": "Next",
                "airdate": future_date,
            }],
        }):
            metadata = media_metadata.lookup(
                "show",
                "Reference Show",
                2098,
                [episode_path],
                refresh_hours=1,
            )

        self.assertEqual(metadata["metadata_source"], "local")
        self.assertTrue(metadata["poster_url"].startswith("/api/media/artwork/local-"))
        cached_name = metadata["poster_url"].rsplit("/", 1)[-1]
        self.assertTrue(os.path.isfile(media_metadata.artwork_path(cached_name)))

        pairing_response = self.client.post("/api/mobile/pairing_code", json={"scope": "read"})
        code = pairing_response.get_json()["pairing"]["code"]
        paired = self.client.post(
            "/api/mobile/v1/pair",
            json={"code": code, "device_id": "calendar-phone", "device_name": "Calendar phone", "platform": "android"},
        )
        headers = {"Authorization": f"Bearer {paired.get_json()['access_token']}"}
        library = {
            "release_calendar": {
                "generated_at": 1,
                "provider": {"name": "TVmaze"},
                "episodes": [{
                    "library_show_id": "show-1",
                    "show_title": "Reference Show",
                    "airdate": future_date,
                    "tracked": True,
                    "monitor_releases": True,
                    "poster_url": metadata["poster_url"],
                }],
            },
            "movies": [],
            "shows": [],
        }
        with patch("webui.app.routes._beta_load_library_cache", return_value=library):
            response = self.client.get("/api/mobile/v1/calendar?days=730", headers=headers)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertIn("TVmaze", response.get_data(as_text=True))
        self.assertEqual(response.get_json()["calendar"]["count"], 1)
        self.assertTrue(response.get_json()["calendar"]["episodes"][0]["poster_url"].startswith("http://localhost/api/media/artwork/"))

    def test_library_fine_tuning_is_transient_and_reaches_smart_candidates(self):
        media_path = os.path.join(TEST_MEDIA, "Fine.Tuned.Movie.2160p.mkv")
        with open(media_path, "wb") as handle:
            handle.write(b"0" * 4096)

        self.client.post(
            "/api/smart_presets/profile",
            json={
                "never_downscale": False,
                "keep_black_bars": False,
                "keep_aspect_ratio": False,
                "never_transcode_audio": False,
                "keep_all_audio_languages": False,
                "keep_all_subtitle_languages": False,
            },
        )

        probe = {
            "duration_sec": 7200.0,
            "width": 3840,
            "height": 2160,
            "fps": 23.976,
            "is_hdr": True,
        }
        tuning = {
            "goal": "small",
            "compatibility": "modern",
            "hardware": "software",
            "resolution_mode": "1080",
            "audio_strategy": "copy",
            "subtitle_mode": "none",
            "target_scale": 0.8,
        }
        with patch("webui.app.routes._probe_media", return_value=probe):
            response = self.client.post(
                "/api/smart_presets/recommend",
                json={"src": media_path, "preset": "auto", "smart_tuning": tuning},
            )

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        payload = response.get_json()
        self.assertEqual(payload["profile"]["goal"], "small")
        self.assertEqual(payload["tuning"], tuning)
        self.assertEqual(len(payload["candidates"]), 3)
        for candidate in payload["candidates"]:
            self.assertEqual(candidate["options"]["resolution_mode"], "1080")
            self.assertEqual(candidate["options"]["audio_mode"], "copy")
            self.assertEqual(candidate["options"]["subtitle_mode"], "none")

    def test_library_preview_starts_real_smart_plan_and_exposes_status(self):
        media_path = os.path.join(TEST_MEDIA, "Preview.Movie.2160p.mkv")
        with open(media_path, "wb") as handle:
            handle.write(b"preview-source")
        tuning = {
            "goal": "quality",
            "resolution_mode": "1080",
            "target_scale": 1.1,
        }
        recommendation = {
            "selected_plan": {
                "options": {
                    "resolution_mode": "1080",
                    "audio_mode": "copy",
                    "subtitle_mode": "all",
                },
                "inputs": {"target_mb": 4096},
            },
            "recommended_id": "quality",
            "candidates": [
                {
                    "id": "quality",
                    "name": "Quality guard",
                    "summary": "Protects detail",
                }
            ],
            "tuning": tuning,
        }

        with (
            patch("webui.app.routes._smart_recommendation", return_value=recommendation),
            patch("webui.app.routes.threading.Thread") as thread_class,
        ):
            started = self.client.post(
                "/api/library/preview",
                json={"src": media_path, "smart_tuning": tuning},
            )

        self.assertEqual(started.status_code, 202, started.get_data(as_text=True))
        preview = started.get_json()["preview"]
        self.assertTrue(preview["preview_id"].startswith("library_"))
        self.assertEqual(preview["candidate_name"], "Quality guard")
        self.assertEqual(preview["tuning"], tuning)
        thread_class.assert_called_once()
        thread_class.return_value.start.assert_called_once()
        preview_payload = thread_class.call_args.kwargs["args"][1]
        self.assertEqual(preview_payload["src"], media_path)
        self.assertEqual(preview_payload["resolution_mode"], "1080")
        self.assertEqual(preview_payload["target_size_value"], 4096)

        status = self.client.get(f"/api/library/preview/{preview['preview_id']}")
        self.assertEqual(status.status_code, 200, status.get_data(as_text=True))
        self.assertEqual(status.get_json()["preview"]["state"], "queued")

        missing = self.client.post(
            "/api/library/preview",
            json={"src": os.path.join(TEST_MEDIA, "missing.mkv")},
        )
        self.assertEqual(missing.status_code, 400)

    def test_smart_presets_generate_candidates_and_unlock_after_feedback(self):
        try:
            os.remove(SMART_PRESETS_FILE)
        except FileNotFoundError:
            pass

        media_path = os.path.join(TEST_MEDIA, "Example.Movie.1080p.mkv")
        with open(media_path, "wb") as handle:
            handle.write(b"0" * 4096)

        profile = self.client.post(
            "/api/smart_presets/profile",
            json={
                "goal": "balanced",
                "compatibility": "modern",
                "hardware": "software",
                "audio_strategy": "eac3_surround",
                "never_downscale": False,
                "keep_black_bars": False,
                "keep_aspect_ratio": False,
                "keep_all_audio_languages": False,
                "keep_all_subtitle_languages": False,
                "never_transcode_audio": False,
                "automation_enabled": True,
                "minimum_feedback": 3,
                "confidence_threshold": 0.55,
            },
        )
        self.assertEqual(profile.status_code, 200)
        saved_profile = profile.get_json()["profile"]
        self.assertEqual(saved_profile["audio_strategy"], "eac3_surround")
        self.assertEqual(saved_profile["audio_languages"], ["eng", "spa"])
        self.assertEqual(saved_profile["subtitle_languages"], ["eng", "spa"])
        self.assertFalse(saved_profile["never_downscale"])
        self.assertFalse(saved_profile["never_transcode_audio"])

        partial_profile = self.client.post(
            "/api/smart_presets/profile",
            json={"goal": "balanced"},
        )
        self.assertEqual(partial_profile.status_code, 200)
        self.assertEqual(partial_profile.get_json()["profile"]["audio_strategy"], "eac3_surround")

        probe = {
            "duration_sec": 7200.0,
            "width": 1920,
            "height": 1080,
            "fps": 23.976,
            "is_hdr": False,
        }
        with patch("webui.app.routes._probe_media", return_value=probe):
            response = self.client.post(
                "/api/smart_presets/recommend",
                json={"src": media_path, "preset": "auto", "target_size_auto": True},
            )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        payload = response.get_json()
        self.assertEqual(len(payload["candidates"]), 3)
        self.assertIn(payload["recommended_id"], {row["id"] for row in payload["candidates"]})
        self.assertFalse(payload["learning"]["automation_ready"])
        for candidate in payload["candidates"]:
            options = candidate["options"]
            self.assertEqual(options["smart_audio_strategy"], "eac3_surround")
            self.assertEqual(options["audio_mode"], "eac3")
            self.assertEqual(options["audio_bitrate"], "1024")
            self.assertEqual(options["audio_tracks"], "all")
            self.assertEqual(options["audio_languages"], ["eng", "spa"])
            self.assertEqual(options["subtitle_mode"], "all")
            self.assertEqual(options["subtitle_languages"], ["eng", "spa"])

        with patch("webui.app.routes._probe_media", return_value=probe):
            preview = self.client.post(
                "/wizard_preview",
                json={
                    "src": media_path,
                    "ai_mode": True,
                    "smart_audio_strategy": "eac3_surround",
                    "audio_languages": ["fra"],
                    "subtitle_languages": ["fra"],
                    "ai_audio_scope": "all",
                    "ai_subtitle_scope": "all",
                },
            )
        self.assertEqual(preview.status_code, 200, preview.get_data(as_text=True))
        preview_payload = preview.get_json()
        args = preview_payload["suggested_extra_args"]
        self.assertIn("--all-audio", args)
        self.assertIn("--all-subtitles", args)
        self.assertEqual(args[args.index("--audio-lang-list") + 1], "fre")
        self.assertEqual(args[args.index("--subtitle-lang-list") + 1], "fre")
        self.assertEqual(args[args.index("-E") + 1], "eac3")
        self.assertEqual(args[args.index("-B") + 1], "1024")
        self.assertEqual(args[args.index("-6") + 1], "5point1")

        with patch("webui.app.routes._probe_media", return_value=probe):
            copy_preview = self.client.post(
                "/wizard_preview",
                json={
                    "src": media_path,
                    "ai_mode": True,
                    "smart_audio_strategy": "copy",
                    "ai_copy_audio": False,
                    "audio_languages": ["fra"],
                    "subtitle_languages": ["fra"],
                    "ai_audio_scope": "first",
                    "ai_subtitle_scope": "none",
                },
            )
        self.assertEqual(copy_preview.status_code, 200, copy_preview.get_data(as_text=True))
        copy_payload = copy_preview.get_json()
        copy_args = copy_payload["suggested_extra_args"]
        self.assertEqual(copy_args[copy_args.index("-E") + 1], "copy")
        self.assertEqual(copy_args[copy_args.index("--audio-lang-list") + 1], "fre")
        self.assertEqual(copy_args[copy_args.index("--subtitle-lang-list") + 1], "fre")
        self.assertIn("--all-audio", copy_args)
        self.assertIn("--all-subtitles", copy_args)

        sample_plan = {
            "preset": "1080",
            "probe": {
                **probe,
                "source_type": "movie",
                "source_size_bytes": 8 * 1024**3,
            },
            "options": {
                "ai_goal": "balanced",
                "video_codec": "h265",
                "encoder_family": "software",
                "quality": "balanced",
                "encoder_speed": "medium",
            },
            "inputs": {"target_mb": 5120},
            "estimates": {
                "output_resolution": {"width": 1920, "height": 1080},
                "video_bitrate_kbps": 5600,
                "encoder": "x265_10bit",
                "quality_code": "good",
            },
        }
        context = feedback_context(sample_plan, "balanced")
        self.assertIn("duration", context["source"])
        self.assertFalse(context["features"]["target_size_auto"])
        for _index in range(3):
            record_feedback(context, "approve", "looks_good")

        state = self.client.get("/api/smart_presets")
        self.assertEqual(state.status_code, 200)
        self.assertTrue(state.get_json()["learning"]["automation_ready"])

    def test_smart_preset_preservation_guardrails_override_season_tuning(self):
        try:
            os.remove(SMART_PRESETS_FILE)
        except FileNotFoundError:
            pass

        media_path = os.path.join(TEST_MEDIA, "Example.Show.S01E01.2160p.mkv")
        with open(media_path, "wb") as handle:
            handle.write(b"0" * 4096)

        profile_response = self.client.post(
            "/api/smart_presets/profile",
            json={
                "goal": "small",
                "audio_strategy": "eac3_surround",
                "audio_languages": ["eng", "jpn"],
                "subtitle_languages": ["eng", "jpn"],
            },
        )
        self.assertEqual(profile_response.status_code, 200)
        profile = profile_response.get_json()["profile"]
        self.assertTrue(profile["never_downscale"])
        self.assertTrue(profile["keep_black_bars"])
        self.assertTrue(profile["keep_aspect_ratio"])
        self.assertTrue(profile["keep_all_audio_languages"])
        self.assertTrue(profile["keep_all_subtitle_languages"])
        self.assertTrue(profile["never_transcode_audio"])

        probe = {
            "duration_sec": 2700.0,
            "width": 3840,
            "height": 2160,
            "fps": 23.976,
            "is_hdr": False,
        }
        with patch("webui.app.routes._probe_media", return_value=probe):
            recommendation = app_routes._smart_recommendation(
                {
                    "src": media_path,
                    "preset": "auto",
                    "smart_tuning": {
                        "resolution_mode": "720",
                        "audio_strategy": "eac3_surround",
                        "subtitle_mode": "none",
                    },
                }
            )

        plan = recommendation["selected_plan"]
        options = plan["options"]
        args = plan["extra_args"]
        self.assertEqual(options["resolution_mode"], "keep")
        self.assertEqual(plan["estimates"]["output_resolution"], {"width": 3840, "height": 2160})
        self.assertEqual(options["crop_mode"], "none")
        self.assertEqual(options["audio_mode"], "copy")
        self.assertEqual(options["audio_languages"], [])
        self.assertEqual(options["subtitle_languages"], [])
        self.assertEqual(options["subtitle_mode"], "all")
        self.assertEqual(args[args.index("--width") + 1], "3840")
        self.assertEqual(args[args.index("--height") + 1], "2160")
        self.assertIn("--keep-display-aspect", args)
        self.assertEqual(args[args.index("--crop") + 1], "0:0:0:0")
        self.assertIn("--all-audio", args)
        self.assertIn("--all-subtitles", args)
        self.assertNotIn("--audio-lang-list", args)
        self.assertNotIn("--subtitle-lang-list", args)
        self.assertEqual(args[args.index("-E") + 1], "copy")
        self.assertEqual(args[args.index("--audio-fallback") + 1], "none")
        self.assertIn("--cfr", args)
        self.assertEqual(args[args.index("--rate") + 1], "23.976")
        self.assertEqual(plan["episode_plan"]["target"]["framerate_mode"], "cfr")
        self.assertEqual(plan["episode_plan"]["target"]["fps"], "23.976")

    def test_smart_runtime_replaces_conflicting_frame_rate_options(self):
        locked = app_jobs._smart_source_framerate_args(
            "--encoder qsv_h265_10bit --vfr --rate 60 --pfr",
            24000 / 1001,
        )
        args = app_jobs._split_extra_args(locked)
        self.assertEqual(args.count("--cfr"), 1)
        self.assertNotIn("--vfr", args)
        self.assertNotIn("--pfr", args)
        self.assertEqual(args.count("--rate"), 1)
        self.assertEqual(args[args.index("--rate") + 1], "23.976")
        self.assertEqual(args[args.index("--encoder") + 1], "qsv_h265_10bit")


if __name__ == "__main__":
    unittest.main()
