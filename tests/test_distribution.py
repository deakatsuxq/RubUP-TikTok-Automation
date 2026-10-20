"""Cross-process exclusion and a distributable, private source archive."""
import hashlib
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

from application import AppService
from config import Config, save_config
from scheduler import ProcessLock
from tools.package_project import build
from validators import ValidationError


def test_worker_lock_blocks_independent_process_and_recovers(tmp_path):
    lock_path = tmp_path / 'worker.lock'
    lock = ProcessLock(lock_path)
    probe = (
        'from scheduler import ProcessLock; import sys; '
        'lock=ProcessLock(sys.argv[1]); ok=lock.acquire(); '
        'print(ok); lock.release()'
    )
    def check():
        result = subprocess.run([sys.executable, '-c', probe, str(lock_path)],
                                capture_output=True, text=True, timeout=10, check=True)
        return result.stdout.strip()
    assert lock.acquire()
    try:
        assert check() == 'False'
    finally:
        lock.release()
    assert check() == 'True'


def test_two_services_cannot_prepare_same_database_at_once(tmp_path):
    path = tmp_path / 'config.yaml'
    save_config(Config(db_path=str(tmp_path / 'history.sqlite3')), path)
    first, second = AppService(path), AppService(path)
    first._begin_cycle()
    try:
        with pytest.raises(ValidationError, match='экземпляр'):
            second.prepare_pending()
    finally:
        first._end_cycle()
        first.close()
    assert second.prepare_pending()['prepared'] == 0
    second.close()


def test_real_mode_cannot_use_reserved_dry_run_database(tmp_path):
    path = tmp_path / 'config.yaml'
    save_config(Config(db_path=str(tmp_path / 'data/dry_run.sqlite3')), path)
    with pytest.raises(ValueError, match='зарезервирована'):
        AppService(path)


def test_zip_excludes_runtime_data_and_has_verified_source_manifest(tmp_path):
    archive_path = build(tmp_path / 'release.zip')
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
        relative = [str(Path(n).relative_to('RubUP-TikTok-Automation')) for n in names]
        assert '.env.example' in relative and 'main.py' in relative
        assert '.env' not in relative
        assert not any('.git/' in n or '__pycache__' in n or '.sqlite3' in n for n in names)
        assert not any(n.startswith(('data/', 'images/', 'logs/')) and not n.endswith('.gitkeep') for n in relative)
        manifest = archive.read('RubUP-TikTok-Automation/SHA256SUMS.txt').decode()
        for line in manifest.splitlines():
            digest, path = line.split('  ', 1)
            assert hashlib.sha256(archive.read('RubUP-TikTok-Automation/' + path)).hexdigest() == digest
