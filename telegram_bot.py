"""Telegram delivery of canonical video links, with a durable at-most-once outbox.

A transport timeout cannot prove that Telegram rejected a message. Such an
attempt is recorded as UNKNOWN and is never retried automatically.
"""
from __future__ import annotations

import os
import re
import time
from typing import Any

import httpx

from api_client import IntegrationError
from validators import ValidationError, canonical_tiktok_url


class TelegramBot:
    def __init__(self, config, db, client=None, token=None, chat_id=None):
        self.config = config
        self.db = db
        self._token = token if token is not None else os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self._chat_id = chat_id if chat_id is not None else os.environ.get("TELEGRAM_CHAT_ID", "")
        self._owns_client = client is None
        self.client = client or httpx.Client(
            timeout=float(config.timeout), follow_redirects=False,
        )

    def _endpoint(self, method: str) -> str:
        # A token is part of the Telegram URL. Never expose the URL or HTTPX
        # exception text in errors or logs. Restrict its characters to the
        # documented bot-token structure so it cannot change the destination.
        if not isinstance(self._token, str) or not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", self._token):
            raise IntegrationError("Не задан корректный TELEGRAM_BOT_TOKEN")
        return f"https://api.telegram.org/bot{self._token}/{method}"

    def _eligible(self, task: dict[str, Any]) -> bool:
        if self.config.dry_run or task.get("is_test"):
            return False
        mode = self.config.telegram_mode
        if mode == "disabled":
            return False
        published = task.get("publication_status") == "PUBLISHED"
        if mode == "published":
            return published
        processing = task.get("processing_status", "NEW")
        draft = bool(task.get("comment", "").strip()) and processing in {
            "DRAFT_READY", "PENDING_APPROVAL", "PUBLISHED",
        }
        if mode == "draft":
            return published or draft
        if mode == "verified":
            return published or processing in {
                "ANALYZED", "DRAFT_READY", "PENDING_APPROVAL", "PUBLISHED",
            }
        return False

    def _finish(self, task, url: str, status: str, error: str = "") -> None:
        self.db.finish_telegram(url, status, error)
        if task.get("id") is not None:
            self.db.update_task(task["id"], telegram_status=status)
            self.db.add_event(task["id"], f"TELEGRAM_{status}", error)

    @staticmethod
    def _json(response) -> dict:
        try:
            body = response.json()
        except (ValueError, TypeError):
            return {}
        return body if isinstance(body, dict) else {}

    def send_video(self, task: dict, allow_dry_run: bool = False) -> bool:
        """Send exactly one URL when the task has reached the chosen stage.

        ``allow_dry_run`` is intentionally unable to override safety. A real
        send requires a production task and config.dry_run=False. Returns False
        for ineligible tasks or an already reserved/sent/uncertain URL.
        """
        if not self._eligible(task):
            return False
        canonical, video_id = canonical_tiktok_url(task.get("url", ""))
        if str(task.get("video_id", "")) != video_id:
            raise ValidationError("ID видео не соответствует прямой ссылке TikTok")
        endpoint = self._endpoint("sendMessage")
        if not str(self._chat_id).strip():
            raise IntegrationError("Не задан TELEGRAM_CHAT_ID")
        if not self.db.reserve_telegram(canonical):
            return False

        payload = {
            "chat_id": str(self._chat_id),
            "text": canonical,
            "link_preview_options": {"is_disabled": True},
        }
        attempts = int(self.config.retries) + 1
        for attempt in range(attempts):
            try:
                response = self.client.post(endpoint, json=payload, timeout=float(self.config.timeout), follow_redirects=False)
            except (httpx.HTTPError, OSError):
                self._finish(task, canonical, "UNKNOWN", "Ответ Telegram не получен; повторная отправка заблокирована")
                raise IntegrationError("Результат отправки Telegram неизвестен; проверьте чат вручную") from None
            body = self._json(response)
            if response.status_code == 200 and body.get("ok") is True:
                result = body.get("result")
                if not isinstance(result, dict) or not isinstance(result.get("message_id"), int):
                    self._finish(task, canonical, "UNKNOWN", "Telegram вернул неподтверждённый результат")
                    raise IntegrationError("Ответ Telegram не подтверждает отправку; проверьте чат вручную")
                self._finish(task, canonical, "SENT")
                return True

            # Only an explicit API rejection is safe to retry. A 5xx response
            # can arrive after the message was accepted, so it is uncertain.
            explicit_reject = body.get("ok") is False
            code = body.get("error_code", response.status_code)
            if explicit_reject and code == 429:
                parameters = body.get("parameters")
                retry_after = parameters.get("retry_after", 1) if isinstance(parameters, dict) else 1
                try:
                    delay = float(retry_after)
                except (TypeError, ValueError):
                    delay = 1.0
                if 0 <= delay <= 60 and attempt + 1 < attempts:
                    time.sleep(delay)
                    continue
                self._finish(task, canonical, "FAILED", "Telegram ограничил частоту запросов")
                raise IntegrationError("Telegram ограничил частоту запросов; повторите позже")
            if explicit_reject and isinstance(code, int) and 400 <= code < 500:
                self._finish(task, canonical, "FAILED", f"Telegram отклонил запрос (код {code})")
                raise IntegrationError(f"Telegram отклонил запрос (код {code})")
            self._finish(task, canonical, "UNKNOWN", "Telegram не подтвердил результат; повторная отправка заблокирована")
            raise IntegrationError("Результат отправки Telegram неизвестен; проверьте чат вручную")
        return False  # Every loop branch returns or raises; defensive only.

    def test_connection(self) -> bool:
        """Validate bot authentication with getMe; never send a test message."""
        endpoint = self._endpoint("getMe")
        try:
            response = self.client.get(endpoint, timeout=float(self.config.timeout), follow_redirects=False)
        except (httpx.HTTPError, OSError):
            raise IntegrationError("Не удалось подключиться к Telegram") from None
        body = self._json(response)
        if response.status_code != 200 or body.get("ok") is not True:
            raise IntegrationError("Telegram не подтвердил доступ бота")
        if not isinstance(body.get("result"), dict) or not body["result"].get("is_bot"):
            raise IntegrationError("Telegram не подтвердил учётную запись бота")
        return True

    def close(self) -> None:
        if self._owns_client:
            self.client.close()
