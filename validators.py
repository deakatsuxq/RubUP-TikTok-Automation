"""Local validation independent of network clients and the graphical interface."""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from urllib.parse import urlsplit


class ValidationError(ValueError):
    """A value does not satisfy the task requirements."""


_RUBUP = re.compile(r"(?<!\w)@?(?:rubup|рубап(?:а|у|ом|е|ы|ов|ам|ами|ах)?)(?!\w)", re.IGNORECASE)
_VIDEO_PATH = re.compile(r"/@([A-Za-z0-9_.]{1,24})/video/([0-9]{1,25})/?\Z")


def normalize_comment(text: str) -> str:
    """Make case, spacing and punctuation variations compare as duplicates."""
    normalized = unicodedata.normalize("NFKC", str(text)).casefold()
    normalized = re.sub(r"[^\w\s]", "", normalized)
    return " ".join(normalized.split())


def contains_rubup(text: str) -> bool:
    return bool(_RUBUP.search(unicodedata.normalize("NFKC", str(text))))


has_rubup = contains_rubup


def validate_comment(
    text: str,
    max_length: int = 100,
    recent_comments: Iterable[str] = (),
    advertising: bool = False,
    disclosure: str = "реклама",
) -> str:
    """Return clean comment or reject missing brand, length or duplicates.

    This checks textual requirements. A reviewer must separately verify factual
    claims and any legal advertising requirements beyond the configured label.
    """
    if not isinstance(text, str):
        raise ValidationError("Комментарий должен быть строкой")
    value = " ".join(text.strip().split())
    if not value:
        raise ValidationError("Комментарий пуст")
    if max_length < 1 or len(value) > max_length:
        raise ValidationError(f"Комментарий длиннее {max_length} символов")
    if not contains_rubup(value):
        raise ValidationError("В комментарии отсутствует RubUP или допустимая форма «рубап»")
    normalized = normalize_comment(value)
    if any(normalized == normalize_comment(previous) for previous in recent_comments):
        raise ValidationError("Этот комментарий уже использовался")
    if advertising:
        label = disclosure.strip()
        if not label:
            raise ValidationError("Не настроено обозначение рекламы")
        if not re.search(r"(?<!\w)" + re.escape(label) + r"(?!\w)", value, re.IGNORECASE):
            raise ValidationError("В рекламном комментарии отсутствует настроенное обозначение рекламы")
    return value


def canonical_tiktok_url(url: str) -> tuple[str, str]:
    """Accept direct TikTok video URLs only, without resolving redirects.

    Tracking parameters are discarded; arbitrary hosts, credentials, ports,
    encoded paths and short URLs are rejected before any network request.
    """
    if not isinstance(url, str):
        raise ValidationError("Ссылка должна быть строкой")
    value = url.strip()
    if not value or len(value) > 2048 or any(ch.isspace() or ord(ch) < 32 for ch in value):
        raise ValidationError("Некорректная ссылка TikTok")
    try:
        parsed = urlsplit(value)
        if (parsed.scheme.casefold() != "https" or parsed.hostname not in {"tiktok.com", "www.tiktok.com"}
                or parsed.username is not None or parsed.password is not None or parsed.port is not None):
            raise ValueError
    except ValueError:
        raise ValidationError("Нужна прямая HTTPS-ссылка на видео tiktok.com") from None
    match = _VIDEO_PATH.fullmatch(parsed.path)
    if not match:
        raise ValidationError("Нужна ссылка вида https://www.tiktok.com/@author/video/123456789")
    author, video_id = match.groups()
    return f"https://www.tiktok.com/@{author}/video/{video_id}", video_id
