"""Conservative lexical diversity checks without remote models or dependencies.

The rule catches templates whose wording is effectively the same after removing
required branding, disclosure and non-semantic account/video identifiers. It is
not a claim to understand every Russian paraphrase or to establish originality.
"""
from __future__ import annotations

from difflib import SequenceMatcher
import re
import unicodedata


_BRAND = re.compile(r"(?<!\w)@?(?:rubup|рубап(?:а|у|ом|е|ы|ов|ам|ами|ах)?)(?!\w)", re.I)
_AD_LABEL = re.compile(r"(?<!\w)(?:реклама|рекламный\s+комментарий|тест(?:овый\s+пример)?)(?!\w)", re.I)
_MENTION = re.compile(r"(?<!\w)@[\w.]+", re.UNICODE)
_UUID = re.compile(r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b", re.I)
_MARKER = re.compile(
    r"\b(?:аккаунт|профиль|account|profile|video|видео|ролик|id)\s*(?:№|#|:|=)?\s*"
    r"(?:[0-9]+|[a-z]+[0-9_-]+[a-z0-9_-]*)\b", re.I,
)
_WORDS = re.compile(r"[^\W_]+", re.UNICODE)
_FUNCTION_WORDS = frozenset({"а", "в", "во", "на", "и", "из", "к", "ко", "по", "с", "со", "у", "о", "об"})
_COMMON_FORMS = {
    "скины": "скин", "скина": "скин", "скинов": "скин", "скином": "скин",
    "кейсы": "кейс", "кейса": "кейс", "кейсов": "кейс", "кейсе": "кейс",
    "какая": "какой", "какие": "какой", "какое": "какой",
    "выбрать": "выбор", "выбирать": "выбор", "брать": "выбор",
    "обсудить": "обсудим", "обсуждать": "обсудим",
}


def _words(text: str) -> list[str]:
    value = unicodedata.normalize("NFKC", str(text)).casefold().replace("ё", "е")
    for pattern in (_BRAND, _AD_LABEL, _MENTION, _UUID, _MARKER):
        value = pattern.sub(" ", value)
    words = []
    for word in _WORDS.findall(value):
        if word.isdecimal() or word in _FUNCTION_WORDS:
            continue
        words.append(_COMMON_FORMS.get(word, word))
    return words


def comments_too_similar(candidate: str, previous: str) -> bool:
    """Flag identical wording, reordered templates and very close lexical edits.

    Required RubUP/disclosure do not contribute to similarity. Numeric IDs and
    explicit account markers cannot make a duplicated production template look
    unique. The high overlap threshold intentionally permits distinct short
    questions rather than rejecting every mention of the same gaming subject.
    """
    first, second = _words(candidate), _words(previous)
    if not first or not second:
        return first == second
    if first == second or sorted(first) == sorted(second):
        return True
    first_set, second_set = set(first), set(second)
    overlap = 2 * len(first_set & second_set) / (len(first_set) + len(second_set))
    # One added/deleted filler in an otherwise common multi-word template.
    if min(len(first_set), len(second_set)) >= 3 and overlap >= 0.8:
        return True
    # Small spelling/inflection differences still need substantial lexical
    # overlap; unrelated very short words must not collide by character alone.
    character_ratio = SequenceMatcher(None, " ".join(first), " ".join(second), autojunk=False).ratio()
    return overlap >= 0.6 and character_ratio >= 0.9
