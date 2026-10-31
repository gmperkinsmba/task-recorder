"""Native boundary and owned ephemeral backend checks."""
import threading
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

import server
from build_desktop import validated_assets
from desktop_policy import navigation_policy


class DesktopTests(unittest.TestCase):
    def test_asset_manifest_rejects_symlinks(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "real.css").write_text("body{}", encoding="utf-8")
            (root / "link.css").symlink_to(root / "real.css")
            manifest = root / "assets.txt"
            manifest.write_text("link.css\n", encoding="utf-8")
            with self.assertRaises(SystemExit):
                validated_assets(root, manifest)

    def test_make_server_skips_reverse_dns_lookup(self):
        with patch("socket.getfqdn", side_effect=AssertionError("Reverse DNS must not block startup")):
            owned = server.make_server(port=0)
        try:
            self.assertEqual(owned.server_name, "127.0.0.1")
            self.assertGreater(owned.server_port, 0)
            self.assertTrue(owned.daemon_threads)
        finally:
            owned.server_close()

    def test_navigation_boundary(self):
        origin = "http://127.0.0.1:49125/"
        self.assertEqual(navigation_policy(origin, origin, False, False), "allow")
        self.assertEqual(navigation_policy(origin + "app.js", origin, False, False), "allow")
        self.assertEqual(navigation_policy("blob:" + origin + "export", origin, False, True), "download")
        for target in ["file:///etc/passwd", "javascript:alert(1)", "data:text/html,hello", "http://127.0.0.1:49126/", "http://127.0.0.1.evil.test:49125/", "http://user@127.0.0.1:49125/", "blob:https://evil.test/export"]:
            self.assertEqual(navigation_policy(target, origin, False, False), "cancel")
        self.assertEqual(navigation_policy("https://example.com", origin, True, False), "external")
        self.assertEqual(navigation_policy("https://example.com", origin, False, False), "cancel")

    def test_owned_ephemeral_server_auth_boundary_and_shutdown(self):
        owned = server.make_server(port=0)
        thread = threading.Thread(target=owned.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{owned.server_port}/"
        try:
            with urlopen(url, timeout=3) as response:
                self.assertEqual(response.status, 200)
                self.assertIn(b"macroforge-token", response.read())
            with self.assertRaises(HTTPError) as failure:
                urlopen(Request(url, headers={"Host": "evil.test"}), timeout=3)
            self.assertEqual(failure.exception.code, 403)
            failure.exception.close()
        finally:
            owned.shutdown()
            owned.server_close()
            thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(owned.fileno(), -1)


if __name__ == "__main__":
    unittest.main()
