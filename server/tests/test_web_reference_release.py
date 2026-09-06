"""Synthetic filesystem and mocked Docker only; never touches deployment paths."""
import argparse
import io
from pathlib import Path
import tarfile
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from deploy import release_web_reference as release


class ReferenceReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        for directory in ('dist', 'server', 'config'):
            (self.root / directory).mkdir()
        (self.root / 'dist/index.html').write_text('old site')
        (self.root / 'server/app.py').write_text('old server')
        (self.root / 'REVISION').write_text('a' * 40)
        self.archive = self.root / 'candidate.tar.gz'
        self.bundle({'dist/index.html': 'new site', 'server/monthly_review.py': 'synthetic'})
        self.report = self.root / 'input-report.json'
        self.report.write_text('synthetic private report')
        self.args = argparse.Namespace(expected='a'*40, revision='b'*40,
            archive=self.archive, report=self.report,
            archive_sha256=release.digest(self.archive), report_sha256=release.digest(self.report))
        self.root_patch = patch.object(release, 'ROOT', self.root)
        self.root_patch.start()
        self.command = patch.object(release, 'command', return_value='')
        self.command.start()
        self.previous_umask = release.os.umask(0o022)

    def tearDown(self):
        release.os.umask(self.previous_umask)
        self.command.stop()
        self.root_patch.stop()
        self.temp.cleanup()

    def bundle(self, files):
        with tarfile.open(self.archive, 'w:gz') as bundle:
            for name, value in files.items():
                body = value.encode()
                member = tarfile.TarInfo(name)
                member.size = len(body)
                bundle.addfile(member, io.BytesIO(body))

    def test_success_keeps_private_file_outside_site(self):
        def denied(url, **kwargs):
            status = 401 if '/api/' in url else 404
            raise HTTPError(url, status, 'Denied', {}, None)
        with patch.object(release, 'recreate'), patch.object(release.urllib.request, 'urlopen', side_effect=denied):
            release.run(self.args)
        self.assertEqual((self.root / 'REVISION').read_text().strip(), 'b'*40)
        self.assertEqual((self.root / 'dist/index.html').read_text(), 'new site')
        self.assertEqual((self.root / 'config/monthly-reconciliation-review.json').read_text(), 'synthetic private report')
        self.assertFalse((self.root / 'dist/monthly-reconciliation-review.json').exists())

    def test_failed_candidate_restores_web_and_existing_report(self):
        target = self.root / 'config/monthly-reconciliation-review.json'
        target.write_text('old private report')
        with patch.object(release, 'recreate', side_effect=[RuntimeError('synthetic failure'), None]):
            with self.assertRaisesRegex(RuntimeError, 'synthetic failure'):
                release.run(self.args)
        self.assertEqual((self.root / 'REVISION').read_text(), 'a'*40)
        self.assertEqual((self.root / 'dist/index.html').read_text(), 'old site')
        self.assertEqual((self.root / 'server/app.py').read_text(), 'old server')
        self.assertEqual(target.read_text(), 'old private report')

    def test_wrong_baseline_does_not_stop_production(self):
        self.args.expected = 'c'*40
        with patch.object(release, 'command') as command:
            with self.assertRaisesRegex(RuntimeError, 'baseline changed'):
                release.run(self.args)
            command.assert_not_called()

    def test_rollback_restores_files_even_if_candidate_container_is_missing(self):
        stop_count = 0
        def docker(*args):
            nonlocal stop_count
            if args[:2] == ('docker', 'stop'):
                stop_count += 1
                if stop_count == 2:
                    raise subprocess.CalledProcessError(1, args, output='No such container')
            return ''
        with patch.object(release, 'command', side_effect=docker), patch.object(release, 'recreate', side_effect=[RuntimeError('candidate missing'), None]) as recreate:
            with self.assertRaisesRegex(RuntimeError, 'candidate missing'):
                release.run(self.args)
            self.assertEqual(recreate.call_count, 2)
        self.assertEqual((self.root / 'REVISION').read_text(), 'a'*40)
        self.assertEqual((self.root / 'dist/index.html').read_text(), 'old site')
        self.assertEqual((self.root / 'server/app.py').read_text(), 'old server')
        self.assertFalse((self.root / 'config/monthly-reconciliation-review.json').exists())

    def test_archive_rejects_path_escape_and_private_assets(self):
        for files in ({'../escape': 'bad'}, {'dist/report.json': 'bad'},
                      {'dist/index.html': 'ledgerbridge.monthly-review-package.v1', 'server/monthly_review.py': ''}):
            self.bundle(files)
            with tempfile.TemporaryDirectory(dir=self.root) as stage:
                with self.assertRaises(RuntimeError):
                    release.validate_archive(self.archive, Path(stage))


if __name__ == '__main__':
    unittest.main()
