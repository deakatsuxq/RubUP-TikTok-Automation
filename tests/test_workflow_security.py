"""Cross-module regressions for review gates and dry-run side-effect isolation."""
from pathlib import Path

import httpx
from PIL import Image
import pytest

from application import AppService
from config import Config, save_config
from models import Video
from validators import ValidationError


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setattr(httpx.Client, "request", lambda *a, **k: pytest.fail("unexpected live network"))
    path = tmp_path / "config.yaml"
    save_config(Config(db_path=str(tmp_path / "production.sqlite3")), path)
    app = AppService(path)
    yield app
    app.close()


def candidate(service, number="123"):
    service.db.add_video(Video(number, f"https://www.tiktok.com/@fixture/video/{number}",
                               description="#cs2 пример", hashtags=["cs2"], published_at="2026-06-01"))
    return service.db.ensure_task(service.config.account_id, number)


def image_files(service, tmp_path):
    for slot in range(1, 5):
        path = tmp_path / f"original{slot}.png"
        Image.new("RGB", (20, 10), (slot * 50, 0, 0)).save(path)
        service.images.set_image(slot, path)


def test_confirmation_cannot_override_stored_like_requirement(service):
    task = candidate(service)
    service.set_task_requirements(task["id"], False, require_like=True)
    service.save_comment(task["id"], "реклама: что там в RubUP?")
    with pytest.raises((ValueError, ValidationError)):
        service.confirm_publication(task["id"], {"published": True, "comment_present": True,
              "comment_not_deleted": True, "require_like": False, "own_comment_liked": False})
    assert service.db.get_task(task["id"])["publication_status"] == "UNCONFIRMED"


def test_confirmation_cannot_override_stored_image_requirement(service, tmp_path):
    image_files(service, tmp_path)
    task = candidate(service)
    service.set_task_requirements(task["id"], True, required_image_slot=2)
    service.save_comment(task["id"], "реклама: что там в RubUP?")
    with pytest.raises((ValueError, ValidationError)):
        service.confirm_publication(task["id"], {"published": True, "comment_present": True,
            "comment_not_deleted": True, "require_image": False, "required_image_slot": None,
            "image_attached": False})
    assert service.db.get_task(task["id"])["publication_status"] == "UNCONFIRMED"


def test_dry_run_preserves_production_images_rotation_and_history(service, tmp_path):
    image_files(service, tmp_path)
    service.images.select(mode="rotate")
    before = {p.name: p.read_bytes() for p in service.images.directory.iterdir() if p.is_file()}
    production_before = service.db.stats()
    result = service.dry_run()
    after = {p.name: p.read_bytes() for p in service.images.directory.iterdir() if p.is_file()}
    assert result["test_data"] is True
    assert before == after
    assert service.db.stats() == production_before
    assert service.images.select(mode="rotate")["slot"] == 2


def test_cached_draft_never_advances_image_rotation_or_repeats_generation(service, tmp_path):
    image_files(service, tmp_path)
    service.config.require_image = True
    service.config.image_mode = "rotate"
    task = candidate(service)
    calls = []
    def generate(task_id, video):
        calls.append(task_id)
        return "реклама: что там в RubUP?"
    service.generator.generate = generate
    first = service.prepare_one(task["id"])
    second = service.prepare_one(task["id"])
    assert calls == [task["id"]]
    assert first["image_slot"] == second["image_slot"] == 1
    assert service.images.select(mode="rotate")["slot"] == 2
    assert len(service.images.usage_history()) == 1


def test_revoked_metadata_blocks_status_based_telegram_delivery(service):
    task = candidate(service)
    service.save_comment(task["id"], "реклама: что там в RubUP?")
    service.update_metadata(task["id"], None, "#cs2 дата больше не подтверждена")
    service.config.telegram_mode = "draft"
    calls = []
    # Network remains forbidden. This spy represents the send gate, not a fake
    # successful Telegram API result.
    service.telegram.send_video = lambda row: calls.append(row) or True
    result = service.send_links()
    assert calls == []
    assert result["sent"] == 0


def test_test_data_cannot_be_manually_promoted_to_real_publication(service):
    service.db.add_video(Video("999", "https://www.tiktok.com/@fixture/video/999", description="#cs2 test",
                              hashtags=["cs2"], published_at="2026-06-01", is_test=True))
    task = service.db.ensure_task(service.config.account_id, "999")
    service.save_comment(task["id"], "тест: реклама, что там в RubUP?")
    with pytest.raises((ValueError, ValidationError)):
        service.confirm_publication(task["id"], {"published": True, "comment_present": True,
                                                "comment_not_deleted": True})
    assert service.db.stats()["published"] == 0
