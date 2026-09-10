#!/usr/bin/env python3
"""Offline pairing-recovery tests using temporary local sockets and test credentials."""
import json
import os
from pathlib import Path
import socket
import stat
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import control
from tests_remote import voice


class LocalPairingControlTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="cv-control-", dir="/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "control.sock"
        self.receipt = self.root / "connection.json"
        self.access = voice.RemoteAccess("https://voice.example.test")
        self.renewals = []

    def write_receipt(self, code):
        connection = {"origin": self.access.origin, "pairing_url": self.access.origin + "/#pair=" + code}
        control.write_private_json(self.receipt, connection)
        self.renewals.append(connection)
        return connection

    def start_control(self, renew=None):
        if renew is None:
            renew = lambda: self.access.renew_pairing(self.write_receipt)
        service = control.LocalPairingControl(self.path, renew)
        self.addCleanup(service.close)
        service.start()
        return service

    def raw_request(self, payload):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(str(self.path))
            client.sendall(payload)
            if not payload.endswith(b"\n"):
                client.shutdown(socket.SHUT_WR)
            with client.makefile("rb") as stream:
                return json.loads(stream.readline(16385))

    def test_socket_and_receipt_are_private(self):
        self.start_control()
        connection = control.request_pairing(self.path)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        self.assertTrue(stat.S_ISSOCK(self.path.stat().st_mode))
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.receipt.stat().st_mode), 0o600)
        self.assertEqual(json.loads(self.receipt.read_text()), connection)

    def test_rotation_preserves_cookie_and_replaces_pending_one_use_code(self):
        token = self.access.pair(self.access.pairing_code, remember=True)
        cookie = self.access.cookie_name + "=" + token
        self.access.renew_pairing(self.write_receipt)
        old_code = self.access.pairing_code
        self.start_control()
        connection = control.request_pairing(self.path)
        new_code = connection["pairing_url"].split("#pair=", 1)[1]
        self.assertNotEqual(new_code, old_code)
        self.assertTrue(self.access.valid(cookie))
        self.assertIsNone(self.access.pair(old_code))
        self.assertIsNotNone(self.access.pair(new_code))
        self.assertIsNone(self.access.pair(new_code))
        self.assertTrue(self.access.valid(cookie))
        self.assertEqual(len(self.access.sessions), 2)

    def test_rotation_refreshes_deadline_and_clears_failed_attempt_window(self):
        self.access.pairing_expires = time.monotonic() - 1
        self.access.attempts.extend([time.monotonic()] * 12)
        self.start_control()
        before = time.monotonic()
        control.request_pairing(self.path)
        self.assertGreaterEqual(self.access.pairing_expires, before + 1800)
        self.assertEqual(list(self.access.attempts), [])
        self.assertIsNotNone(self.access.pair(self.access.pairing_code))

    def test_failed_receipt_write_preserves_previous_invitation(self):
        old_code = self.access.pairing_code
        self.write_receipt(old_code)
        original = self.receipt.read_bytes()

        def fail_write(_code):
            raise OSError("Offline test disk failure")

        self.start_control(lambda: self.access.renew_pairing(fail_write))
        with self.assertRaisesRegex(RuntimeError, "could not be renewed"):
            control.request_pairing(self.path)
        self.assertEqual(self.receipt.read_bytes(), original)
        self.assertEqual(self.access.pairing_code, old_code)
        self.assertIsNotNone(self.access.pair(old_code))

    def test_requests_must_be_bounded_complete_and_exact(self):
        self.start_control()
        original = self.access.pairing_code
        for payload in [b"x" * 1025 + b"\n", b'{"action":"pair"}', b"[]\n", b"{\n",
                        b'{"action":"other"}\n', b'{"action":"pair","extra":true}\n']:
            with self.subTest(payload_length=len(payload)):
                response = self.raw_request(payload)
                self.assertFalse(response["ok"])
                self.assertNotIn("connection", response)
                self.assertEqual(self.access.pairing_code, original)
        self.assertEqual(self.renewals, [])
        self.assertTrue(self.raw_request(b'{"action":"pair"}\n')["ok"])

    def test_insecure_parent_is_rejected_without_changing_permissions(self):
        self.root.chmod(0o755)
        with self.assertRaises(ValueError):
            control.LocalPairingControl(self.path, lambda: {})
        with self.assertRaises(ValueError):
            control.write_private_json(self.receipt, {})
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o755)
        self.assertFalse(self.path.exists())
        self.assertFalse(self.receipt.exists())

    def test_symlink_parent_is_rejected(self):
        target = self.root / "target"
        target.mkdir(mode=0o700)
        link = self.root / "linked"
        link.symlink_to(target, target_is_directory=True)
        with self.assertRaises(ValueError):
            control.LocalPairingControl(link / "control.sock", lambda: {})
        self.assertFalse((target / "control.sock").exists())

    def test_socket_path_symlink_and_regular_file_are_preserved(self):
        self.path.write_text("Unrelated file")
        with self.assertRaises(ValueError):
            control.LocalPairingControl(self.path, lambda: {})
        self.assertEqual(self.path.read_text(), "Unrelated file")
        self.path.unlink()
        target = self.root / "target.txt"
        target.write_text("Unrelated target")
        self.path.symlink_to(target)
        with self.assertRaises(ValueError):
            control.LocalPairingControl(self.path, lambda: {})
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(target.read_text(), "Unrelated target")

    def test_foreign_socket_owner_is_rejected(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(self.path))
        original_lstat = Path.lstat

        def foreign_lstat(path):
            info = original_lstat(path)
            if path == self.path:
                fields = list(info)
                fields[4] = os.getuid() + 1
                return os.stat_result(fields)
            return info

        with patch.object(Path, "lstat", autospec=True, side_effect=foreign_lstat):
            with self.assertRaises(ValueError):
                control.LocalPairingControl(self.path, lambda: {})
            with self.assertRaises(ValueError):
                control.request_pairing(self.path)
        self.assertTrue(self.path.exists())

    def test_live_owned_socket_is_not_replaced(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(self.path))
            listener.listen(1)
            inode = self.path.stat().st_ino
            with self.assertRaisesRegex(RuntimeError, "already running"):
                control.LocalPairingControl(self.path, lambda: {})
            self.assertEqual(self.path.stat().st_ino, inode)

    def test_stale_owned_socket_is_recovered(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(self.path))
        self.start_control()
        self.assertEqual(control.request_pairing(self.path)["origin"], self.access.origin)

    def test_client_rejects_public_socket(self):
        self.start_control()
        self.path.chmod(0o666)
        with self.assertRaises(ValueError):
            control.request_pairing(self.path)
        self.assertEqual(self.renewals, [])

    def test_close_does_not_unlink_replacement_file(self):
        service = self.start_control()
        self.path.unlink()
        self.path.write_text("Replacement owned by another operation")
        service.close()
        self.assertEqual(self.path.read_text(), "Replacement owned by another operation")

    def test_receipt_writer_rejects_symlink_and_preserves_target(self):
        target = self.root / "target.json"
        target.write_text('{"existing":true}')
        self.receipt.symlink_to(target)
        with self.assertRaises(ValueError):
            control.write_private_json(self.receipt, {"replacement": True})
        self.assertTrue(self.receipt.is_symlink())
        self.assertEqual(target.read_text(), '{"existing":true}')

    def test_receipt_serialization_failure_preserves_original_and_cleans_temporary_file(self):
        self.receipt.write_text('{"existing":true}')
        with self.assertRaises(TypeError):
            control.write_private_json(self.receipt, {"invalid": object()})
        self.assertEqual(self.receipt.read_text(), '{"existing":true}')
        self.assertEqual(list(self.root.glob(".pairing-*")), [])

    def test_receipt_file_sync_failure_preserves_original_invitation(self):
        old_code = self.access.pairing_code
        self.write_receipt(old_code)
        original = self.receipt.read_bytes()
        with patch.object(control.os, "fsync", side_effect=OSError("Offline test file sync failure")):
            with self.assertRaises(OSError):
                self.access.renew_pairing(self.write_receipt)
        self.assertEqual(self.access.pairing_code, old_code)
        self.assertEqual(self.receipt.read_bytes(), original)
        self.assertEqual(list(self.root.glob(".pairing-*")), [])

    def test_post_replace_directory_sync_failure_keeps_visible_invitation_usable(self):
        original_fsync = os.fsync

        def fail_directory_sync(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("Offline test directory sync failure")
            return original_fsync(fd)

        with patch.object(control.os, "fsync", side_effect=fail_directory_sync):
            self.access.renew_pairing(self.write_receipt)
        connection = json.loads(self.receipt.read_text())
        code = connection["pairing_url"].split("#pair=", 1)[1]
        self.assertEqual(self.access.pairing_code, code)
        self.assertIsNotNone(self.access.pair(code))

    def test_receipt_replacement_does_not_modify_existing_hard_link(self):
        self.receipt.write_text('{"existing":true}')
        original = self.root / "original.json"
        os.link(self.receipt, original)
        control.write_private_json(self.receipt, {"replacement": True})
        self.assertEqual(json.loads(self.receipt.read_text()), {"replacement": True})
        self.assertEqual(original.read_text(), '{"existing":true}')
        self.assertEqual(stat.S_IMODE(self.receipt.stat().st_mode), 0o600)

    def test_client_rejects_malformed_incomplete_and_oversized_responses(self):
        payloads = [b"[]\n", b"null\n", b"{\n", b"x" * 16385 + b"\n",
                    b'{"ok":true,"connection":{"pairing_url":"test"}}',
                    b'{"ok":true,"connection":[]}\n']
        for index, payload in enumerate(payloads):
            with self.subTest(response_index=index):
                path = self.root / f"response-{index}.sock"
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                    listener.bind(str(path))
                    path.chmod(0o600)
                    listener.listen(1)

                    def respond():
                        client, _ = listener.accept()
                        with client:
                            client.settimeout(2)
                            client.recv(1024)
                            try:
                                client.sendall(payload)
                            except OSError:
                                pass

                    thread = threading.Thread(target=respond, daemon=True)
                    thread.start()
                    try:
                        with self.assertRaises((RuntimeError, ValueError)):
                            control.request_pairing(path)
                    finally:
                        thread.join(timeout=3)
                    self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main(verbosity=2)
