"""Scoped VM103 Web-only cutover with a private reference report and rollback.

Run only under the external LedgerBridge release lock. The archive must contain
dist/ and server/ from the exact tested Git revision. Never packages state, env,
credentials or ledger data. Does not migrate Core or change business facts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import time
import urllib.error
import urllib.request

ROOT = Path('/home/aiadmin/services/ledgerbridge-web-auth-preview')
CONTAINER = 'ledgerbridge-web-core'


def command(*args):
    return subprocess.check_output(args, cwd=ROOT, stderr=subprocess.STDOUT, text=True)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def health():
    for _ in range(40):
        state = json.loads(command('docker', 'inspect', CONTAINER))[0]['State']
        if state.get('Health', {}).get('Status') == 'healthy':
            return
        time.sleep(1)
    raise RuntimeError('Web health check did not pass')


def recreate():
    command('docker', 'compose', '-f', 'compose.core-backed.yaml', 'up', '-d', '--no-deps', '--force-recreate', 'web')
    health()


def validate_archive(archive, destination):
    with tarfile.open(archive, 'r:gz') as bundle:
        names = set()
        for entry in bundle.getmembers():
            path = PurePosixPath(entry.name)
            if (path.is_absolute() or '..' in path.parts or not path.parts
                    or path.parts[0] not in {'dist', 'server'}
                    or not (entry.isfile() or entry.isdir()) or entry.name in names):
                raise RuntimeError('Invalid release archive')
            names.add(entry.name)
        bundle.extractall(destination, filter='data')
    if not (destination / 'dist/index.html').is_file() or not (destination / 'server/monthly_review.py').is_file():
        raise RuntimeError('Incomplete release archive')
    for path in (destination / 'dist').rglob('*'):
        if path.is_file() and (path.suffix == '.json' or b'ledgerbridge.monthly-review-package.v1' in path.read_bytes()):
            raise RuntimeError('Private data or unexpected JSON in public assets')
    # Windows tar entries can omit POSIX directory modes. These two trees contain
    # only public assets and application code; never normalize private config.
    for name in ('dist', 'server'):
        base = destination / name
        for path in (base, *base.rglob('*')):
            os.chmod(path, 0o755 if path.is_dir() else 0o644)


def preflight_container(candidate):
    owner = (ROOT / 'config').stat()
    user = f'{owner.st_uid}:{owner.st_gid}'
    if command('docker', 'inspect', '--format', '{{.Config.User}}', CONTAINER).strip() != user:
        raise RuntimeError('Private report owner differs from the running service identity')
    image = command('docker', 'inspect', '--format', '{{.Image}}', CONTAINER).strip()
    command('docker', 'run', '--rm', '--pull', 'never', '--network', 'none',
        '--read-only', '--user', user, '--cap-drop', 'ALL',
        '--security-opt', 'no-new-privileges:true', '--workdir', '/app',
        '-e', 'PYTHONPATH=/vendor', '-e', 'PYTHONDONTWRITEBYTECODE=1',
        '-v', f'{candidate / "server"}:/app/server:ro',
        '-v', f'{candidate / "dist"}:/site:ro',
        '-v', f'{ROOT / "vendor"}:/vendor:ro',
        '-v', f'{candidate / "private-report.json"}:/reference.json:ro',
        image, 'python', '-c',
        'from pathlib import Path; import server.app; '
        'from server.monthly_review import load_monthly_review; '
        'assert Path("/site/index.html").read_bytes(); '
        'r=load_monthly_review(Path("/reference.json"),"2026-08"); '
        'assert r["authority"]=="NON_AUTHORITATIVE_REFERENCE"')


def run(args):
    os.umask(0o077)
    if ROOT.resolve() != ROOT or ROOT.is_symlink():
        raise RuntimeError('Unexpected deployment root')
    if not re.fullmatch('[0-9a-f]{40}', args.revision) or not re.fullmatch('[0-9a-f]{40}', args.expected):
        raise RuntimeError('Exact Git revisions required')
    if (ROOT / 'REVISION').read_text().strip() != args.expected:
        raise RuntimeError('Production baseline changed')
    if digest(args.archive) != args.archive_sha256 or digest(args.report) != args.report_sha256:
        raise RuntimeError('Frozen input hash mismatch')
    for path in (ROOT, ROOT / 'server', ROOT / 'dist', ROOT / 'REVISION', ROOT / 'config'):
        if not os.access(path, os.W_OK):
            raise RuntimeError('Deployment file ownership requires the authorized scoped maintenance account; nothing stopped')
    release = ROOT / ('.monthly-release-' + args.revision[:12])
    backup = ROOT / ('.monthly-before-' + args.revision[:12])
    release.mkdir(mode=0o700)
    backup.mkdir(mode=0o700)
    validate_archive(args.archive, release)
    report = ROOT / 'config/monthly-reconciliation-review.json'
    if report.is_symlink():
        raise RuntimeError('Private report must not be a symlink')
    shutil.copyfile(args.report, release / 'private-report.json')
    os.chmod(release / 'private-report.json', 0o600)
    if hasattr(os, 'chown'):
        owner = (ROOT / 'config').stat()
        os.chown(release / 'private-report.json', owner.st_uid, owner.st_gid)
    # Validate with the same candidate parser before stopping anything.
    command('python3', '-S', '-c',
        'import sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); '
        'from monthly_review import load_monthly_review; '
        'r=load_monthly_review(Path(sys.argv[2]),"2026-08"); '
        'assert r["authority"]=="NON_AUTHORITATIVE_REFERENCE"',
        str(release / 'server'), str(release / 'private-report.json'))
    preflight_container(release)
    existing_report = report.exists()
    for name in ('dist', 'server'):
        shutil.copytree(ROOT / name, backup / name)
    shutil.copy2(ROOT / 'REVISION', backup / 'REVISION')
    if existing_report:
        shutil.copy2(report, backup / 'private-report.json')
    manifest = dict(before=args.expected, after=args.revision,
                    archive_sha256=args.archive_sha256, report_sha256=args.report_sha256,
                    report_preexisted=existing_report, core_changed=False, ledger_written=False)
    (backup / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    # Prove backup contents before stop. Backups remain private and recoverable.
    for name in ('dist', 'server'):
        for file in (ROOT / name).rglob('*'):
            if file.is_file() and digest(file) != digest(backup / file.relative_to(ROOT)):
                raise RuntimeError('Backup verification failed')
    if (ROOT / 'REVISION').read_text().strip() != args.expected:
        raise RuntimeError('Production changed during staging')
    swapped = []
    report_replaced = False
    revision_replaced = False
    try:
        command('docker', 'stop', CONTAINER)
        for name in ('dist', 'server'):
            (ROOT / name).rename(release / (name + '.previous'))
            swapped.append(name)
            (release / name).rename(ROOT / name)
        # Keep bind-mounted config directory intact; replace only our own report.
        shutil.copyfile(release / 'private-report.json', report.with_suffix('.staged'))
        os.chmod(report.with_suffix('.staged'), 0o600)
        if hasattr(os, 'chown'):
            owner = (ROOT / 'config').stat()
            os.chown(report.with_suffix('.staged'), owner.st_uid, owner.st_gid)
        os.replace(report.with_suffix('.staged'), report)
        report_replaced = True
        (ROOT / 'REVISION').write_text(args.revision + '\n')
        revision_replaced = True
        recreate()
        for path, expected in (('/api/v1/monthly-reconciliation-reviews/2026-08', 401),
                               ('/monthly-reconciliation-review.json', 404),
                               ('/config/monthly-reconciliation-review.json', 404)):
            try:
                urllib.request.urlopen('http://127.0.0.1:8781' + path, timeout=5)
                raise RuntimeError('Unauthenticated report access unexpectedly succeeded')
            except urllib.error.HTTPError as error:
                if error.code != expected:
                    raise RuntimeError('Access guard smoke check failed') from None
        print(json.dumps(dict(status='deployed', revision=args.revision, backup=str(backup),
                              report_sha256=args.report_sha256)))
    except BaseException:
        # A failed force-recreate may already have removed the container. Restore
        # files regardless; missing-container stop must never prevent rollback.
        try:
            command('docker', 'stop', CONTAINER)
        except subprocess.CalledProcessError:
            pass
        for name in reversed(swapped):
            if (ROOT / name).exists():
                (ROOT / name).rename(release / (name + '.failed'))
            (release / (name + '.previous')).rename(ROOT / name)
        if report_replaced:
            if existing_report:
                shutil.copy2(backup / 'private-report.json', report)
            else:
                report.rename(release / 'private-report.failed.json')
        if revision_replaced:
            shutil.copy2(backup / 'REVISION', ROOT / 'REVISION')
        recreate()
        print(json.dumps(dict(status='rolled_back', revision=args.expected, backup=str(backup))))
        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    for name in ('expected', 'revision', 'archive-sha256', 'report-sha256'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    run(parser.parse_args())
