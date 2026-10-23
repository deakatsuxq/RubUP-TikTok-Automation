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

    A remote mutation may succeed even if a response is lost. Transport
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
                before_attempt=None, after_attempt=None,
                **kwargs: Any) -> dict:
        self.last_attempt_states = []
        kwargs.setdefault("timeout", self.timeout)
        kwargs.setdefault("follow_redirects", False)
        for attempt in range(self.retries + 1):
            reservation = before_attempt() if before_attempt is not None else None
            try:
                response = self.client.request(method, url, **kwargs)
            except httpx.HTTPError:
                self.last_attempt_states.append("unknown")
                if after_attempt is not None:
                    after_attempt(reservation, 'unknown', None)
                if retry_safe and attempt < self.retries:
                    self.sleep(min(2 ** attempt, 8))
                    continue
                raise IntegrationError("Сетевая ошибка API; результат запроса неизвестен. Автоповтор не выполнен") from None
            if response.status_code == 429:
                self.last_attempt_states.append("rejected")
                if after_attempt is not None:
                    after_attempt(reservation, 'rejected', None)
                if attempt < self.retries:
                    wait = self._retry_delay(response, attempt)
                    self.sleep(wait)
                    continue
                raise IntegrationError("API ограничил частоту запросов (429). Повторите позже")
            if 500 <= response.status_code < 600 and retry_safe and attempt < self.retries:
                self.last_attempt_states.append("unknown")
                if after_attempt is not None:
                    after_attempt(reservation, 'unknown', None)
                self.sleep(min(2 ** attempt, 8))
                continue
            if not 200 <= response.status_code < 300:
                state = "rejected" if 400 <= response.status_code < 500 else "unknown"
                self.last_attempt_states.append(state)
                if after_attempt is not None:
                    after_attempt(reservation, state, None)
                raise IntegrationError(f"API отклонил запрос (HTTP {response.status_code})")
            self.last_attempt_states.append("response")
            try:
                data = response.json()
            except (ValueError, UnicodeError):
                if after_attempt is not None:
                    after_attempt(reservation, 'unknown', None)
                raise IntegrationError("API вернул некорректный JSON") from None
            if not isinstance(data, dict):
                if after_attempt is not None:
                    after_attempt(reservation, 'unknown', None)
                raise IntegrationError("API вернул неожиданный формат ответа")
            if after_attempt is not None:
                after_attempt(reservation, 'response', data)
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
