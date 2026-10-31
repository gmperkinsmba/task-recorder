import http.client
import contextlib
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path

import server


def state_value():
    return {
        "schemaVersion": 1,
        "revision": 0,
        "macroName": "Follow-up flow",
        "settings": {"maxLoops": 2},
        "steps": [],
        "items": [],
    }


class ServerBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state_path = Path(self.temp.name) / "private" / "state.json"
        self._start_server()

    def tearDown(self):
        self._stop_server()
        self.temp.cleanup()

    def _start_server(self):
        self.httpd = server.make_server(port=0, state_path=self.state_path)
        self.port = self.httpd.server_port
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def _stop_server(self):
        if getattr(self, "httpd", None) is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.thread.join(timeout=3)
            self.httpd = None

    def request(self, method, path, body=None, *, host=None, origin="match", token=True, content_type="application/json"):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        headers = {
            "Host": host or f"127.0.0.1:{self.port}",
            "Content-Type": content_type,
        }
        if origin == "match":
            headers["Origin"] = f"http://127.0.0.1:{self.port}"
        elif origin is not None:
            headers["Origin"] = origin
        if token:
            headers["X-MacroForge-Request"] = self.httpd.request_token
        raw = body
        if isinstance(body, (dict, list)):
            raw = json.dumps(body).encode("utf-8")
        connection.request(method, path, body=raw, headers=headers)
        response = connection.getresponse()
        data = response.read()
        result = (response.status, response.getheaders(), data)
        connection.close()
        return result

    def json_request(self, path, body, **kwargs):
        status, headers, data = self.request("POST", path, body, **kwargs)
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            payload = {}
        return status, dict(headers), payload, data

    def test_random_loopback_server_injects_token_and_serves_only_allowlisted_assets(self):
        self.assertNotEqual(self.port, 0)
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(self.httpd.request_token.encode(), body)
        self.assertNotIn(b"__TOKEN__", body)
        header_map = dict(headers)
        self.assertEqual(header_map["Cache-Control"], "no-store")
        self.assertIn("frame-ancestors 'none'", header_map["Content-Security-Policy"])
        self.assertNotIn("Access-Control-Allow-Origin", header_map)
        self.assertEqual(self.request("GET", "/server.py")[0], 404)
        self.assertEqual(self.request("GET", "/.gitignore")[0], 404)
        self.assertEqual(self.request("GET", "/style.css")[0], 200)

    def test_api_requires_loopback_host_exact_origin_and_random_token(self):
        request = {"POST": "/api/state/load", "body": {}}
        cases = [
            {"host": "attacker.example", "origin": "match", "token": True},
            {"host": None, "origin": "https://attacker.example", "token": True},
            {"host": None, "origin": None, "token": True},
            {"host": None, "origin": "match", "token": False},
        ]
        for case in cases:
            status, _, payload, _ = self.json_request(request["POST"], request["body"], **case)
            self.assertEqual(status, 403, payload)
        self.assertFalse(self.state_path.exists())

    def test_rejected_credential_payload_is_not_echoed_or_logged(self):
        secret = "ghp_" + "Z" * 36
        state = state_value()
        state["steps"] = [{"id":"secret-step","type":"input","target":"crm-demo","field":"summary","value":secret}]
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured), contextlib.redirect_stdout(captured):
            status, _, payload, raw = self.json_request("/api/state/bootstrap", {"state":state})
        self.assertEqual(status, 400)
        self.assertNotIn(secret, raw.decode())
        self.assertNotIn(secret, captured.getvalue())

    def test_startup_bootstrap_persist_and_restart_on_a_different_ephemeral_port(self):
        status, _, loaded, _ = self.json_request("/api/state/load", {})
        self.assertEqual(status, 200)
        self.assertFalse(loaded["persisted"])
        self.assertTrue(loaded["bootstrap_allowed"])
        self.assertEqual(loaded["state"]["steps"], [])
        self.assertEqual(self.state_path.parent.stat().st_mode & 0o777, 0o700)

        initial = state_value()
        initial["steps"] = [{"id": "step-1", "type": "wait", "target": "crm-demo", "value": 250}]
        status, _, created, _ = self.json_request("/api/state/bootstrap", {"state": initial})
        self.assertEqual(status, 200, created)
        self.assertEqual(created["state"]["revision"], 1)
        self.assertEqual(self.state_path.stat().st_mode & 0o777, 0o600)
        status, _, conflict, _ = self.json_request("/api/state/bootstrap", {"state": initial})
        self.assertEqual(status, 409, conflict)

        revised = dict(created["state"])
        revised["macroName"] = "Persisted across launches"
        status, _, saved, _ = self.json_request("/api/state/save", {"state": revised, "expected_revision": 1})
        self.assertEqual(status, 200, saved)
        self.assertEqual(saved["state"]["revision"], 2)

        previous_port = self.port
        self._stop_server()
        self._start_server()
        self.assertNotEqual(self.port, previous_port)
        status, _, restarted, _ = self.json_request("/api/state/load", {})
        self.assertEqual(status, 200, restarted)
        self.assertTrue(restarted["persisted"])
        self.assertFalse(restarted["bootstrap_allowed"])
        self.assertEqual(restarted["state"]["macroName"], "Persisted across launches")
        self.assertEqual(restarted["state"]["revision"], 2)

    def test_corrupt_state_is_reported_and_preserved(self):
        raw = b'{"schemaVersion":1,"schemaVersion":2}'
        self.state_path.parent.mkdir(mode=0o700, exist_ok=True)
        self.state_path.write_bytes(raw)
        status, _, payload, _ = self.json_request("/api/state/load", {})
        self.assertEqual(status, 500, payload)
        self.assertTrue(payload["preserved"])
        self.assertIn("preserved", payload["error"])
        self.assertEqual(self.state_path.read_bytes(), raw)

    def test_rejects_bad_route_query_content_type_duplicate_keys_and_oversize(self):
        status, _, payload, _ = self.json_request("/api/state/load?path=other", {})
        self.assertEqual(status, 400, payload)
        status, _, payload, _ = self.json_request("/api/state/load", {}, content_type="text/plain")
        self.assertEqual(status, 415, payload)
        status, _, payload, _ = self.json_request("/api/state/load", b'{"x":1,"x":2}')
        self.assertEqual(status, 400, payload)
        status, _, payload, _ = self.json_request("/api/state/load", b" " * (server.MAX_REQUEST + 1))
        self.assertEqual(status, 413, payload)
        self.assertFalse(self.state_path.exists())

    def test_invalid_state_payload_does_not_create_file(self):
        invalid = state_value()
        invalid["steps"] = [{"id": "bad", "type": "click", "target": "https://example.test", "action": "open-url"}]
        status, _, payload, _ = self.json_request("/api/state/bootstrap", {"state": invalid})
        self.assertEqual(status, 400, payload)
        self.assertFalse(self.state_path.exists())


if __name__ == "__main__":
    unittest.main()
