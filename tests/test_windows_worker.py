import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from worker import encode_runner
from worker.windows_app import configure_environment, default_config, local_addresses, windows_firewall_script
from webui.app import node_linking
from webui.app import jobs
from webui.app.process_utils import background_process_options


class WindowsEncodeRunnerTests(unittest.TestCase):
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
            self.assertEqual(command[-1], str(source.with_name("Movie Source-TSD.mp4")))

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

    def test_mixed_nvidia_generations_get_per_gpu_av1_capabilities(self):
        global_encoders = ["nvenc_h264", "nvenc_h265_10bit", "nvenc_av1_10bit"]
        old_gpu = {"name": "NVIDIA GeForce RTX 3070"}
        new_gpu = {"name": "NVIDIA GeForce RTX 5070"}
        old_caps = node_linking._windows_gpu_encoder_capabilities(old_gpu, "nvenc", global_encoders)
        new_caps = node_linking._windows_gpu_encoder_capabilities(new_gpu, "nvenc", global_encoders)
        self.assertNotIn("nvenc_av1_10bit", old_caps)
        self.assertIn("nvenc_h265_10bit", old_caps)
        self.assertIn("nvenc_av1_10bit", new_caps)

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


class WindowsGpuRoutingTests(unittest.TestCase):
    HARDWARE = {
        "gpus": [
            {
                "index": 0,
                "name": "NVIDIA GeForce RTX 5080",
                "vendor": "nvidia",
                "encoder_family": "nvenc",
                "memory_bytes": 16_000,
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
                    "encoders": ["nvenc_h264", "nvenc_h265_10bit"],
                },
                {
                    "index": 1,
                    "name": "NVIDIA GeForce RTX 5070",
                    "vendor": "nvidia",
                    "encoder_family": "nvenc",
                    "memory_bytes": 12_000,
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
        ):
            available, assignment = jobs._assign_windows_gpu_route(job, [])
        self.assertTrue(available)
        self.assertEqual(assignment["key"], "gpu:1")
        self.assertEqual(assignment["encoder"], "nvenc_av1_10bit")


if __name__ == "__main__":
    unittest.main()
