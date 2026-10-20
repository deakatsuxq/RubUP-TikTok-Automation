"""HTTP helpers with bounded retries and deliberately sanitized exceptions."""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx


class IntegrationError(RuntimeError):
    """Safe local error: never includes request URLs, headers or server bodies."""


class APIClient:
    """Small JSON HTTP adapter; mutation retries require explicit opt-in.

    OpenAI generation can be billed even if a response is lost. Transport
    failures are therefore never retried for mutations. Explicit rate-limit
    rejection may be retried because the operation was not accepted.
    """

    def __init__(self, client: httpx.Client | None = None, *, timeout: float = 30,
                 retries: int = 2, sleep: Callable[[float], None] = time.sleep):
        self.client = client or httpx.Client(timeout=timeout, follow_redirects=False)
        self.owns_client = client is None
        self.timeout = timeout
        self.retries = max(0, min(int(retries), 5))
        self.sleep = sleep
        self.last_attempt_states: list[str] = []

    def request(self, method: str, url: str, *, retry_safe: bool = False,
                **kwargs: Any) -> dict:
        self.last_attempt_states = []
        kwargs.setdefault("timeout", self.timeout)
        kwargs.setdefault("follow_redirects", False)
        for attempt in range(self.retries + 1):
            try:
                response = self.client.request(method, url, **kwargs)
            except httpx.HTTPError:
                self.last_attempt_states.append("unknown")
                if retry_safe and attempt < self.retries:
                    self.sleep(min(2 ** attempt, 8))
                    continue
                raise IntegrationError("Сетевая ошибка API; результат запроса неизвестен. Автоповтор не выполнен") from None
            if response.status_code == 429:
                self.last_attempt_states.append("rejected")
                if attempt < self.retries:
                    wait = self._retry_delay(response, attempt)
                    self.sleep(wait)
                    continue
                raise IntegrationError("API ограничил частоту запросов (429). Повторите позже")
            if 500 <= response.status_code < 600 and retry_safe and attempt < self.retries:
                self.last_attempt_states.append("unknown")
                self.sleep(min(2 ** attempt, 8))
                continue
            if not 200 <= response.status_code < 300:
                self.last_attempt_states.append("rejected" if 400 <= response.status_code < 500 else "unknown")
                raise IntegrationError(f"API отклонил запрос (HTTP {response.status_code})")
            self.last_attempt_states.append("response")
            try:
                data = response.json()
            except (ValueError, UnicodeError):
                raise IntegrationError("API вернул некорректный JSON") from None
            if not isinstance(data, dict):
                raise IntegrationError("API вернул неожиданный формат ответа")
            return data
        raise IntegrationError("Лимит попыток API исчерпан")

    @staticmethod
    def _retry_delay(response: httpx.Response, attempt: int) -> float:
        try:
            raw = float(response.headers.get("Retry-After", 2 ** attempt))
        except (TypeError, ValueError):
            raw = 2 ** attempt
        # Long provider cooldowns should pause the operation rather than be cut
        # short and immediately hammered again.
        if raw > 30:
            raise IntegrationError("API запросил длительную паузу. Повторите операцию позже")
        return max(0, min(raw, 30))

    def close(self) -> None:
        if self.owns_client:
            self.client.close()
