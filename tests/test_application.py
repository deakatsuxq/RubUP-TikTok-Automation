"""Behavioral checks across storage, preparation and the application facade."""
from pathlib import Path
import threading

import httpx
from PIL import Image
import pytest

from application import AppService
from config import Config, save_config
from database import Database
from validators import ValidationError


class CountingGenerator:
    def __init__(self):
        self.calls = []

    def generate(self,task_id,video):
        self.calls.append(task_id)
        return f"реклама: RubUP, какой скин выбрать {video['video_id']}?"


@pytest.fixture
def app(tmp_path,monkeypatch):
    def forbidden_network(*args,**kwargs):
        raise AssertionError("These integration tests must never make a live network request")
    monkeypatch.setattr(httpx.Client,"request",forbidden_network)
    cfg = Config(db_path=str(tmp_path/"real.sqlite3"),target_per_minute=60)
    cfg_path = tmp_path/"config.yaml"
    save_config(cfg,cfg_path)
    service = AppService(cfg_path)
    service.generator.close()
    service.generator = CountingGenerator()
    yield service
    service.close()


def add_candidate(app,video_id="123456789",published_at="2026-06-01",account=None):
    app.db.add_video({"video_id":video_id,"url":f"https://www.tiktok.com/@test_fixture/video/{video_id}",
                      "description":"#cs2 выбор скинов","hashtags":["cs2"],"published_at":published_at})
    return app.db.ensure_task(account or app.config.account_id,video_id)


def test_unknown_date_stays_manual_and_never_spends_generation(app):
    task = add_candidate(app,published_at=None)
    app.refresh()
    result = app.prepare_pending()
    assert result["manual_review"] == 1
    assert result["prepared"] == 0
    with pytest.raises(ValidationError):
        app.prepare_one(task["id"])
    assert app.generator.calls == []
    assert app.db.get_task(task["id"])["comment"] == ""


def test_draft_cached_and_account_video_pair_survives_restart(app):
    task = add_candidate(app)
    app.prepare_one(task["id"])
    cached = app.prepare_one(task["id"])
    assert app.generator.calls == [task["id"]]
    assert cached["publication_status"] == "UNCONFIRMED"
    app.close()
    restored = AppService(app.config_path)
    try:
        again = restored.db.ensure_task(restored.config.account_id,task["video_id"])
        assert again["id"] == task["id"]
        assert again["comment"] == cached["comment"]
        assert restored.prepare_pending()["prepared"] == 0
        assert restored.stats()["published"] == 0
    finally:
        restored.close()


def test_exhausted_generation_is_skipped_without_automatic_new_charge(app):
    task = add_candidate(app)
    calls = []

    def exhausted(task_id,video):
        calls.append(task_id)
        app.db.update_task(task_id,processing_status="SKIPPED",error="Нет допустимого варианта")
        raise ValidationError("Исчерпаны три попытки")

    app.generator.generate = exhausted
    first = app.prepare_pending()
    second = app.prepare_pending()
    assert first["failed"] == 1
    assert second["failed"] == second["prepared"] == 0
    assert calls == [task["id"]]
    assert app.db.get_task(task["id"])["processing_status"] == "SKIPPED"


def test_dry_run_uses_isolated_test_database_and_never_network(app):
    task = add_candidate(app)
    result = app.dry_run()
    assert result["test_data"] is True
    assert result["network_requests"] == 0
    assert result["prepared"] == 2
    assert app.stats()["tasks"] == 1
    assert app.db.get_task(task["id"])["comment"] == ""
    assert app.generator.calls == []
    test_db = Database(result["database"])
    try:
        rows = test_db.list_tasks()
        assert len(rows) == 2
        assert all(row["is_test"] for row in rows)
        assert all(row["publication_status"] == "UNCONFIRMED" for row in rows)
        assert test_db.stats()["requests"] == 0
        assert test_db.stats()["telegram_sent"] == 0
        assert test_db.stats()["confirmed_reward"] == 0
    finally:
        test_db.close()
    repeated = app.dry_run()
    assert repeated["prepared"] == 0
    assert repeated["stats"]["tasks"] == 2


def test_other_account_cannot_prepare_edit_or_confirm(app):
    task = add_candidate(app,account="other-account")
    with pytest.raises(ValidationError):
        app.prepare_one(task["id"])
    with pytest.raises(ValidationError):
        app.save_comment(task["id"],"реклама: RubUP, какой скин выбрать?")
    with pytest.raises(ValidationError):
        app.confirm_publication(task["id"],{"published":True,"comment_present":True,"comment_not_deleted":True})
    assert app.db.get_task(task["id"])["comment"] == ""
    assert app.generator.calls == []


