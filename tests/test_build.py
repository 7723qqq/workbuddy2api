import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import build


class Console:
    def __init__(self, text):
        self.text = iter(text)
        self.next = None

    def kbhit(self):
        self.next = next(self.text, None)
        return self.next is not None

    def getwch(self):
        return self.next


class BuildTests(unittest.TestCase):
    def prompt(self, text):
        timer = [0]
        def pause(seconds):
            timer[0] += seconds
        with contextlib.redirect_stdout(io.StringIO()):
            return build.timed_version('1.0.0', console=Console(text), now=lambda: timer[0], pause=pause)

    def test_timeout_and_partial_input_keep_version(self):
        self.assertEqual(self.prompt(''), '1.0.0')
        self.assertEqual(self.prompt('1.0.2'), '1.0.0')

    def test_enter_and_backspace(self):
        self.assertEqual(self.prompt('\r'), '1.0.0')
        self.assertEqual(self.prompt('1.0.3\b2\r'), '1.0.2')

    def test_version_ranges(self):
        self.assertEqual(build.validate_version('v1.9.10'), '1.9.10')
        for bad in ['1.2', '1.0.1-beta', '01.0.0', '1.1000.0', '1.0.1000', '2101.0.0', '0.0.0']:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                build.validate_version(bad)

    def test_both_builders_read_same_version_and_produce_manifest(self):
        import hashlib
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            metadata = root / 'desktop/version.json'
            metadata.parent.mkdir()
            metadata.write_text(json.dumps({'version':'1.0.0','repository':'test/repo','asset':'keep'}))
            calls = []
            def fake_run(command, log, env):
                name = str(command[1])
                if name.endswith('build_desktop.py') or name.endswith('build_android.py'):
                    version = json.loads(metadata.read_text())['version']
                    calls.append((name, version))
                    suffix = 'windows-x64.exe' if name.endswith('build_desktop.py') else 'android-arm64.apk'
                    target = root / 'dist' / ('' if suffix.endswith('.exe') else 'android') / f'WorkBuddy2API-{version}-{suffix}'
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(b'test artifact')
                    target.with_suffix(target.suffix+'.sha256').write_text(hashlib.sha256(target.read_bytes()).hexdigest()+'  '+target.name)
            with patch.object(build, 'ROOT', root), patch.object(build, 'METADATA', metadata), \
                 patch.object(build, 'tools_for_android', return_value=(root,root,root/'gradle.bat')), \
                 patch.object(build, 'run', side_effect=fake_run), patch('sys.version_info',(3,10)), \
                 patch('sys.argv',['build.py','--version','1.0.2']), contextlib.redirect_stdout(io.StringIO()):
                build.main()
            self.assertEqual([version for _,version in calls], ['1.0.2','1.0.2'])
            manifest = json.loads((root/'dist/build-1.0.2.json').read_text())
            self.assertEqual(len(manifest['files']), 2)
            self.assertEqual(json.loads(metadata.read_text())['asset'], 'keep')

    def test_dry_run_does_not_change_version(self):
        with patch.object(build, 'tools_for_android', return_value=(Path('jdk'),Path('sdk'),Path('gradle.bat'))), \
             patch('sys.version_info',(3,10)), patch('sys.argv',['build.py','--dry-run','--version','99.0.0']), \
             patch.object(build.Path, 'write_text', side_effect=AssertionError('mutated')), \
             contextlib.redirect_stdout(io.StringIO()):
            build.main()


if __name__ == '__main__':
    unittest.main()
