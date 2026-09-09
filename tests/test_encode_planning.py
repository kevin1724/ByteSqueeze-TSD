import json
import os
import tempfile
import unittest
from unittest import mock

from webui.app import jobs, presets


class EncodePlanningTests(unittest.TestCase):
    def test_output_container_plan_controls_muxer_fast_start_and_extension(self):
        default_plan = jobs._output_container_plan({})
        self.assertEqual(default_plan["container"], "mkv")
        self.assertEqual(default_plan["extension"], ".mkv")
        self.assertEqual(default_plan["cli_args"], ["--format", "av_mkv"])

        mp4_plan = jobs._output_container_plan(
            {"output_container": "mp4", "web_optimized": True}
        )
        self.assertEqual(mp4_plan["extension"], ".mp4")
        self.assertEqual(
            mp4_plan["cli_args"],
            ["--format", "av_mp4", "--optimize"],
        )
        self.assertEqual(
            jobs._output_path_for_source(
                "/media/Movie.Source.mkv",
                "TSD",
                {"output_container": "mp4", "web_optimized": True},
            ),
            os.path.join("/media", "Movie.Source-TSD.mp4"),
        )

        mkv_plan = jobs._output_container_plan(
            {"output_container": "mkv", "web_optimized": True}
        )
        self.assertFalse(mkv_plan["web_optimized"])
        self.assertNotIn("--optimize", mkv_plan["cli_args"])

    def test_requested_qsv_scenarios_use_the_expected_decode_and_resolution_plan(self):
        scenarios = (
            ("local H.264 1080p", "local", "1080", "h264", 1920, 1080, (1920, 1080)),
            ("remote HEVC 10-bit 4K", "remote_transfer", "4k", "hevc", 3840, 2160, (3840, 2160)),
            ("remote HEVC 10-bit 4K to 1080p", "remote_transfer", "1080", "hevc", 3840, 2160, (1920, 1080)),
        )
        for label, job_mode, preset_key, codec, width, height, expected in scenarios:
            with self.subTest(label=label):
                source = {
                    "codec": codec,
                    "profile": "Main 10" if codec == "hevc" else "High",
                    "pix_fmt": "yuv420p10le" if codec == "hevc" else "yuv420p",
                    "width": width,
                    "height": height,
                    "error": "",
                }
                decode = jobs._hardware_decode_plan("qsv_h265_10bit", source, "auto")
                dimensions = jobs._resolution_plan(preset_key, source)

                self.assertIn(job_mode, {"local", "remote_transfer"})
                self.assertTrue(decode["enabled"])
                self.assertEqual(decode["cli_args"], ["--enable-hw-decoding", "qsv"])
                self.assertEqual(
                    (dimensions["target_width"], dimensions["target_height"]),
                    expected,
                )

    def test_resolution_ceiling_never_upscales_and_preserves_aspect_ratio(self):
        source = {"width": 1280, "height": 720}
        plan_1080 = jobs._resolution_plan("1080", source)
        plan_4k = jobs._resolution_plan("4k", source)
        width_only = jobs._resolution_plan("1080", {"width": 3840, "height": 2160}, "--width 1280")

        self.assertEqual((plan_1080["target_width"], plan_1080["target_height"]), (1280, 720))
        self.assertEqual((plan_4k["target_width"], plan_4k["target_height"]), (1280, 720))
        self.assertEqual((width_only["target_width"], width_only["target_height"]), (1280, 720))
        self.assertEqual(plan_1080["cli_args"], ["--maxWidth", "1920", "--maxHeight", "1080"])

    def test_hardware_decode_modes_and_unsupported_source_fall_back_safely(self):
        h264 = {"codec": "h264", "error": ""}
        av1 = {"codec": "av1", "error": ""}

        self.assertTrue(jobs._hardware_decode_plan("x265", h264, "qsv")["enabled"])
        self.assertFalse(jobs._hardware_decode_plan("x265", h264, "auto")["enabled"])
        self.assertFalse(jobs._hardware_decode_plan("qsv_h265_10bit", h264, "off")["enabled"])
        unsupported = jobs._hardware_decode_plan("qsv_h265_10bit", av1, "auto")
        self.assertFalse(unsupported["enabled"])
        self.assertEqual(unsupported["cli_args"], ["--disable-hw-decoding"])
        self.assertIn("not in the supported", unsupported["reason"])

    def test_audio_encoder_copy_never_overrides_the_qsv_video_encoder(self):
        preset_file = os.path.join(
            os.path.dirname(__file__),
            "..",
            "presets",
            "Plex-HEVC-QSV-1080p-ICQ28-Smaller-AudioCopy-ENG-SPA.json",
        )
        actual_args = (
            "-b 2035 --encoder qsv_h265_10bit --encoder-preset balanced "
            "--width 1920 --height 1080 --all-audio -E copy "
            "--audio-copy-mask aac,ac3,eac3,truehd,dts,dtshd"
        )
        encoder = jobs._selected_video_encoder(
            {"extra_args": actual_args},
            preset_file,
            "Plex-HEVC-QSV-1080p-ICQ28-Smaller-AudioCopy-ENG-SPA",
        )
        decode = jobs._hardware_decode_plan(
            encoder,
            {"codec": "h264", "error": ""},
            "auto",
        )

        self.assertEqual(jobs._argument_value(["-E", "copy"], "--encoder", "-e"), "")
        self.assertEqual(encoder, "qsv_h265_10bit")
        self.assertTrue(decode["enabled"])
        self.assertEqual(decode["cli_args"], ["--enable-hw-decoding", "qsv"])

    def test_preset_video_encoder_is_used_when_only_audio_encoder_is_overridden(self):
        preset_file = os.path.join(
            os.path.dirname(__file__),
            "..",
            "presets",
            "Plex-HEVC-QSV-1080p-ICQ28-Smaller-AudioCopy-ENG-SPA.json",
        )
        encoder = jobs._selected_video_encoder(
            {"extra_args": "--all-audio -E copy"},
            preset_file,
            "Plex-HEVC-QSV-1080p-ICQ28-Smaller-AudioCopy-ENG-SPA",
        )
        self.assertEqual(encoder, "qsv_h265_10bit")

    def test_decode_policy_is_written_only_to_the_video_preset(self):
        payload = {
            "PresetList": [
                {
                    "PresetName": "QSV video",
                    "VideoEncoder": "qsv_h265_10bit",
                    "AudioList": [{"PresetEncoder": "copy", "AudioEncoder": "copy"}],
                }
            ]
        }
        self.assertTrue(jobs._set_preset_hardware_decode(payload, "QSV video", True))
        selected = payload["PresetList"][0]
        self.assertEqual(selected["VideoEncoder"], "qsv_h265_10bit")
        self.assertEqual(selected["VideoHWDecode"], 2)
        self.assertIs(selected["VideoQSVDecode"], True)
        self.assertEqual(selected["VideoAdapterIndex"], 0)
        self.assertNotIn("VideoHWDecode", selected["AudioList"][0])

    def test_materialized_preset_enforces_qsv_decode_for_handbrake_import(self):
        source = {
            "PresetList": [
                {
                    "PresetName": "Imported QSV",
                    "VideoEncoder": "qsv_h265_10bit",
                    "AudioList": [{"AudioEncoder": "copy"}],
                }
            ]
        }
        with tempfile.TemporaryDirectory() as tempdir:
            source_path = os.path.join(tempdir, "source.json")
            with open(source_path, "w", encoding="utf-8") as stream:
                json.dump(source, stream)
            result = jobs._materialize_decode_policy_preset(
                "job-id",
                source_path,
                "Imported QSV",
                True,
                tempdir,
            )
            self.assertIsNotNone(result)
            with open(result[0], "r", encoding="utf-8") as stream:
                materialized = json.load(stream)["PresetList"][0]

        self.assertEqual(materialized["VideoEncoder"], "qsv_h265_10bit")
        self.assertEqual(materialized["VideoHWDecode"], 2)
        self.assertIs(materialized["VideoQSVDecode"], True)
        self.assertEqual(materialized["VideoAdapterIndex"], 0)
        self.assertEqual(materialized["AudioList"][0]["AudioEncoder"], "copy")

    def test_qsv_adapter_index_is_explicit_and_safely_normalized(self):
        with mock.patch.dict(os.environ, {"TSD_QSV_ADAPTER": "3"}):
            self.assertEqual(jobs._qsv_adapter_index(), 3)
        for bad_value in ("", "-1", "device-zero"):
            with self.subTest(value=bad_value), mock.patch.dict(
                os.environ,
                {"TSD_QSV_ADAPTER": bad_value},
            ):
                self.assertEqual(jobs._qsv_adapter_index(), 0)

    def test_decode_log_evidence_distinguishes_active_qsv_from_encode_only(self):
        positive = (
            '"HWDecode": 1',
            '"HardwareDecode": 4',
            '"QSV": {"Decode": true}',
            'encqsvInit: using full QSV path',
            'decoder: h264_qsv 8-bit (yuv420p)',
            'decoder: qsv hevc 10-bit (p010le, sw)',
            'hevc_qsv-decoder: opening decoder',
            'encavcodec: QSV hardware decode and QSV hardware encode via system memory transfer',
        )
        negative = (
            '"QSV": {"Decode": false}',
            'qsv decoder failed to initialize',
            'h264_qsv-decoder error: invalid stream',
            '[ByteSqueeze] Hardware decode: software fallback (QSV decode attempt exited 1)',
        )
        for line in positive:
            self.assertEqual(jobs._qsv_decode_log_evidence(line), "active")
        for line in negative:
            self.assertEqual(jobs._qsv_decode_log_evidence(line), "fallback")
        # In HandBrake 1.9 on Linux these generic fields and the encoder's
        # memory-path message can coexist with a verified h264_qsv decoder.
        for line in (
            '"HWDecode": 0',
            '"HardwareDecode": 0',
            'encqsvInit: using encode-only via system memory path',
        ):
            self.assertEqual(jobs._qsv_decode_log_evidence(line), "")

    def test_1080_mapping_repair_replaces_a_4k_preset_and_persists_it(self):
        with tempfile.TemporaryDirectory() as tempdir:
            bad_file = os.path.join(tempdir, "bad-4k.json")
            good_file = os.path.join(tempdir, "good-1080.json")
            config_file = os.path.join(tempdir, "preset_config.json")
            with open(bad_file, "w", encoding="utf-8") as stream:
                json.dump({"PresetList": [{"PresetName": "Wrong 4K", "PictureWidth": 3840, "PictureHeight": 2160}]}, stream)
            with open(good_file, "w", encoding="utf-8") as stream:
                json.dump({"PresetList": [{"PresetName": "Correct 1080p", "PictureWidth": 1920, "PictureHeight": 1080}]}, stream)
            defaults = {
                "1080": {"file": good_file, "name": "Correct 1080p"},
                "4k": {"file": bad_file, "name": "Wrong 4K"},
            }
            with open(config_file, "w", encoding="utf-8") as stream:
                json.dump({"1080": {"file": bad_file, "name": "Wrong 4K"}, "4k": defaults["4k"]}, stream)

            with mock.patch.object(presets, "PRESET_CONFIG_FILE", config_file), mock.patch.object(
                presets,
                "DEFAULT_PRESET_CONFIG",
                defaults,
            ):
                presets.load_preset_config()
                selected_file, selected_name = presets.resolve_preset_file_and_name("1080")

            self.assertEqual((selected_file, selected_name), (good_file, "Correct 1080p"))
            self.assertEqual(presets.preset_config["4k"]["name"], "Wrong 4K")
            with open(config_file, "r", encoding="utf-8") as stream:
                persisted = json.load(stream)
            self.assertEqual(persisted["1080"], defaults["1080"])

    def test_shell_command_enforces_caps_and_retries_failed_qsv_decode_in_software(self):
        script_path = os.path.join(os.path.dirname(__file__), "..", "worker", "encode-one.sh")
        with open(script_path, "r", encoding="utf-8") as stream:
            script = stream.read()

        self.assertLess(script.index("${EXTRA_ARGS}"), script.index("${DIMENSION_OPTS}"))
        self.assertLess(script.index("${DIMENSION_OPTS}"), script.index("${HW_DECODE_OPTS}"))
        self.assertLess(script.index("${HW_DECODE_OPTS}"), script.index("${CONTAINER_OPTS}"))
        self.assertIn('HW_DECODE_OPTS="${HB_HW_DECODE_OPTS:---disable-hw-decoding}"', script)
        self.assertIn('OUTPUT_CONTAINER=$(printf', script)
        self.assertIn('CONTAINER_OPTS="--format av_mp4"', script)
        self.assertIn('CONTAINER_OPTS="$CONTAINER_OPTS --optimize"', script)
        self.assertIn('CONTAINER_OPTS="--format av_mkv"', script)
        self.assertEqual(script.count("${CONTAINER_OPTS}"), 3)
        self.assertIn('OUT="${DIR}/${NAME}-${SUFFIX}.${EXT}"', script)
        self.assertIn("bytesqueeze-qsv-preflight encode", script)
        self.assertIn('QSV_ADAPTER_OPTS="--qsv-adapter $QSV_ADAPTER"', script)
        self.assertIn('rm -f -- "$OUT"', script)
        self.assertIn("--disable-hw-decoding", script)

    def test_container_build_patches_qsv_child_device_to_the_render_node(self):
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        with open(os.path.join(root, "Dockerfile"), "r", encoding="utf-8") as stream:
            dockerfile = stream.read()
        with open(
            os.path.join(root, "patches", "handbrake-1.11.2-qsv-linux-render-node.patch"),
            "r",
            encoding="utf-8",
        ) as stream:
            handbrake_patch = stream.read()
        with open(os.path.join(root, "worker", "qsv-preflight.sh"), "r", encoding="utf-8") as stream:
            preflight = stream.read()

        self.assertIn("git apply --unidiff-zero /tmp/handbrake-qsv-render-node.patch", dockerfile)
        self.assertIn('ENTRYPOINT ["/usr/local/bin/bytesqueeze-entrypoint"]', dockerfile)
        self.assertIn("hb_qsv_get_adapter_render_node(device_index)", handbrake_patch)
        self.assertIn('"/dev/dri/renderD%u"', handbrake_patch)
        self.assertIn('"child_device_type", "vaapi"', handbrake_patch)
        self.assertIn(
            "QSV hardware decode and QSV hardware encode via system memory transfer",
            handbrake_patch,
        )
        self.assertIn('vainfo --display drm --device "$RENDER_DEVICE"', preflight)
        self.assertIn("child_device=$RENDER_DEVICE,child_device_type=vaapi", preflight)


if __name__ == "__main__":
    unittest.main()
