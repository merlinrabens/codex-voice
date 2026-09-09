#!/usr/bin/env python3
"""Offline regression tests. No Codex process, model call, microphone or GUI."""
import http.client
import importlib.util
import json
from pathlib import Path
import queue
import tempfile
import threading
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("voice_server_under_test", Path(__file__).with_name("server.py"))
voice = importlib.util.module_from_spec(spec)
spec.loader.exec_module(voice)


class FakeOutput:
    def __init__(self):
        self.queue = queue.Queue()

    def __iter__(self):
        while True:
            value = self.queue.get()
            if value is None:
                return
            yield json.dumps(value) + "\n"


class FakeInput:
    def __init__(self, process):
        self.process = process

    def write(self, text):
        for line in text.splitlines():
            value = json.loads(line)
            self.process.sent.append(value)
            if "method" in value and "id" in value:
                result = self.process.results.get(value["method"], {})
                self.process.stdout.queue.put({"id": value["id"], "result": result})

    def flush(self):
        pass


class FakeProcess:
    def __init__(self):
        self.sent = []
        self.results = {}
        self.stdout = FakeOutput()
        self.stdin = FakeInput(self)
        self.stopped = False

    def poll(self):
        return 0 if self.stopped else None

    def terminate(self):
        if not self.stopped:
            self.stopped = True
            self.stdout.queue.put(None)

    kill = terminate

    def wait(self, timeout=None):
        return 0


class StateTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="voice-unit-")
        self.process = FakeProcess()
        with patch.object(voice.subprocess, "Popen", return_value=self.process):
            self.codex = voice.Codex(self.folder.name)
        self.codex.state.update(threadId="selected-thread", activeTurnId="selected-turn")

    def tearDown(self):
        self.codex.state["voiceActive"] = False
        self.codex.close()
        self.folder.cleanup()

    def inject(self, method, params, ident=None):
        item = {"method": method, "params": params}
        if ident is not None:
            item["id"] = ident
        self.process.stdout.queue.put(item)
        # FIFO reply forms a deterministic barrier for the reader thread.
        self.codex.rpc("test/barrier", {})

    def replies(self, ident):
        return [v for v in self.process.sent if v.get("id") == ident and "method" not in v]

    def approval(self, ident=9001, **extra):
        self.inject("item/commandExecution/requestApproval", {
            "threadId": "selected-thread", "turnId": "selected-turn", "itemId": "command-item",
            "startedAtMs": 1, "command": "printf ready", **extra,
        }, ident)

    def test_foreign_thread_cannot_change_turn_or_voice_answer(self):
        self.codex.state.update(voiceActive=True, voiceStatus="connecting")
        self.inject("turn/completed", {"threadId": "other-thread", "turn": {"id": "other-turn"}})
        self.inject("thread/realtime/sdp", {"threadId": "other-thread", "sdp": "private-answer"})
        self.inject("thread/realtime/error", {"threadId": "other-thread", "message": "unrelated"})
        self.assertEqual(self.codex.state["activeTurnId"], "selected-turn")
        self.assertTrue(self.codex.state["voiceActive"])
        self.assertIsNone(self.codex.answer)
        self.assertIsNone(self.codex.voice_error)
        self.assertFalse(any(e[1]["params"].get("threadId") == "other-thread" for e in self.codex.events))

    def test_protocol_closed_event_clears_voice_state(self):
        self.codex.state.update(voiceActive=True, voiceStatus="connecting")
        self.inject("thread/realtime/closed", {"threadId": "selected-thread", "reason": "remote close"})
        self.assertFalse(self.codex.state["voiceActive"])
        self.assertNotEqual(self.codex.state["voiceStatus"], "connecting")

    def test_unsupported_interactive_request_fails_closed(self):
        self.inject("item/tool/requestUserInput", {"threadId": "selected-thread"}, 9002)
        replies = self.replies(9002)
        self.assertEqual(len(replies), 1)
        self.assertIn("error", replies[0])
        self.assertNotIn("result", replies[0])

    def test_approval_waits_for_explicit_decision_and_cannot_replay(self):
        self.approval()
        self.assertEqual(self.replies(9001), [])
        self.assertIn("9001", self.codex.approvals)
        self.codex.approve({"id": 9001, "decision": "decline"})
        self.assertEqual(self.replies(9001), [{"id": 9001, "result": {"decision": "decline"}}])
        with self.assertRaises(voice.RequestError):
            self.codex.approve({"id": 9001, "decision": "accept"})

    def test_foreign_approval_is_not_presented_or_accepted(self):
        self.approval(threadId="other-thread")
        self.assertNotIn("9001", self.codex.approvals)
        self.assertFalse(any(e[1]["method"] == "approval/request" for e in self.codex.events))
        with self.assertRaises(voice.RequestError):
            self.codex.approve({"id": 9001, "decision": "accept"})

    def test_resolved_request_removes_stale_approval(self):
        self.approval()
        self.inject("serverRequest/resolved", {"threadId": "selected-thread", "requestId": 9001})
        self.assertNotIn("9001", self.codex.approvals)
        with self.assertRaises(voice.RequestError):
            self.codex.approve({"id": 9001, "decision": "accept"})

    def test_accept_must_be_an_available_decision(self):
        self.approval(availableDecisions=["decline"])
        with self.assertRaises(voice.RequestError):
            self.codex.approve({"id": 9001, "decision": "accept"})
        self.assertEqual(self.replies(9001), [])

    def test_invalid_approval_decision_does_not_consume_request(self):
        self.approval()
        with self.assertRaises(voice.RequestError):
            self.codex.approve({"id": 9001, "decision": "approve-everything"})
        self.assertIn("9001", self.codex.approvals)
        self.assertEqual(self.replies(9001), [])

    def test_text_steers_active_turn_with_required_precondition(self):
        self.codex.text({"text": "continue"})
        request = next(v for v in self.process.sent if v.get("method") == "turn/steer")
        self.assertEqual(request["params"]["threadId"], "selected-thread")
        self.assertEqual(request["params"]["expectedTurnId"], "selected-turn")
        self.assertEqual(request["params"]["input"], [{"type": "text", "text": "continue"}])

    def test_invalid_text_never_reaches_codex(self):
        for value in [None, [], {}, "", " " * 2, "x" * 30001]:
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(voice.RequestError):
                    self.codex.text({"text": value})
        self.assertFalse(any(v.get("method") in {"turn/start", "turn/steer"} for v in self.process.sent))

    def test_session_permission_modes_are_explicit_native_options(self):
        self.codex.state["activeTurnId"] = None
        self.process.results["thread/start"] = {"thread": {"id": "new-thread"}}
        for mode, approval, sandbox in [
            ("ask", "on-request", "workspace-write"),
            ("yolo", "never", "danger-full-access"),
        ]:
            with self.subTest(mode=mode):
                result = self.codex.session({"cwd": self.folder.name, "permissionMode": mode})
                request = [v for v in self.process.sent if v.get("method") == "thread/start"][-1]
                self.assertEqual(request["params"]["approvalPolicy"], approval)
                self.assertEqual(request["params"]["sandbox"], sandbox)
                self.assertNotIn("permissions", request["params"])
                self.assertEqual(result["permissionMode"], mode)

    def test_resume_receives_selected_permissions_and_native_only_instruction(self):
        self.codex.state["activeTurnId"] = None
        self.process.results["thread/resume"] = {"thread": {"id": "stored-thread"}}
        self.codex.session({"cwd": self.folder.name, "threadId": "stored-thread", "permissionMode": "yolo"})
        request = next(v for v in self.process.sent if v.get("method") == "thread/resume")
        self.assertEqual(request["params"]["approvalPolicy"], "never")
        self.assertEqual(request["params"]["sandbox"], "danger-full-access")
        self.assertEqual(request["params"]["developerInstructions"], voice.COMPUTER_USE_INSTRUCTIONS)
        self.assertNotIn("baseInstructions", request["params"])

    def test_new_session_preserves_base_instructions_and_adds_native_only_policy(self):
        self.codex.state["activeTurnId"] = None
        self.process.results["thread/start"] = {"thread": {"id": "new-thread"}}
        self.codex.session({"cwd": self.folder.name})
        params = next(v["params"] for v in self.process.sent if v.get("method") == "thread/start")
        self.assertEqual(params["developerInstructions"], voice.COMPUTER_USE_INSTRUCTIONS)
        self.assertTrue(params["developerInstructions"].strip())
        self.assertNotIn("baseInstructions", params)
        self.assertEqual(params["approvalPolicy"], "on-request")
        self.assertEqual(params["sandbox"], "workspace-write")

    def test_missing_permission_mode_uses_constructor_default_not_previous_session(self):
        self.codex.state["activeTurnId"] = None
        self.process.results["thread/start"] = {"thread": {"id": "new-thread"}}
        self.codex.session({"cwd": self.folder.name, "permissionMode": "yolo"})
        result = self.codex.session({"cwd": self.folder.name})
        request = [v for v in self.process.sent if v.get("method") == "thread/start"][-1]
        self.assertEqual(result["permissionMode"], "ask")
        self.assertEqual(request["params"]["approvalPolicy"], "on-request")

    def test_constructor_yolo_default_is_used_when_session_mode_omitted(self):
        process = FakeProcess()
        process.results["thread/start"] = {"thread": {"id": "new-thread"}}
        codex = None
        try:
            with patch.object(voice.subprocess, "Popen", return_value=process):
                codex = voice.Codex(self.folder.name, permission_mode="yolo")
            result = codex.session({"cwd": self.folder.name})
            self.assertEqual(result["permissionMode"], "yolo")
            request = next(v for v in process.sent if v.get("method") == "thread/start")
            self.assertEqual(request["params"]["approvalPolicy"], "never")
            self.assertEqual(request["params"]["sandbox"], "danger-full-access")
        finally:
            if codex:
                codex.close()
            else:
                process.terminate()

    def test_invalid_permission_modes_never_reach_app_server(self):
        self.codex.state["activeTurnId"] = None
        self.process.results["thread/start"] = {"thread": {"id": "new-thread"}}
        for mode in [None, "", "admin", "never", True, [], {}]:
            with self.subTest(mode=mode):
                with self.assertRaises(voice.RequestError):
                    self.codex.session({"cwd": self.folder.name, "permissionMode": mode})
        self.assertFalse(any(v.get("method") in {"thread/start", "thread/resume"} for v in self.process.sent))

    def test_permission_change_is_rejected_while_voice_or_turn_is_active(self):
        for voice_active, turn_id in [(True, None), (False, "running-turn")]:
            with self.subTest(voice_active=voice_active, turn_id=turn_id):
                self.codex.state.update(voiceActive=voice_active, activeTurnId=turn_id)
                with self.assertRaises(voice.RequestError):
                    self.codex.session({"cwd": self.folder.name, "permissionMode": "yolo"})
        self.assertFalse(any(v.get("method") in {"thread/start", "thread/resume"} for v in self.process.sent))

    def test_persistent_approval_uses_only_the_stored_native_proposal(self):
        proposal = ["git", "status"]
        native = {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": proposal}}
        self.approval(proposedExecpolicyAmendment=proposal, availableDecisions=["accept", native, "decline"])
        previous_mode = self.codex.state.get("permissionMode")
        self.codex.approve({"id": 9001, "decision": "always", "proposedExecpolicyAmendment": ["sh"],
                            "execpolicy_amendment": ["python3"], "permissionMode": "yolo"})
        self.assertEqual(self.replies(9001), [{"id": 9001, "result": {"decision": native}}])
        self.assertEqual(self.codex.state.get("permissionMode"), previous_mode)

    def test_persistent_approval_rejects_missing_empty_or_malformed_proposal(self):
        for ident, proposal in enumerate([None, [], "git status", [1], [""]], 9100):
            with self.subTest(proposal=proposal):
                self.approval(ident=ident, proposedExecpolicyAmendment=proposal)
                with self.assertRaises(voice.RequestError):
                    self.codex.approve({"id": ident, "decision": "always"})
                self.assertEqual(self.replies(ident), [])
                self.assertIn(str(ident), self.codex.approvals)

    def test_persistent_approval_requires_exact_available_decision(self):
        self.approval(proposedExecpolicyAmendment=["git", "status"], availableDecisions=[
            "accept", {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": ["git", "log"]}}, "decline",
        ])
        with self.assertRaises(voice.RequestError):
            self.codex.approve({"id": 9001, "decision": "always"})
        self.assertEqual(self.replies(9001), [])

    def test_persistent_command_rule_cannot_be_created_from_file_approval(self):
        self.inject("item/fileChange/requestApproval", {"threadId": "selected-thread", "itemId": "file-1",
            "turnId": "selected-turn", "proposedExecpolicyAmendment": ["git", "status"]}, 9200)
        with self.assertRaises(voice.RequestError):
            self.codex.approve({"id": 9200, "decision": "always"})
        self.assertEqual(self.replies(9200), [])

    def test_session_scoped_approval_is_distinct_from_persistent_approval(self):
        self.approval(availableDecisions=["accept", "acceptForSession", "decline"])
        self.codex.approve({"id": 9001, "decision": "acceptForSession"})
        self.assertEqual(self.replies(9001), [{"id": 9001, "result": {"decision": "acceptForSession"}}])

    def test_file_session_approval_still_requires_reviewable_changes(self):
        self.inject("item/fileChange/requestApproval", {"threadId": "selected-thread", "itemId": "file-1",
            "turnId": "selected-turn"}, 9201)
        with self.assertRaises(voice.RequestError):
            self.codex.approve({"id": 9201, "decision": "acceptForSession"})
        self.assertEqual(self.replies(9201), [])


class FakeApi:
    def __init__(self):
        self.calls = []

    def snapshot(self):
        return {"threadId": None}

    def text(self, data):
        self.calls.append(data)
        return {"ok": True}

    def stop_voice(self):
        return self.snapshot()

    def interrupt(self):
        return self.snapshot()


class HttpBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.server = voice.ThreadingHTTPServer(("127.0.0.1", 0), voice.Handler)
        self.server.daemon_threads = True
        self.server.codex = FakeApi()
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.port = self.server.server_port
        self.origin = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def request(self, method="POST", body=None, headers=None, path="/api/text"):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        data = json.dumps({"text": "test"} if body is None else body)
        request_headers = {"Content-Type": "application/json", **(headers or {})}
        connection.request(method, path, body=data if method == "POST" else None, headers=request_headers)
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        return response.status, raw

    def test_same_origin_mutation_reaches_action(self):
        code, _ = self.request(headers={"Origin": self.origin})
        self.assertEqual(code, 200)
        self.assertEqual(self.server.codex.calls, [{"text": "test"}])

    def test_missing_null_and_foreign_origins_cannot_mutate(self):
        for origin in [None, "null", "https://evil.example", "http://127.0.0.1:1", "https://127.0.0.1:" + str(self.port)]:
            with self.subTest(origin=origin):
                code, _ = self.request(headers={} if origin is None else {"Origin": origin})
                self.assertEqual(code, 403)
        self.assertEqual(self.server.codex.calls, [])

    def test_forged_host_is_rejected_even_with_good_origin(self):
        for host in ["evil.example", "localhost:1", f"evil.example:{self.port}"]:
            with self.subTest(host=host):
                code, _ = self.request(headers={"Origin": self.origin, "Host": host})
                self.assertEqual(code, 403)
        self.assertEqual(self.server.codex.calls, [])

    def test_local_top_level_state_request_works(self):
        code, raw = self.request(method="GET", path="/api/state")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(raw), {"threadId": None})

    def test_non_object_json_is_rejected_before_action(self):
        for body in [[], "text", 42]:
            with self.subTest(body=body):
                code, _ = self.request(body=body, headers={"Origin": self.origin})
                self.assertEqual(code, 400)
        self.assertEqual(self.server.codex.calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
