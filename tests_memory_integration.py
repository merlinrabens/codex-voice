#!/usr/bin/env python3
"""Offline protocol-to-private-archive tests. No model, audio, or indexer runs."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests import FakeProcess, voice


class MemoryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voice-memory-integration-")
        self.directory = Path(self.temp.name) / "archive"
        self.archive = voice.VoiceMemory(self.directory)
        self.process = FakeProcess()
        with patch.object(voice.subprocess, "Popen", return_value=self.process):
            self.codex = voice.Codex(self.temp.name, memory=self.archive)
        self.codex.state.update(threadId="selected-thread", activeTurnId=None)
        self.codex.known_threads["selected-thread"] = self.temp.name

    def tearDown(self):
        self.codex.state["voiceActive"] = False
        self.codex.flush_memory()
        self.codex.close()
        self.archive.close()
        self.temp.cleanup()

    def inject(self, method, params):
        self.process.stdout.queue.put({"method": method, "params": params})
        self.codex.rpc("test/barrier", {})
        self.codex.memory_queue.join()

    def transcript(self, role="user", text="A final spoken note.", item="voice-1", thread="selected-thread"):
        self.inject("thread/realtime/transcript/done", {
            "threadId": thread, "role": role, "text": text, "itemId": item,
        })

    def archived(self):
        return "\n".join(path.read_text() for path in self.directory.glob("*.md"))

    def test_final_spoken_user_and_assistant_transcripts_are_archived(self):
        self.transcript()
        self.transcript("assistant", "A final spoken reply.", "voice-2")
        text = self.archived()
        self.assertIn("> A final spoken note.", text)
        self.assertIn("> A final spoken reply.", text)
        self.assertIn("| User", text)
        self.assertIn("| Assistant", text)
        self.assertEqual(self.codex.snapshot()["memoryStatus"], "saved")

    def test_partial_transcript_deltas_are_not_archived(self):
        self.inject("thread/realtime/transcript/delta", {
            "threadId": "selected-thread", "role": "user", "delta": "unfinished phrase",
        })
        self.assertEqual(self.archived(), "")
        self.transcript(text="The complete final phrase.")
        self.assertNotIn("unfinished phrase", self.archived())
        self.assertIn("The complete final phrase.", self.archived())

    def test_completed_agent_message_archived_once(self):
        event = {"threadId": "selected-thread", "item": {
            "type": "agentMessage", "id": "answer-1", "text": "Completed task result.",
        }}
        self.inject("item/completed", event)
        self.inject("item/completed", event)
        self.assertEqual(self.archived().count("> Completed task result."), 1)

    def test_tool_output_and_agent_message_deltas_are_not_archived(self):
        self.inject("item/agentMessage/delta", {
            "threadId": "selected-thread", "itemId": "answer-1", "delta": "partial answer",
        })
        self.inject("item/completed", {"threadId": "selected-thread", "item": {
            "type": "commandExecution", "id": "command-1", "text": "command output",
        }})
        self.assertEqual(self.archived(), "")

    def test_successfully_submitted_typed_text_is_archived(self):
        self.codex.text({"text": "A typed personal note."})
        self.codex.memory_queue.join()
        self.assertIn("> A typed personal note.", self.archived())
        self.assertTrue(any(item.get("method") == "turn/start" for item in self.process.sent))

    def test_failed_typed_submission_is_not_recorded_as_sent(self):
        with patch.object(self.codex, "rpc", side_effect=voice.RequestError("offline failure")):
            with self.assertRaises(voice.RequestError):
                self.codex.text({"text": "Not submitted."})
        self.codex.memory_queue.join()
        self.assertEqual(self.archived(), "")

    def test_foreign_thread_events_cannot_enter_selected_archive(self):
        self.codex.known_threads["previous-thread"] = self.temp.name
        for thread in ["unknown-thread", "previous-thread"]:
            self.transcript(text="Unrelated conversation.", thread=thread)
            self.inject("item/completed", {"threadId": thread, "item": {
                "type": "agentMessage", "id": "answer", "text": "Unrelated task result.",
            }})
        self.assertEqual(self.archived(), "")

    def test_session_registration_enables_capture_for_returned_thread(self):
        self.process.results["thread/start"] = {"thread": {"id": "new-thread"}}
        self.codex.session({"cwd": self.temp.name})
        self.transcript(text="The new session.", thread="new-thread")
        self.assertIn("The new session.", self.archived())
        self.assertEqual(self.codex.known_threads["new-thread"], str(Path(self.temp.name).resolve()))

    def test_stop_returns_after_pending_transcript_has_been_flushed(self):
        self.codex.state.update(voiceActive=True, voiceStatus="connected")
        self.codex.capture("user", "Saved before stop returns.", "pending")
        with patch.object(self.archive, "flush", wraps=self.archive.flush) as flush:
            result = self.codex.stop_voice()
        self.assertTrue(flush.called)
        self.assertFalse(result["voiceActive"])
        self.assertIn("Saved before stop returns.", self.archived())
        self.assertEqual(self.codex.memory_queue.unfinished_tasks, 0)

    def test_close_drains_pending_records_and_stops_the_archive_worker(self):
        self.codex.capture("user", "Preserved on shutdown.", "shutdown-record")
        self.codex.close()
        self.assertIn("Preserved on shutdown.", self.archived())
        self.assertFalse(self.codex.reader_thread.is_alive())
        self.assertFalse(self.codex.memory_thread.is_alive())
        self.assertEqual(self.codex.memory_queue.unfinished_tasks, 0)
        with self.assertRaises(RuntimeError):
            self.archive.record("selected-thread", "", "user", "Too late", "late")

    def test_write_failure_is_visible_and_flush_retries_the_original_record(self):
        with patch.object(self.archive, "record", side_effect=OSError("simulated full disk")):
            self.transcript(text="Must survive a retry.")
        self.assertEqual(self.codex.snapshot()["memoryStatus"], "error")
        self.assertTrue(any(event["method"] == "state" and event["params"].get("memoryStatus") == "error"
                            for _, event in self.codex.events))
        self.assertEqual(len(self.codex.memory_failed), 1)
        self.assertEqual(self.archived(), "")
        self.codex.flush_memory()
        self.assertEqual(self.codex.memory_failed, [])
        self.assertEqual(self.codex.snapshot()["memoryStatus"], "saved")
        self.assertEqual(self.archived().count("> Must survive a retry."), 1)

    def test_invalid_or_empty_final_transcripts_do_not_become_records(self):
        for role, text in [("tool", "not speech"), ("user", ""), ("assistant", None)]:
            self.transcript(role=role, text=text)
        self.assertEqual(self.archived(), "")

    def test_browser_fallback_requires_known_thread_and_stable_event_id(self):
        valid = {"threadId": "selected-thread", "role": "user", "text": "Fallback final.", "itemId": "fallback-1"}
        for invalid in [{**valid, "threadId": "foreign"}, {**valid, "itemId": ""}, {**valid, "role": "tool"}]:
            with self.assertRaises(voice.RequestError):
                self.codex.save_transcript(invalid)
        self.assertEqual(self.archived(), "")
        self.assertTrue(self.codex.save_transcript(valid)["saved"])
        self.assertIn("Fallback final.", self.archived())

    def test_browser_fallback_write_failure_is_not_acknowledged_as_saved(self):
        with patch.object(self.archive, "record", side_effect=OSError("simulated full disk")):
            with self.assertRaises(OSError):
                self.codex.save_transcript({"threadId": "selected-thread", "role": "user", "text": "Retry this.", "itemId": "fallback-1"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
