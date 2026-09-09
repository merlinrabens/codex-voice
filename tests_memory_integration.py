#!/usr/bin/env python3
"""Offline protocol-to-private-archive tests. No model, audio, or indexer runs."""
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from tests import FakeProcess, voice


class ControlledIndexer:
    """A subprocess substitute whose completion is controlled by the test."""
    def __init__(self):
        self.returncode = None

    def poll(self):
        return self.returncode

    def wait(self):
        return self.returncode


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
        self.index_processes = []
        self.index_teardown = False

    def tearDown(self):
        self.index_teardown = True
        for process in self.index_processes:
            process.returncode = 0
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

    def thread_archive(self, thread):
        path = self.directory / f"Codex Voice {self.archive._digest(thread)}.md"
        return path.read_text() if path.exists() else ""

    def enable_indexer(self, startup_failures=0):
        def launch(*args, **kwargs):
            nonlocal startup_failures
            if startup_failures:
                startup_failures -= 1
                raise OSError("Simulated indexer startup failure")
            process = ControlledIndexer()
            if self.index_teardown:
                process.returncode = 0
            self.index_processes.append(process)
            return process
        patcher = patch.object(voice.subprocess, "Popen", side_effect=launch)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.codex.memory_index_command = ["offline-indexer"]
        self.codex.memory_index_retry_seconds = 0.02
        self.codex.memory_index_coalesce_seconds = 0.01
        self.codex.state["memoryIndexStatus"] = "ready"
        self.codex.memory_index_thread = threading.Thread(target=self.codex.memory_index_worker, daemon=True)
        self.codex.memory_index_thread.start()

    def wait_for(self, predicate):
        with self.codex.condition:
            self.assertTrue(self.codex.condition.wait_for(predicate, timeout=3), "Background work did not complete")

    def finish_index(self, index, returncode=0):
        with self.codex.condition:
            self.index_processes[index].returncode = returncode
            self.codex.condition.notify_all()

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

    def test_unknown_thread_and_subagent_events_cannot_enter_archive(self):
        for thread in ["unknown-thread", "subagent-thread"]:
            self.transcript(text="Unrelated conversation.", thread=thread)
            self.inject("item/completed", {"threadId": thread, "item": {
                "type": "agentMessage", "id": "answer", "text": "Unrelated task result.",
            }})
        self.assertEqual(self.archived(), "")

    def test_late_previous_thread_finals_are_kept_separate_from_current_session(self):
        self.transcript(text="First conversation begins.")
        self.codex.stop_voice()
        self.process.results["thread/start"] = {"thread": {"id": "new-thread"}}
        self.codex.session({"cwd": self.temp.name})
        self.transcript(text="Second conversation begins.", thread="new-thread", item="new-voice")
        self.transcript(text="Late first conversation decision.", item="late-first")
        self.inject("item/completed", {"threadId": "selected-thread", "item": {
            "type": "agentMessage", "id": "late-answer", "text": "Late first task result.",
        }})
        first = self.thread_archive("selected-thread")
        second = self.thread_archive("new-thread")
        self.assertIn("Late first conversation decision.", first)
        self.assertIn("Late first task result.", first)
        self.assertNotIn("Late first", second)
        self.assertIn("Second conversation begins.", second)
        self.assertNotIn("Second conversation", first)
        self.assertEqual(self.codex.state["threadId"], "new-thread")
        self.assertFalse(any(event["params"].get("text", "").startswith("Late first")
                             for _, event in self.codex.events))

    def test_final_events_without_original_thread_are_not_misattributed(self):
        self.inject("thread/realtime/transcript/done", {"role": "user", "text": "No source thread."})
        self.inject("item/completed", {"item": {
            "type": "agentMessage", "id": "missing-thread", "text": "No source thread.",
        }})
        self.assertEqual(self.archived(), "")

    def test_resuming_thread_appends_to_the_same_archive(self):
        self.transcript(text="Before resuming.")
        self.process.results["thread/resume"] = {"thread": {"id": "selected-thread"}}
        self.codex.session({"cwd": self.temp.name, "threadId": "selected-thread"})
        self.transcript(text="After resuming.", item="after-resume")
        self.assertEqual(len(list(self.directory.glob("*.md"))), 1)
        self.assertIn("Before resuming.", self.archived())
        self.assertIn("After resuming.", self.archived())

    def test_typed_submission_keeps_original_thread_if_session_changes_during_rpc(self):
        self.codex.known_threads["next-thread"] = self.temp.name
        def switch_thread(method, params):
            self.codex.state["threadId"] = "next-thread"
            return {}
        with patch.object(self.codex, "rpc", side_effect=switch_thread):
            self.codex.text({"text": "Submitted to the original thread."})
        self.codex.memory_queue.join()
        self.assertIn("Submitted to the original thread.", self.thread_archive("selected-thread"))
        self.assertEqual(self.thread_archive("next-thread"), "")

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
        self.assertEqual(self.codex.snapshot()["memoryStatus"], "error")

    def test_two_rapid_stops_coalesce_index_work_without_losing_second_conversation(self):
        self.enable_indexer()
        self.codex.state.update(voiceActive=True, voiceStatus="connected")
        self.transcript(text="First conversation to index.")
        self.codex.stop_voice()
        self.wait_for(lambda: len(self.index_processes) == 1)
        self.process.results["thread/start"] = {"thread": {"id": "second-thread"}}
        self.codex.session({"cwd": self.temp.name})
        self.codex.state.update(voiceActive=True, voiceStatus="connected")
        self.transcript(text="Second conversation to index.", thread="second-thread")
        self.codex.stop_voice()
        self.assertEqual(len(self.index_processes), 1)
        self.assertTrue(self.codex.memory_index_dirty)
        # A new live conversation must not suppress the pending follow-up.
        self.codex.state["voiceActive"] = True
        self.finish_index(0)
        self.wait_for(lambda: len(self.index_processes) == 2)
        self.finish_index(1)
        self.wait_for(lambda: self.codex.state.get("memoryIndexStatus") == "indexed")
        self.assertFalse(self.codex.memory_index_dirty)
        self.assertIn("First conversation to index.", self.thread_archive("selected-thread"))
        self.assertIn("Second conversation to index.", self.thread_archive("second-thread"))

    def test_late_final_after_voice_stops_triggers_index_without_another_stop(self):
        self.enable_indexer()
        self.codex.stop_voice()
        self.transcript(text="Final transcript delivered after stop returned.")
        self.wait_for(lambda: len(self.index_processes) == 1)
        self.finish_index(0)
        self.wait_for(lambda: self.codex.state.get("memoryIndexStatus") == "indexed")

    def test_late_previous_thread_final_indexes_while_next_voice_is_active(self):
        self.enable_indexer()
        self.transcript(text="First conversation indexed before the next starts.")
        self.codex.stop_voice()
        self.wait_for(lambda: len(self.index_processes) == 1)
        self.finish_index(0)
        self.wait_for(lambda: self.codex.state.get("memoryIndexStatus") == "indexed")
        self.process.results["thread/start"] = {"thread": {"id": "second-thread"}}
        self.codex.session({"cwd": self.temp.name})
        self.codex.state.update(voiceActive=True, voiceStatus="connected")
        self.transcript(text="Late first conversation update.", item="late-after-index")
        self.wait_for(lambda: len(self.index_processes) == 2)
        self.assertTrue(self.codex.state["voiceActive"])
        self.assertEqual(self.codex.state["threadId"], "second-thread")
        self.finish_index(1)
        self.wait_for(lambda: self.codex.state.get("memoryIndexStatus") == "indexed")

    def test_empty_flush_does_not_enable_every_turn_indexing_during_next_voice(self):
        self.enable_indexer()
        self.codex.flush_memory()
        self.assertFalse(self.codex.memory_index_requested)
        self.codex.state.update(voiceActive=True, voiceStatus="connected")
        self.transcript(text="An active conversation can wait for its stop boundary.")
        self.assertTrue(self.codex.memory_index_dirty)
        self.assertFalse(self.codex.memory_index_requested)
        with self.codex.condition:
            self.assertFalse(self.codex.condition.wait_for(lambda: bool(self.index_processes), timeout=0.1))
        self.codex.stop_voice()
        self.wait_for(lambda: len(self.index_processes) == 1)
        self.finish_index(0)
        self.wait_for(lambda: self.codex.state.get("memoryIndexStatus") == "indexed")

    def test_index_startup_failure_retries_without_another_conversation(self):
        self.enable_indexer(startup_failures=1)
        self.transcript(text="Retry indexer startup.")
        self.wait_for(lambda: len(self.index_processes) == 1)
        self.finish_index(0)
        self.wait_for(lambda: self.codex.state.get("memoryIndexStatus") == "indexed")
        self.assertFalse(self.codex.memory_index_dirty)

    def test_failed_or_busy_indexer_keeps_dirty_state_and_retries(self):
        self.enable_indexer()
        self.transcript(text="Must remain pending until indexed.")
        self.wait_for(lambda: len(self.index_processes) == 1)
        self.finish_index(0, returncode=75)
        self.wait_for(lambda: len(self.index_processes) == 2)
        self.finish_index(1, returncode=1)
        self.wait_for(lambda: len(self.index_processes) == 3)
        self.finish_index(2)
        self.wait_for(lambda: self.codex.state.get("memoryIndexStatus") == "indexed")
        self.assertFalse(self.codex.memory_index_dirty)

    def test_browser_fallback_write_updates_status_and_schedules_index(self):
        self.enable_indexer()
        self.codex.save_transcript({"threadId": "selected-thread", "role": "user",
                                   "text": "Browser fallback to index.", "itemId": "browser-index"})
        self.assertEqual(self.codex.state["memoryStatus"], "saved")
        self.wait_for(lambda: len(self.index_processes) == 1)
        self.finish_index(0)
        self.wait_for(lambda: self.codex.state.get("memoryIndexStatus") == "indexed")

    def test_index_worker_stops_on_clean_shutdown(self):
        self.enable_indexer()
        self.codex.close()
        self.assertFalse(self.codex.memory_index_thread.is_alive())


if __name__ == "__main__":
    unittest.main(verbosity=2)
