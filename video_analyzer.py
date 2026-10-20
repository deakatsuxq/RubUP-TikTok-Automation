"""Deterministic checks of imported metadata, without guessing TikTok dates."""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import date, datetime
import re

HASHTAG_PATTERN = re.compile(r"(?<!\w)#([\w]+)", re.UNICODE)
CATEGORY_TERMS = (
    ("Dota 2", ("dota", "дота")),
    ("CS2", ("cs2", "counter-strike", "кс2")),
    ("Telegram-подарки", ("тгподарки", "подаркитг", "telegram", "телеграм")),
    ("NFT", ("nft", "нфт")),
    ("RubUP", ("rubup", "рубап")),
    ("Стримы", ("twitch", "твич", "стрим")),
    ("Промокоды", ("промокод", "промик")),
    ("Обмены", ("обмен",)),
    ("Кейсы", ("кейс", "case", "caser")),
    ("Скины", ("скин", "skin")),
    ("Нарезки", ("нарезк",)),
    ("Подарки", ("подар",)),
)


def extract_hashtags(description: str) -> list[str]:
    return list(dict.fromkeys(x.casefold() for x in HASHTAG_PATTERN.findall(description or "")))


def parse_publication_date(value: str | None) -> date | None:
    if not value or not isinstance(value, str):
        return None
    try:
        if len(value) == 10:
            return date.fromisoformat(value)
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        return None


def analyze_video(video, config) -> dict:
    data = asdict(video) if is_dataclass(video) else dict(video)
    description = str(data.get("description") or "")
    supplied_tags = data.get("hashtags") or []
    if isinstance(supplied_tags, str):
        supplied_tags = supplied_tags.replace(",", " ").split()
    tags = {str(x).lstrip("#").casefold() for x in supplied_tags} | set(extract_hashtags(description))
    content = (description + " " + " ".join(tags)).casefold()
    category = data.get("category") or next((category for category, terms in CATEGORY_TERMS if any(term in content for term in terms)), "Игровые предметы")
    result = {"eligible": False, "needs_review": False, "reason": "", "category": category}
    published = parse_publication_date(data.get("published_at"))
    if published is None:
        return {**result, "needs_review": True, "reason": "Неизвестна или некорректна дата публикации — нужна ручная проверка"}
    if published < date.fromisoformat(config.min_date):
        return {**result, "reason": "Видео опубликовано раньше минимальной даты"}
    if published > date.today():
        return {**result, "needs_review": True, "reason": "Дата публикации в будущем — проверьте метаданные"}
    enabled_tags = set(config.enabled_hashtags)
    keywords = [word.casefold() for word in config.keywords]
    matching = tags & enabled_tags or any(word in content for word in keywords)
    if not matching:
        if not description and not tags:
            return {**result, "needs_review": True, "reason": "Нет описания или хештегов для проверки тематики"}
        return {**result, "reason": "Нет включённых хештегов или ключевых слов"}
    return {**result, "eligible": True, "reason": "Дата и тематика соответствуют фильтрам"}
