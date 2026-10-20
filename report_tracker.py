"""Explicit user confirmations; publication, reporting and rewards stay separate."""
from __future__ import annotations

import csv
import json
import math
from pathlib import Path


class ReportTracker:
    def __init__(self, db):
        self.db = db

    def set_checklist(self, task_id, checklist: dict):
        if not isinstance(checklist, dict):
            raise ValueError("Чек-лист должен быть словарём")
        merged = {**self.db.get_task(task_id)["checklist"], **checklist}
        self.db.update_task(task_id, checklist=merged)
        return self.db.get_task(task_id)

    def confirm_publication(self, task_id, checklist: dict):
        """Record an explicit manual attestation; this does not call TikTok."""
        from validators import validate_comment

        task = self.db.get_task(task_id)
        if task.get("is_test"):
            raise ValueError("Тестовые данные не подтверждают реальную публикацию")
        if task["publication_status"] == "PUBLISHED":
            raise ValueError("Публикация уже подтверждена для этого аккаунта и видео")
        if task["processing_status"] not in {"DRAFT_READY", "PENDING_APPROVAL"}:
            raise ValueError("Сначала подготовьте и проверьте комментарий")
        if not isinstance(checklist,dict):
            raise ValueError("Требуется явное подтверждение чек-листа")
        protected = {"require_image", "require_like", "required_image_slot", "image_sha256"}
        if protected.intersection(checklist):
            raise ValueError("Требования задания нельзя менять через подтверждение публикации")
        checks = {**task["checklist"], **checklist}
        if checks.get("published") is not True:
            raise ValueError("Подтвердите ручную публикацию комментария")
        if checks.get("comment_present") is not True:
            raise ValueError("Подтвердите, что комментарий существует и не удалён")
        if checks.get("comment_not_deleted") is not True:
            raise ValueError("Подтвердите отдельную проверку, что комментарий не удалён")
        # Validation protects RubUP and duplicates even if the local draft was
        # modified outside the graphical editor. Custom length was enforced at
        # preparation; here its stored length avoids a hidden 100-char setting.
        validate_comment(task["comment"], max_length=max(100,len(task["comment"])),
                         recent_comments=self.db.recent_comments(100000,exclude_task_id=task_id))
        if self.db.comment_exists(task["comment"],exclude_task_id=task_id):
            raise ValueError("Комментарий повторяет другую задачу")
        if checks.get("require_image"):
            if task["image_slot"] is None or checks.get("image_attached") is not True:
                raise ValueError("Подтвердите прикрепление обязательной картинки")
        required_slot = checks.get("required_image_slot")
        if required_slot is not None and task["image_slot"] != required_slot:
            raise ValueError("Картинка не соответствует требованиям задания")
        if checks.get("require_like") and checks.get("own_comment_liked") is not True:
            raise ValueError("Подтвердите лайк собственному комментарию")
        checks["comment_contains_rubup"] = True  # checked above, never assumed
        self.db.update_task(task_id, publication_status="PUBLISHED",
                            processing_status="PUBLISHED", checklist=checks, error="")
        self.db.add_event(task_id,"MANUAL_PUBLICATION_CONFIRMED","Подтверждено пользователем; удалённая проверка не выполнялась")
        return self.db.get_task(task_id)

    def report_submitted(self, task_id):
        task = self.db.get_task(task_id)
        if task["publication_status"] != "PUBLISHED":
            raise ValueError("Сначала подтвердите публикацию")
        if task["report_status"] != "NONE":
            raise ValueError("Отчёт уже отмечен отправленным")
        checks = {**task["checklist"], "report_submitted": True}
        self.db.update_task(task_id,report_status="REPORT_SUBMITTED",checklist=checks)
        self.db.add_event(task_id,"REPORT_SUBMITTED","Отправка отчёта подтверждена пользователем")
        return self.db.get_task(task_id)

    def report_accepted(self, task_id):
        task = self.db.get_task(task_id)
        if task["report_status"] != "REPORT_SUBMITTED":
            raise ValueError("Сначала отметьте фактическую отправку отчёта")
        checks = {**task["checklist"], "report_accepted": True}
        self.db.update_task(task_id,report_status="REPORT_ACCEPTED",checklist=checks)
        self.db.add_event(task_id,"REPORT_ACCEPTED","Принятие отчёта подтверждено пользователем")
        return self.db.get_task(task_id)

    def confirm_reward(self, task_id, amount):
        if isinstance(amount,bool):
            raise ValueError("Укажите сумму подтверждённого вознаграждения")
        try:
            amount = float(amount)
        except (ValueError,TypeError):
            raise ValueError("Укажите сумму подтверждённого вознаграждения") from None
        if not math.isfinite(amount) or amount < 0:
            raise ValueError("Сумма должна быть конечной и неотрицательной")
        task = self.db.get_task(task_id)
        if task["report_status"] != "REPORT_ACCEPTED":
            raise ValueError("Сначала подтвердите принятие отчёта")
        if task["reward_status"] == "REWARD_CONFIRMED":
            raise ValueError("Вознаграждение уже подтверждено; повторное начисление запрещено")
        self.db.update_task(task_id,reward_status="REWARD_CONFIRMED",confirmed_reward=amount,
                            checklist={**task["checklist"],"reward_confirmed":True})
        self.db.add_event(task_id,"REWARD_CONFIRMED",f"Подтверждено пользователем: {amount}")
        return self.db.get_task(task_id)

    def export_csv(self, path):
        """Export UTF-8 with BOM for Excel, neutralizing spreadsheet formulas."""
        rows = self.db.list_tasks()
        fields = ["id","account_id","video_id","url","author","published_at",
                  "description","hashtags","query","category","comment","image_slot",
                  "processing_status","publication_status","telegram_status","report_status",
                  "reward_status","expected_reward","confirmed_reward","attempts","error",
                  "checklist","is_test","created_at","updated_at"]

        def cell(value):
            if isinstance(value,(dict,list)):
                value = json.dumps(value,ensure_ascii=False,sort_keys=True)
            if isinstance(value,str) and value.lstrip().startswith(("=","+","-","@")):
                return "'" + value
            return value

        destination = Path(path)
        destination.parent.mkdir(parents=True,exist_ok=True)
        with destination.open("w",encoding="utf-8-sig",newline="") as stream:
            writer = csv.DictWriter(stream,fieldnames=fields,extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({key:cell(value) for key,value in row.items()})
        return len(rows)
