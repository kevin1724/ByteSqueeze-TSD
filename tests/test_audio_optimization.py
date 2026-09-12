import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from webui.app import audio_optimization as audio_module

from webui.app.audio_optimization import (
    ffmpeg_audio_only_command,
    handbrake_audio_args,
    normalize_operations,
    scan_media,
    storage_breakdown,
    validate_audio_only,
)


def inventory(*audio):
    streams = []
    for ordinal, row in enumerate(audio):
        streams.append({
            "index": ordinal + 1,
            "type": "audio",
            "type_ordinal": ordinal,
            "language": row.get("language", "eng"),
            "title": row.get("title", ""),
            "codec": row.get("codec", "dts"),
            "codec_label": row.get("codec_label", "DTS-HD MA"),
            "profile": row.get("profile", "DTS-HD MA"),
            "channels": row.get("channels", 8),
            "channel_layout": row.get("channel_layout", "7.1"),
            "bitrate": row.get("bitrate", 4_000_000),
            "sample_rate": row.get("sample_rate", 48_000),
            "size_bytes": row.get("size_bytes", 4 * 1024**3),
            "lossless": row.get("lossless", True),
            "object_audio": row.get("object_audio", False),
            "object_audio_capable": row.get("object_audio_capable", False),
            "commentary": row.get("commentary", False),
            "default": row.get("default", ordinal == 0),
            "forced": False,
        })
    video = {
        "index": 0, "type": "video", "type_ordinal": 0, "codec": "av1",
        "profile": "Main", "width": 3840, "height": 2160,
        "pix_fmt": "yuv420p10le", "nb_frames": 1000, "size_bytes": 20 * 1024**3,
    }
    return {
        "duration_seconds": 7200,
        "audio_streams": streams,
        "video_streams": [video],
        "subtitle_streams": [{"codec": "subrip", "language": "eng"}],
        "attachment_streams": [{"codec": "ttf"}],
        "chapter_count": 12,
        "aggregate": {
            "video_bytes": video["size_bytes"],
            "audio_bytes": sum(row["size_bytes"] for row in streams),
        },
    }


