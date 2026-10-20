import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from database import Database


def video(n="100", **extra):
    return {"video_id":n,"url":f"https://www.tiktok.com/@fixture/video/{n}",
            "hashtags":["cs2"],"description":"проверка","published_at":"2026-06-01",**extra}


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path/"history.sqlite3")
    yield database
    database.close()


def test_video_task_persist_after_restart(tmp_path):
    path = tmp_path/"state.sqlite3"
    db = Database(path)
    assert db.add_video(video()) is True
    task = db.ensure_task("my-account","100")
    db.update_task(task["id"],comment="в RubUP какой скин выбрать",processing_status="PENDING_APPROVAL",checklist={"require_like":True})
    db.close()
    restored = Database(path)
    row = restored.get_task(task["id"])
    assert row["hashtags"] == ["cs2"]
    assert row["comment"] == "в RubUP какой скин выбрать"
    assert row["checklist"]["require_like"] is True
    assert restored.events()[0]["kind"] == "TASK_UPDATED"
    assert row["publication_status"] == "UNCONFIRMED"
    assert row["reward_status"] == "UNCONFIRMED"


def test_duplicate_video_and_account_pair_are_idempotent(db):
    assert db.add_video(video())
    assert not db.add_video(video())
    first = db.ensure_task("one","100")
    assert db.ensure_task("one","100")["id"] == first["id"]
    assert db.ensure_task("two","100")["id"] != first["id"]
    assert len(db.list_tasks()) == 2
    assert len(db.list_tasks("one")) == 1


def test_normalized_comment_uniqueness_is_database_constraint(db):
    for n in ("100","101"):
        db.add_video(video(n))
    one = db.ensure_task("a","100")
    two = db.ensure_task("a","101")
    db.update_task(one["id"],comment="  RubUP, какой скин? ")
    assert db.comment_exists("rubup какой   скин")
    assert not db.comment_exists("rubup какой скин",exclude_task_id=one["id"])
    with pytest.raises(sqlite3.IntegrityError):
        db.update_task(two["id"],comment="RUBUP какой скин")
    assert db.get_task(two["id"])["comment"] == ""
    assert len(db.events()) == 1  # failed write and its event rolled back


def test_invalid_task_field_and_status_rejected(db):
    db.add_video(video())
    task = db.ensure_task("a","100")
    with pytest.raises(ValueError):
        db.update_task(task["id"],account_id="hijack")
    with pytest.raises(sqlite3.IntegrityError):
        db.update_task(task["id"],processing_status="imaginary")
    with pytest.raises(sqlite3.IntegrityError):
        db.update_task(task["id"],reward_status="REWARD_CONFIRMED")
    assert db.get_task(task["id"])["account_id"] == "a"


def test_published_comment_is_immutable(db):
    db.add_video(video())
    task = db.ensure_task("a","100")
    db.update_task(task["id"],comment="RubUP какой скин выбрать",publication_status="PUBLISHED")
    with pytest.raises(ValueError):
        db.update_task(task["id"],comment="RubUP другой новый комментарий")
    with pytest.raises(ValueError):
        db.update_task(task["id"],publication_status="UNCONFIRMED")


def test_atomic_account_pair_and_telegram_reservations_across_threads(db):
    db.add_video(video())
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda _: db.ensure_task("same","100")["id"],range(20)))
    assert len(set(results)) == 1
    with ThreadPoolExecutor(8) as pool:
        reserved = list(pool.map(lambda _:db.reserve_telegram(video()["url"]),range(20)))
    assert sum(reserved) == 1


@pytest.mark.parametrize("status",["SENT","UNKNOWN","RESERVED"])
def test_telegram_uncertain_success_or_inflight_never_automatically_retries(db,status):
    url = video()["url"]
    assert db.reserve_telegram(url)
    db.finish_telegram(url,status)
    assert not db.reserve_telegram(url)


def test_only_definite_telegram_failure_can_be_retried(db):
    db.add_video(video())
    task = db.ensure_task("a","100")
    url = video()["url"]
    db.reserve_telegram(url)
    db.finish_telegram(url,"FAILED","429")
    assert db.reserve_telegram(url)
    db.finish_telegram(url,"SENT")
    assert db.get_task(task["id"])["telegram_status"] == "SENT"
    assert db.get_task(task["id"])["publication_status"] == "UNCONFIRMED"
    with pytest.raises(ValueError):
        db.finish_telegram(url,"FAILED")


def test_usage_unknown_pricing_not_reported_as_free(db):
    db.record_usage(None,"model",100,20,0.002,False,1)
    db.record_usage(None,"model",200,30,None,True,2)
    usage = db.usage_summary()
    assert usage["requests"] == 2
    assert usage["successful_generations"] == 1
    assert usage["retries"] == 1
    assert usage["input_tokens"] == 300
    assert usage["output_tokens"] == 50
    assert usage["total_cost"] is None
    assert usage["known_cost"] == pytest.approx(0.002)


def test_update_metadata_keeps_video_identity(db):
    db.add_video(video())
    db.update_video("100",published_at=None,author="creator",hashtags=["nft"])
    assert db.get_video("100")["published_at"] is None
    assert db.get_video("100")["hashtags"] == ["nft"]
    with pytest.raises((ValueError,TypeError)):
        db.update_video("100",video_id="change")


def test_memory_database_survives_per_operation_connections():
    db = Database(":memory:")
    db.add_video(video())
    assert db.ensure_task("a","100")["url"] == video()["url"]
    db.close()


def test_foreign_keys_prevent_missing_video_tasks(db):
    with pytest.raises(KeyError):
        db.ensure_task("account","missing")
    assert db.list_tasks() == []


@pytest.mark.parametrize("cost",[-1,float("nan"),float("inf")])
def test_nonfinite_or_negative_cost_rejected(db,cost):
    with pytest.raises(ValueError):
        db.record_usage(None,"model",1,1,cost,True,1)
