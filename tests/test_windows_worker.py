import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from worker import encode_runner
from worker.windows_app import configure_environment, default_config
from webui.app import node_linking


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
                "HB_EXTRA_ARGS": "--encoder nvenc_h265_10bit --quality 24",
                "HB_AUDIO_POLICY_OPTS": "--all-audio --aname 'English Main' --aencoder copy",
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
            self.assertIn("English Main", command)
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


class WindowsHardwareInventoryTests(unittest.TestCase):
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

    def test_environment_uses_persistent_windows_paths_and_all_families(self):
        config = default_config()
        with tempfile.TemporaryDirectory() as tempdir:
            config["work_dir"] = str(Path(tempdir) / "jobs")
            config["preferred_encoder_family"] = "nvenc"
            with mock.patch.dict(os.environ, {}, clear=False):
                configure_environment(config)
                self.assertEqual(os.environ["TSD_WORKER_MODE"], "1")
                self.assertEqual(os.environ["TSD_PREFERRED_ENCODER_FAMILY"], "nvenc")
                self.assertIn("vce", os.environ["TSD_ENCODER_FAMILIES"])
                self.assertTrue(Path(os.environ["TSD_WORKER_TEMP_DIR"]).is_dir())


if __name__ == "__main__":
    unittest.main()
