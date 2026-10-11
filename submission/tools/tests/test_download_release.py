import importlib.util
from pathlib import Path
from datetime import datetime, timezone
import tempfile
import unittest
import zipfile
SCRIPT = Path(__file__).resolve().parents[1] / "download_release.py"
spec = importlib.util.spec_from_file_location("download_release", SCRIPT)
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)

class ReleaseTests(unittest.TestCase):
    def test_no_early_download(self):
        with self.assertRaises(ValueError):
            release.require_release(datetime(2026, 10, 11, 16, 59, tzinfo=timezone.utc))
        release.require_release(release.RELEASE)
    def test_discovery_uses_submit_only(self):
        html = '<section id="data"><a href="/hackathon/build-the-builder-v3.zip">practice</a></section><section id="submit"><a href="/hackathon/final.zip">final</a></section>'
        self.assertEqual(release.discover_url(html), "https://www.freyavoice.ai/hackathon/final.zip")
        with self.assertRaises(ValueError):
            release.discover_url('<section id="submit">Not published</section>')
    def test_valid_archive_preserves_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with zipfile.ZipFile(root / "kit.zip", "w") as z:
                z.writestr("bank/BRIEF.md", b"Turkish brief\r\n")
                z.writestr("bank/README.md", b"Documents\n")
                z.writestr("bank/customers.xlsx", b"\x00\xff\r\n")
            case, brief, readme = release.unpack(root / "kit.zip", root / "case")
            self.assertEqual(case.name, "bank")
            self.assertEqual(brief.read_bytes(), b"Turkish brief\r\n")
            self.assertEqual((case / "customers.xlsx").read_bytes(), b"\x00\xff\r\n")
    def test_reject_traversal_and_practice(self):
        for member in ("../escape", "/escape", "C:/escape", "bank/CON.txt", "bank/x."):
            with self.subTest(member=member), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                with zipfile.ZipFile(root / "bad.zip", "w") as z:
                    z.writestr(member, "bad")
                with self.assertRaises(ValueError):
                    release.unpack(root / "bad.zip", root / "case")
                self.assertFalse((root / "escape").exists())
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with zipfile.ZipFile(root / "practice.zip", "w") as z:
                z.writestr("README.md", "Kit version 3")
            with self.assertRaises(ValueError):
                release.unpack(root / "practice.zip", root / "case")

if __name__ == "__main__":
    unittest.main()