class AudioOptimizationTests(unittest.TestCase):
    def test_quick_audio_estimate_reads_only_short_audio_intervals(self):
        packets = {
            "packets": [
                {"stream_index": 1, "size": "1000", "duration_time": "1.0"},
                {"stream_index": 1, "size": "1000", "duration_time": "1.0"},
                {"stream_index": 2, "size": "9999", "duration_time": "1.0"},
            ]
        }
        completed = subprocess.CompletedProcess(
            ["ffprobe"], 0, stdout=json.dumps(packets), stderr=""
        )
        with patch.object(
            audio_module.subprocess, "run", return_value=completed
        ) as probe:
            result = audio_module._quick_audio_estimates("movie.mkv", 100.0, {1})
        command = probe.call_args.args[0]
        self.assertIn("-select_streams", command)
        self.assertEqual(command[command.index("-select_streams") + 1], "a")
        self.assertIn("-read_intervals", command)
        self.assertEqual(result[1]["bitrate"], 8000)
        self.assertEqual(result[1]["size_bytes"], 100000)

    def test_fast_inventory_samples_missing_audio_size_without_full_packet_scan(self):
        with tempfile.TemporaryDirectory(prefix="bytesqueeze-audio-fast-") as folder:
            source_path = os.path.join(folder, "sample.mkv")
            cache_path = os.path.join(folder, "cache.json")
            with open(source_path, "wb") as stream:
                stream.write(b"media")
            metadata = {
                "format": {
                    "duration": "100.0",
                    "size": "10000000",
                    "format_name": "matroska",
                },
                "streams": [
                    {
                        "index": 0,
                        "codec_type": "video",
                        "codec_name": "hevc",
                        "duration": "100.0",
                    },
                    {
                        "index": 1,
                        "codec_type": "audio",
                        "codec_name": "truehd",
                        "profile": "TrueHD",
                        "channels": 8,
                        "duration": "100.0",
                    },
                ],
            }
            completed = subprocess.CompletedProcess(
                ["ffprobe"], 0, stdout=json.dumps(metadata), stderr=""
            )
            sampled = {
                1: {
                    "bitrate": 800000,
                    "size_bytes": 10000000,
                    "sample_seconds": 24.0,
                }
            }
            with (
                patch.object(audio_module, "AUDIO_CACHE_FILE", cache_path),
                patch.object(audio_module.subprocess, "run", return_value=completed),
                patch.object(
                    audio_module, "_quick_audio_estimates", return_value=sampled
                ) as quick,
                patch.object(audio_module, "_packet_sizes") as full_scan,
            ):
                result = scan_media(source_path, exact=False, force=True)
            full_scan.assert_not_called()
            quick.assert_called_once()
            track = result["audio_streams"][0]
            self.assertEqual(track["size_bytes"], 10000000)
            self.assertEqual(track["size_source"], "packet_sample")
            self.assertEqual(track["size_accuracy"], 0.8)
            self.assertFalse(track["size_exact"])

    def test_exact_inventory_uses_container_sizes_without_packet_scan(self):
        with tempfile.TemporaryDirectory(prefix="bytesqueeze-audio-tags-") as folder:
            source_path = os.path.join(folder, "tagged.mkv")
            cache_path = os.path.join(folder, "cache.json")
            with open(source_path, "wb") as stream:
                stream.write(b"media")
            metadata = {
                "format": {
                    "duration": "100.0",
                    "size": "9000000",
                    "format_name": "matroska",
                },
                "streams": [
                    {
                        "index": 0,
                        "codec_type": "video",
                        "codec_name": "hevc",
                        "tags": {"NUMBER_OF_BYTES": "8000000"},
                    },
                    {
                        "index": 1,
                        "codec_type": "audio",
                        "codec_name": "eac3",
                        "channels": 6,
                        "tags": {"NUMBER_OF_BYTES-eng": "1000000"},
                    },
                ],
            }
            completed = subprocess.CompletedProcess(
                ["ffprobe"], 0, stdout=json.dumps(metadata), stderr=""
            )
            with (
                patch.object(audio_module, "AUDIO_CACHE_FILE", cache_path),
                patch.object(audio_module.subprocess, "run", return_value=completed),
                patch.object(audio_module, "_packet_sizes") as full_scan,
            ):
                result = scan_media(source_path, exact=True, force=True)
            full_scan.assert_not_called()
            self.assertTrue(result["packet_sizes_exact"])
            self.assertTrue(result["audio_sizes_exact"])
            self.assertEqual(result["audio_streams"][0]["size_source"], "container_tag")

    def test_preserve_is_safe_default_and_copies_every_track(self):
        source = inventory(
            {},
            {"codec": "ac3", "codec_label": "AC3", "channels": 6, "size_bytes": 500 * 1024**2, "lossless": False},
        )
        plan = normalize_operations({}, source)
        self.assertEqual(plan["job_type"], "video_preserve_audio")
        self.assertTrue(all(row["action"] == "copy" for row in plan["audio_actions"]))
        args = handbrake_audio_args(plan, source)
        self.assertEqual(args[args.index("--aencoder") + 1], "copy,copy")

    def test_optimize_lossless_keeps_efficient_audio_and_channel_count(self):
        source = inventory(
            {},
            {"codec": "eac3", "codec_label": "E-AC3", "channels": 6, "size_bytes": 600 * 1024**2, "lossless": False},
        )
        plan = normalize_operations(
            {"job_type": "video_audio", "audio_policy": "optimize_lossless"},
            source,
        )
        self.assertEqual(plan["audio_actions"][0]["action"], "encode")
        self.assertEqual(plan["audio_actions"][0]["channels"], 8)
        self.assertEqual(plan["audio_actions"][1]["action"], "copy")
        self.assertGreater(plan["estimate"]["audio_savings_bytes"], 0)

    def test_object_audio_and_commentary_are_not_changed_automatically(self):
        source = inventory(
            {"object_audio": True, "title": "TrueHD Atmos"},
            {"commentary": True, "title": "Director commentary"},
        )
        plan = normalize_operations(
            {"job_type": "audio_only", "audio_policy": "optimize_lossless"},
            source,
        )
        self.assertEqual([row["action"] for row in plan["audio_actions"]], ["copy", "copy"])
        self.assertTrue(any("object audio" in message for message in plan["warnings"]))

    def test_object_audio_capable_lossless_track_requires_explicit_permission(self):
        source = inventory({"object_audio_capable": True})
        safe = normalize_operations(
            {"job_type": "audio_only", "audio_policy": "optimize_lossless"},
            source,
        )
        self.assertEqual(safe["audio_actions"][0]["action"], "copy")
        approved = normalize_operations(
            {
                "job_type": "audio_only",
                "audio_policy": "optimize_lossless",
                "allow_object_audio_loss": True,
            },
            source,
        )
        self.assertEqual(approved["audio_actions"][0]["action"], "encode")

    def test_client_cannot_override_scanned_stream_identity(self):
        source = inventory({})
        plan = normalize_operations(
            {
                "job_type": "audio_only",
                "audio_policy": "custom",
                "audio_actions": [{
                    "stream_index": 1,
                    "action": "encode",
                    "source_codec": "aac",
                    "source_channels": 2,
                    "audio_ordinal": 9,
                }],
            },
            source,
        )
        track = plan["audio_actions"][0]
        self.assertEqual(track["source_codec"], "dts")
        self.assertEqual(track["source_channels"], 8)
        self.assertEqual(track["audio_ordinal"], 0)

    def test_downmix_requires_an_explicit_per_track_choice(self):
        source = inventory({})
        base = normalize_operations(
            {
                "job_type": "audio_only",
                "audio_policy": "custom",
                "audio_actions": [{"stream_index": 1, "action": "encode", "channels": 6}],
            },
            source,
        )
        self.assertEqual(base["audio_actions"][0]["channels"], 8)
        allowed = normalize_operations(
            {
                "job_type": "audio_only",
                "audio_policy": "custom",
                "audio_actions": [{"stream_index": 1, "action": "encode", "channels": 6, "allow_downmix": True}],
            },
            source,
        )
        self.assertEqual(allowed["audio_actions"][0]["channels"], 6)

    def test_audio_only_command_copies_everything_except_selected_audio_codec(self):
        source = inventory({})
        plan = normalize_operations(
            {
                "job_type": "audio_only",
                "audio_policy": "custom",
                "audio_actions": [{"stream_index": 1, "action": "encode", "target_codec": "aac", "bitrate_kbps": 1024}],
            },
            source,
        )
        command = ffmpeg_audio_only_command("input.mkv", "output.mkv", plan)
        self.assertIn("-map_metadata", command)
        self.assertIn("-map_chapters", command)
        self.assertEqual(command[command.index("-c") + 1], "copy")
        self.assertEqual(command[command.index("-c:a:0") + 1], "aac")
        self.assertEqual(command[command.index("-b:a:0") + 1], "1024k")

    def test_audio_only_validation_and_real_split_savings(self):
        source = inventory({})
        plan = normalize_operations(
            {
                "job_type": "audio_only", "audio_policy": "custom",
                "audio_actions": [{"stream_index": 1, "action": "encode", "target_codec": "aac", "bitrate_kbps": 1024}],
            },
            source,
        )
        output = inventory({"codec": "aac", "codec_label": "AAC", "lossless": False, "size_bytes": 900 * 1024**2})
        valid, errors = validate_audio_only(source, output, plan)
        self.assertTrue(valid, errors)
        split = storage_breakdown(source, output, 24 * 1024**3, 21 * 1024**3)
        self.assertEqual(split["video_saved_bytes"], 0)
        self.assertGreater(split["audio_saved_bytes"], 0)
        self.assertEqual(split["total_saved_bytes"], 3 * 1024**3)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg tools are unavailable")
    def test_real_ffmpeg_audio_only_encode_stream_copies_video(self):
        with tempfile.TemporaryDirectory(prefix="bytesqueeze-audio-") as folder:
            source_path = os.path.join(folder, "source.mkv")
            output_path = os.path.join(folder, "output.tmp.mkv")
            create = [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000",
                "-t", "2", "-map", "0:v:0", "-map", "1:a:0", "-map", "2:a:0",
                "-c:v", "ffv1", "-c:a:0", "flac", "-c:a:1", "ac3", "-b:a:1", "192k",
                "-metadata:s:a:0", "language=eng", "-metadata:s:a:1", "language=spa",
                source_path,
            ]
            subprocess.run(create, check=True, capture_output=True)
            before = scan_media(source_path, exact=True, force=True)
            plan = normalize_operations(
                {
                    "job_type": "audio_only",
                    "audio_policy": "custom",
                    "replace_source": False,
                    "audio_actions": [{
                        "stream_index": before["audio_streams"][0]["index"],
                        "action": "encode",
                        "target_codec": "aac",
                        "bitrate_kbps": 128,
                    }],
                },
                before,
            )
            completed = subprocess.run(
                ffmpeg_audio_only_command(source_path, output_path, plan),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
            after = scan_media(output_path, exact=True, force=True)
            valid, errors = validate_audio_only(before, after, plan)
            self.assertTrue(valid, errors)
            self.assertEqual(before["aggregate"]["video_bytes"], after["aggregate"]["video_bytes"])
            self.assertEqual([row["codec"] for row in after["audio_streams"]], ["aac", "ac3"])


if __name__ == "__main__":
    unittest.main()
