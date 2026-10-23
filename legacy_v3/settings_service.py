"""Live desktop configuration and explicit read-only connection checks."""
from __future__ import annotations

from dataclasses import replace
from contextlib import contextmanager
from pathlib import Path

from ai_comment_generator import CommentGenerator
from config import save_config
from credentials import CredentialStore
from credentials import CredentialError, _atomic_write
from telegram_bot import TelegramBot
from validators import ValidationError


class SettingsService:
    def __init__(self, service):
        self.service = service
        self.store = getattr(service, 'credential_store', None) or CredentialStore(service.root)

    def status(self):
        data = self.store.get_all()
        return {'openai_key': bool(data['OPENAI_API_KEY']),
                'telegram_token': bool(data['TELEGRAM_BOT_TOKEN']),
                'telegram_chat_id': bool(data['TELEGRAM_CHAT_ID']),
                'secure_storage': self.store.storage_label}

    def get_credentials(self):
        return self.store.get_all()

    @contextmanager
    def _probe(self):
        self.service._begin_cycle()
        try:
            yield
        finally:
            self.service._end_cycle()

    def apply(self, config=None, credentials=None):
        service = self.service
        service._begin_cycle()
        new_generator = new_telegram = None
        try:
            candidate = replace(config or service.config)
            candidate._base_dir = service.root
            candidate.db_path = str(candidate.resolve_path(candidate.db_path))
            candidate.validate()
            changes = self.store._validate({} if credentials is None else credentials)
            if candidate.model != service.config.model:
                old = service.config
                if (candidate.input_price_per_million == old.input_price_per_million
                        and candidate.output_price_per_million == old.output_price_per_million):
                    candidate = replace(candidate, input_price_per_million=None,
                                        output_price_per_million=None)
            restart = any(getattr(candidate, name) != getattr(service.config, name)
                          for name in ('db_path', 'account_id', 'dry_run'))
            data = self.store.get_all()
            data.update(changes)
            # Scope changes are deferred together, while currently active clients
            # can still safely adopt explicitly changed credentials. No database
            # or user history is reopened or reset here.
            active = service.config if restart else candidate
            active._base_dir = service.root
            new_generator = CommentGenerator(active, service.db, api_key=data['OPENAI_API_KEY'])
            new_telegram = TelegramBot(active, service.db, token=data['TELEGRAM_BOT_TOKEN'],
                                       chat_id=data['TELEGRAM_CHAT_ID'])
            # Two local files may be involved. Retain exact bytes for rollback,
            # without printing them or creating plaintext backup artifacts.
            paths = []
            if changes:
                paths.append(self.store.private_path if self.store.windows else self.store.env_path)
            if config is not None:
                paths.append(Path(service.config_path))
            snapshots = {path: path.read_bytes() if path.exists() else None for path in paths}
            try:
                if changes:
                    self.store.update(changes)
                if config is not None:
                    save_config(candidate, service.config_path)
            except Exception:
                try:
                    for path, before in snapshots.items():
                        if before is None:
                            path.unlink(missing_ok=True)
                        else:
                            _atomic_write(path, before)
                except Exception:
                    raise CredentialError('Не удалось полностью сохранить настройки. Перезапустите программу и проверьте настройки подключения') from None
                raise CredentialError('Не удалось сохранить настройки; предыдущие значения восстановлены') from None
            old_generator, old_telegram = service.generator, service.telegram
            service.config = active
            service.search.config = active
            service.publisher.config = active
            service.cost_tracker.config = active
            service.generator, service.telegram = new_generator, new_telegram
            new_generator = new_telegram = None
            for old_client in (old_generator, old_telegram):
                close = getattr(old_client, 'close', None)
                if callable(close):
                    close()
            return {'restart_required': restart, 'saved': True}
        finally:
            for client in (new_generator, new_telegram):
                if client is not None:
                    client.close()
            service._end_cycle()

    def models(self, api_key=None):
        with self._probe():
            if api_key is None:
                return self.service.generator.available_models()
            key = self.store._validate({'OPENAI_API_KEY': api_key})['OPENAI_API_KEY']
            current = self.service.generator
            probe = CommentGenerator(self.service.config, self.service.db,
                                     client=current.http.client, api_key=key)
            return probe.available_models()

    def test_openai(self, api_key=None, model=None):
        with self._probe():
            current = self.service.generator
            config = replace(self.service.config, model=model or self.service.config.model)
            key = current.api_key if api_key is None else self.store._validate({'OPENAI_API_KEY': api_key})['OPENAI_API_KEY']
            # An injected client is reused in tests and avoids closing live clients.
            probe = CommentGenerator(config, self.service.db, client=current.http.client,
                                     api_key=key)
            return probe.test_connection()

    def test_telegram(self, token=None, chat_id=None):
        with self._probe():
            current = self.service.telegram
            supplied = self.store._validate({'TELEGRAM_BOT_TOKEN': token, 'TELEGRAM_CHAT_ID': chat_id})
            probe = TelegramBot(self.service.config, self.service.db, client=current.client,
                                token=supplied.get('TELEGRAM_BOT_TOKEN', current._token),
                                chat_id=supplied.get('TELEGRAM_CHAT_ID', current._chat_id))
            return probe.test_connection()

    @staticmethod
    def suggest_economical(models):
        available = [str(model) for model in models]
        # Smaller model families are candidates only, never a verified cheapest
        # price. Their actual tariffs remain manually configured and unknown.
        for size in ('nano', 'mini'):
            candidates = sorted(model for model in available if size in model.split('-'))
            if candidates:
                return candidates[0]
        return None
