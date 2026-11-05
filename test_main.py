import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import main


class Response(io.BytesIO):
    def __init__(self, content, length=None):
        super().__init__(content)
        self.headers = {"Content-Length": str(len(content) if length is None else length)}

    def geturl(self):
        return "https://software.download.prss.microsoft.com/test.iso"


class Opener:
    def __init__(self, content, length=None):
        self.content, self.length = content, length

    def open(self, *args, **kwargs):
        return Response(self.content, self.length)


class BotTests(unittest.TestCase):
    def test_microsoft_domain_only(self):
        main.official_url("https://software.download.prss.microsoft.com/a.iso")
        for value in ["http://download.microsoft.com/a", "https://microsoft.com.evil.test/a",
                      "https://evil.test/microsoft.com", "https://u:p@microsoft.com/a",
                      "https://microsoft.com:8443/a", "https://127.0.0.1/a"]:
            with self.subTest(url=value), self.assertRaises(main.Problem):
                main.official_url(value)

    def test_page_parser_selects_x64_and_russian_hash(self):
        digest = "a" * 64
        source = ('<option value="5">Windows 11 (multi-edition ISO for x64 devices)</option>'
                  '<option value="6">Windows 11 for ARM64</option>'
                  '<table><tr><td>Russian  64-bit</td><td>' + digest + '</td></tr></table>')
        self.assertEqual(main.page_details(source), ("5", digest))
        with self.assertRaises(main.Problem):
            main.page_details(source.replace("Russian", "English"))

    def test_download_integrity_and_incomplete_cleanup(self):
        content = bytearray(65536)
        content[32769:32774] = b"CD001"
        content = bytes(content)
        sha = hashlib.sha256(content).hexdigest()
        with tempfile.TemporaryDirectory() as folder, patch.object(main, "MIN_ISO_BYTES", 1):
            target = Path(folder) / "test.iso"
            info = main.download_iso(Opener(content), "https://download.microsoft.com/a.iso",
                                     sha, target, lambda: None, lambda s: None)
            self.assertEqual(target.read_bytes(), content)
            self.assertEqual(info["md5"], hashlib.md5(content).hexdigest())
            target.unlink()
            with self.assertRaises(main.Problem):
                main.download_iso(Opener(content), "https://download.microsoft.com/a.iso",
                                  "0" * 64, target, lambda: None, lambda s: None)
            self.assertFalse(target.exists())
            self.assertFalse(target.with_suffix(".partial").exists())
            with self.assertRaises(main.Problem):
                main.download_iso(Opener(content, len(content) + 1),
                                  "https://download.microsoft.com/a.iso", sha, target,
                                  lambda: None, lambda s: None)
            self.assertFalse(target.exists())

    def test_cancellation_never_commits_iso(self):
        def cancel():
            raise main.Problem("cancelled")
        with tempfile.TemporaryDirectory() as folder, patch.object(main, "MIN_ISO_BYTES", 1):
            target = Path(folder) / "test.iso"
            with self.assertRaises(main.Problem):
                main.download_iso(Opener(b"x" * 65536), "https://download.microsoft.com/a.iso",
                                  "0" * 64, target, cancel, lambda s: None)
            self.assertFalse(target.exists())
            self.assertFalse(target.with_suffix(".partial").exists())

    def test_html_cannot_be_committed_as_iso(self):
        content = b"<html>error</html>" * 3000
        with tempfile.TemporaryDirectory() as folder, patch.object(main, "MIN_ISO_BYTES", 1):
            target = Path(folder) / "test.iso"
            with self.assertRaises(main.Problem):
                main.download_iso(Opener(content), "https://download.microsoft.com/a.iso",
                                  hashlib.sha256(content).hexdigest(), target,
                                  lambda: None, lambda s: None)
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
