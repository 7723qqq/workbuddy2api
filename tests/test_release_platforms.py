import io
import json
from pathlib import Path
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

from desktop.common import REPOSITORY
from desktop.updates import UpdateService


def release(version, platforms):
    return {'id':int(version.split('.')[1])+1,'tag_name': 'v'+version,
            'html_url':f'https://github.com/{REPOSITORY}/releases/tag/v{version}',
            'body':'### 更新\n- 修复问题', 'assets':[
                {'name': f'WorkBuddy2API-{version}-{platform}',
                 'digest':'sha256:'+'a'*64,
                 'browser_download_url':f'https://github.com/{REPOSITORY}/releases/download/v{version}/WorkBuddy2API-{version}-{platform}'}
                for platform in platforms]}


class Response(io.BytesIO):
    headers = {'ETag':'test-etag'}


class PlatformTests(unittest.TestCase):
    def test_platform_latest_is_highest_available_semver(self):
        # Deliberately not publication-order sorted: platforms may publish separately.
        rows = [release('99.8.0',['android-arm64.apk']), release('99.2.0',['windows-x64.exe']),
                release('99.10.0',['windows-x64.exe']), release('99.1.0',['windows-x64.exe','android-arm64.apk'])]
        with tempfile.TemporaryDirectory() as work:
            for platform, expected in [('windows','v99.10.0'),('android','v99.8.0')]:
                service = UpdateService(Path(work), platform)
                with patch('urllib.request.urlopen', return_value=Response(json.dumps(rows).encode())):
                    data = service.check()
                self.assertEqual(data['latest']['tag'],expected)
                self.assertEqual(data['platform'],platform)
                self.assertEqual(len(data['releases']),4)
                self.assertTrue(data['latest']['installable'])
                # Platform-specific ETags and history survive 304/offline.
                cached = UpdateService(Path(work),platform)
                with patch('urllib.request.urlopen', side_effect=urllib.error.HTTPError('url',304,'same',{},None)):
                    self.assertEqual(cached.check()['latest']['tag'],expected)
                with patch('urllib.request.urlopen', side_effect=OSError('offline')):
                    self.assertEqual(cached.check()['latest']['tag'],expected)

    def test_other_platform_does_not_prompt_update(self):
        with tempfile.TemporaryDirectory() as work:
            for platform, other in [('windows','android-arm64.apk'),('android','windows-x64.exe')]:
                service = UpdateService(Path(work),platform)
                with patch('urllib.request.urlopen', return_value=Response(json.dumps([release('99.0.0',[other])]).encode())):
                    data = service.check()
                self.assertFalse(data['available'])
                self.assertIsNone(data['latest'])
                self.assertEqual(len(data['releases']),1)

    def test_android_cannot_stage_executable(self):
        with tempfile.TemporaryDirectory() as work:
            service = UpdateService(Path(work),'android')
            with self.assertRaises(ValueError):
                service.stage(1)

    def test_draft_and_prerelease_ignored(self):
        rows = [dict(release('99.0.0',['windows-x64.exe']),draft=True),
                dict(release('99.1.0',['windows-x64.exe']),prerelease=True)]
        with tempfile.TemporaryDirectory() as work:
            with patch('urllib.request.urlopen', return_value=Response(json.dumps(rows).encode())):
                data = UpdateService(Path(work),'windows').check()
            self.assertFalse(data['available'])
            self.assertEqual(data['releases'],[])


if __name__ == '__main__':
    unittest.main()
