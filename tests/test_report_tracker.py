import csv
import pytest

from database import Database
from report_tracker import ReportTracker


@pytest.fixture
def prepared(tmp_path):
    db = Database(tmp_path/"reports.sqlite3")
    db.add_video({"video_id":"321","url":"https://www.tiktok.com/@fixture/video/321",
                  "author":"=HYPERLINK(\"bad\")","hashtags":["cs2"]})
    task = db.ensure_task("account","321")
    db.update_task(task["id"],comment="в RubUP какой скин выбрать",processing_status="PENDING_APPROVAL",
                   expected_reward=7.5)
    yield db,ReportTracker(db),task["id"]
    db.close()


def test_outcomes_require_separate_confirmations(prepared):
    db,tracker,task_id = prepared
    with pytest.raises(ValueError):
        tracker.report_submitted(task_id)
    with pytest.raises(ValueError):
        tracker.confirm_reward(task_id,7.5)
    task = tracker.confirm_publication(task_id,{"published":True,"comment_present":True,"comment_not_deleted":True})
    assert task["publication_status"] == "PUBLISHED"
    assert task["report_status"] == "NONE"
    assert db.stats()["confirmed_reward"] == 0
    tracker.report_submitted(task_id)
    assert db.get_task(task_id)["report_status"] == "REPORT_SUBMITTED"
    assert db.get_task(task_id)["reward_status"] == "UNCONFIRMED"
    tracker.report_accepted(task_id)
    assert db.get_task(task_id)["confirmed_reward"] == 0
    tracker.confirm_reward(task_id,7.5)
    assert db.stats()["confirmed_reward"] == 7.5
    with pytest.raises(ValueError):
        tracker.confirm_reward(task_id,7.5)


def test_image_like_and_comment_presence_are_required_when_task_requests(prepared):
    db,tracker,task_id = prepared
    tracker.set_checklist(task_id,{"require_image":True,"required_image_slot":2,"require_like":True})
    checks = {"published":True,"comment_present":True,"comment_not_deleted":True,"image_attached":True}
    with pytest.raises(ValueError):
        tracker.confirm_publication(task_id,checks)
    db.update_task(task_id,image_slot=2)
    with pytest.raises(ValueError):
        tracker.confirm_publication(task_id,checks)
    checks["own_comment_liked"] = True
    tracker.confirm_publication(task_id,checks)
    assert db.get_task(task_id)["checklist"]["comment_contains_rubup"] is True


@pytest.mark.parametrize("checks",[{}, {"published":True}, {"published":1,"comment_present":True},
                                  {"published":True,"comment_present":True}])
def test_implicit_confirmation_is_rejected(prepared,checks):
    db,tracker,task_id = prepared
    with pytest.raises(ValueError):
        tracker.confirm_publication(task_id,checks)
    assert db.get_task(task_id)["publication_status"] == "UNCONFIRMED"


def test_missing_rubup_cannot_be_confirmed(prepared):
    db,tracker,task_id = prepared
    db.update_task(task_id,comment="просто другой комментарий")
    with pytest.raises(ValueError):
        tracker.confirm_publication(task_id,{"published":True,"comment_present":True,"comment_not_deleted":True})


@pytest.mark.parametrize("amount",[-1,float("nan"),float("inf"),True,"bad"])
def test_invalid_reward_amount_does_not_change_data(prepared,amount):
    db,tracker,task_id = prepared
    with pytest.raises(ValueError):
        tracker.confirm_reward(task_id,amount)
    assert db.get_task(task_id)["confirmed_reward"] == 0


def test_csv_exports_state_and_prevents_excel_formula_injection(prepared,tmp_path):
    db,tracker,task_id = prepared
    path = tmp_path/"history.csv"
    assert tracker.export_csv(path) == 1
    with path.open(encoding="utf-8-sig",newline="") as stream:
        row = next(csv.DictReader(stream))
    assert row["author"].startswith("'=HYPERLINK")
    assert row["publication_status"] == "UNCONFIRMED"
    assert row["expected_reward"] == "7.5"
    assert row["confirmed_reward"] == "0.0"
