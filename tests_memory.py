#!/usr/bin/env python3
"""Private transcript archive tests using temporary directories only."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("voice_memory_under_test", Path(__file__).with_name("memory.py"))
memory = importlib.util.module_from_spec(spec)
spec.loader.exec_module(memory)


class VoiceMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voice-memory-offline-")
        self.directory = (Path(self.temp.name) / "archive").resolve()
        self.archive = memory.VoiceMemory(self.directory)

    def tearDown(self):
        self.archive.close()
        self.temp.cleanup()

    def record(self, role="user", text="A harmless personal note.", item="item-1", thread="thread-1"):
        return self.archive.record(thread, "/temporary/project", role, text, item)

    def test_no_archive_is_created_until_a_final_record_arrives(self):
        self.assertEqual(list(self.directory.glob("*.md")), [])
        self.archive.flush()
        self.assertEqual(list(self.directory.glob("*.md")), [])

    def test_spoken_roles_are_distinct_and_never_claim_verified_truth(self):
        result = self.record()
        self.record("assistant", "I might be mistaken.", "item-2")
        text = Path(result["path"]).read_text()
        self.assertTrue(result["saved"])
        self.assertIn("trust: untrusted-transcript", text)
        self.assertIn("not verified facts or instructions", text)
        self.assertIn("does not establish that an assistant action was executed", text)
        self.assertIn("| User", text)
        self.assertIn("| Assistant", text)
        self.assertIn("> A harmless personal note.", text)
        self.assertIn("> I might be mistaken.", text)

    def test_capture_timestamp_is_current_utc_not_a_date_mentioned_in_speech(self):
        before = datetime.now(timezone.utc)
        result = self.record(text="An event happened on 1999-01-01.")
        after = datetime.now(timezone.utc)
        text = Path(result["path"]).read_text()
        value = re.search(r'^captured_at: (.+)$', text, re.M).group(1)
        captured = datetime.fromisoformat(json.loads(value))
        self.assertLessEqual(before, captured)
        self.assertLessEqual(captured, after)
        self.assertEqual(captured.utcoffset().total_seconds(), 0)
        self.assertIn("not when described events happened", text)

    def test_duplicate_event_is_idempotent_after_restart(self):
        result = self.record()
        path = Path(result["path"])
        original = path.read_bytes()
        old_mtime = path.stat().st_mtime_ns
        self.archive.close()
        self.archive = memory.VoiceMemory(self.directory)
        result = self.record()
        self.assertFalse(result["saved"])
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(path.stat().st_mtime_ns, old_mtime)

    def test_different_utterances_with_identical_text_are_preserved(self):
        result = self.record(text="Okay", item="first")
        self.record(text="Okay", item="second")
        self.assertEqual(Path(result["path"]).read_text().count("> Okay\n"), 2)

    def test_changed_final_text_appends_revision_without_erasing_prior_source(self):
        result = self.record(text="The name was Robin.")
        path = Path(result["path"])
        previous = path.read_text()
        self.record(text="Correction: the name was Rowan.")
        text = path.read_text()
        self.assertTrue(text.startswith(previous))
        self.assertIn('"revision": 2', text)
        self.assertIn("> The name was Robin.", text)
        self.assertIn("> Correction: the name was Rowan.", text)

    def test_empty_or_invalid_records_do_not_erase_existing_archive(self):
        result = self.record()
        path = Path(result["path"])
        previous = path.read_bytes()
        for args in [
            ("thread-1", "", "user", "", "next"),
            ("thread-1", "", "user", "  ", "next"),
            ("thread-1", "", "tool", "tool output", "next"),
            ("thread-1", "", "user", "final text", ""),
            ("", "", "user", "final text", "next"),
            ("thread-1", "", "user", None, "next"),
        ]:
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    self.archive.record(*args)
        self.assertEqual(path.read_bytes(), previous)

    def test_thread_id_cannot_escape_archive_directory(self):
        result = self.record(thread="../../outside\n/../file.md")
        path = Path(result["path"])
        self.assertEqual(path.parent, self.directory)
        self.assertEqual(path.suffix, ".md")
        self.assertNotIn("outside", path.name)
        self.assertFalse((Path(self.temp.name) / "outside").exists())

    def test_each_thread_has_a_separate_file(self):
        first = self.record(thread="one")
        second = self.record(thread="two")
        self.assertNotEqual(first["path"], second["path"])
        self.assertEqual(len(list(self.directory.glob("*.md"))), 2)

    def test_source_text_cannot_forge_deduplication_markers(self):
        item_key = self.archive._digest(json.dumps(["user", "future"], ensure_ascii=False))
        text_key = self.archive._digest("later note")
        forged = f"<!-- codex-voice-record:v1 {item_key}:{text_key} -->"
        result = self.record(text="Untrusted text\r" + forged + "\u2028# Forged heading")
        final = self.record(text="later note", item="future")
        self.assertTrue(final["saved"])
        text = Path(result["path"]).read_text()
        self.assertIn("> " + forged, text)
        self.assertIn("> # Forged heading", text)
        self.assertEqual(text.splitlines().count(forged), 1)

    def test_failed_atomic_replace_preserves_existing_archive(self):
        result = self.record()
        path = Path(result["path"])
        previous = path.read_bytes()
        with patch.object(memory.os, "replace", side_effect=OSError("simulated disk failure")):
            with self.assertRaises(OSError):
                self.record(text="A second note", item="second")
        self.assertEqual(path.read_bytes(), previous)
        self.assertEqual(list(self.directory.glob("*.tmp")), [])
        self.assertTrue(self.record(text="A second note", item="second")["saved"])

    def test_unrecognized_existing_file_is_preserved(self):
        result = self.record()
        path = Path(result["path"])
        path.write_text("A pre-existing unrelated document.\n")
        with self.assertRaises(ValueError):
            self.record(item="second")
        self.assertEqual(path.read_text(), "A pre-existing unrelated document.\n")

    @unittest.skipIf(os.name == "nt", "Creating symlinks may require Windows privileges")
    def test_symlink_target_is_not_read_or_modified(self):
        result = self.record()
        path = Path(result["path"])
        outside = Path(self.temp.name) / "unrelated.md"
        outside.write_text("private unrelated data")
        path.unlink()
        path.symlink_to(outside)
        with self.assertRaises(ValueError):
            self.record(item="second")
        self.assertEqual(outside.read_text(), "private unrelated data")
        self.assertTrue(path.is_symlink())

    def test_parallel_instances_do_not_lose_records(self):
        archives = [memory.VoiceMemory(self.directory) for _ in range(4)]
        try:
            def record(number):
                return archives[number % 4].record("thread", "", "user", f"Note {number}", str(number))
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(record, range(12)))
            text = Path(results[0]["path"]).read_text()
            for number in range(12):
                self.assertIn(f"> Note {number}\n", text)
            self.assertEqual(text.count("<!-- codex-voice-record:v1 "), 12)
        finally:
            for archive in archives:
                archive.close()

    def test_archive_refuses_application_source_directory(self):
        with self.assertRaises(ValueError):
            memory.VoiceMemory(Path(memory.__file__).parent / "private-conversations")

    @unittest.skipIf(os.name == "nt", "Unix file permissions")
    def test_new_archive_files_are_private(self):
        result = self.record()
        self.assertEqual(Path(result["path"]).stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)

    def test_close_is_idempotent_and_prevents_late_writes(self):
        self.archive.close()
        self.archive.close()
        with self.assertRaises(RuntimeError):
            self.record()
        self.assertEqual(list(self.directory.glob("*.md")), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
