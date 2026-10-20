"""Known-link import and local search, with optional public TikTok oEmbed lookup.

This module is not a global hashtag crawler. Research API access is unsuitable
for this commercial preparation workflow. Dates imported from CSV/JSON or entered
by the user remain user-supplied metadata; oEmbed does not supply publish dates.
"""
from __future__ import annotations

import csv
import io
from itertools import islice
import json
from pathlib import Path
import re

import httpx

from models import Video
from validators import canonical_tiktok_url
from video_analyzer import analyze_video, extract_hashtags


class DiscoveryError(RuntimeError):
    """A bounded, user-visible discovery failure."""


class SearchService:
    def __init__(self, config, db, client=None):
        self.config = config
        self.db = db
        self._owns_client = client is None
        self.client = client or httpx.Client(timeout=config.timeout, follow_redirects=False)

    def add_url(self, url: str, **metadata) -> Video:
        canonical, video_id = canonical_tiktok_url(url.strip())
        if metadata.get("video_id") and str(metadata["video_id"]) != video_id:
            raise ValueError("ID в метаданных не совпадает с URL")
        description = str(metadata.get("description") or "")
        tags = metadata.get("hashtags") or []
        if isinstance(tags, str):
            tags = re.split(r"[,;\s]+", tags.strip())
        if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
            raise ValueError("hashtags должен быть строкой или списком строк")
        tags = list(dict.fromkeys([tag.lstrip("#").casefold() for tag in tags if tag.lstrip("#")] + extract_hashtags(description)))
        published_at = metadata.get("published_at") or metadata.get("date") or None
        if published_at is not None:
            published_at = str(published_at)
        video = Video(
            video_id=video_id,
            url=canonical,
            author=str(metadata.get("author") or ""),
            description=description,
            hashtags=tags,
            published_at=published_at,
            query=str(metadata.get("query") or "Ручной импорт"),
            category=str(metadata.get("category") or ""),
            is_test=bool(metadata.get("is_test", False)) and self.config.dry_run,
        )
        self.db.add_video(video)
        return video

    def _import_records(self, records) -> dict:
        # Enforce the queue limit before touching the database, not halfway
        # through an oversized import whose result could otherwise be lost.
        records = list(islice(records, 10001))
        if len(records) > 10000:
            raise ValueError("За один импорт поддерживается не более 10 000 строк")
        counts = {"added": 0, "duplicate": 0, "rejected": 0, "manual_review": 0, "errors": []}
        for index, record in enumerate(records, 1):
            try:
                if isinstance(record, str):
                    record = {"url": record}
                if not isinstance(record, dict) or not record.get("url"):
                    raise ValueError("В строке нет URL")
                canonical, video_id = canonical_tiktok_url(str(record["url"]).strip())
                if self.db.get_video(video_id):
                    counts["duplicate"] += 1
                    continue
                metadata = {k: v for k, v in record.items() if k != "url"}
                video = self.add_url(canonical, **metadata)
                counts["added"] += 1
                if analyze_video(video, self.config)["needs_review"]:
                    counts["manual_review"] += 1
            except (ValueError, TypeError, AttributeError) as error:
                counts["rejected"] += 1
                counts["errors"].append(f"Строка {index}: {error}")
        return counts

    def import_urls(self, text: str) -> dict:
        return self._import_records(line.strip() for line in text.splitlines() if line.strip())

    def import_file(self, path: str | Path) -> dict:
        path = Path(path)
        if path.stat().st_size > 10 * 1024 * 1024:
            raise ValueError("Файл импорта больше 10 МБ")
        content = path.read_text(encoding="utf-8-sig")
        if path.suffix.casefold() == ".json":
            data = json.loads(content)
            if isinstance(data, dict):
                data = data.get("videos")
            if not isinstance(data, list):
                raise ValueError("JSON должен содержать список ссылок/объектов или {videos: [...]}")
            return self._import_records(data)
        if path.suffix.casefold() == ".csv":
            try:
                dialect = csv.Sniffer().sniff(content[:4096], delimiters=",;\t")
            except csv.Error:
                dialect = csv.excel
            rows = csv.DictReader(io.StringIO(content), dialect=dialect)
            if not rows.fieldnames or "url" not in rows.fieldnames:
                raise ValueError("CSV должен содержать столбец url")
            return self._import_records(rows)
        return self.import_urls(content)

    def search(self) -> list[dict]:
        """Filter persisted candidates. Unknown dates remain in a manual queue."""
        processed = set()
        existing = set()
        for task in self.db.list_tasks(self.config.account_id):
            existing.add(task["video_id"])
            if task.get("comment") or task.get("publication_status") == "PUBLISHED" or task.get("processing_status") == "SKIPPED":
                processed.add(task["video_id"])
        results = []
        for row in self.db.list_videos():
            if row["video_id"] in processed:
                continue
            analysis = analyze_video(row, self.config)
            if analysis["eligible"] or analysis["needs_review"]:
                results.append({**row, **analysis})
        # Prioritize unqueued candidates so repeated small cycles do not starve
        # later imports behind the same unknown-date review rows.
        results.sort(key=lambda row: (row["video_id"] in existing, not row["eligible"]))
        return results[:self.config.max_videos]

    def enrich(self, video_id: str) -> Video:
        row = self.db.get_video(str(video_id))
        if not row:
            raise ValueError("Видео не найдено в локальной базе")
        canonical, _ = canonical_tiktok_url(row["url"])
        if self.config.dry_run:
            raise DiscoveryError("В dry-run сетевое получение метаданных отключено")
        try:
            response = self.client.get("https://www.tiktok.com/oembed", params={"url": canonical}, timeout=self.config.timeout)
            if response.status_code == 429:
                raise DiscoveryError("TikTok ограничил частоту запросов; получение метаданных остановлено")
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("Некорректный ответ oEmbed")
            title = payload.get("title", "")
            author = payload.get("author_name", "")
            if not isinstance(title, str) or not isinstance(author, str):
                raise ValueError("Некорректные метаданные oEmbed")
        except (httpx.HTTPError, ValueError) as error:
            raise DiscoveryError(f"Не удалось получить метаданные TikTok ({type(error).__name__}); можно заполнить вручную") from None
        # Crucially do not infer publication date from ID, current time or oEmbed.
        changes = {"description": title or row.get("description", ""), "author": author or row.get("author", "")}
        changes["hashtags"] = extract_hashtags(changes["description"])
        self.db.update_video(video_id, **changes)
        updated = self.db.get_video(video_id)
        return Video(**{key: updated.get(key, default) for key, default in {
            "video_id": video_id, "url": canonical, "author": "", "description": "", "hashtags": [],
            "published_at": None, "query": "", "category": "", "is_test": False,
        }.items()})

    def close(self):
        if self._owns_client:
            self.client.close()