def test_stop_after_one_task_retains_queue_for_next_cycle(app):
    first = add_candidate(app,"1111")
    second = add_candidate(app,"2222")
    stop = threading.Event()
    result = app.prepare_pending(stop_event=stop,progress=lambda _:stop.set())
    assert result["prepared"] == 1
    assert result["stopped"] is True
    assert app.generator.calls == [first["id"]]
    assert app.db.get_task(second["id"])["comment"] == ""
    stop.clear()
    continued = app.prepare_pending(stop_event=stop)
    assert continued["prepared"] == 1
    assert app.generator.calls == [first["id"],second["id"]]


def test_no_generation_is_charged_when_required_image_missing(app):
    app.config.require_image = True
    app.config.image_mode = "task"
    task = add_candidate(app)
    app.db.update_task(task["id"],checklist={"require_image":True,"required_image_slot":2})
    with pytest.raises((ValueError,ValidationError)):
        app.prepare_one(task["id"])
    assert app.generator.calls == []
    assert app.db.get_task(task["id"])["comment"] == ""


@pytest.mark.parametrize("mode",["specific","rotate","task"])
def test_task_specific_image_requirement_overrides_general_order(app,tmp_path,mode):
    app.config.require_image = True
    app.config.image_mode = mode
    app.config.selected_image = 1
    for slot in range(1,5):
        source = tmp_path/f"original_{slot}.png"
        Image.new("RGB",(20,10),(slot*30,0,0)).save(source)
        app.images.set_image(slot,source)
    task = add_candidate(app)
    app.db.update_task(task["id"],checklist={"require_image":True,"required_image_slot":2})
    prepared = app.prepare_one(task["id"])
    assert prepared["image_slot"] == 2
    assert app.generator.calls == [task["id"]]


def test_changed_metadata_invalidates_eligibility_before_confirmation(app):
    task = add_candidate(app)
    app.prepare_one(task["id"])
    app.update_metadata(task["id"],None,"#cs2 без даты")
    with pytest.raises(ValidationError):
        app.confirm_publication(task["id"],{"published":True,"comment_present":True})
    assert app.db.get_task(task["id"])["publication_status"] == "UNCONFIRMED"


def test_full_manual_outcome_chain_does_not_assume_report_or_money(app):
    task = add_candidate(app)
    app.prepare_one(task["id"])
    with pytest.raises(ValueError):
        app.confirm_publication(task["id"],{"published":True,"comment_present":True})
    confirmed = app.confirm_publication(task["id"],{
        "published":True,"comment_present":True,"comment_not_deleted":True})
    assert confirmed["publication_status"] == "PUBLISHED"
    assert confirmed["report_status"] == "NONE"
    assert confirmed["reward_status"] == "UNCONFIRMED"
    with pytest.raises(ValidationError):
        app.prepare_one(task["id"],regenerate=True)
    assert app.generator.calls == [task["id"]]
    app.report_tracker.report_submitted(task["id"])
    app.report_tracker.report_accepted(task["id"])
    app.report_tracker.confirm_reward(task["id"],1.25)
    assert app.stats()["confirmed_reward"] == 1.25


def test_manual_review_queue_does_not_starve_later_eligible_tasks(app):
    app.config.max_videos = 1
    unknown = add_candidate(app,"3333",published_at=None)
    eligible = add_candidate(app,"4444")
    result = app.prepare_pending()
    assert result["prepared"] == 1
    assert app.generator.calls == [eligible["id"]]
    assert app.db.get_task(unknown["id"])["comment"] == ""


def test_replacing_selected_original_cannot_silently_change_prepared_task(app,tmp_path):
    app.config.require_image = True
    app.config.image_mode = "specific"
    app.config.selected_image = 2
    original = tmp_path/"image.png"
    Image.new("RGB",(20,10),"red").save(original)
    app.images.set_image(2,original)
    task = add_candidate(app)
    prepared = app.prepare_one(task["id"])
    old_hash = prepared["checklist"]["image_sha256"]
    Image.new("RGB",(20,10),"blue").save(original)
    app.images.set_image(2,original)
    assert app.images.verify(2)
    assert app.images.get_image(2)["sha256"] != old_hash
    with pytest.raises(ValidationError):
        app.confirm_publication(task["id"],{
            "published":True,"comment_present":True,"comment_not_deleted":True,"image_attached":True})
    assert app.db.get_task(task["id"])["publication_status"] == "UNCONFIRMED"
