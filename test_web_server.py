"""
test_web_server.py — Automated Unit Tests for Web Server Interface.
"""

import json
import threading
import time
import unittest
from unittest.mock import patch
from web_server import create_handler_class, SIMULATION_LOCK
from http.server import ThreadingHTTPServer
import urllib.request
import urllib.error


class TestWebServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = {
            "sheet": "Test Sheet",
            "creds": "credentials.json",
            "token": "token.json",
            "service_account": None
        }
        handler_cls = create_handler_class(cls.config)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        cls.port = cls.server.server_address[1]
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_get_root_serves_html(self):
        req = urllib.request.Request(f"{self.base_url}/")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/html", resp.headers.get("Content-Type"))
            body = resp.read().decode("utf-8")
            self.assertIn("50vh", body)
            self.assertIn("50vw", body)
            self.assertIn("Run Simulation", body)

    def test_get_health(self):
        req = urllib.request.Request(f"{self.base_url}/health")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data.get("status"), "healthy")

    def test_get_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(f"{self.base_url}/nonexistent")
        self.assertEqual(ctx.exception.code, 404)

    @patch("web_server.execute_simulation")
    def test_post_run_success(self, mock_exec):
        mock_exec.return_value = (True, "Ledger written to Google Sheet.")
        req = urllib.request.Request(f"{self.base_url}/run", method="POST")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data.get("status"), "success")
            self.assertIn("written to Google Sheet", data.get("message"))
        mock_exec.assert_called_once_with(
            sheet_name="Test Sheet",
            creds_path="credentials.json",
            token_path="token.json",
            service_account=None
        )

    @patch("web_server.execute_simulation")
    def test_post_run_failure(self, mock_exec):
        mock_exec.return_value = (False, "Sheet authentication error.")
        req = urllib.request.Request(f"{self.base_url}/run", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 500)
        data = json.loads(ctx.exception.read().decode("utf-8"))
        self.assertEqual(data.get("status"), "error")
        self.assertIn("Sheet authentication error", data.get("message"))

    def test_post_run_concurrency_lock(self):
        # Simulate lock being held
        SIMULATION_LOCK.acquire()
        try:
            req = urllib.request.Request(f"{self.base_url}/run", method="POST")
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req)
            self.assertEqual(ctx.exception.code, 429)
            data = json.loads(ctx.exception.read().decode("utf-8"))
            self.assertEqual(data.get("status"), "busy")
        finally:
            SIMULATION_LOCK.release()


if __name__ == "__main__":
    unittest.main()
