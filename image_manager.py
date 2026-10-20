"""Four byte-for-byte original images, integrity checks and durable rotation."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading

from PIL import Image, UnidentifiedImageError


class ImageManager:
    def __init__(self, directory: str | Path, db=None):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.directory / "manifest.json"
        self.db = db
        self._lock = threading.RLock()
        self._load()

    def _load(self) -> dict:
        if not self.manifest_path.exists():
            return {"slots": {}, "cursor": 0, "history": []}
        try:
            data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("slots"), dict) or type(data.get("cursor")) is not int or data["cursor"] < 0 or not isinstance(data.get("history"), list):
                raise ValueError
            for slot, item in data["slots"].items():
                if slot not in {"1", "2", "3", "4"} or not isinstance(item, dict):
                    raise ValueError
                name = item["filename"]
                if name not in {f"slot_{slot}{extension}" for extension in (".png", ".jpg", ".jpeg")}:
                    raise ValueError
                if not isinstance(item.get("sha256"), str) or len(item["sha256"]) != 64 or any(c not in "0123456789abcdef" for c in item["sha256"]):
                    raise ValueError
                if any(type(item.get(key)) is not int or item[key] <= 0 for key in ("width", "height")):
                    raise ValueError
            if not all(isinstance(item, dict) for item in data["history"]):
                raise ValueError
            return data
        except (ValueError, TypeError, KeyError, OSError):
            raise ValueError("Повреждён журнал картинок; восстановите images/manifest.json из резервной копии") from None

    def _save(self, data):
        fd, temporary = tempfile.mkstemp(prefix=".images-", suffix=".tmp", dir=self.directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.manifest_path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    @staticmethod
    def _slot(slot: int) -> int:
        if type(slot) is not int or slot not in (1, 2, 3, 4):
            raise ValueError("Номер картинки должен быть от 1 до 4")
        return slot

    @staticmethod
    def _hash(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _inspect(path: Path) -> tuple[int, int]:
        try:
            with Image.open(path) as image:
                if image.format not in ("JPEG", "PNG"):
                    raise ValueError("Допустимы только JPG, JPEG и PNG")
                size = image.size
                image.verify()
            # verify() checks structure; load() additionally catches truncated data.
            with Image.open(path) as image:
                image.load()
            return size
        except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError) as error:
            raise ValueError("Изображение повреждено или имеет неподдерживаемый формат") from error

    def set_image(self, slot: int, source: str | Path) -> dict:
        slot = self._slot(slot)
        source = Path(source).resolve()
        extension = source.suffix.casefold()
        if extension not in (".jpg", ".jpeg", ".png"):
            raise ValueError("Допустимы только расширения JPG, JPEG и PNG")
        if not source.is_file():
            raise ValueError("Файл изображения не найден")
        # Validate a private copy, then install those exact validated bytes.
        with self._lock:
            data = self._load()
            previous = data["slots"].get(str(slot))
            fd, temporary = tempfile.mkstemp(prefix=".original-", suffix=extension, dir=self.directory)
            os.close(fd)
            temporary_path = Path(temporary)
            try:
                shutil.copyfile(source, temporary_path)
                width, height = self._inspect(temporary_path)
                digest = self._hash(temporary_path)
                filename = f"slot_{slot}{extension}"
                if previous:
                    previous_path = self.directory / previous["filename"]
                    if previous_path.is_file():
                        # Retain the actual bytes even if a local edit damaged
                        # the previous slot. Never relabel changed bytes as the
                        # old expected digest.
                        old_digest = self._hash(previous_path)
                        archive = self.directory / "archive"
                        archive.mkdir(exist_ok=True)
                        archive_path = archive / f"{old_digest}{previous_path.suffix}"
                        if archive_path.exists():
                            if self._hash(archive_path) != old_digest:
                                raise ValueError("Повреждён архив оригиналов; восстановите его перед заменой")
                        else:
                            shutil.copyfile(previous_path, archive_path)
                            if self._hash(archive_path) != old_digest:
                                raise ValueError("Не удалось сохранить предыдущий оригинал")
                os.replace(temporary_path, self.directory / filename)
                data["slots"][str(slot)] = {"filename": filename, "sha256": digest, "width": width, "height": height}
                self._save(data)
                if previous and previous["filename"] != filename:
                    (self.directory / previous["filename"]).unlink(missing_ok=True)
            finally:
                temporary_path.unlink(missing_ok=True)
            return self.get_image(slot)

    def get_image(self, slot: int) -> dict | None:
        slot = self._slot(slot)
        with self._lock:
            item = self._load()["slots"].get(str(slot))
            if item is None:
                return None
            return {"slot": slot, "path": str(self.directory / item["filename"]), "sha256": item["sha256"], "width": item["width"], "height": item["height"]}

    def list_images(self) -> list[dict]:
        with self._lock:
            return [item for slot in (1, 2, 3, 4) if (item := self.get_image(slot)) is not None]

    def verify(self, slot: int) -> bool:
        with self._lock:
            item = self.get_image(slot)
            if item is None:
                return False
            path = Path(item["path"])
            try:
                return self._hash(path) == item["sha256"] and self._inspect(path) == (item["width"], item["height"])
            except (OSError, ValueError):
                return False

    def select(self, mode: str = "none", order=(1, 2, 3, 4), specific: int = 1, required_slot: int | None = None) -> dict | None:
        with self._lock:
            if mode == "none":
                return None
            if mode == "rotate":
                if len(order) != 4 or set(order) != {1, 2, 3, 4} or any(type(slot) is not int for slot in order):
                    raise ValueError("Порядок должен содержать четыре разных номера 1–4")
                if not all(self.verify(slot) for slot in order):
                    raise ValueError("Для чередования загрузите все четыре неповреждённых оригинала")
                data = self._load()
                slot = order[data["cursor"] % 4]
                data["cursor"] += 1
                self._save(data)
            elif mode == "specific":
                slot = self._slot(specific)
            elif mode == "task":
                if required_slot is None:
                    raise ValueError("В задании не указан номер необходимой картинки")
                slot = self._slot(required_slot)
            else:
                raise ValueError("Неизвестный режим использования картинки")
            if not self.verify(slot):
                raise ValueError(f"Картинка {slot} отсутствует или повреждена")
            return self.get_image(slot)

    def record_usage(self, slot: int, task_id: int) -> None:
        slot = self._slot(slot)
        if not self.verify(slot):
            raise ValueError("Нельзя записать использование отсутствующего или повреждённого оригинала")
        with self._lock:
            data = self._load()
            if any(item.get("slot") == slot and item.get("task_id") == task_id and item.get("sha256") == data["slots"][str(slot)]["sha256"] for item in data["history"]):
                return
            data["history"].append({"slot": slot, "task_id": task_id, "sha256": data["slots"][str(slot)]["sha256"], "at": datetime.now(timezone.utc).isoformat(), "kind": "prepared"})
            self._save(data)

    def usage_history(self) -> list[dict]:
        with self._lock:
            return list(self._load()["history"])
