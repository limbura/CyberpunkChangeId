import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from release_notes import library_release_notes


class ReleaseNotesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.archive = Path(self.tmp.name) / "change_id_v1.0.4_windows_x64.zip"
        self.info = {
            "application": "change_id",
            "version": "1.0.4",
            "reader_version": "1.8.1",
            "printer_version": "1.3.0",
            "timini_revision": "679fc0650e61c7ab28b44542b9b9ba000e45aa78",
        }

    def make_archive(self, info=None, include_info=True):
        with zipfile.ZipFile(self.archive, "w") as archive:
            archive.writestr("change_id.exe", "executable")
            archive.writestr("config.toml", "[database]\n")
            if include_info:
                archive.writestr("BUILD_INFO.json", json.dumps(
                    self.info if info is None else info))

    def test_release_notes_use_versions_inside_zip(self):
        self.make_archive()
        notes = library_release_notes(self.archive, "v1.0.4")
        self.assertIn("wcopy_reader` v1.8.1", notes)
        self.assertIn("cyberwall_id_printer` v1.3.0", notes)
        self.assertIn(self.info["timini_revision"], notes)

    def test_release_tag_must_match_archive(self):
        self.make_archive()
        with self.assertRaises(ValueError):
            library_release_notes(self.archive, "v1.0.5")
        with self.assertRaises(ValueError):
            library_release_notes(self.archive, "not-a-tag")

    def test_missing_or_invalid_metadata_is_rejected(self):
        self.make_archive(include_info=False)
        with self.assertRaises(ValueError):
            library_release_notes(self.archive, "v1.0.4")
        for key in ("reader_version", "printer_version", "timini_revision"):
            with self.subTest(key=key):
                info = dict(self.info)
                del info[key]
                self.make_archive(info)
                with self.assertRaises(ValueError):
                    library_release_notes(self.archive, "v1.0.4")
