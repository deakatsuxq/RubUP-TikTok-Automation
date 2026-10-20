"""Credential persistence never prints, exports, or packages secret values."""
from pathlib import Path
import json
import os
import stat

import pytest

from credentials import CredentialError, CredentialStore
from user_state import UserState


class FakeProtector:
    """A reversible test double, deliberately not production encryption."""
    def protect(self, raw):
        return b'test-encrypted:' + bytes(b ^ 0xA5 for b in raw)

    def unprotect(self, raw):
        assert raw.startswith(b'test-encrypted:')
        return bytes(b ^ 0xA5 for b in raw[len(b'test-encrypted:'):])


def test_legacy_env_read_and_environment_are_not_modified(tmp_path):
    path = tmp_path / '.env'
    path.write_text('# existing file\nOPENAI_API_KEY=local-value\nOTHER=value\n', encoding='utf-8')
    inherited = {'OPENAI_API_KEY': 'inherited', 'TELEGRAM_CHAT_ID': '24'}
    store = CredentialStore(tmp_path, windows=False, environ=inherited)
    assert store.get('OPENAI_API_KEY') == 'local-value'
    assert store.get('TELEGRAM_CHAT_ID') == '24'
    assert store.get('TELEGRAM_BOT_TOKEN') == ''
    assert inherited == {'OPENAI_API_KEY': 'inherited', 'TELEGRAM_CHAT_ID': '24'}
    assert path.read_text() == '# existing file\nOPENAI_API_KEY=local-value\nOTHER=value\n'


def test_private_env_updates_preserve_unrelated_lines_and_roundtrip_special_chars(tmp_path):
    path = tmp_path / '.env'
    unrelated = '# keep this explanation\nOTHER="value with spaces"\nMULTILINE="one\ntwo"\n'
    path.write_text(unrelated + "OPENAI_API_KEY='old'\nOPENAI_API_KEY=duplicate\n", encoding='utf-8')
    store = CredentialStore(tmp_path, windows=False, environ={})
    key = "my key'with\\characters"
    store.update({'OPENAI_API_KEY': key, 'TELEGRAM_CHAT_ID': '-100123'})
    assert path.read_text().startswith(unrelated)
    assert path.read_text().count('OPENAI_API_KEY=') == 1
    assert store.get('OPENAI_API_KEY') == key
    assert store.get('TELEGRAM_CHAT_ID') == '-100123'
    if os.name != 'nt':
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(tmp_path.glob('.rubup-private-*'))


def test_replacing_multiline_credential_removes_old_continuation(tmp_path):
    path = tmp_path / '.env'
    path.write_text("OPENAI_API_KEY='old\nsecret'\nOTHER=keep\n", encoding='utf-8')
    store = CredentialStore(tmp_path, windows=False, environ={})
    store.update({'OPENAI_API_KEY': 'new'})
    assert store.get('OPENAI_API_KEY') == 'new'
    assert 'old' not in path.read_text() and 'secret' not in path.read_text()
    assert 'OTHER=keep' in path.read_text()


def test_empty_saved_value_suppresses_inherited_key_without_mutating_environment(tmp_path):
    inherited = {'OPENAI_API_KEY': 'still-owned-by-parent-shell'}
    store = CredentialStore(tmp_path, windows=False, environ=inherited)
    store.update({'OPENAI_API_KEY': ''})
    assert store.get('OPENAI_API_KEY') == ''
    assert inherited['OPENAI_API_KEY'] == 'still-owned-by-parent-shell'
    store.update({'OPENAI_API_KEY': None})
    assert store.get('OPENAI_API_KEY') == ''


def test_windows_new_keys_encrypted_and_legacy_values_remain_compatible(tmp_path):
    old = '# legacy credentials are not deleted\nTELEGRAM_CHAT_ID=-100123\nOTHER=keep\n'
    (tmp_path / '.env').write_text(old, encoding='utf-8')
    store = CredentialStore(tmp_path, windows=True, protector=FakeProtector(), environ={})
    store.update({'OPENAI_API_KEY': 'sensitive-key', 'TELEGRAM_BOT_TOKEN': '123:secret'})
    raw = (tmp_path / 'data/credentials.dpapi').read_bytes()
    assert b'sensitive-key' not in raw and b'123:secret' not in raw
    assert (tmp_path / '.env').read_text() == old
    restored = CredentialStore(tmp_path, windows=True, protector=FakeProtector(), environ={})
    assert restored.get('OPENAI_API_KEY') == 'sensitive-key'
    assert restored.get('TELEGRAM_CHAT_ID') == '-100123'
    assert 'Windows' in restored.storage_label


def test_failed_dpapi_never_falls_back_to_plaintext_and_sanitizes(tmp_path):
    class Broken:
        def protect(self, raw):
            raise RuntimeError('sensitive-key')
    store = CredentialStore(tmp_path, windows=True, protector=Broken(), environ={})
    with pytest.raises(CredentialError) as error:
        store.update({'OPENAI_API_KEY': 'sensitive-key'})
    assert 'sensitive-key' not in str(error.value)
    assert not (tmp_path / '.env').exists()
    assert not (tmp_path / 'data/credentials.dpapi').exists()


def test_corrupt_encrypted_store_is_preserved_not_silently_replaced(tmp_path):
    data = tmp_path / 'data'
    data.mkdir()
    path = data / 'credentials.dpapi'
    path.write_bytes(b'broken encrypted file')
    store = CredentialStore(tmp_path, windows=True, protector=FakeProtector(), environ={})
    with pytest.raises(CredentialError):
        store.update({'OPENAI_API_KEY': 'new'})
    assert path.read_bytes() == b'broken encrypted file'


@pytest.mark.parametrize('updates', [
    {'UNRELATED': 'value'}, {'OPENAI_API_KEY': 'secret\nSECOND=value'},
    {'OPENAI_API_KEY': 'secret\rvalue'}, {'OPENAI_API_KEY': 42},
])
def test_invalid_credential_settings_do_not_write_files(tmp_path, updates):
    store = CredentialStore(tmp_path, windows=False, environ={})
    with pytest.raises(CredentialError):
        store.update(updates)
    assert not (tmp_path / '.env').exists()


def test_first_run_state_survives_restart_without_touching_history(tmp_path):
    db = tmp_path / 'history.sqlite3'
    db.write_bytes(b'existing database fixture, not opened by this test')
    path = tmp_path / 'ui_state.json'
    state = UserState(path)
    assert state.get('onboarding_complete', False) is False
    state.set('onboarding_complete', True)
    assert UserState(path).get('onboarding_complete') is True
    assert db.read_bytes() == b'existing database fixture, not opened by this test'
    with pytest.raises(ValueError):
        state.set('OPENAI_API_KEY', 'must never be stored here')
    assert 'OPENAI' not in path.read_text()


def test_corrupt_wizard_state_not_overwritten(tmp_path):
    path = tmp_path / 'ui_state.json'
    path.write_text('not json', encoding='utf-8')
    with pytest.raises(ValueError):
        UserState(path).set('onboarding_complete', True)
    assert path.read_text() == 'not json'
