import os
import json
import tempfile
import threading
import unittest
from unittest import mock

from webui.app import jobs, node_linking, settings
from worker.app import _normalized_worker_job_intent, create_worker_app


class HeadlessWorkerServiceTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.work_dir = os.path.join(self.tempdir.name, "work", "jobs")
        self.node_file = os.path.join(self.tempdir.name, "work", "state", "linked_nodes.json")
        self.settings_file = os.path.join(self.tempdir.name, "work", "state", "settings.json")
        self.original_node_file = node_linking.NODE_LINK_FILE
        self.original_settings_file = settings.SETTINGS_FILE
        self.original_settings_cache = settings._settings_cache
        node_linking.NODE_LINK_FILE = self.node_file
        settings.SETTINGS_FILE = self.settings_file
        settings._settings_cache = None
        self.env = mock.patch.dict(
            os.environ,
            {
                "TSD_WORKER_MODE": "1",
                "TSD_WORKER_NAME": "Test headless worker",
                "TSD_WORKER_TEMP_DIR": self.work_dir,
            },
        )
        self.env.start()
        self.app = create_worker_app(announce_pairing=False)
        self.app.config.update(TESTING=True)
        self.client = self.app.test_client()

    def tearDown(self):
        self.env.stop()
        node_linking.NODE_LINK_FILE = self.original_node_file
        settings.SETTINGS_FILE = self.original_settings_file
        settings._settings_cache = self.original_settings_cache
        self.tempdir.cleanup()

    def test_worker_exposes_only_headless_service_and_node_api(self):
        root = self.client.get("/")
        self.assertEqual(root.status_code, 200)
        self.assertEqual(root.get_json()["service"], "bytesqueeze-headless-worker")
        self.assertEqual(self.client.get("/settings").status_code, 404)

        discovery = self.client.get("/api/node/discovery")
        self.assertEqual(discovery.status_code, 200)
        self.assertEqual(discovery.get_json()["worker_mode"], "headless")
        self.assertTrue(discovery.get_json()["requires_remote_transfer"])
        self.assertIn("hardware", discovery.get_json())

        health = self.client.get("/api/health")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.get_json()["work"]["path"], self.work_dir)

    def test_automatic_dispatch_job_persists_and_keeps_fifo_claims(self):
        original_jobs = jobs.jobs
        original_queue = jobs.job_queue
        original_threads = jobs.RUNNING_JOB_THREADS
        original_queue_paused = jobs.queue_paused
        original_jobs_file = jobs.JOBS_FILE
        jobs.jobs = {}
        jobs.job_queue = []
        jobs.RUNNING_JOB_THREADS = {}
        jobs.JOBS_FILE = os.path.join(self.tempdir.name, "auto-dispatch-jobs.json")
        jobs.queue_paused = True
        try:
            with (
                mock.patch.object(jobs, "ensure_dispatcher"),
                mock.patch.object(jobs, "log_event"),
            ):
                job_id = jobs.create_job(
                    "/media/Automatic.Movie.1080p.mkv",
                    "1080",
                    extra_args="--encoder qsv_h265_10bit",
                    preset_bundle={
                        "key": "1080",
                        "file_name": "base-av1.json",
                        "name": "Plex-AV1-Source-Dimensions-CFR-CRF24",
                        "contents": json.dumps({
                            "PresetList": [{
                                "PresetName": "Plex-AV1-Source-Dimensions-CFR-CRF24",
                                "VideoEncoder": "svt_av1_10bit",
                            }],
                        }),
                    },
                    encode_metadata={
                        "encoder": "qsv_h265_10bit",
                        "encoder_family": "qsv",
                        "video_codec": "h265",
                        "bit_depth": "10",
                        "smart_preset": True,
                        "smart_candidate_id": "balanced",
                        "estimated_output_bytes": 5 * 1024 * 1024 * 1024,
                        "smart_episode_plan": {
                            "target": {"width": 1920, "height": 1080, "target_mb": 5120},
                        },
                    },
                    dispatch_mode="auto",
                    preset_selection="smart",
                    preset_adaptive=True,
                    encoding_policy={
                        "output_container": "mp4",
                        "web_optimized": True,
                    },
                )
                queued = jobs.get_job(job_id)
                self.assertEqual(queued["mode"], "auto_node")
                self.assertEqual(queued["phase"], "waiting_for_node")
                self.assertEqual(queued["dispatch_node_name"], "Next available node")
                self.assertEqual(queued["encoding_policy"]["output_container"], "mp4")
                self.assertTrue(queued["encoding_policy"]["web_optimized"])
                self.assertEqual(
                    queued["dispatch_plan"]["encoding_policy"],
                    queued["encoding_policy"],
                )
                self.assertEqual(
                    queued["queued_preset_name"],
                    "Smart Balanced · H.265 10-bit · Intel QSV · 1080p",
                )
                self.assertEqual(queued["estimated_out_bytes"], 5 * 1024 * 1024 * 1024)
                self.assertEqual(queued["planned_out_bytes"], 5 * 1024 * 1024 * 1024)
                self.assertEqual(queued["estimated_out_source"], "planned")
                self.assertEqual(jobs.get_next_auto_dispatch_job()[0], job_id)

                # Simulate the incorrect label persisted by an older release.
                queued["queued_preset_name"] = "Plex-AV1-Source-Dimensions-CFR-CRF24"
                queued["dispatch_plan"]["queued_preset_name"] = "Plex-AV1-Source-Dimensions-CFR-CRF24"
                jobs.save_jobs()
                jobs.jobs = {}
                jobs.job_queue = []
                jobs.load_jobs()
                queued = jobs.get_job(job_id)
                self.assertEqual(queued["mode"], "auto_node")
                self.assertEqual(queued["dispatch_plan"]["encode_metadata"]["encoder"], "qsv_h265_10bit")
                self.assertEqual(
                    queued["queued_preset_name"],
                    "Smart Balanced · H.265 10-bit · Intel QSV · 1080p",
                )
                self.assertEqual(
                    queued["dispatch_plan"]["queued_preset_name"],
                    queued["queued_preset_name"],
                )
                self.assertEqual(
                    queued["dispatch_plan"]["encoding_policy"]["output_container"],
                    "mp4",
                )
                self.assertEqual(queued["estimated_out_bytes"], 5 * 1024 * 1024 * 1024)
                self.assertEqual(queued["estimated_out_source"], "planned")
                self.assertEqual(jobs.get_next_auto_dispatch_job()[0], job_id)

                claimed = jobs.claim_auto_dispatch_job(job_id, "worker-1", "Worker one")
                self.assertEqual(claimed["status"], "dispatching")
                self.assertEqual(claimed["dispatch_node_id"], "worker-1")
                self.assertTrue(jobs.release_auto_dispatch_job(job_id, "busy", retry_seconds=0.5))
                queued = jobs.get_job(job_id)
                self.assertEqual(queued["status"], "queued")
                self.assertEqual(queued["dispatch_node_name"], "Next available node")

                queued["dispatch_retry_at"] = 0
                self.assertTrue(jobs.activate_auto_dispatch_locally(job_id, "main", "Main controller"))
                local = jobs.get_job(job_id)
                self.assertEqual(local["mode"], "local")
                self.assertEqual(local["dispatch_mode"], "auto")
                self.assertEqual(local["dispatch_node_name"], "Main controller")
        finally:
            jobs.jobs = original_jobs
            jobs.job_queue = original_queue
            jobs.RUNNING_JOB_THREADS = original_threads
            jobs.JOBS_FILE = original_jobs_file
            jobs.queue_paused = original_queue_paused

    def test_live_output_estimate_retries_until_output_exists(self):
        with tempfile.TemporaryDirectory() as tempdir:
            output = os.path.join(tempdir, "encode.tmp.mkv")
            job = {
                "estimated_out_bytes": 4096,
                "planned_out_bytes": 4096,
                "estimated_out_source": "planned",
                "estimate_checkpoints_seen": [],
            }

            self.assertFalse(jobs._maybe_update_output_estimate(job, output, 25.0))
            self.assertEqual(job["estimate_checkpoints_seen"], [])
            self.assertEqual(job["estimated_out_bytes"], 4096)
            self.assertEqual(job["estimated_out_source"], "planned")

            with open(output, "wb") as handle:
                handle.write(b"x" * 1024)
            self.assertTrue(jobs._maybe_update_output_estimate(job, output, 25.0))
            self.assertEqual(job["estimated_out_bytes"], 4096)
            self.assertEqual(job["estimated_out_checked_progress"], 25.0)
            self.assertEqual(job["estimated_out_source"], "live")

    def test_next_available_bypasses_pinned_local_only_for_another_node(self):
        original_jobs = jobs.jobs
        original_queue = jobs.job_queue
        original_threads = jobs.RUNNING_JOB_THREADS
        original_queue_paused = jobs.queue_paused
        jobs.jobs = {
            "pinned-local": {
                "status": "queued",
                "mode": "local",
                "src": "/media/Pinned.Movie.mkv",
                "preset": "1080",
            },
            "automatic": {
                "status": "queued",
                "mode": "auto_node",
                "src": "/media/Automatic.Movie.mkv",
                "preset": "1080",
                "dispatch_retry_at": 0,
                "dispatch_attempts": 0,
            },
        }
        jobs.job_queue = ["pinned-local", "automatic"]
        jobs.RUNNING_JOB_THREADS = {}
        # create_app() owns a real background dispatcher during the full test
        # suite. Pause it while this unit test inspects queue ordering so a
        # fast Linux runner cannot claim the mocked pinned job mid-assertion.
        jobs.queue_paused = True
        try:
            pending = jobs.get_next_auto_dispatch_job()
            self.assertIsNotNone(pending)
            self.assertEqual(pending[0], "automatic")
            self.assertFalse(jobs.auto_dispatch_local_available("automatic"))
            self.assertFalse(
                jobs.activate_auto_dispatch_locally(
                    "automatic",
                    "main",
                    "Main controller",
                )
            )
            with mock.patch.object(jobs, "save_jobs"):
                claimed = jobs.claim_auto_dispatch_job(
                    "automatic",
                    "worker-2",
                    "Worker two",
                )
            self.assertIsNotNone(claimed)
            self.assertEqual(claimed["dispatch_node_id"], "worker-2")
            self.assertEqual(jobs.jobs["pinned-local"]["status"], "queued")
        finally:
            jobs.jobs = original_jobs
            jobs.job_queue = original_queue
            jobs.RUNNING_JOB_THREADS = original_threads
            jobs.queue_paused = original_queue_paused

    def test_pairing_response_marks_worker_as_transfer_only(self):
        pairing = node_linking.create_pairing_code()
        response = self.client.post(
            "/api/node/pair/accept",
            json={
                "code": pairing["code"],
                "controller_id": "controller-test",
                "controller_name": "Main controller",
                "controller_url": "http://controller:8080",
                "protocol_version": 2,
            },
        )

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.get_json()["worker_mode"], "headless")
        self.assertTrue(response.get_json()["requires_remote_transfer"])
        self.assertIn("gpu-multi-encode", response.get_json()["capabilities"])
        self.assertIn(
            "controller-encoding-policy",
            response.get_json()["capabilities"],
        )
        self.assertTrue(response.get_json()["token"])
        self.assertEqual(self.client.get("/api/node/status").status_code, 401)

    def test_worker_receiver_preserves_audio_only_operations(self):
        pairing = node_linking.create_pairing_code()
        paired = self.client.post(
            "/api/node/pair/accept",
            json={
                "code": pairing["code"],
                "controller_id": "controller-audio-only",
                "controller_name": "Main controller",
                "controller_url": "http://controller:8080",
                "protocol_version": 2,
            },
        ).get_json()
        operations = {
            "job_type": "audio_only",
            "audio_policy": "optimize_lossless",
            "preferred_languages": ["eng", "spa"],
            "deduplicate_languages": True,
            "transcode_selected_audio": True,
            "default_target_codec": "aac",
            "default_target_bitrate_kbps": 1024,
        }
        payload = {
            "jobs": [{
                "src": "/media/Movie.mkv",
                "preset": "4k",
                "parent_job_id": "controller-job-id",
                "operations": operations,
                "encode_metadata": {
                    "preset_selection": "audio-only",
                    "encode_method": "svt_av1_10bit",
                },
                "transfer": {
                    "id": "transfer-audio-only",
                    "controller_id": "controller-audio-only",
                    "controller_url": "http://controller:8080",
                    "source_url": "http://controller:8080/source",
                    "upload_url": "http://controller:8080/output",
                    "download_token": "download-token",
                    "upload_token": "upload-token",
                    "worker_node_id": "worker-id",
                    "original_path": "/media/Movie.mkv",
                    "source_basename": "Movie.mkv",
                },
            }],
        }
        body = json.dumps(payload).encode("utf-8")
        headers = node_linking.hmac_headers(
            "POST",
            "/api/node/jobs",
            body,
            node_id="controller-audio-only",
            token=paired["token"],
        )
        with mock.patch(
            "worker.app.create_remote_transfer_job",
            return_value=("worker-audio-job", True),
        ) as create:
            response = self.client.post(
                "/api/node/jobs",
                data=body,
                content_type="application/json",
                headers=headers,
            )

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.get_json()["count"], 1)
        call = create.call_args
        self.assertEqual(call.kwargs["parent_job_id"], "controller-job-id")
        self.assertEqual(call.kwargs["operations"]["job_type"], "audio_only")
        self.assertEqual(call.kwargs["operations"]["video_action"], "copy")
        self.assertEqual(call.kwargs["operations"]["preferred_languages"], ["eng", "spa"])
        self.assertEqual(call.kwargs["encode_metadata"]["encode_method"], "ffmpeg_audio_only")
        self.assertEqual(call.kwargs["encode_metadata"]["encoder_family"], "stream_copy")

    def test_worker_receiver_repairs_legacy_audio_only_marker(self):
        job = {
            "preset_selection": "audio-only",
            "encode_metadata": {"encode_method": "svt_av1_10bit"},
        }
        metadata, operations = _normalized_worker_job_intent(job, {})

        self.assertEqual(operations["job_type"], "audio_only")
        self.assertEqual(operations["audio_policy"], "optimize_lossless")
        self.assertEqual(metadata["encode_method"], "ffmpeg_audio_only")
        self.assertEqual(metadata["encoder"], "copy")

    def test_pairing_prefers_controller_route_observed_by_worker(self):
        pairing = node_linking.create_pairing_code()
        response = self.client.post(
            "/api/node/pair/accept",
            json={
                "code": pairing["code"],
                "controller_id": "controller-route",
                "controller_name": "Main controller",
                "controller_url": "http://100.111.94.118:8081",
                "protocol_version": 2,
            },
            environ_base={"REMOTE_ADDR": "192.168.12.108"},
        )

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.get_json()["controller_url"], "http://192.168.12.108:8081")
        controller = node_linking.trusted_controller("controller-route")
        self.assertEqual(controller["advertised_url"], "http://100.111.94.118:8081")
        self.assertEqual(controller["observed_url"], "http://192.168.12.108:8081")

        transfer = jobs._apply_observed_controller_route(
            {
                "controller_id": "controller-route",
                "controller_url": "http://100.111.94.118:8081",
                "source_url": "http://100.111.94.118:8081/api/node/transfers/abc/source",
                "upload_url": "http://100.111.94.118:8081/api/node/transfers/abc/output",
            }
        )
        self.assertEqual(
            transfer["source_url"],
            "http://192.168.12.108:8081/api/node/transfers/abc/source",
        )
        self.assertEqual(
            transfer["upload_url"],
            "http://192.168.12.108:8081/api/node/transfers/abc/output",
        )

    def test_worker_job_api_includes_persisted_error_log_diagnostics(self):
        original_jobs = jobs.jobs
        original_log_dir = jobs.LOG_DIR
        jobs.jobs = {
            "job-error": {
                "status": "error",
                "phase": "download_error",
                "src": "/media/example.mkv",
                "preset": "1080",
                "mode": "remote_transfer",
                "transfer": {"last_error": "controller timed out"},
                "error_message": "Remote source download failed: controller timed out",
                "log": "Downloading source...\nERROR: controller timed out\n",
            }
        }
        jobs.LOG_DIR = os.path.join(self.tempdir.name, "logs")
        try:
            jobs._append_job_log("job-error", "Retry attempts exhausted")
            item = jobs.list_jobs_for_api(include_log_tail=True)[0]
            self.assertTrue(item["has_log"])
            self.assertEqual(item["phase"], "download_error")
            self.assertIn("controller timed out", item["error_message"])
            self.assertIn("Retry attempts exhausted", item["log_tail"])
            with open(jobs._job_log_path("job-error"), "ab") as handle:
                handle.write(b"invalid-byte:\xff\n")
            contents, truncated = jobs.read_job_log("job-error")
            self.assertFalse(truncated)
            self.assertIn("Retry attempts exhausted", contents)
            self.assertIn("invalid-byte:\ufffd", contents)
        finally:
            jobs.jobs = original_jobs
            jobs.LOG_DIR = original_log_dir

    def test_remote_source_download_retries_transient_timeout(self):
        class FakeResponse:
            headers = {"Content-Length": "4"}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _size=-1):
                if getattr(self, "used", False):
                    return b""
                self.used = True
                return b"test"

        destination = os.path.join(self.tempdir.name, "download", "source.mkv")
        attempts = []
        with mock.patch.dict(
            os.environ,
            {
                "TSD_WORKER_TRANSFER_ATTEMPTS": "2",
                "TSD_WORKER_TRANSFER_TIMEOUT_SECONDS": "30",
            },
        ), mock.patch.object(
            jobs,
            "urlopen",
            side_effect=[jobs.URLError("timed out"), FakeResponse()],
        ), mock.patch.object(jobs.time, "sleep"):
            size = jobs._download_transfer_source(
                "http://controller:8081/source",
                "download-token",
                "worker-id",
                destination,
                expected_size=4,
                attempt_callback=attempts.append,
            )

        self.assertEqual(size, 4)
        with open(destination, "rb") as handle:
            self.assertEqual(handle.read(), b"test")
        self.assertTrue(any("attempt 2/2" in line.lower() for line in attempts))

    def test_upload_renewal_reports_retained_output_identity(self):
        output = os.path.join(self.tempdir.name, "retained-output.mkv")
        with open(output, "wb") as stream:
            stream.write(b"completed-output")
        transfer = {
            "id": "transfer-id",
            "controller_id": "controller-id",
            "controller_url": "http://controller:8081",
        }
        with (
            mock.patch.object(node_linking, "trusted_controller", return_value={"id": "controller-id"}),
            mock.patch.object(
                node_linking,
                "signed_json_request",
                return_value={"ok": True, "complete": True, "out_path": "/media/output.mkv"},
            ) as request_json,
        ):
            result = jobs._renew_transfer_upload_grant(
                "worker-job",
                transfer,
                out_path=output,
                output_container="mkv",
            )

        self.assertTrue(result["complete"])
        body = request_json.call_args.kwargs["body"]
        self.assertEqual(body["out_bytes"], len(b"completed-output"))
        self.assertEqual(body["output_container"], "mkv")
        self.assertRegex(body["output_fingerprint"], r"^[0-9a-f]{64}$")
        self.assertEqual(request_json.call_args.kwargs["timeout"], 1800)

    def test_remote_source_download_cancellation_removes_partial_file(self):
        cancel_event = threading.Event()

        class FakeResponse:
            headers = {"Content-Length": "8"}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _size=-1):
                if not getattr(self, "used", False):
                    self.used = True
                    cancel_event.set()
                    return b"partial"
                return b""

        destination = os.path.join(self.tempdir.name, "download-cancel", "source.mkv")
        with mock.patch.object(jobs, "urlopen", return_value=FakeResponse()):
            with self.assertRaises(jobs.TransferDownloadCanceled):
                jobs._download_transfer_source(
                    "http://controller:8081/source",
                    "download-token",
                    "worker-id",
                    destination,
                    expected_size=8,
                    cancel_event=cancel_event,
                )

        self.assertFalse(os.path.exists(destination))
        self.assertFalse(os.path.exists(destination + ".part"))

    def test_cancel_running_transfer_closes_response_and_marks_transfer_canceled(self):
        class FakeResponse:
            closed = False

            def close(self):
                self.closed = True

        original_jobs = jobs.jobs
        response = FakeResponse()
        cancel_event = jobs._begin_transfer_download("transfer-job")
        jobs._set_transfer_download_response("transfer-job", response)
        jobs.jobs = {
            "transfer-job": {
                "status": "running",
                "phase": "downloading",
                "src": "/media/show.mkv",
                "pid": None,
                "started_at": 1.0,
                "transfer": {
                    "status": "downloading",
                    "progress": {"phase": "downloading", "speed_bps": 1000},
                },
            }
        }
        try:
            with (
                mock.patch.object(jobs, "save_jobs"),
                mock.patch.object(jobs, "_append_job_log"),
                mock.patch.object(jobs, "log_event"),
            ):
                ok, error = jobs.cancel_job("transfer-job")

            self.assertTrue(ok)
            self.assertIsNone(error)
            self.assertTrue(cancel_event.is_set())
            self.assertTrue(response.closed)
            self.assertEqual(jobs.jobs["transfer-job"]["status"], "canceled")
            self.assertEqual(jobs.jobs["transfer-job"]["transfer"]["status"], "canceled")
            self.assertTrue(jobs.jobs["transfer-job"]["cancel_transfer_interrupted"])
        finally:
            jobs._finish_transfer_download("transfer-job")
            jobs.jobs = original_jobs

    def test_controller_encoding_policy_keeps_large_output_auto_stop(self):
        job = {
            "mode": "remote_transfer",
            "encoding_policy": {
                "hb_threads": 6,
                "hardware_transcode_concurrency": 3,
                "auto_stop_large_output_enabled": True,
                "auto_stop_large_output_percent": 85,
            },
            "estimated_out_checked_progress": 25,
            "estimated_out_bytes": 900,
            "src_bytes": 1000,
        }

        with mock.patch.object(
            jobs,
            "load_settings",
            return_value={"auto_stop_large_output_enabled": False},
        ):
            stop = jobs._estimated_output_stop_guard(job)

        self.assertIsNotNone(stop)
        self.assertEqual(stop["threshold_percent"], 85.0)
        self.assertEqual(jobs._job_encoding_policy(job)["hb_threads"], 6)
        self.assertEqual(jobs._hardware_transcode_limit(job=job), 3)

    def test_gpu_jobs_can_share_slots_but_software_jobs_remain_exclusive(self):
        qsv = {"encoder": "qsv_h265_10bit", "encoder_family": "qsv"}
        nvenc = {"encoder": "nvenc_h265", "encoder_family": "nvenc"}
        software = {"encoder": "x265_10bit", "encoder_family": "software"}

        self.assertTrue(jobs._job_uses_hardware_encoder(qsv))
        self.assertTrue(jobs._job_uses_hardware_encoder(nvenc))
        self.assertFalse(jobs._job_uses_hardware_encoder(software))
        self.assertTrue(jobs._can_dispatch_job(nvenc, [qsv], 2))
        self.assertFalse(jobs._can_dispatch_job(nvenc, [qsv, nvenc], 2))
        self.assertFalse(jobs._can_dispatch_job(software, [qsv], 8))
        self.assertFalse(jobs._can_dispatch_job(qsv, [software], 8))
        self.assertTrue(jobs._can_dispatch_job(software, [], 8))

    def test_main_controller_can_change_worker_gpu_capacity(self):
        pairing = node_linking.create_pairing_code()
        paired = self.client.post(
            "/api/node/pair/accept",
            json={
                "code": pairing["code"],
                "controller_id": "controller-capacity",
                "controller_name": "Main node",
                "controller_url": "http://controller:8080",
                "protocol_version": 2,
            },
        ).get_json()
        policy = {
            "hardware_transcode_concurrency": 3,
            "output_container": "mp4",
            "web_optimized": True,
        }
        body = json.dumps(policy).encode("utf-8")
        headers = node_linking.hmac_headers(
            "POST",
            "/api/node/config",
            body,
            node_id="controller-capacity",
            token=paired["token"],
        )
        response = self.client.post(
            "/api/node/config",
            data=body,
            content_type="application/json",
            headers=headers,
        )

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        applied = response.get_json()["encoding_policy"]
        self.assertEqual(applied["hardware_transcode_concurrency"], 3)
        self.assertEqual(applied["output_container"], "mp4")
        self.assertTrue(applied["web_optimized"])
        self.assertTrue(applied["controller_managed"])
        stale_job = {
            "mode": "remote_transfer",
            "encoding_policy": {"hardware_transcode_concurrency": 1},
        }
        self.assertEqual(jobs._hardware_transcode_limit(job=stale_job), 3)

        health = self.client.get("/api/health").get_json()
        self.assertEqual(health["release"], "2.13.7")
        self.assertEqual(health["encoding_policy"]["hardware_transcode_concurrency"], 3)
        self.assertEqual(health["encoding_policy"]["output_container"], "mp4")
        self.assertTrue(health["encoding_policy"]["web_optimized"])

    def test_hardware_preset_and_concurrency_limit_are_detected_safely(self):
        preset = {
            "PresetList": [
                {
                    "PresetName": "Remote QSV",
                    "VideoEncoder": "qsv_h265_10bit",
                }
            ]
        }
        job = {
            "preset": "4k",
            "encoder_family": "preset",
            "preset_bundle": {
                "name": "Remote QSV",
                "file_name": "remote-qsv.json",
                "contents": json.dumps(preset),
            },
        }

        self.assertTrue(jobs._job_uses_hardware_encoder(job))
        self.assertEqual(jobs._hardware_transcode_limit({"hardware_transcode_concurrency": 0}), 1)
        self.assertEqual(jobs._hardware_transcode_limit({"hardware_transcode_concurrency": 4}), 4)
        self.assertEqual(jobs._hardware_transcode_limit({"hardware_transcode_concurrency": 99}), 8)

    def test_controller_can_clear_finished_worker_history_without_touching_active_jobs(self):
        pairing = node_linking.create_pairing_code()
        paired = self.client.post(
            "/api/node/pair/accept",
            json={
                "code": pairing["code"],
                "controller_id": "controller-clear-history",
                "controller_name": "Main node",
                "controller_url": "http://controller:8080",
                "protocol_version": 2,
            },
        ).get_json()
        body = json.dumps({"target": "finished"}).encode("utf-8")
        headers = node_linking.hmac_headers(
            "POST",
            "/api/node/jobs/clear",
            body,
            node_id="controller-clear-history",
            token=paired["token"],
        )
        remaining = [{"id": "still-running", "status": "running"}]
        with mock.patch("worker.app.clear_finished_jobs", return_value=2) as clear, mock.patch(
            "worker.app.list_jobs_for_api",
            return_value=remaining,
        ), mock.patch(
            "worker.app.get_job_summary",
            return_value={"counts": {"running": 1}},
        ):
            response = self.client.post(
                "/api/node/jobs/clear",
                data=body,
                content_type="application/json",
                headers=headers,
            )

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.get_json()["removed"], 2)
        self.assertEqual(response.get_json()["jobs"], remaining)
        clear.assert_called_once_with()

    def test_controller_can_reorder_and_edit_worker_queue(self):
        pairing = node_linking.create_pairing_code()
        paired = self.client.post(
            "/api/node/pair/accept",
            json={
                "code": pairing["code"],
                "controller_id": "controller-worker-control",
                "controller_name": "Main node",
                "controller_url": "http://controller:8080",
                "protocol_version": 2,
            },
        ).get_json()

        action_body = json.dumps({"action": "top"}).encode("utf-8")
        action_headers = node_linking.hmac_headers(
            "POST",
            "/api/node/jobs/queued-job/action",
            action_body,
            node_id="controller-worker-control",
            token=paired["token"],
        )
        with mock.patch("worker.app.move_queued_job", return_value=(True, None)) as move:
            action = self.client.post(
                "/api/node/jobs/queued-job/action",
                data=action_body,
                content_type="application/json",
                headers=action_headers,
            )
        self.assertEqual(action.status_code, 200, action.get_data(as_text=True))
        move.assert_called_once_with("queued-job", "top")

        plan = {"preset": "1080", "extra_args": "--encoder qsv_h265_10bit"}
        preset_body = json.dumps({"plan": plan}).encode("utf-8")
        preset_headers = node_linking.hmac_headers(
            "POST",
            "/api/node/jobs/queued-job/preset",
            preset_body,
            node_id="controller-worker-control",
            token=paired["token"],
        )
        with mock.patch(
            "worker.app.replace_queued_job_preset", return_value=(True, None)
        ) as replace:
            preset = self.client.post(
                "/api/node/jobs/queued-job/preset",
                data=preset_body,
                content_type="application/json",
                headers=preset_headers,
            )
        self.assertEqual(preset.status_code, 200, preset.get_data(as_text=True))
        replace.assert_called_once_with("queued-job", plan)

        clear_body = json.dumps({"target": "queued"}).encode("utf-8")
        clear_headers = node_linking.hmac_headers(
            "POST",
            "/api/node/jobs/clear",
            clear_body,
            node_id="controller-worker-control",
            token=paired["token"],
        )
        with mock.patch("worker.app.clear_queued_jobs", return_value=4) as clear:
            cleared = self.client.post(
                "/api/node/jobs/clear",
                data=clear_body,
                content_type="application/json",
                headers=clear_headers,
            )
        self.assertEqual(cleared.status_code, 200, cleared.get_data(as_text=True))
        self.assertEqual(cleared.get_json()["removed"], 4)
        clear.assert_called_once_with()

        pause_body = json.dumps({"paused": True}).encode("utf-8")
        pause_headers = node_linking.hmac_headers(
            "POST",
            "/api/node/queue",
            pause_body,
            node_id="controller-worker-control",
            token=paired["token"],
        )
        original_paused = jobs.get_queue_state()
        try:
            paused = self.client.post(
                "/api/node/queue",
                data=pause_body,
                content_type="application/json",
                headers=pause_headers,
            )
            self.assertEqual(paused.status_code, 200, paused.get_data(as_text=True))
            self.assertTrue(paused.get_json()["paused"])
            self.assertTrue(jobs.get_queue_state())
        finally:
            jobs.set_queue_paused(original_paused)


if __name__ == "__main__":
    unittest.main()
