"""Exercise the shipped entry point without pytest's archive import path."""
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def test_v4_startup_does_not_load_archived_ai_runtime(tmp_path):
    probe = '''
import json, sys
from automation_service import AutomationService
service = AutomationService(sys.argv[1])
try:
    print(json.dumps({'total': service.stats()['total'], 'archive_modules': [
        name for name, module in sys.modules.items()
        if '/legacy_v3/' in str(getattr(module, '__file__', '')).replace('\\\\', '/')
    ]}))
finally:
    service.close()
'''
    result = subprocess.run([sys.executable, '-c', probe, str(tmp_path / 'config.yaml')],
                            cwd=ROOT, capture_output=True, text=True, timeout=15, check=True)
    assert json.loads(result.stdout) == {'total': 0, 'archive_modules': []}
    assert not (tmp_path / 'config.yaml').exists()


def test_cli_demo_prepares_50_videos_and_100_user_bank_assignments(tmp_path):
    config = tmp_path / 'Моя программа' / 'config.yaml'
    result = subprocess.run([sys.executable, str(ROOT / 'main.py'), '--config', str(config), '--batch-demo'],
                            capture_output=True, text=True, encoding='utf-8', timeout=20, check=True)
    report = json.loads(result.stdout)
    assert report['videos'] == 50
    assert report['total'] == report['remaining'] == report['used_comments'] == 100
    assert [profile['total'] for profile in report['profiles']] == [50, 50]
    assert report['published'] == report['completed'] == report['telegram_sent'] == report['api_requests'] == 0
    assert report['is_test'] is True
    assert not config.exists()
    assert not (config.parent / 'data' / 'history.sqlite3').exists()
