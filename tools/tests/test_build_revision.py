"""版本戳要对应真实提交，不能把脏目录或历史SHA伪装成当前构建。"""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'release/write_build.py'


class BuildRevisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'tools/release').mkdir(parents=True)
        (self.root / 'backend/src/youju').mkdir(parents=True)
        shutil.copyfile(SCRIPT, self.root / 'tools/release/write_build.py')
        (self.root / '.gitignore').write_text('backend/src/youju/_build.json\n')
        self.git('init', '-q')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('add', '.')
        self.git('-c', 'commit.gpgsign=false', 'commit', '-qm', 'fixture')
        self.sha = self.git('rev-parse', 'HEAD').strip()

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args], text=True)

    def run_build(self, *args):
        return subprocess.run([sys.executable, str(self.root / 'tools/release/write_build.py'), *args], capture_output=True, text=True)

    def test_clean_head_only_outputs_revision(self):
        self.assertEqual(self.run_build().returncode, 0)
        data = json.loads((self.root / 'backend/src/youju/_build.json').read_text())
        self.assertEqual(data, {'revision': self.sha})
        self.assertEqual(self.run_build().returncode, 0)

    def test_dirty_checkout_refused(self):
        (self.root / 'uncommitted.txt').write_text('not in commit')
        self.assertNotEqual(self.run_build().returncode, 0)
        self.assertFalse((self.root / 'backend/src/youju/_build.json').exists())

    def test_history_only_for_explicit_archive_destination(self):
        (self.root / 'next.txt').write_text('second commit')
        self.git('add', '.')
        self.git('-c', 'commit.gpgsign=false', 'commit', '-qm', 'second')
        self.assertNotEqual(self.run_build('--revision', self.sha).returncode, 0)
        destination = self.root / 'archive-stamp.json'
        self.assertEqual(self.run_build('--revision', self.sha, '--output', str(destination)).returncode, 0)
        self.assertEqual(json.loads(destination.read_text()), {'revision': self.sha})

    def test_nonexistent_revision_refused(self):
        self.assertNotEqual(self.run_build('--revision', 'missing-ref').returncode, 0)
        self.assertFalse((self.root / 'backend/src/youju/_build.json').exists())


if __name__ == '__main__':
    unittest.main()
