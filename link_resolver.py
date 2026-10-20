"""Resolve TikTok's own short links without following arbitrary destinations.

Only HTTP redirects are supported. HTML, JavaScript, CAPTCHA and other access
controls are deliberately not interpreted or bypassed. Every destination is
validated *before* it can be requested, including relative redirect targets.
"""
from __future__ import annotations

import math
import re
import time
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from validators import ValidationError, canonical_tiktok_url


SHORT_LINK_ERROR = (
    "Не удалось автоматически открыть короткую ссылку. "
    "Открой видео в браузере и скопируй полный адрес"
)
ALLOWED_HOSTS = frozenset({"vt.tiktok.com", "vm.tiktok.com", "www.tiktok.com", "tiktok.com"})
SHORT_HOSTS = frozenset({"vt.tiktok.com", "vm.tiktok.com"})
_SHORT_PATH = re.compile(r"/[A-Za-z0-9_-]{1,128}/?\Z")
_WEB_SHORT_PATH = re.compile(r"/t/[A-Za-z0-9_-]{1,128}/?\Z")
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})


class LinkResolver:
    def __init__(self, client=None, timeout=20, max_redirects=5):
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or timeout <= 0):
            raise ValueError("Тайм-аут должен быть положительным числом")
        if type(max_redirects) is not int or not 0 <= max_redirects <= 10:
            raise ValueError("Число перенаправлений должно быть от 0 до 10")
        self.timeout = float(timeout)
        self.max_redirects = max_redirects
        self._owns_client = client is None
        self.client = client or httpx.Client(timeout=self.timeout, follow_redirects=False)

    @staticmethod
    def _validate_destination(url):
        if not isinstance(url, str):
            raise ValidationError("Вставьте ссылку на видео TikTok")
        value = url.strip()
        if (not value or len(value) > 2048 or "\\" in value
                or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in value)):
            raise ValidationError("Некорректная ссылка TikTok")
        try:
            parsed = urlsplit(value)
            if (parsed.scheme.casefold() != "https" or parsed.hostname not in ALLOWED_HOSTS
                    or parsed.username is not None or parsed.password is not None
                    or parsed.port is not None or parsed.netloc.casefold() != parsed.hostname
                    or "%" in parsed.path):
                raise ValueError
        except (ValueError, UnicodeError):
            raise ValidationError("Разрешены только безопасные HTTPS-ссылки TikTok") from None
        # A complete video URL is never fetched by the resolver. It is validated
        # by the existing strict validator and canonicalized without trackers.
        try:
            direct = canonical_tiktok_url(value)
        except ValidationError:
            direct = None
        if direct:
            return direct[0], direct
        if parsed.hostname in SHORT_HOSTS:
            valid_path = _SHORT_PATH.fullmatch(parsed.path)
        else:
            # TikTok may route a vm/vt link through its own /t/<code>/ alias.
            valid_path = _WEB_SHORT_PATH.fullmatch(parsed.path)
        if not valid_path:
            raise ValidationError("Нужна ссылка на видео или короткая ссылка TikTok")
        # Fragments never belong in an HTTP request. Host/scheme are rebuilt
        # after validation, preventing parser disagreement about authority.
        return urlunsplit(("https", parsed.hostname, parsed.path, parsed.query, "")), None

    def resolve(self, url):
        current, direct = self._validate_destination(url)
        if direct:
            return direct
        deadline = time.monotonic() + self.timeout
        seen = set()
        redirects = 0
        while True:
            if current in seen:
                raise ValidationError(SHORT_LINK_ERROR)
            seen.add(current)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValidationError(SHORT_LINK_ERROR)
            try:
                # Explicit False also protects injected clients whose default
                # follow_redirects=True would otherwise escape validation.
                with self.client.stream("GET", current, timeout=remaining, follow_redirects=False) as response:
                    status = response.status_code
                    location = response.headers.get("Location")
            except (httpx.HTTPError, OSError, ValueError):
                raise ValidationError(SHORT_LINK_ERROR) from None
            if status not in _REDIRECT_CODES:
                raise ValidationError(SHORT_LINK_ERROR)
            if not location or redirects >= self.max_redirects:
                raise ValidationError(SHORT_LINK_ERROR)
            if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in location):
                raise ValidationError(SHORT_LINK_ERROR)
            redirects += 1
            try:
                target = urljoin(current, location)
                next_url, direct = self._validate_destination(target)
            except (ValidationError, ValueError, UnicodeError):
                raise ValidationError(SHORT_LINK_ERROR) from None
            if direct:
                return direct
            current = next_url

    def close(self):
        if self._owns_client:
            self.client.close()
