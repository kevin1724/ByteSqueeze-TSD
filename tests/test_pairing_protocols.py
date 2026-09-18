import os
import tempfile
import threading
import unittest
from unittest import mock

from webui.app import mobile_linking, node_linking


class NodePairingProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_node_file = node_linking.NODE_LINK_FILE
        self.original_transfer_file = node_linking.NODE_TRANSFER_FILE
        node_linking.NODE_LINK_FILE = os.path.join(self.tempdir.name, "linked_nodes.json")
        node_linking.NODE_TRANSFER_FILE = os.path.join(self.tempdir.name, "node_transfers.json")

    def tearDown(self):
        node_linking.NODE_LINK_FILE = self.original_node_file
        node_linking.NODE_TRANSFER_FILE = self.original_transfer_file
        self.tempdir.cleanup()

    def test_cancel_transfer_grant_revokes_download_and_upload_tokens(self):
        grant = node_linking.create_transfer_grant(
            "/media/show.mkv",
            "worker-a",
            source_size=123,
        )
        canceled = node_linking.cancel_transfer_grant(grant["id"], "worker-a")

        self.assertEqual(canceled["status"], "canceled")
        self.assertFalse(
            node_linking.transfer_token_matches(
                canceled,
                "download",
                grant["download_token"],
                require_unused=False,
            )
        )
        self.assertFalse(
            node_linking.transfer_token_matches(
                canceled,
                "upload",
                grant["upload_token"],
                require_unused=False,
            )
        )
        with self.assertRaisesRegex(ValueError, "canceled"):
            node_linking.renew_transfer_upload_grant(grant["id"], "worker-a")

    def test_upload_renewal_waits_while_controller_is_still_validating(self):
        grant = node_linking.create_transfer_grant("/media/show.mkv", "worker-a", source_size=123)
        row = node_linking.get_transfer(grant["id"])
        row.update({
            "status": "validating",
            "upload_started_at": node_linking._now(),
        })
        node_linking.save_transfer(row)

        renewal = node_linking.renew_transfer_upload_grant(grant["id"], "worker-a")

        self.assertFalse(renewal["complete"])
        self.assertTrue(renewal["in_progress"])
        self.assertNotIn("upload_token", renewal)

    def test_idle_online_worker_releases_abandoned_download_reservation(self):
        with mock.patch.object(node_linking, "_now", return_value=1000):
            grant = node_linking.create_transfer_grant(
                "/media/abandoned-movie.mkv",
                "worker-a",
                source_size=123,
            )
            transfer = node_linking.get_transfer(grant["id"])
            transfer.update({
                "status": "downloading",
                "created_at": 100,
                "download_used_at": 120,
            })
            node_linking.save_transfer(transfer)
            node_linking.save_node({
                "id": "worker-a",
                "online": True,
                "status": "idle",
                "last_heartbeat": 1000,
                "summary": {
                    "counts": {
                        "queued": 0,
                        "running": 0,
                        "waiting_to_upload": 0,
                    }
                },
            })

            active = node_linking.find_active_transfer_for_src(
                "/media/abandoned-movie.mkv"
            )

        self.assertIsNone(active)
        self.assertEqual(node_linking.get_transfer(grant["id"])["status"], "canceled")

    def test_post_transfer_idle_heartbeat_releases_reservation_after_worker_goes_offline(self):
        with mock.patch.object(node_linking, "_now", return_value=1000):
            grant = node_linking.create_transfer_grant(
                "/media/offline-abandoned-movie.mkv",
                "worker-a",
                source_size=123,
            )
            transfer = node_linking.get_transfer(grant["id"])
            transfer.update({
                "status": "downloading",
                "created_at": 100,
                "download_used_at": 120,
            })
            node_linking.save_transfer(transfer)
            node_linking.save_node({
                "id": "worker-a",
                "online": False,
                "status": "offline",
                "last_heartbeat": 300,
                "summary": {
                    "counts": {
                        "queued": 0,
                        "running": 0,
                        "waiting_to_upload": 0,
                    }
                },
            })

            active = node_linking.find_active_transfer_for_src(
                "/media/offline-abandoned-movie.mkv"
            )

        self.assertIsNone(active)
        self.assertEqual(node_linking.get_transfer(grant["id"])["status"], "canceled")

    def test_worker_without_status_counts_does_not_release_transfer_reservation(self):
        with mock.patch.object(node_linking, "_now", return_value=1000):
            grant = node_linking.create_transfer_grant(
                "/media/legacy-worker-movie.mkv",
                "worker-a",
                source_size=123,
            )
            transfer = node_linking.get_transfer(grant["id"])
            transfer.update({
                "status": "downloading",
                "created_at": 100,
                "download_used_at": 120,
            })
            node_linking.save_transfer(transfer)
            node_linking.save_node({
                "id": "worker-a",
                "online": True,
                "status": "idle",
                "last_heartbeat": 1000,
                "summary": {},
            })

            active = node_linking.find_active_transfer_for_src(
                "/media/legacy-worker-movie.mkv"
            )

        self.assertEqual(active["id"], grant["id"])
        self.assertEqual(node_linking.get_transfer(grant["id"])["status"], "downloading")

    def test_lost_pair_response_can_be_retried_by_same_controller(self):
        pairing = node_linking.create_pairing_code()
        controller = {
            "controller_id": "controller-a",
            "controller_name": "Main",
            "request_id": "first-attempt",
            "protocol_version": "not-a-number",
        }
        first = node_linking.accept_pairing(pairing["code"], controller)
        second = node_linking.accept_pairing(pairing["code"], {**controller, "request_id": "retry"})

        self.assertFalse(first["retry_recovered"])
        self.assertTrue(second["retry_recovered"])
        self.assertNotEqual(first["token"], second["token"])
        self.assertEqual(second["protocol_version"], node_linking.NODE_PROTOCOL_VERSION)

        with self.assertRaisesRegex(ValueError, "already used"):
            node_linking.accept_pairing(pairing["code"], {"controller_id": "controller-b"})

    def test_backup_failure_does_not_break_successful_pairing(self):
        with mock.patch.object(node_linking.shutil, "copy2", side_effect=OSError("NAS backup unavailable")):
            pairing = node_linking.create_pairing_code()
            accepted = node_linking.accept_pairing(pairing["code"], {"controller_id": "controller-a"})
        self.assertTrue(accepted["token"])
        self.assertTrue(os.path.isfile(node_linking.NODE_LINK_FILE))

    def test_concurrent_node_updates_do_not_erase_each_other(self):
        threads = [
            threading.Thread(target=node_linking.save_node, args=({"id": f"node-{index}", "name": f"Node {index}"},))
            for index in range(20)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(len(node_linking.list_nodes_private()), 20)

    def test_new_controller_preserves_case_sensitive_v1_pairing_codes(self):
        responses = [
            {"protocol_version": 1, "capabilities": []},
            {"token": "worker-token", "recovery_token": "recovery", "worker_id": "legacy-worker"},
        ]
        with mock.patch.object(node_linking, "_request_json", side_effect=responses) as request_json:
            node_linking.pair_worker("http://legacy-worker:8080", "AbC_def-12")
        pairing_body = request_json.call_args_list[1].kwargs["body"]
        self.assertEqual(pairing_body["code"], "AbC_def-12")

    def test_legacy_worker_without_discovery_still_pairs(self):
        responses = [
            RuntimeError("HTTP Error 404: Not Found"),
            {"token": "worker-token", "recovery_token": "recovery", "worker_id": "legacy-worker"},
        ]
        with mock.patch.object(node_linking, "_request_json", side_effect=responses):
            paired = node_linking.pair_worker("http://legacy-worker:8080", "AbC_def-12")
        self.assertEqual(paired["id"], "legacy-worker")

    def test_unreachable_worker_reports_windows_firewall_repair(self):
        with (
            mock.patch.object(
                node_linking,
                "_request_json",
                side_effect=RuntimeError("node request failed: timed out"),
            ) as request_json,
            self.assertRaisesRegex(RuntimeError, "Allow controller access"),
        ):
            node_linking.pair_worker("http://192.168.1.50:8082", "ABCDE-FGHJK")
        self.assertEqual(request_json.call_count, 1)

    def test_valid_pairing_can_rotate_a_cloned_worker_identity_once(self):
        old_worker_id = node_linking.local_node_info()["id"]
        node_linking._mutate_state(
            lambda data: data.update({
                "trusted_controllers": {
                    "copied-controller": {
                        "id": "copied-controller",
                        "token": "copied-secret",
                    },
                },
            })
        )
        pairing = node_linking.create_pairing_code()
        request = {
            "controller_id": "new-controller",
            "controller_name": "Main",
            "request_id": "rotate-first",
            "protocol_version": 2,
            "rotate_worker_identity": True,
            "expected_worker_id": old_worker_id,
        }

        first = node_linking.accept_pairing(pairing["code"], request)
        retry = node_linking.accept_pairing(pairing["code"], {**request, "request_id": "rotate-retry"})
        state = node_linking._load_state()

        self.assertTrue(first["identity_rotated"])
        self.assertEqual(first["identity_rotated_from"], old_worker_id)
        self.assertNotEqual(first["worker_id"], old_worker_id)
        self.assertEqual(retry["worker_id"], first["worker_id"])
        self.assertTrue(retry["retry_recovered"])
        self.assertEqual(set(state["trusted_controllers"]), {"new-controller"})

    def test_pairing_second_cloned_worker_keeps_original_and_requests_new_identity(self):
        duplicated_id = "duplicated-worker-id"
        node_linking.save_node({
            "id": duplicated_id,
            "name": "ByteSqueeze Worker",
            "url": "http://worker-one:8080",
            "token": "original-token",
        })
        responses = [
            {
                "node_id": duplicated_id,
                "node_name": "ByteSqueeze Worker",
                "protocol_version": 2,
                "capabilities": ["pair-recovery"],
            },
            {
                "token": "second-token",
                "recovery_token": "second-recovery",
                "worker_id": "rotated-worker-id",
                "worker_name": "ByteSqueeze Worker",
                "protocol_version": 2,
                "identity_rotated": True,
                "identity_rotated_from": duplicated_id,
            },
        ]

        with mock.patch.object(node_linking, "_request_json", side_effect=responses) as request_json:
            second = node_linking.pair_worker("http://worker-two:8080", "ABCDE-FGHJK")

        pairing_body = request_json.call_args_list[1].kwargs["body"]
        self.assertTrue(pairing_body["rotate_worker_identity"])
        self.assertEqual(pairing_body["expected_worker_id"], duplicated_id)
        self.assertIsNotNone(node_linking.get_node_private(duplicated_id))
        self.assertIsNotNone(node_linking.get_node_private("rotated-worker-id"))
        self.assertEqual(len(node_linking.list_nodes_private()), 2)
        self.assertNotEqual(second["name"], "ByteSqueeze Worker")
        self.assertIn("worker-two", second["name"])
        self.assertTrue(second["identity_rotated"])

    def test_pairing_same_address_retains_older_worker_record(self):
        node_linking.save_node({
            "id": "older-worker",
            "name": "Older worker",
            "url": "http://shared-address:8080",
            "token": "older-token",
            "online": True,
            "status": "idle",
        })
        responses = [
            {"node_id": "new-worker", "protocol_version": 2, "capabilities": []},
            {
                "token": "new-token",
                "recovery_token": "new-recovery",
                "worker_id": "new-worker",
                "worker_name": "New worker",
                "protocol_version": 2,
            },
        ]

        with mock.patch.object(node_linking, "_request_json", side_effect=responses):
            new_worker = node_linking.pair_worker("http://shared-address:8080", "ABCDE-FGHJK")

        older = node_linking.get_node_private("older-worker")
        self.assertIsNotNone(older)
        self.assertEqual(older["name"], "Older worker")
        self.assertTrue(older["online"])
        self.assertEqual(older["address_conflict_with"], "new-worker")
        self.assertIn("retained", older["pairing_notice"])
        self.assertIn("retained", new_worker["pairing_notice"])

    def test_legacy_pair_response_cannot_overwrite_existing_worker_identity(self):
        node_linking.save_node({
            "id": "legacy-duplicate",
            "name": "Original legacy worker",
            "url": "http://legacy-one:8080",
            "token": "original-token",
        })
        responses = [
            {"protocol_version": 1, "capabilities": []},
            {
                "token": "duplicate-token",
                "recovery_token": "duplicate-recovery",
                "worker_id": "legacy-duplicate",
                "worker_name": "ByteSqueeze Worker",
                "protocol_version": 1,
            },
        ]

        with (
            mock.patch.object(node_linking, "_request_json", side_effect=responses),
            self.assertRaisesRegex(RuntimeError, "original record was retained"),
        ):
            node_linking.pair_worker("http://legacy-two:8080", "AbC_def-12")

        original = node_linking.get_node_private("legacy-duplicate")
        self.assertEqual(original["name"], "Original legacy worker")
        self.assertEqual(original["url"], "http://legacy-one:8080")
        self.assertEqual(original["token"], "original-token")

    def test_repairing_same_worker_keeps_existing_friendly_name(self):
        node_linking.save_node({
            "id": "same-worker",
            "name": "Garage Arc GPU",
            "url": "http://garage-worker:8080",
            "token": "old-token",
            "paired_at": 123.0,
        })
        responses = [
            {"node_id": "same-worker", "protocol_version": 2, "capabilities": []},
            {
                "token": "new-token",
                "recovery_token": "new-recovery",
                "worker_id": "same-worker",
                "worker_name": "ByteSqueeze Worker",
                "protocol_version": 2,
            },
        ]

        with mock.patch.object(node_linking, "_request_json", side_effect=responses):
            repaired = node_linking.pair_worker("http://garage-worker:8080", "ABCDE-FGHJK")

        self.assertEqual(repaired["name"], "Garage Arc GPU")
        self.assertEqual(repaired["worker_reported_name"], "ByteSqueeze Worker")
        self.assertEqual(node_linking.get_node_private("same-worker")["paired_at"], 123.0)

    def test_controller_alias_is_persistent_and_unique(self):
        node_linking.save_node({
            "id": "worker-one",
            "name": "ByteSqueeze Worker",
            "worker_reported_name": "ByteSqueeze Worker",
        })
        node_linking.save_node({
            "id": "worker-two",
            "name": "ByteSqueeze Worker",
            "worker_reported_name": "ByteSqueeze Worker",
        })

        renamed = node_linking.rename_node("worker-one", "  Garage   Arc GPU  ")

        self.assertEqual(renamed["name"], "Garage Arc GPU")
        self.assertEqual(renamed["worker_reported_name"], "ByteSqueeze Worker")
        self.assertEqual(renamed["name_source"], "controller")
        with self.assertRaisesRegex(ValueError, "already uses"):
            node_linking.rename_node("worker-two", "garage arc gpu")
        with self.assertRaisesRegex(ValueError, "required"):
            node_linking.rename_node("worker-two", "   ")

    def test_headless_discovery_requires_remote_transfer(self):
        with mock.patch.dict(os.environ, {"TSD_WORKER_MODE": "1"}):
            discovery = node_linking.node_discovery()

        self.assertEqual(discovery["worker_mode"], "headless")
        self.assertTrue(discovery["requires_remote_transfer"])
        self.assertEqual(discovery["recommended_transfer_mode"], "remote")
        self.assertIn("remote-transfer-only", discovery["capabilities"])
        self.assertIn("controller-encoding-policy", discovery["capabilities"])
        self.assertIn("gpu-multi-encode", discovery["capabilities"])
        self.assertIn("cpu-software-exclusive", discovery["capabilities"])
        self.assertIn("hardware-profile", discovery["capabilities"])
        self.assertIn("hardware", discovery)

    def test_encoder_hardware_profile_reports_available_gpu_families(self):
        def fake_glob(pattern):
            if pattern == "/dev/dri/renderD*":
                return ["/dev/dri/renderD128"]
            if pattern == "/sys/class/drm/renderD*/device/vendor":
                return ["/sys/class/drm/renderD128/device/vendor"]
            if pattern == "/dev/nvidia[0-9]*":
                return ["/dev/nvidia0"]
            return []

        with (
            mock.patch.object(node_linking.glob, "glob", side_effect=fake_glob),
            mock.patch.object(node_linking, "_read_gpu_vendor", return_value="intel"),
            mock.patch.object(node_linking.shutil, "which", return_value=None),
        ):
            profile = node_linking.encoder_hardware_profile(force=True)

        self.assertEqual(profile["encoder_families"], ["qsv", "nvenc", "software"])
        self.assertIn("qsv_h265_10bit", profile["encoders"])
        self.assertIn("nvenc_h265_10bit", profile["encoders"])
        self.assertIn("x265_10bit", profile["encoders"])

    def test_headless_worker_forces_remote_mode_and_drops_path_mappings(self):
        responses = [
            {
                "protocol_version": 2,
                "capabilities": ["headless-worker", "remote-transfer-only"],
                "worker_mode": "headless",
                "requires_remote_transfer": True,
            },
            {
                "token": "worker-token",
                "recovery_token": "recovery-token",
                "worker_id": "headless-worker",
                "worker_name": "Garage worker",
                "protocol_version": 2,
                "worker_mode": "headless",
                "requires_remote_transfer": True,
            },
        ]
        with mock.patch.object(node_linking, "_request_json", side_effect=responses):
            worker = node_linking.pair_worker(
                "http://worker:8080",
                "ABCDE-FGHJK",
                transfer_mode="local",
                path_mappings=[{"controller": "/media", "worker": "/mnt/media"}],
                controller_url="http://controller:8080",
                hardware_transcode_concurrency=4,
            )

        self.assertEqual(worker["worker_mode"], "headless")
        self.assertTrue(worker["requires_remote_transfer"])
        self.assertEqual(worker["transfer_mode"], "remote")
        self.assertEqual(worker["path_mappings"], [])
        self.assertEqual(worker["hardware_transcode_concurrency"], 4)


class MobilePairingProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_mobile_file = mobile_linking.MOBILE_STATE_FILE
        mobile_linking.MOBILE_STATE_FILE = os.path.join(self.tempdir.name, "mobile_devices.json")

    def tearDown(self):
        mobile_linking.MOBILE_STATE_FILE = self.original_mobile_file
        self.tempdir.cleanup()

    def test_mobile_pairing_refresh_and_revoke(self):
        pairing = mobile_linking.create_mobile_pairing(scope="control")
        first = mobile_linking.accept_mobile_pairing(
            pairing["code"],
            {"device_id": "phone-a", "device_name": "Kevin's phone", "platform": "android"},
        )
        retry = mobile_linking.accept_mobile_pairing(
            pairing["code"],
            {"device_id": "phone-a", "device_name": "Kevin's phone", "platform": "android"},
        )
        self.assertTrue(retry["retry_recovered"])
        self.assertIsNone(mobile_linking.authenticate_mobile_token(first["access_token"]))
        self.assertEqual(mobile_linking.authenticate_mobile_token(retry["access_token"])["id"], "phone-a")

        refreshed = mobile_linking.refresh_mobile_token("phone-a", retry["refresh_token"])
        self.assertIsNone(mobile_linking.authenticate_mobile_token(retry["access_token"]))
        self.assertEqual(mobile_linking.authenticate_mobile_token(refreshed["access_token"])["scope"], "control")

        self.assertTrue(mobile_linking.revoke_mobile_device("phone-a"))
        self.assertIsNone(mobile_linking.authenticate_mobile_token(refreshed["access_token"]))

    def test_read_only_device_cannot_get_control_scope(self):
        pairing = mobile_linking.create_mobile_pairing(scope="read")
        credentials = mobile_linking.accept_mobile_pairing(pairing["code"], {"device_id": "tablet-a"})
        self.assertIsNotNone(mobile_linking.authenticate_mobile_token(credentials["access_token"], required_scope="read"))
        self.assertIsNone(mobile_linking.authenticate_mobile_token(credentials["access_token"], required_scope="control"))

    def test_forgotten_and_inactive_mobile_devices_can_be_cleared(self):
        first_pairing = mobile_linking.create_mobile_pairing(scope="control")
        first = mobile_linking.accept_mobile_pairing(first_pairing["code"], {"device_id": "phone-old"})
        self.assertTrue(mobile_linking.revoke_mobile_device(first["device_id"]))
        self.assertEqual(mobile_linking.clear_mobile_devices(inactive_only=True), 1)
        self.assertEqual(mobile_linking.list_mobile_devices(), [])

        second_pairing = mobile_linking.create_mobile_pairing(scope="control")
        second = mobile_linking.accept_mobile_pairing(second_pairing["code"], {"device_id": "phone-forget"})
        self.assertTrue(mobile_linking.forget_mobile_device(second["device_id"]))
        self.assertFalse(mobile_linking.forget_mobile_device(second["device_id"]))
        self.assertIsNone(mobile_linking.authenticate_mobile_token(second["access_token"]))


if __name__ == "__main__":
    unittest.main()
