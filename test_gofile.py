import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import gofile_upload as gf


class Reply(io.BytesIO):
    status = 200

    def __init__(self, data):
        super().__init__(json.dumps(data).encode())


class Connection:
    def __init__(self, reply):
        self.reply = reply
        self.body = bytearray()
        self.headers = {}

    def putrequest(self, method, path):
        self.method, self.path = method, path

    def putheader(self, key, value):
        self.headers[key] = value

    def endheaders(self, data):
        self.body.extend(data)

    def send(self, data):
        self.body.extend(data)

    def getresponse(self):
        return self.reply

    def close(self):
        pass


class GofileTests(unittest.TestCase):
    def test_only_real_gofile_download_pages(self):
        gf.checked_link("https://gofile.io/d/test123")
        for url in ["http://gofile.io/d/test", "https://gofile.io.evil.test/d/a",
                    "https://gofile.my/d/test", "https://u:p@gofile.io/d/a",
                    "https://gofile.io/d/a?token=secret", "https://gofile.io/"]:
            with self.assertRaises(gf.UploadProblem):
                gf.checked_link(url)

    def test_api_envelope_and_html_are_rejected(self):
        for status, body in [(200, b"<html>not api</html>"),
                             (200, b'{"status":"error-token"}'),
                             (403, b"access denied"), (200, b'{"status":"ok","data":[]}' )]:
            with self.assertRaises(gf.UploadProblem):
                gf.parse_response(status, body)

    def fixture(self, root):
        path = Path(root) / "Windows11_Russian_x64.iso"
        content = b"test data for streamed upload" * 100000
        path.write_bytes(content)
        info = {"size": len(content), "sha256": hashlib.sha256(content).hexdigest(),
                "md5": hashlib.md5(content).hexdigest()}
        reply = {"status": "ok", "data": {"size": info["size"], "md5": info["md5"],
                 "id": "file-123", "guestToken": "MUST-NOT-LEAK",
                 "downloadPage": "https://gofile.io/d/test123"}}
        return path, content, info, reply

    def test_streamed_content_length_integrity_and_secret_filter(self):
        with tempfile.TemporaryDirectory() as folder:
            path, content, info, reply = self.fixture(folder)
            conn = Connection(Reply(reply))
            with patch.object(gf.http.client, "HTTPSConnection", return_value=conn):
                result = gf.upload(path, info, "test-secret", lambda: None, lambda s: None)
            self.assertEqual(len(conn.body), int(conn.headers["Content-Length"]))
            self.assertEqual(conn.headers["Authorization"], "Bearer test-secret")
            self.assertIn(content, conn.body)
            self.assertNotIn("guestToken", result)
            self.assertNotIn("MUST-NOT-LEAK", json.dumps(result))
            self.assertNotIn("test-secret", json.dumps(result))
            self.assertEqual(result["sha256"], info["sha256"])

    def test_server_md5_and_size_must_match(self):
        with tempfile.TemporaryDirectory() as folder:
            path, content, info, reply = self.fixture(folder)
            reply["data"]["md5"] = "0" * 32
            with patch.object(gf.http.client, "HTTPSConnection", return_value=Connection(Reply(reply))):
                with self.assertRaises(gf.UploadProblem):
                    gf.upload(path, info, "secret", lambda: None, lambda s: None)

    def test_modified_local_iso_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path, content, info, reply = self.fixture(folder)
            path.write_bytes(b"x" * len(content))
            with patch.object(gf.http.client, "HTTPSConnection", return_value=Connection(Reply(reply))):
                with self.assertRaises(gf.UploadProblem):
                    gf.upload(path, info, "secret", lambda: None, lambda s: None)

    def test_env_token_and_saved_guest_are_reused(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.dict("os.environ", {"GOFILE_TOKEN": "manual"}):
                self.assertEqual(gf.account_token(folder, lambda: None), "manual")
            gf.atomic_json(Path(folder) / "gofile-account.json", {"token": "saved"}, secret=True)
            with patch.dict("os.environ", {"GOFILE_TOKEN": ""}):
                self.assertEqual(gf.account_token(folder, lambda: None), "saved")


if __name__ == "__main__":
    unittest.main()
