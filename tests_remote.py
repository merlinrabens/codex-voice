#!/usr/bin/env python3
"""Offline remote-boundary tests; no model, microphone, tunnel, or user service."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from http.cookies import SimpleCookie
import http.client
import importlib.util
import json
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("voice_remote_under_test", Path(__file__).with_name("server.py"))
voice = importlib.util.module_from_spec(spec)
spec.loader.exec_module(voice)


class FakeCodex:
    def __init__(self):
        self.calls = []
        self.condition = threading.Condition()
        self.events = deque([
            (1, {"method": "first", "params": {"text": "earlier"}}),
            (2, {"method": "second", "params": {"text": "later"}}),
        ])
        self.event_id = 2
        self.closed = False

    def snapshot(self):
        return {"threadId": "private-thread", "voiceActive": False}

    def text(self, data):
        self.calls.append(data)
        return {"ok": True}

    def stop_voice(self):
        return self.snapshot()

    def interrupt(self):
        return self.snapshot()


class RemoteBoundaryTests(unittest.TestCase):
    origin = "https://voice.example.test"
    public_host = "voice.example.test"
    cookie_name = "__Host-codex_voice"

    def setUp(self):
        self.server = voice.ThreadingHTTPServer(("127.0.0.1", 0), voice.Handler)
        self.server.daemon_threads = True
        self.server.codex = FakeCodex()
        self.server.remote_access = voice.RemoteAccess(self.origin)
        self.access = self.server.remote_access
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.port = self.server.server_port

    def tearDown(self):
        with self.server.codex.condition:
            self.server.codex.closed = True
            self.server.codex.condition.notify_all()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method="GET", path="/api/state", data=None, headers=None, raw=None):
        request_headers = {"Host": self.public_host, "Connection": "close", **(headers or {})}
        if method == "POST":
            request_headers.setdefault("Content-Type", "application/json")
        body = raw if raw is not None else json.dumps(data if data is not None else {}).encode() if method == "POST" else None
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def pair(self):
        status, headers, body = self.request("POST", "/api/pair", {"code": self.access.pairing_code}, {"Origin": self.origin})
        self.assertEqual(status, 200, body)
        self.assertIn("Set-Cookie", headers)
        cookies = SimpleCookie()
        cookies.load(headers["Set-Cookie"])
        self.assertIn(self.cookie_name, cookies)
        return self.cookie_name + "=" + cookies[self.cookie_name].value, headers

    def test_all_private_api_reads_require_cookie(self):
        for path in ["/api/state", "/api/events", "/api/events/poll?after=0&wait=0"]:
            with self.subTest(path=path):
                status, _, body = self.request(path=path)
                self.assertEqual(status, 401)
                self.assertNotIn(b"private-thread", body)

    def test_unauthenticated_post_never_dispatches(self):
        status, _, _ = self.request("POST", "/api/text", {"text": "must not execute"}, {"Origin": self.origin})
        self.assertEqual(status, 401)
        self.assertEqual(self.server.codex.calls, [])

    def test_local_host_headers_cannot_bypass_remote_auth(self):
        for host in [f"127.0.0.1:{self.port}", f"localhost:{self.port}"]:
            with self.subTest(host=host):
                status, _, body = self.request(headers={"Host": host})
                self.assertEqual(status, 401)
                self.assertNotIn(b"private-thread", body)

    def test_forwarded_headers_never_authenticate(self):
        status, _, _ = self.request(headers={
            "X-Forwarded-For": "127.0.0.1", "X-Real-IP": "127.0.0.1",
            "X-Forwarded-Proto": "https", "Forwarded": "for=127.0.0.1;proto=https",
        })
        self.assertEqual(status, 401)

    def test_wrong_host_rejected_even_with_valid_cookie_and_origin(self):
        cookie, _ = self.pair()
        status, _, _ = self.request("POST", "/api/text", {"text": "denied"}, {
            "Host": "other.example.test", "Origin": self.origin, "Cookie": cookie,
            "X-Forwarded-Host": self.public_host,
        })
        self.assertEqual(status, 403)
        self.assertEqual(self.server.codex.calls, [])

    def test_pairing_requires_exact_origin_before_consuming_code(self):
        for origin in [None, "null", "https://other.example.test", "http://voice.example.test"]:
            with self.subTest(origin=origin):
                headers = {} if origin is None else {"Origin": origin}
                status, _, _ = self.request("POST", "/api/pair", {"code": self.access.pairing_code}, headers)
                self.assertEqual(status, 403)
        self.pair()

    def test_authenticated_post_still_requires_exact_origin(self):
        cookie, _ = self.pair()
        for origin in [None, "null", "https://other.example.test", "http://voice.example.test"]:
            with self.subTest(origin=origin):
                headers = {"Cookie": cookie}
                if origin is not None:
                    headers["Origin"] = origin
                status, _, _ = self.request("POST", "/api/text", {"text": "denied"}, headers)
                self.assertEqual(status, 403)
        self.assertEqual(self.server.codex.calls, [])

    def test_pairing_cookie_has_secure_browser_scope(self):
        cookie, headers = self.pair()
        cookies = SimpleCookie()
        cookies.load(headers["Set-Cookie"])
        value = cookies[self.cookie_name]
        self.assertTrue(value["secure"])
        self.assertTrue(value["httponly"])
        self.assertEqual(value["samesite"].lower(), "strict")
        self.assertEqual(value["path"], "/")
        self.assertEqual(value["domain"], "")
        self.assertGreaterEqual(len(value.value), 32)
        self.assertNotIn(value.value, self.access.sessions)
        status, headers, body = self.request(headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {**self.server.codex.snapshot(), "clientTransport": "poll"})
        self.assertIn("no-store", headers.get("Cache-Control", ""))

    def test_pairing_code_cannot_be_replayed(self):
        code = self.access.pairing_code
        self.pair()
        status, _, _ = self.request("POST", "/api/pair", {"code": code}, {"Origin": self.origin})
        self.assertEqual(status, 403)

    def test_expired_pairing_code_cannot_create_session(self):
        self.access.pairing_expires = time.monotonic() - 1
        status, _, _ = self.request("POST", "/api/pair", {"code": self.access.pairing_code}, {"Origin": self.origin})
        self.assertEqual(status, 403)
        self.assertEqual(self.access.sessions, {})

    def test_wrong_pairing_code_does_not_create_session(self):
        status, _, _ = self.request("POST", "/api/pair", {"code": "incorrect-test-code"}, {"Origin": self.origin})
        self.assertEqual(status, 403)
        self.assertEqual(self.access.sessions, {})

    def test_pairing_rate_limit_blocks_even_correct_code_until_window_expires(self):
        for _ in range(12):
            status, _, _ = self.request("POST", "/api/pair", {"code": "incorrect-test-code"}, {"Origin": self.origin})
            self.assertEqual(status, 403)
        status, _, _ = self.request("POST", "/api/pair", {"code": self.access.pairing_code}, {"Origin": self.origin})
        self.assertEqual(status, 403)
        self.assertEqual(self.access.sessions, {})
        future = time.monotonic() + 61
        with patch.object(voice.time, "monotonic", return_value=future):
            self.pair()

    def test_concurrent_pairing_consumes_code_exactly_once(self):
        code = self.access.pairing_code
        barrier = threading.Barrier(4)

        def attempt(_):
            barrier.wait(timeout=2)
            return self.request("POST", "/api/pair", {"code": code}, {"Origin": self.origin})[0]

        with ThreadPoolExecutor(max_workers=4) as pool:
            statuses = list(pool.map(attempt, range(4)))
        self.assertEqual(sorted(statuses), [200, 403, 403, 403])
        self.assertEqual(len(self.access.sessions), 1)

    def test_expired_cookie_is_rejected(self):
        cookie, _ = self.pair()
        for key in self.access.sessions:
            self.access.sessions[key] = time.monotonic() - 1
        status, _, _ = self.request(headers={"Cookie": cookie})
        self.assertEqual(status, 401)

    def test_forged_cookie_is_rejected(self):
        status, _, _ = self.request(headers={"Cookie": self.cookie_name + "=unissued-cookie"})
        self.assertEqual(status, 401)

    def test_cookie_cannot_be_supplied_by_query_string(self):
        cookie, _ = self.pair()
        status, _, _ = self.request(path="/api/state?" + cookie)
        self.assertEqual(status, 401)

    def test_logout_invalidates_cookie_for_future_reads_and_actions(self):
        cookie, _ = self.pair()
        status, _, _ = self.request("POST", "/api/logout", {}, {"Cookie": cookie, "Origin": self.origin})
        self.assertEqual(status, 200)
        status, _, _ = self.request(headers={"Cookie": cookie})
        self.assertEqual(status, 401)
        status, _, _ = self.request("POST", "/api/text", {"text": "denied"}, {"Cookie": cookie, "Origin": self.origin})
        self.assertEqual(status, 401)
        self.assertEqual(self.server.codex.calls, [])

    def test_logout_while_poll_waits_does_not_leak_new_events(self):
        cookie, _ = self.pair()
        waiting = threading.Event()
        condition = self.server.codex.condition
        original_wait = condition.wait_for

        def observed_wait(predicate, timeout=None):
            waiting.set()
            return original_wait(predicate, timeout)

        with patch.object(condition, "wait_for", side_effect=observed_wait):
            with ThreadPoolExecutor(max_workers=1) as pool:
                result = pool.submit(self.request, "GET", "/api/events/poll?after=2&wait=2", headers={"Cookie": cookie})
                self.assertTrue(waiting.wait(timeout=1))
                status, _, _ = self.request("POST", "/api/logout", {}, {"Cookie": cookie, "Origin": self.origin})
                self.assertEqual(status, 200)
                with condition:
                    self.server.codex.events.append((3, {"method": "private-update", "params": {}}))
                    self.server.codex.event_id = 3
                    condition.notify_all()
                status, _, body = result.result(timeout=3)
        self.assertEqual(status, 401)
        self.assertNotIn(b"private-update", body)
        self.assertNotIn(b"private-thread", body)

    def test_authorized_mutation_dispatches_once(self):
        cookie, _ = self.pair()
        status, _, _ = self.request("POST", "/api/text", {"text": "offline test"}, {"Cookie": cookie, "Origin": self.origin})
        self.assertEqual(status, 200)
        self.assertEqual(self.server.codex.calls, [{"text": "offline test"}])

    def test_poll_respects_cursor_and_includes_current_state(self):
        cookie, _ = self.pair()
        status, _, body = self.request(path="/api/events/poll?after=1&wait=0", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertEqual(result["cursor"], 2)
        self.assertEqual(result["state"], self.server.codex.snapshot())
        self.assertEqual(result["events"], [{"id": 2, "method": "second", "params": {"text": "later"}}])

    def test_completed_poll_returns_no_old_events(self):
        cookie, _ = self.pair()
        status, _, body = self.request(path="/api/events/poll?after=2&wait=0", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertEqual(result["events"], [])
        self.assertEqual(result["cursor"], 2)

    def test_request_size_limit_prevents_dispatch(self):
        cookie, _ = self.pair()
        status, _, _ = self.request("POST", "/api/text", headers={"Cookie": cookie, "Origin": self.origin}, raw=b"x" * 150001)
        self.assertEqual(status, 413)
        self.assertEqual(self.server.codex.calls, [])

    def test_pairing_has_smaller_request_size_limit(self):
        status, _, _ = self.request("POST", "/api/pair", headers={"Origin": self.origin}, raw=b"x" * 4097)
        self.assertEqual(status, 413)
        self.assertEqual(self.access.sessions, {})
        self.pair()

    def test_public_login_never_exposes_pairing_code_or_private_state(self):
        code = self.access.pairing_code.encode()
        for path in ["/", "/pair.js"]:
            with self.subTest(path=path):
                status, headers, body = self.request(path=path)
                self.assertEqual(status, 200)
                self.assertNotIn(code, body)
                self.assertNotIn(b"private-thread", body)
                self.assertNotIn("Set-Cookie", headers)
                self.assertIn("no-store", headers.get("Cache-Control", ""))

    def test_malformed_json_cannot_dispatch(self):
        cookie, _ = self.pair()
        for raw in [b"{", b"[]", b"null", b'"text"']:
            with self.subTest(raw=raw):
                status, _, _ = self.request("POST", "/api/text", headers={"Cookie": cookie, "Origin": self.origin}, raw=raw)
                self.assertEqual(status, 400)
        self.assertEqual(self.server.codex.calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
