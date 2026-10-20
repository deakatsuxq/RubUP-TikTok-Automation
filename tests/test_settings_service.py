"""Live settings, API-model visibility, and read-only explicit connection probes."""
from dataclasses import replace
import json

import httpx
import pytest

from application import AppService
from config import Config, load_config, save_config
from credentials import CredentialError
from validators import ValidationError


@pytest.fixture
def service(tmp_path, monkeypatch):
    for name in ('OPENAI_API_KEY', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID'):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / 'config.yaml'
    save_config(Config(db_path=str(tmp_path / 'history.sqlite3')), path)
    app = AppService(path)
    yield app
    app.close()


def inject_openai(service, route):
    service.generator.http.close()
    service.generator.http.client = httpx.Client(transport=httpx.MockTransport(route))
    service.generator.http.owns_client = True


def inject_telegram(service, route):
    service.telegram.close()
    service.telegram.client = httpx.Client(transport=httpx.MockTransport(route))
    service.telegram._owns_client = True


def test_credentials_and_model_apply_live_and_survive_restart(service):
    old_generator = service.generator
    candidate = replace(service.config, model='gpt-4o-mini', max_comment_length=90,
                        input_price_per_million=2.0, output_price_per_million=4.0)
    result = service.settings.apply(config=candidate, credentials={
        'OPENAI_API_KEY': 'private-openai', 'TELEGRAM_BOT_TOKEN': '123:private-bot',
        'TELEGRAM_CHAT_ID': '-100321',
    })
    assert result['restart_required'] is False
    assert service.generator is not old_generator
    assert service.generator.api_key == 'private-openai'
    assert service.generator.config.model == 'gpt-4o-mini'
    assert service.publisher.config.max_comment_length == 90
    assert service.telegram._token == '123:private-bot'
    assert service.telegram._chat_id == '-100321'
    assert 'private-openai' not in service.config_path.read_text()
    assert 'private-bot' not in service.config_path.read_text()
    second = AppService(service.config_path)
    try:
        assert second.config.model == 'gpt-4o-mini'
        assert second.generator.api_key == 'private-openai'
        assert second.telegram._chat_id == '-100321'
    finally:
        second.close()


def test_status_exposes_only_presence_and_keys_are_available_to_masked_editor(service):
    service.settings.apply(credentials={'OPENAI_API_KEY': 'private-openai'})
    status = service.settings.status()
    assert status['openai_key'] is True
    assert status['telegram_token'] is False
    assert 'private-openai' not in repr(status)
    assert service.settings.get_credentials()['OPENAI_API_KEY'] == 'private-openai'


def test_scope_changing_settings_saved_but_not_applied_to_open_database(service):
    original = service.config
    candidate = replace(original, account_id='second-account', db_path=str(service.root / 'other.sqlite3'),
                        model='gpt-4o-mini')
    result = service.settings.apply(config=candidate)
    assert result['restart_required'] is True
    assert service.config.account_id == original.account_id
    assert service.config.db_path == original.db_path
    assert service.config.model == original.model
    assert load_config(service.config_path).account_id == 'second-account'
    assert not (service.root / 'other.sqlite3').exists()


def test_settings_cannot_apply_during_generation_cycle(service):
    before = service.config_path.read_bytes()
    service._begin_cycle()
    try:
        with pytest.raises(ValidationError):
            service.settings.apply(config=replace(service.config, model='gpt-4o-mini'))
    finally:
        service._end_cycle()
    assert service.config_path.read_bytes() == before


def test_model_change_drops_old_tariff_when_not_reentered_for_new_model(service):
    service.settings.apply(config=replace(service.config, input_price_per_million=3,
                                          output_price_per_million=8))
    service.settings.apply(config=replace(service.config, model='gpt-4o-mini'))
    assert service.config.input_price_per_million is None
    assert service.config.output_price_per_million is None
    assert service.costs.estimate()['pricing_known'] is False


def test_settings_write_failure_restores_credentials_and_keeps_live_clients(service, monkeypatch):
    service.settings.apply(credentials={'OPENAI_API_KEY': 'old-private'})
    previous = (service.root / '.env').read_bytes()
    yaml_before = service.config_path.read_bytes()
    active = service.generator
    def fail(config, path):
        raise OSError('never log private-key')
    monkeypatch.setattr('settings_service.save_config', fail)
    with pytest.raises(CredentialError) as error:
        service.settings.apply(config=replace(service.config, max_comment_length=80),
                               credentials={'OPENAI_API_KEY': 'new-private'})
    assert 'private-key' not in str(error.value)
    assert (service.root / '.env').read_bytes() == previous
    assert service.config_path.read_bytes() == yaml_before
    assert service.generator is active
    assert service.generator.api_key == 'old-private'
    assert service._cycle_lock.acquire(False)
    service._cycle_lock.release()


def test_model_list_uses_only_actual_api_results_for_unsaved_candidate_key(service):
    calls = []
    def route(request):
        calls.append(request)
        assert request.headers['Authorization'] == 'Bearer candidate-private'
        assert str(request.url) == 'https://api.openai.com/v1/models'
        return httpx.Response(200, json={'data': [
            {'id': 'gpt-4.1-mini'}, {'id': 'gpt-4o-mini'}, {'id': 'text-embedding-3-small'},
            {'id': 'gpt-image-1'}, {'id': 'gpt-4o-realtime-preview'}, {'id': 'o3'},
            {'id': 'gpt-3.5-turbo'}, {'id': 'gpt-4.1-mini'}, {'id': 42},
        ]})
    inject_openai(service, route)
    assert service.settings.models(api_key='candidate-private') == ['gpt-4.1-mini', 'gpt-4o-mini', 'o3']
    assert len(calls) == 1
    assert service.generator.api_key == ''
    assert service.settings.status()['openai_key'] is False
    assert not (service.root / '.env').exists()


def test_candidate_openai_connection_is_readonly_and_does_not_persist(service):
    def route(request):
        assert request.method == 'GET'
        assert request.headers['Authorization'] == 'Bearer unsaved-key'
        return httpx.Response(200, json={'data': [{'id': 'gpt-4o-mini'}]})
    inject_openai(service, route)
    assert service.settings.test_openai(api_key='unsaved-key', model='gpt-4o-mini') is True
    assert service.generator.api_key == ''
    assert service.config.model == 'gpt-4.1-mini'
    assert service.db.stats()['requests'] == 0


def test_candidate_telegram_probe_never_sends_message_or_persists(service):
    calls = []
    def route(request):
        calls.append(request)
        assert request.method == 'GET'
        assert str(request.url).endswith('/getMe')
        return httpx.Response(200, json={'ok': True, 'result': {'is_bot': True}})
    inject_telegram(service, route)
    assert service.settings.test_telegram(token='123:unsaved', chat_id='42') is True
    assert len(calls) == 1
    assert service.telegram._token == ''
    assert service.db.stats()['telegram_sent'] == 0
    assert not (service.root / '.env').exists()


def test_economical_suggestion_never_invents_unavailable_model(service):
    assert service.settings.suggest_economical(['o3', 'gpt-4.1-mini']) == 'gpt-4.1-mini'
    assert service.settings.suggest_economical(['o3']) is None
    assert service.settings.suggest_economical([]) is None


def test_invalid_credentials_do_not_change_configuration_or_clients(service):
    active = service.generator
    previous = service.config_path.read_bytes()
    with pytest.raises(CredentialError):
        service.settings.apply(config=replace(service.config, model='gpt-4o-mini'),
                               credentials={'OPENAI_API_KEY': 'private\ninvalid'})
    assert service.config_path.read_bytes() == previous
    assert service.generator is active
    assert not (service.root / '.env').exists()


def test_connection_probes_do_not_race_active_processing(service):
    service._begin_cycle()
    try:
        with pytest.raises(ValidationError):
            service.settings.models(api_key='candidate')
        with pytest.raises(ValidationError):
            service.settings.test_openai(api_key='candidate')
        with pytest.raises(ValidationError):
            service.settings.test_telegram(token='123:candidate')
    finally:
        service._end_cycle()


def test_candidate_secret_header_injection_is_rejected_without_network(service):
    def forbidden(request):
        pytest.fail('Invalid candidate secret reached HTTP client')
    inject_openai(service, forbidden)
    with pytest.raises(CredentialError) as error:
        service.settings.models(api_key='private\nheader')
    assert 'private' not in str(error.value)
    assert not (service.root / '.env').exists()
