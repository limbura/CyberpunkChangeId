import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
import zipfile

import build_release as b


class ReleaseTests(unittest.TestCase):
    def test_version_read_without_executing_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'app.py'
            source.write_text('__version__ = "1.2.3"\nraise RuntimeError()\n')
            self.assertEqual(b.read_version(source), '1.2.3')
            source.write_text('__version__ = "../bad"\n')
            with self.assertRaises(ValueError):
                b.read_version(source)

    def test_zip_complete_and_failure_preserves_previous(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = root / 'bundle'
            bundle.mkdir()
            (bundle / 'change_id.exe').write_bytes(b'exe')
            (bundle / 'config.toml').write_text('[database]\n')
            target = root / 'bulds' / 'release.zip'
            b.publish_zip(bundle, target)
            original = target.read_bytes()
            with zipfile.ZipFile(target) as archive:
                self.assertEqual(set(archive.namelist()), {'change_id.exe', 'config.toml'})
            with patch.object(zipfile.ZipFile, 'write', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    b.publish_zip(bundle, target)
            self.assertEqual(target.read_bytes(), original)
            self.assertEqual(list(target.parent.iterdir()), [target])
            (bundle / 'config.toml').unlink()
            with self.assertRaises(RuntimeError):
                b.publish_zip(bundle, root / 'new.zip')
            self.assertFalse((root / 'new.zip').exists())

    def test_build_failure_exit_code(self):
        with patch.object(b, 'build', side_effect=RuntimeError('failed')):
            with patch('sys.argv', ['build_release.py']):
                self.assertEqual(b.main(), 1)
