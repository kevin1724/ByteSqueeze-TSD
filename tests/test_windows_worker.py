import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from worker.windows_app import configure_environment, default_config, local_addresses, windows_firewall_script
from webui.app import encode_runner
from webui.app import node_linking
from webui.app import jobs
from webui.app import process_utils
from webui.app.process_utils import background_process_options


class WindowsEncodeRunnerTests(unittest.TestCase):
    def test_linux_qsv_preflight_uses_the_image_helper(self):
        with mock.patch.object(encode_runner.os, "name", "posix"), mock.patch.object(
            encode_runner,
            "_qsv_job",
            return_value=True,
        ), mock.patch.object(
            encode_runner.shutil,
            "which",
            return_value="/usr/local/bin/bytesqueeze-qsv-preflight",
        ), mock.patch.object(encode_runner, "_run", return_value=0) as run:
            ok, reason = encode_runner._qsv_preflight({})

        self.assertTrue(ok)
        self.assertEqual(reason, "passed")
        run.assert_called_once_with(
            ["/usr/local/bin/bytesqueeze-qsv-preflight", "encode"]
        )

    def test_command_preserves_dispatcher_contract_and_mp4_fast_start(self):
        with tempfile.TemporaryDirectory() as tempdir:
            source = Path(tempdir) / "Movie Source.mkv"
            source.write_bytes(b"source")
            preset = Path(tempdir) / "preset.json"
            preset.write_text("{}", encoding="utf-8")
            env = {
                "SRC": str(source),
                "SUFFIX": "TSD",
                "HB_PRESET_FILE": str(preset),
                "HB_PRESET_NAME": "Smart NVENC HEVC",
                "HB_VIDEO_ENCODER": "nvenc_h265_10bit",
                "HB_EXTRA_ARGS": (
                    "--encoder nvenc_h265_10bit -b 13467 --rate 23.976 --quality 24 "
                    "--audio-lang-list eng,spa --all-audio -E eac3 -B 640"
                ),
                "HB_AUDIO_POLICY_OPTS": (
                    "--audio 1,2 --aname 'English Main,Spanish Main' "
                    "--aencoder copy,copy --ab auto,auto"
                ),
                "HB_DIMENSION_OPTS": "--maxWidth 1920 --maxHeight 1080",
                "HB_HW_DECODE_OPTS": "--disable-hw-decoding",
                "HB_OUTPUT_CONTAINER": "mp4",
                "HB_WEB_OPTIMIZED": "1",
            }
            canonical_output = source.with_name("controller-selected-output.mp4")
            env["HB_OUTPUT_PATH"] = str(canonical_output)
            with mock.patch.object(encode_runner, "_tool", return_value="HandBrakeCLI.exe"):
                command = encode_runner.build_command(env)

            self.assertEqual(command[0], "HandBrakeCLI.exe")
            self.assertIn("--preset-import-file", command)
            self.assertIn("nvenc_h265_10bit", command)
            self.assertIn("av_mp4", command)
            self.assertIn("--optimize", command)
            self.assertIn("English Main,Spanish Main", command)
            self.assertNotIn("--audio-lang-list", command)
            self.assertNotIn("--all-audio", command)
            self.assertEqual(command.count("--audio"), 1)
            self.assertEqual(command[command.index("--audio") + 1], "1,2")
            self.assertNotIn("640", command)
            self.assertIn("13467", command)
            self.assertIn("23.976", command)
            self.assertEqual(command[-1], str(canonical_output))

    def test_canonical_output_path_rejects_container_extension_drift(self):
        with tempfile.TemporaryDirectory() as tempdir:
            source = Path(tempdir) / "Episode.mkv"
            source.write_bytes(b"source")
            env = {
                "SRC": str(source),
                "HB_OUTPUT_CONTAINER": "mp4",
                "HB_OUTPUT_PATH": str(source.with_name("Episode-TSD.mkv")),
            }
            with self.assertRaisesRegex(ValueError, "does not match"):
                encode_runner.output_path(str(source), env)

    def test_qsv_jobs_select_adapter_and_keep_decode_request(self):
        with tempfile.TemporaryDirectory() as tempdir:
            source = Path(tempdir) / "Episode.mkv"
            source.write_bytes(b"source")
            env = {
                "SRC": str(source),
                "HB_VIDEO_ENCODER": "qsv_h265_10bit",
                "HB_HW_DECODE_OPTS": "--enable-hw-decoding qsv",
                "TSD_QSV_ADAPTER": "2",
                "HB_OUTPUT_CONTAINER": "mkv",
            }
            with mock.patch.object(encode_runner, "_tool", return_value="HandBrakeCLI.exe"):
                command = encode_runner.build_command(env)
            self.assertEqual(command[command.index("--qsv-adapter") + 1], "2")
            self.assertEqual(command[command.index("--enable-hw-decoding") + 1], "qsv")
            self.assertIn("av_mkv", command)

    def test_nvenc_av1_rejects_invalid_imported_profile_before_launch(self):
        with tempfile.TemporaryDirectory() as tempdir:
            source = Path(tempdir) / "Movie.mkv"
            source.write_bytes(b"source")
            preset = Path(tempdir) / "preset.json"
            preset.write_text(
                json.dumps(
                    {
                        "PresetList": [
                            {
                                "PresetName": "Routed AV1",
                                "VideoEncoder": "nvenc_av1_10bit",
                                "VideoProfile": "main",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            env = {
                "SRC": str(source),
                "HB_PRESET_FILE": str(preset),
                "HB_PRESET_NAME": "Routed AV1",
                "HB_VIDEO_ENCODER": "nvenc_av1_10bit",
            }
            with mock.patch.object(encode_runner, "_tool", return_value="HandBrakeCLI.exe"):
                with self.assertRaisesRegex(ValueError, "Auto/default"):
                    encode_runner.build_command(env)

    def test_nvenc_av1_allows_default_profile_and_hevc_keeps_main10(self):
        with tempfile.TemporaryDirectory() as tempdir:
            source = Path(tempdir) / "Movie.mkv"
            source.write_bytes(b"source")
            for encoder, profile in (
                ("nvenc_av1", ""),
                ("nvenc_av1_10bit", "auto"),
                ("nvenc_h265_10bit", "main10"),
            ):
                with self.subTest(encoder=encoder, profile=profile):
                    preset = Path(tempdir) / f"{encoder}.json"
                    definition = {
                        "PresetName": encoder,
                        "VideoEncoder": encoder,
                    }
                    if profile:
                        definition["VideoProfile"] = profile
                    preset.write_text(
                        json.dumps({"PresetList": [definition]}),
                        encoding="utf-8",
                    )
                    env = {
                        "SRC": str(source),
                        "HB_PRESET_FILE": str(preset),
                        "HB_PRESET_NAME": encoder,
                        "HB_VIDEO_ENCODER": encoder,
                    }
                    with mock.patch.object(encode_runner, "_tool", return_value="HandBrakeCLI.exe"):
                        command = encode_runner.build_command(env)
                    self.assertIn(encoder, command)

    def test_worker_error_excerpt_prefers_real_handbrake_cause(self):
        job = {
            "log": (
                "Incompatible options: --audio-lang-list and --audio\n"
                "ERROR: Encode failed, output file was not created.\n"
                "[ByteSqueeze] ERROR: HandBrake exited with code 1\n"
            )
        }
        self.assertEqual(
            jobs._job_error_excerpt(job),
            "Incompatible options: --audio-lang-list and --audio",
        )


class WindowsHardwareInventoryTests(unittest.TestCase):
    def test_worker_url_prefers_private_lan_over_virtual_network(self):
        addresses = [
            (None, None, None, None, ("100.96.1.71", 0)),
            (None, None, None, None, ("192.168.12.246", 0)),
        ]
        with mock.patch("worker.windows_app.socket.getaddrinfo", return_value=addresses):
            urls = local_addresses(8082)
        self.assertEqual(urls[0], "http://192.168.12.246:8082")

    def test_firewall_rule_is_limited_to_worker_port_and_local_networks(self):
        script = windows_firewall_script(8082)
        self.assertIn("-LocalPort 8082", script)
        self.assertIn("-Protocol TCP", script)
        self.assertIn("-Profile Any", script)
        self.assertIn("-RemoteAddress LocalSubnet,100.64.0.0/10", script)

    def test_windows_inventory_detects_multiple_vendor_adapters(self):
        payload = [
            {"Name": "NVIDIA GeForce RTX 5080", "PNPDeviceID": "PCI\\VEN_10DE", "DriverVersion": "1", "AdapterRAM": 1000, "Status": "OK"},
            {"Name": "AMD Radeon Graphics", "PNPDeviceID": "PCI\\VEN_1002", "DriverVersion": "2", "AdapterRAM": 2000, "Status": "OK"},
        ]
        result = mock.Mock(returncode=0, stdout=json.dumps(payload))
        with mock.patch.object(node_linking.subprocess, "run", return_value=result):
            rows = node_linking._windows_gpu_inventory()
        self.assertEqual([row["vendor"] for row in rows], ["nvidia", "amd"])
        self.assertEqual(rows[0]["name"], "NVIDIA GeForce RTX 5080")

    def test_windows_inventory_maps_wmi_order_to_nvidia_nvenc_order_by_pci_bus(self):
        wmi_payload = [
            {
                "Name": "NVIDIA GeForce RTX 5070",
                "PNPDeviceID": "PCI\\VEN_10DE&DEV_2F04",
                "DriverVersion": "wmi",
                "AdapterRAM": 4_294_967_295,
                "Status": "OK",
                "LocationInfo": "PCI bus 9, device 0, function 0",
            },
            {
                "Name": "NVIDIA GeForce RTX 3070",
                "PNPDeviceID": "PCI\\VEN_10DE&DEV_2484",
                "DriverVersion": "wmi",
                "AdapterRAM": 4_294_967_295,
                "Status": "OK",
                "LocationInfo": "PCI bus 1, device 0, function 0",
            },
        ]
        smi_output = (
            "0, GPU-3070, 00000000:01:00.0, NVIDIA GeForce RTX 3070, 8192, 591.86\n"
            "1, GPU-5070, 00000000:09:00.0, NVIDIA GeForce RTX 5070, 12288, 591.86\n"
        )
        responses = [
            mock.Mock(returncode=0, stdout=json.dumps(wmi_payload)),
            mock.Mock(returncode=0, stdout=smi_output),
        ]
        with mock.patch.object(node_linking.subprocess, "run", side_effect=responses):
            rows = node_linking._windows_gpu_inventory()
        self.assertEqual(rows[0]["index"], 0)  # ByteSqueeze/WMI index
        self.assertEqual(rows[0]["vendor_index"], 1)  # NVIDIA/NVENC index
        self.assertEqual(rows[0]["gpu_uuid"], "GPU-5070")
        self.assertEqual(rows[0]["pci_bus_id"], "00000000:09:00.0")
        self.assertEqual(rows[0]["identity_source"], "pci_bus_id")
        self.assertEqual(rows[1]["vendor_index"], 0)
        self.assertEqual(rows[1]["gpu_uuid"], "GPU-3070")
        self.assertEqual(rows[0]["memory_bytes"], 12288 * 1024 * 1024)

    def test_av1_nvenc_probe_targets_mapped_nvidia_ordinal(self):
        gpu = {
            "vendor_index": 1,
            "gpu_uuid": "GPU-5070",
            "pci_bus_id": "00000000:09:00.0",
            "driver_version": "591.86",
        }
        result = mock.Mock(returncode=0, stdout="", stderr="")
        calls = []

        def run_probe(command, **_kwargs):
            calls.append((command, _kwargs))
            if "HandBrakeCLI.exe" in command[0]:
                Path(command[command.index("-o") + 1]).write_bytes(b"verified")
            return result

        def find_tool(name):
            return "HandBrakeCLI.exe" if name.casefold().startswith("handbrakecli") else "ffmpeg.exe"

        node_linking.NVENC_PROBE_CACHE.clear()
        with mock.patch.object(node_linking.shutil, "which", side_effect=find_tool), mock.patch.object(
            node_linking.subprocess, "run", side_effect=run_probe
        ):
            ok, reason = node_linking.verify_windows_nvenc_encoder(gpu, "nvenc_av1_10bit", force=True)
        self.assertTrue(ok)
        self.assertEqual(
            reason,
            "HandBrake nvenc_av1_10bit verified on physical NVENC adapter 1 (logical gpu=0)",
        )
        self.assertEqual(len(calls), 2)
        source_command = calls[0][0]
        handbrake_command, handbrake_kwargs = calls[1]
        self.assertNotIn("av1_nvenc", source_command)
        self.assertIn("color=c=black:s=1920x1080:r=24:d=1", source_command)
        self.assertEqual(handbrake_command[handbrake_command.index("--encoder") + 1], "nvenc_av1_10bit")
        self.assertEqual(handbrake_command[handbrake_command.index("--encopts") + 1], "gpu=0")
        self.assertEqual(handbrake_kwargs["env"]["CUDA_VISIBLE_DEVICES"], "GPU-5070")
        self.assertEqual(handbrake_kwargs["env"]["NVIDIA_VISIBLE_DEVICES"], "GPU-5070")
        self.assertNotIn("--encoder-profile", handbrake_command)

    def test_av1_nvenc_probe_reports_driver_api_mismatch_instead_of_error_3(self):
        gpu = {
            "vendor_index": 0,
            "gpu_uuid": "GPU-5070",
            "pci_bus_id": "00000000:0a:00.0",
            "driver_version": "595.79",
        }
        source_result = mock.Mock(returncode=0, stdout="", stderr="")
        encode_result = mock.Mock(
            returncode=3,
            stdout="",
            stderr=(
                "[av1_nvenc] Driver does not support the required nvenc API version. "
                "Required: 13.1 Found: 13.0\n"
                "ERROR: Failure to initialise encoder\n"
                "Encode failed (error 3).\n"
            ),
        )

        def find_tool(name):
            return "HandBrakeCLI.exe" if name.casefold().startswith("handbrakecli") else "ffmpeg.exe"

        node_linking.NVENC_PROBE_CACHE.clear()
        with mock.patch.object(node_linking.shutil, "which", side_effect=find_tool), mock.patch.object(
            node_linking.subprocess, "run", side_effect=[source_result, encode_result]
        ):
            ok, reason = node_linking.verify_windows_nvenc_encoder(
                gpu, "nvenc_av1_10bit", force=True
            )
        self.assertFalse(ok)
        self.assertIn("Required: 13.1 Found: 13.0", reason)
        self.assertNotEqual(reason, "Encode failed (error 3).")

    def test_av1_nvenc_probe_requires_stable_adapter_identity(self):
        ok, reason = node_linking.verify_windows_nvenc_encoder(
            {"vendor_index": 1, "name": "NVIDIA GeForce RTX 5070"},
            "nvenc_av1_10bit",
        )
        self.assertFalse(ok)
        self.assertIn("UUID or PCI", reason)

    def test_mixed_nvidia_generations_get_per_gpu_av1_capabilities(self):
        global_encoders = ["nvenc_h264", "nvenc_h265_10bit", "nvenc_av1_10bit"]
        old_gpu = {"name": "NVIDIA GeForce RTX 3070"}
        new_gpu = {"name": "NVIDIA GeForce RTX 5070"}
        old_caps = node_linking._windows_gpu_encoder_capabilities(old_gpu, "nvenc", global_encoders)
        new_caps = node_linking._windows_gpu_encoder_capabilities(new_gpu, "nvenc", global_encoders)
        self.assertNotIn("nvenc_av1_10bit", old_caps)
        self.assertIn("nvenc_h265_10bit", old_caps)
        self.assertIn("nvenc_av1_10bit", new_caps)

    def test_windows_intel_and_amd_av1_capabilities_are_generation_aware(self):
        qsv_encoders = ["qsv_h265_10bit", "qsv_av1_10bit"]
        self.assertNotIn(
            "qsv_av1_10bit",
            node_linking._windows_gpu_encoder_capabilities(
                {"name": "Intel(R) UHD Graphics 770"}, "qsv", qsv_encoders
            ),
        )
        self.assertIn(
            "qsv_av1_10bit",
            node_linking._windows_gpu_encoder_capabilities(
                {"name": "Intel(R) Arc(TM) A770 Graphics"}, "qsv", qsv_encoders
            ),
        )

        vce_encoders = ["vce_h265_10bit", "vce_av1_10bit"]
        self.assertNotIn(
            "vce_av1_10bit",
            node_linking._windows_gpu_encoder_capabilities(
                {"name": "AMD Radeon RX 6800 XT"}, "vce", vce_encoders
            ),
        )
        self.assertIn(
            "vce_av1_10bit",
            node_linking._windows_gpu_encoder_capabilities(
                {"name": "AMD Radeon RX 9070 XT"}, "vce", vce_encoders
            ),
        )

    def test_environment_uses_persistent_windows_paths_and_all_families(self):
        config = default_config()
        with tempfile.TemporaryDirectory() as tempdir:
            config["work_dir"] = str(Path(tempdir) / "jobs")
            config["scratch_dir"] = str(Path(tempdir) / "scratch")
            config["preferred_encoder_family"] = "nvenc"
            config["gpu_routes"] = {"av1": "gpu:0", "h265": "gpu:1", "h264": "auto"}
            with mock.patch.dict(os.environ, {}, clear=False):
                configure_environment(config)
                self.assertEqual(os.environ["TSD_WORKER_MODE"], "1")
                self.assertEqual(os.environ["TSD_PREFERRED_ENCODER_FAMILY"], "nvenc")
                self.assertIn("vce", os.environ["TSD_ENCODER_FAMILIES"])
                self.assertTrue(Path(os.environ["TSD_WORKER_TEMP_DIR"]).is_dir())
                self.assertTrue(Path(os.environ["TSD_WORKER_SCRATCH_DIR"]).is_dir())
                # Windows runners may expose the same temp directory once as
                # an 8.3 short path and once as its long path.
                self.assertTrue(os.path.samefile(os.environ["TEMP"], config["scratch_dir"]))
                self.assertEqual(json.loads(os.environ["TSD_WINDOWS_GPU_ROUTES"])["h265"], "gpu:1")

    def test_background_windows_processes_never_create_a_console(self):
        if os.name != "nt":
            self.skipTest("Windows-only process flags")
        options = background_process_options(process_group=True)
        self.assertTrue(options["creationflags"] & subprocess.CREATE_NO_WINDOW)
        self.assertTrue(options["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP)

    def test_windows_termination_kills_entire_encoder_process_tree(self):
        result = mock.Mock(returncode=0, stdout="SUCCESS", stderr="")
        with mock.patch.object(process_utils.os, "name", "nt"), mock.patch.object(
            process_utils.subprocess, "run", return_value=result
        ) as run:
            ok, detail = process_utils.terminate_process_tree(4242, force=True)
        self.assertTrue(ok)
        self.assertEqual(detail, "SUCCESS")
        command = run.call_args.args[0]
        self.assertEqual(command[:4], ["taskkill.exe", "/PID", "4242", "/T"])
        self.assertIn("/F", command)

    def test_worker_startup_cleans_orphaned_encoder_in_staging_folder(self):
        payload = [{
            "ProcessId": 5151,
            "ParentProcessId": 5000,
            "Name": "HandBrakeCLI.exe",
            "CommandLine": 'HandBrakeCLI.exe -i "G:\\temp movies\\source.mkv" -o output.mkv',
        }]
        result = mock.Mock(returncode=0, stdout=json.dumps(payload), stderr="")
        with mock.patch.object(jobs.os, "name", "nt"), mock.patch.dict(
            os.environ,
            {
                "TSD_WINDOWS_WORKER": "1",
                "TSD_WORKER_TEMP_DIR": "G:\\temp movies",
                "TSD_WORKER_SCRATCH_DIR": "G:\\encoder temp",
            },
            clear=False,
        ), mock.patch.object(jobs.subprocess, "run", return_value=result), mock.patch.object(
            jobs, "terminate_process_tree", return_value=(True, "SUCCESS")
        ) as terminate:
            cleaned = jobs._cleanup_orphaned_windows_encoders()
        self.assertEqual(cleaned, 1)
        terminate.assert_called_once_with(5151, force=True)

    def test_cancel_running_job_terminates_tree_and_releases_waiters(self):
        job_id = "windows-cancel-tree"
        original_jobs = jobs.jobs
        original_queue = jobs.job_queue
        try:
            jobs.jobs = {
                job_id: {
                    "status": "running",
                    "phase": "encoding",
                    "pid": 6161,
                    "src": "G:\\temp movies\\source.mkv",
                    "started_at": 10.0,
                }
            }
            jobs.job_queue = []
            jobs.DISPATCH_WAKE_EVENT.clear()
            with mock.patch.object(jobs, "terminate_process_tree", return_value=(True, "SUCCESS")) as terminate, mock.patch.object(
                jobs, "save_jobs"
            ), mock.patch.object(jobs, "_append_job_log"), mock.patch.object(jobs, "log_event"):
                ok, error = jobs.cancel_job(job_id)
            self.assertTrue(ok)
            self.assertIsNone(error)
            terminate.assert_called_once_with(6161, force=True)
            self.assertEqual(jobs.jobs[job_id]["status"], "canceled")
            self.assertEqual(jobs.jobs[job_id]["phase"], "canceled")
            self.assertTrue(jobs.jobs[job_id]["cancel_process_tree_terminated"])
            self.assertTrue(jobs.DISPATCH_WAKE_EVENT.is_set())
        finally:
            jobs.jobs = original_jobs
            jobs.job_queue = original_queue
            jobs.DISPATCH_WAKE_EVENT.clear()


class WindowsGpuRoutingTests(unittest.TestCase):
    HARDWARE = {
        "gpus": [
            {
                "index": 0,
                "vendor_index": 0,
                "name": "NVIDIA GeForce RTX 5080",
                "vendor": "nvidia",
                "encoder_family": "nvenc",
                "memory_bytes": 16_000,
                "gpu_uuid": "GPU-5080",
                "pci_bus_id": "00000000:01:00.0",
                "encoders": ["nvenc_h265_10bit", "nvenc_av1_10bit"],
            },
            {
                "index": 1,
                "name": "AMD Radeon RX 9070 XT",
                "vendor": "amd",
                "encoder_family": "vce",
                "memory_bytes": 16_000,
                "encoders": ["vce_h265_10bit", "vce_av1_10bit"],
            },
        ]
    }

    def _environment(self, fallback: str = "1") -> dict[str, str]:
        return {
            "TSD_WINDOWS_WORKER": "1",
            "TSD_WINDOWS_GPU_ROUTES": json.dumps({"av1": "gpu:0", "h265": "gpu:1", "h264": "auto"}),
            "TSD_WINDOWS_GPU_FALLBACK_ANY": fallback,
        }

    def test_h265_route_changes_only_video_encoder_to_selected_amd_gpu(self):
        job = {
            "preset": "1080",
            "encoder": "nvenc_h265_10bit",
            "video_codec": "h265",
            "encoder_family": "nvenc",
            "bit_depth": "10",
        }
        with mock.patch.dict(os.environ, self._environment(), clear=False), mock.patch.object(
            node_linking, "encoder_hardware_profile", return_value=self.HARDWARE
        ):
            available, assignment = jobs._assign_windows_gpu_route(job, [])
        self.assertTrue(available)
        self.assertEqual(assignment["key"], "gpu:1")
        self.assertEqual(assignment["encoder"], "vce_h265_10bit")
        self.assertEqual(job["encoder_family"], "vce")

    def test_busy_specific_gpu_falls_back_to_other_compatible_idle_gpu(self):
        job = {
            "preset": "1080",
            "encoder": "vce_h265_10bit",
            "video_codec": "h265",
            "encoder_family": "vce",
            "bit_depth": "10",
        }
        running = [{"gpu_route_assignment": {"key": "gpu:1"}, "encoder": "vce_h265_10bit"}]
        with mock.patch.dict(os.environ, self._environment(), clear=False), mock.patch.object(
            node_linking, "encoder_hardware_profile", return_value=self.HARDWARE
        ):
            available, assignment = jobs._assign_windows_gpu_route(job, running)
        self.assertTrue(available)
        self.assertEqual(assignment["key"], "gpu:0")
        self.assertTrue(assignment["fallback_used"])
        self.assertEqual(assignment["encoder"], "nvenc_h265_10bit")

    def test_av1_skips_rtx_3070_and_routes_to_rtx_5070(self):
        hardware = {
            "gpus": [
                {
                    "index": 0,
                    "name": "NVIDIA GeForce RTX 3070",
                    "vendor": "nvidia",
                    "encoder_family": "nvenc",
                    "memory_bytes": 8_000,
                    "vendor_index": 0,
                    "gpu_uuid": "GPU-3070",
                    "pci_bus_id": "00000000:01:00.0",
                    "encoders": ["nvenc_h264", "nvenc_h265_10bit"],
                },
                {
                    "index": 1,
                    "name": "NVIDIA GeForce RTX 5070",
                    "vendor": "nvidia",
                    "encoder_family": "nvenc",
                    "memory_bytes": 12_000,
                    "vendor_index": 1,
                    "gpu_uuid": "GPU-5070",
                    "pci_bus_id": "00000000:09:00.0",
                    "encoders": ["nvenc_h264", "nvenc_h265_10bit", "nvenc_av1_10bit"],
                },
            ]
        }
        job = {
            "preset": "4k",
            "encoder": "nvenc_av1_10bit",
            "video_codec": "av1",
            "encoder_family": "nvenc",
            "bit_depth": "10",
        }
        environment = self._environment()
        environment["TSD_WINDOWS_GPU_ROUTES"] = json.dumps({"av1": "auto", "h265": "auto", "h264": "auto"})
        with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(
            node_linking, "encoder_hardware_profile", return_value=hardware
        ), mock.patch.object(
            node_linking, "verify_windows_nvenc_encoder", return_value=(True, "AV1 NVENC verified")
        ):
            available, assignment = jobs._assign_windows_gpu_route(job, [])
        self.assertTrue(available)
        self.assertEqual(assignment["key"], "gpu:1")
        self.assertEqual(assignment["vendor_index"], 1)
        self.assertEqual(assignment["gpu_uuid"], "GPU-5070")
        self.assertTrue(assignment["capability_verified"])
        self.assertEqual(assignment["encoder"], "nvenc_av1_10bit")

    def test_auto_hevc_reserves_av1_gpu_and_uses_rtx_3070(self):
        hardware = {
            "gpus": [
                {
                    "index": 0,
                    "vendor_index": 0,
                    "name": "NVIDIA GeForce RTX 3070",
                    "vendor": "nvidia",
                    "encoder_family": "nvenc",
                    "memory_bytes": 8_000,
                    "gpu_uuid": "GPU-3070",
                    "pci_bus_id": "00000000:01:00.0",
                    "encoders": ["nvenc_h264", "nvenc_h265_10bit"],
                },
                {
                    "index": 1,
                    "vendor_index": 1,
                    "name": "NVIDIA GeForce RTX 5070",
                    "vendor": "nvidia",
                    "encoder_family": "nvenc",
                    "memory_bytes": 12_000,
                    "gpu_uuid": "GPU-5070",
                    "pci_bus_id": "00000000:09:00.0",
                    "encoders": ["nvenc_h264", "nvenc_h265_10bit", "nvenc_av1_10bit"],
                },
            ]
        }
        job = {
            "preset": "4k",
            "encoder": "nvenc_h265_10bit",
            "video_codec": "h265",
            "encoder_family": "nvenc",
            "bit_depth": "10",
        }
        environment = self._environment()
        environment["TSD_WINDOWS_GPU_ROUTES"] = json.dumps(
            {"av1": "auto", "h265": "auto", "h264": "auto"}
        )
        with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(
            node_linking, "encoder_hardware_profile", return_value=hardware
        ):
            available, assignment = jobs._assign_windows_gpu_route(job, [])
        self.assertTrue(available)
        self.assertEqual(assignment["name"], "NVIDIA GeForce RTX 3070")
        self.assertEqual(assignment["vendor_index"], 0)

    def test_failed_av1_probe_blocks_launch_on_selected_adapter(self):
        hardware = {
            "gpus": [{
                "index": 1,
                "vendor_index": 1,
                "name": "NVIDIA GeForce RTX 5070",
                "vendor": "nvidia",
                "encoder_family": "nvenc",
                "memory_bytes": 12_000,
                "gpu_uuid": "GPU-5070",
                "pci_bus_id": "00000000:09:00.0",
                "encoders": ["nvenc_av1_10bit"],
            }]
        }
        job = {
            "preset": "4k",
            "encoder": "nvenc_av1_10bit",
            "video_codec": "av1",
            "encoder_family": "nvenc",
            "bit_depth": "10",
        }
        environment = self._environment()
        environment["TSD_WINDOWS_GPU_ROUTES"] = json.dumps(
            {"av1": "auto", "h265": "auto", "h264": "auto"}
        )
        with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(
            node_linking, "encoder_hardware_profile", return_value=hardware
        ), mock.patch.object(
            node_linking,
            "verify_windows_nvenc_encoder",
            return_value=(False, "No capable devices found"),
        ):
            available, assignment = jobs._assign_windows_gpu_route(job, [])
        self.assertFalse(available)
        self.assertIsNone(assignment)
        self.assertIn("No capable devices found", job["gpu_route_error"])
        self.assertNotIn("gpu_route_assignment", job)

    def test_unmapped_nvidia_index_never_falls_back_to_bytesqueeze_index(self):
        hardware = {
            "gpus": [{
                "index": 1,
                "name": "NVIDIA GeForce RTX 5070",
                "vendor": "nvidia",
                "encoder_family": "nvenc",
                "memory_bytes": 12_000,
                "encoders": ["nvenc_h265_10bit", "nvenc_av1_10bit"],
            }]
        }
        job = {
            "preset": "4k",
            "encoder": "nvenc_h265_10bit",
            "video_codec": "h265",
            "encoder_family": "nvenc",
            "bit_depth": "10",
        }
        environment = self._environment()
        environment["TSD_WINDOWS_GPU_ROUTES"] = json.dumps(
            {"av1": "auto", "h265": "auto", "h264": "auto"}
        )
        with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(
            node_linking, "encoder_hardware_profile", return_value=hardware
        ):
            available, assignment = jobs._assign_windows_gpu_route(job, [])
        self.assertFalse(available)
        self.assertIsNone(assignment)
        self.assertIn("UUID/PCI", job["gpu_route_error"])

    def test_failed_startup_av1_advertisement_cannot_launch_job(self):
        hardware = {
            "gpus": [{
                "index": 1,
                "vendor_index": 0,
                "name": "NVIDIA GeForce RTX 5070",
                "vendor": "nvidia",
                "encoder_family": "nvenc",
                "memory_bytes": 12_000,
                "gpu_uuid": "GPU-5070",
                "pci_bus_id": "00000000:0a:00.0",
                "av1_nvenc_verified": False,
                "av1_nvenc_verification": "No capable devices found",
                "encoders": ["nvenc_h264", "nvenc_h265_10bit"],
            }]
        }
        job = {
            "preset": "4k",
            "encoder": "nvenc_av1_10bit",
            "video_codec": "av1",
            "encoder_family": "nvenc",
            "bit_depth": "10",
        }
        environment = self._environment()
        environment["TSD_WINDOWS_GPU_ROUTES"] = json.dumps(
            {"av1": "auto", "h265": "auto", "h264": "auto"}
        )
        with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(
            node_linking, "encoder_hardware_profile", return_value=hardware
        ):
            available, assignment = jobs._assign_windows_gpu_route(job, [])
        self.assertFalse(available)
        self.assertIsNone(assignment)
        self.assertIn("No capable devices found", job["gpu_route_error"])
        self.assertEqual(job["phase"], "waiting_for_av1_gpu_validation")


if __name__ == "__main__":
    unittest.main()
