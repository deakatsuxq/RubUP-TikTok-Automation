"""Validated, non-secret YAML configuration. Credentials are never saved here."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import date
from pathlib import Path
import math
import os
import tempfile

import yaml

# Keep the exact terms from the specification, including «подаркитг».
DEFAULT_HASHTAGS = ["upgrader", "cs2", "caser", "rubup", "кейсер", "апгрейдер", "тгподарки", "подаркитг", "нфт", "nft", "тг", "телеграм", "telegram", "меллстрой", "меллстройнарезки", "mellstroy", "мелстрой", "dota2", "twitch", "leva2k", "buster", "лева2к", "бустер", "твич", "gensyxa", "генсуха", "нарезка"]
DEFAULT_CATEGORIES = ["RubUP", "CS2", "Dota 2", "Скины", "Кейсы", "Игровые предметы", "Подарки", "NFT", "Telegram-подарки", "Стримы", "Нарезки", "Промокоды", "Обмены"]


@dataclass
class Config:
    hashtags: list[str] = field(default_factory=lambda: DEFAULT_HASHTAGS.copy())
    disabled_hashtags: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    min_date: str = "2026-05-01"
    model: str = "gpt-4.1-mini"
    max_videos: int = 50
    search_interval: int = 300
    target_per_minute: int = 6
    max_comment_length: int = 100
    categories: list[str] = field(default_factory=lambda: DEFAULT_CATEGORIES.copy())
    publication_mode: str = "manual"
    telegram_mode: str = "disabled"
    image_mode: str = "none"
    image_order: list[int] = field(default_factory=lambda: [1, 2, 3, 4])
    selected_image: int = 1
    db_path: str = "data/history.sqlite3"
    log_level: str = "INFO"
    retries: int = 2
    timeout: float = 20.0
    dry_run: bool = False
    account_id: str = "local-account"
    input_price_per_million: float | None = None
    output_price_per_million: float | None = None
    third_party_cost: float = 0.0
    max_context_chars: int = 2000
    max_output_tokens: int = 128
    reward_default: float = 0.0
    reward_by_image: dict = field(default_factory=dict)
    require_image: bool = False
    require_like: bool = False
    advertising: bool = True
    disclosure: str = "реклама"
    _base_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parent, init=False, repr=False)

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    @property
    def enabled_hashtags(self) -> list[str]:
        disabled = {x.lstrip("#").casefold() for x in self.disabled_hashtags}
        return [x.lstrip("#").casefold() for x in self.hashtags if x.lstrip("#").casefold() not in disabled]

    def resolve_path(self, value: str) -> Path:
        path = Path(value).expanduser()
        return path if path.is_absolute() else self.base_dir / path

    def validate(self) -> "Config":
        for key in ("hashtags", "disabled_hashtags", "keywords", "categories"):
            items = getattr(self, key)
            if not isinstance(items, list) or not all(isinstance(x, str) and x.strip() for x in items):
                raise ValueError(f"{key}: требуется список непустых строк")
        if any(not x.lstrip("#").replace("_", "").isalnum() for x in self.hashtags + self.disabled_hashtags):
            raise ValueError("Хештеги могут содержать буквы, цифры и подчёркивание")
        try:
            if not isinstance(self.min_date, str) or len(self.min_date) != 10:
                raise ValueError
            date.fromisoformat(self.min_date)
        except (ValueError, TypeError):
            raise ValueError("min_date: требуется дата ГГГГ-ММ-ДД") from None
        bounds = {"max_videos": (1, 10000), "search_interval": (1, 86400), "target_per_minute": (1, 60), "max_comment_length": (15, 1000), "retries": (0, 5), "max_context_chars": (512, 100000), "max_output_tokens": (16, 4096)}
        for key, (lo, hi) in bounds.items():
            value = getattr(self, key)
            if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
                raise ValueError(f"{key}: требуется целое число {lo}–{hi}")
        if isinstance(self.timeout, bool) or not isinstance(self.timeout, (int, float)) or not math.isfinite(self.timeout) or not 0 < self.timeout <= 300:
            raise ValueError("timeout: требуется число больше 0 и не больше 300")
        for key in ("dry_run", "require_image", "require_like", "advertising"):
            if not isinstance(getattr(self, key), bool):
                raise ValueError(f"{key}: требуется true или false")
        for key, allowed in {"publication_mode": {"manual"}, "telegram_mode": {"disabled", "verified", "draft", "published"}, "image_mode": {"none", "rotate", "specific", "task"}, "log_level": {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}}.items():
            if not isinstance(getattr(self, key), str) or getattr(self, key) not in allowed:
                raise ValueError(f"{key}: допустимо {', '.join(sorted(allowed))}")
        if not isinstance(self.image_order, list) or len(self.image_order) != 4 or set(self.image_order) != {1, 2, 3, 4} or any(type(x) is not int for x in self.image_order):
            raise ValueError("image_order: укажите все четыре номера 1–4 без повторов")
        if type(self.selected_image) is not int or self.selected_image not in {1, 2, 3, 4}:
            raise ValueError("selected_image: номер от 1 до 4")
        for key in ("model", "db_path", "account_id", "disclosure"):
            if not isinstance(getattr(self, key), str) or not getattr(self, key).strip():
                raise ValueError(f"{key}: требуется непустая строка")
        for key in ("input_price_per_million", "output_price_per_million", "third_party_cost", "reward_default"):
            value = getattr(self, key)
            if value is None and key.endswith("per_million"):
                continue
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{key}: требуется неотрицательное число")
        if not isinstance(self.reward_by_image, dict):
            raise ValueError("reward_by_image: требуется словарь номер: сумма")
        for slot, value in self.reward_by_image.items():
            if str(slot) not in {"1", "2", "3", "4"} or isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError("reward_by_image: номера 1–4 и неотрицательные суммы")
        self.reward_by_image = {str(k): float(v) for k, v in self.reward_by_image.items()}
        return self


def load_config(path: str | Path = "config.yaml") -> Config:
    config_path = Path(path).resolve()
    with config_path.open("r", encoding="utf-8-sig") as handle:
        values = yaml.safe_load(handle) or {}
    if not isinstance(values, dict):
        raise ValueError("config.yaml должен содержать словарь параметров")
    allowed = {f.name for f in fields(Config) if f.init}
    unknown = set(values) - allowed
    if unknown:
        raise ValueError(f"Неизвестные параметры: {', '.join(sorted(map(str, unknown)))}")
    # YAML interprets unquoted dates as datetime.date; allow this representation.
    if isinstance(values.get("min_date"), date):
        values["min_date"] = values["min_date"].isoformat()
    result = Config(**values)
    result._base_dir = config_path.parent
    result.validate()
    result.db_path = str(result.resolve_path(result.db_path))
    return result


def save_config(config: Config, path: str | Path = "config.yaml") -> None:
    config.validate()
    config_path = Path(path).resolve()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    data = {k: v for k, v in asdict(config).items() if not k.startswith("_")}
    db_path = Path(data["db_path"])
    if db_path.is_absolute() and db_path.is_relative_to(config_path.parent):
        data["db_path"] = db_path.relative_to(config_path.parent).as_posix()
    fd, temporary = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=config_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            yaml.safe_dump(data, handle, allow_unicode=True, sort_keys=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, config_path)
    finally:
        Path(temporary).unlink(missing_ok=True)
