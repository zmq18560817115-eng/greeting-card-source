"""Font uploads use temporary storage and never modify employees or templates."""
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import ImageFont

from app import fonts, main, settings


class FontTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundled = settings.FONT_DIR / "builtin/sans/NotoSansCJKsc-Regular.otf"
        cls.raw = cls.bundled.read_bytes()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for item in (patch.object(fonts, "FONT_DIR", self.root),
                     patch.object(fonts, "CANDIDATES", []),
                     patch.object(fonts, "_system_dirs", return_value=[]),
                     patch.object(main, "ADMIN_TOKEN", "font-test"),
                     patch("requests.sessions.Session.request", side_effect=AssertionError("No network"))):
            item.start()
            self.addCleanup(item.stop)
        self.client = TestClient(main.app)
        self.addCleanup(self.client.close)

    def upload(self, raw=None, name="Brand.OTF", authorized=True):
        return self.client.post("/api/fonts", files={"file": (name, self.raw if raw is None else raw)},
                                headers={"X-Admin-Token": "font-test"} if authorized else {})

    def test_builtin_fonts_work_without_server_fonts(self):
        with patch.object(fonts, "FONT_DIR", settings.FONT_DIR):
            options = fonts.list_fonts()
            builtin = [item for item in options if item.get("source") == "builtin"]
            self.assertEqual(len(builtin), 2)
            self.assertTrue(all(not Path(item["value"]).is_absolute() for item in builtin))
            self.assertEqual(Path(fonts.resolve()), self.bundled)
            for item in builtin:
                font = ImageFont.truetype(fonts.resolve(item["value"]), 32)
                self.assertGreater(font.getlength("生日快乐，入职周年"), 0)

    def test_uploaded_font_is_listed_resolves_and_deduplicates(self):
        response = self.upload(name="../../outside.OTF")
        self.assertEqual(response.status_code, 200, response.text)
        item = response.json()["font"]
        self.assertEqual(item["source"], "uploaded")
        stored = Path(fonts.resolve(item["value"]))
        self.assertTrue(stored.is_relative_to(self.root / "uploaded"))
        self.assertEqual(stored.read_bytes(), self.raw)
        again = self.upload(name="renamed.otf")
        self.assertEqual(again.json()["font"]["value"], item["value"])
        listed = self.client.get("/api/fonts", headers={"X-Admin-Token": "font-test"}).json()["fonts"]
        self.assertTrue(any(font["value"] == item["value"] for font in listed))
        self.assertEqual(len(list((self.root / "uploaded").iterdir())), 1)

    def test_invalid_uploads_do_not_create_files(self):
        for raw, name in ((b"", "empty.otf"), (b"not a font", "broken.ttf"), (b"text", "script.exe")):
            response = self.upload(raw, name)
            self.assertEqual(response.status_code, 400, response.text)
        with patch.object(fonts, "MAX_UPLOAD_BYTES", 1024):
            response = self.upload(b"a" * 1025)
            self.assertEqual(response.status_code, 400)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_font_api_requires_admin(self):
        self.assertEqual(self.upload(authorized=False).status_code, 401)
        self.assertEqual(self.client.get("/api/fonts").status_code, 401)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_upload_does_not_change_auto_font(self):
        builtin = self.root / "builtin/sans/NotoSansCJKsc-Regular.otf"
        builtin.parent.mkdir(parents=True)
        shutil.copyfile(self.bundled, builtin)
        before = fonts.resolve()
        fonts.save_uploaded_font(self.raw, "new-bold.otf")
        self.assertEqual(fonts.resolve(), before)
        self.assertEqual(fonts.resolve(bold=True), before)

    def test_failed_write_does_not_leave_an_unusable_font(self):
        with patch.object(fonts.os, "replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                fonts.save_uploaded_font(self.raw, "brand.otf")
        self.assertEqual(list((self.root / "uploaded").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
