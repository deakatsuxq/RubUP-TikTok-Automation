"""User text validation, permanent deduplication and safe usage resets."""
import pytest

from automation_store import AutomationStore
from comment_bank import CommentBank
from database import Database


@pytest.fixture
def bank(tmp_path):
    db = Database(tmp_path / 'history.sqlite3')
    bank = CommentBank(db)
    yield bank
    db.close()


def links(count=1, start=0):
    return [(f'https://www.tiktok.com/@fixture/video/{n}', str(n)) for n in range(start, start + count)]


def complete(store, task):
    store.mark_filled(task['id'])
    store.confirm_publication(task['id'])
    store.confirm_like(task['id'])


def test_import_500_user_comments_without_generation(bank):
    text = '\n'.join(f'RubUP — мой собственный комментарий №{n}' for n in range(500))
    assert bank.import_text(text) == {'added': 500, 'duplicates': 0, 'rejected': 0, 'errors': []}
    assert bank.stats() == {'total': 500, 'used': 0, 'available': 500}
    assert len(bank.list_comments()) == 500


def test_case_spacing_and_unicode_width_duplicates_are_permanent(bank):
    result = bank.import_text('RubUP очень интересно\n rubup   очень интересно \nＲｕｂＵＰ очень интересно')
    assert result['added'] == 1 and result['duplicates'] == 2
    assert bank.import_text('RUBUP очень интересно')['duplicates'] == 1
    assert bank.list_comments()[0]['text'] == 'RubUP очень интересно'


def test_requires_literal_rubup_token_and_reports_bad_rows_without_echo(bank):
    result = bank.import_text('Без бренда\nRubUPPER не подходит\nподходит #RubUP!\nРубап только кириллицей\nRubUP ' + 'x' * 1000)
    assert result['added'] == 1 and result['rejected'] == 4
    assert all('Без бренда' not in error for error in result['errors'])
    assert 'RubUP' in result['errors'][0]


def test_too_many_lines_do_not_partially_import(bank):
    with pytest.raises(ValueError, match='500'):
        bank.import_text('\n'.join(f'RubUP строка {n}' for n in range(501)))
    assert bank.stats()['total'] == 0
    with pytest.raises(ValueError):
        bank.import_text(None)


def test_empty_import_and_control_characters(bank):
    assert bank.import_text('\n\n')['added'] == 0
    result = bank.import_text('RubUP опасный\0символ\nRubUP нормальный\tкомментарий')
    assert result['added'] == result['rejected'] == 1
    assert bank.list_comments()[0]['text'] == 'RubUP нормальный комментарий'


def test_assignment_uses_each_comment_once_for_both_profiles(bank):
    bank.import_text('RubUP комментарий один\nRubUP комментарий другой')
    bank.store.add_videos(links())
    assert bank.store.assign_comments()['assigned'] == 2
    tasks = bank.store.tasks()
    assert tasks[0]['comment'] != tasks[1]['comment']
    assert bank.stats() == {'total': 2, 'used': 2, 'available': 0}
    assert all(comment['assigned'] for comment in bank.list_comments())
    assert bank.store.assign_comments()['assigned'] == 0


def test_reset_protects_ready_filled_and_published_but_unliked_comments(bank):
    bank.import_text('\n'.join(f'RubUP строка {n}' for n in range(4)))
    bank.store.add_videos(links(2))
    bank.store.assign_comments()
    ready, filled, published, done = bank.store.tasks()
    bank.store.mark_filled(filled['id'])
    bank.store.mark_filled(published['id'])
    bank.store.confirm_publication(published['id'])
    complete(bank.store, done)
    result = bank.reset_usage()
    assert result == {'reset': 1, 'protected': 3, 'available': 1}
    comments = {comment['id']: comment for comment in bank.list_comments()}
    assert not comments[done['comment_id']]['used']
    assert all(comments[task['comment_id']]['used'] for task in (ready, filled, published))
    assert bank.store.tasks()[0]['comment_id'] == ready['comment_id']


def test_reset_can_reuse_finished_comment_on_new_video_without_touching_history(bank):
    bank.import_text('RubUP первый комментарий\nRubUP второй комментарий')
    bank.store.add_videos(links())
    bank.store.assign_comments()
    first_tasks = bank.store.tasks()
    for task in first_tasks:
        complete(bank.store, task)
    assert bank.reset_usage()['reset'] == 2
    bank.store.add_videos(links(1, 1))
    assert bank.store.assign_comments()['assigned'] == 2
    second_tasks = [task for task in bank.store.tasks() if task['video_id'] == '1']
    assert {task['comment_id'] for task in first_tasks} == {task['comment_id'] for task in second_tasks}
    assert second_tasks[0]['comment_id'] != second_tasks[1]['comment_id']
    assert all(bank.store.get_task(task['id'])['status'] == 'DONE' for task in first_tasks)
    assert all(row['use_count'] == 2 for row in bank.list_comments())


def test_reset_never_shares_one_comment_between_accounts_same_video(bank):
    bank.import_text('RubUP единственный комментарий')
    bank.store.add_videos(links())
    assert bank.store.assign_comments()['missing'] == 1
    assigned = next(task for task in bank.store.tasks() if task['comment_id'])
    complete(bank.store, assigned)
    assert bank.reset_usage()['reset'] == 1
    result = bank.store.assign_comments()
    assert result == {'assigned': 0, 'missing': 1, 'available': 1}
    assert len([task for task in bank.store.tasks() if task['comment_id']]) == 1


def test_reset_matching_finds_all_valid_assignments_despite_bad_random_order(bank, monkeypatch):
    bank.import_text('RubUP первый\nRubUP второй\nRubUP третий')
    bank.store.add_videos(links(3))
    comments = [row['id'] for row in bank.list_comments()]
    # This represents completed work over several explicit resets. Three
    # unfinished partner tasks exclude [first, second, second] respectively.
    tasks = bank.store.tasks()
    with bank.db._connection(write=True) as conn:
        for task, comment_id in zip(tasks[::2], (comments[0], comments[1], comments[1])):
            conn.execute("UPDATE v4_tasks SET comment_id=?,status='DONE',published=1,liked=1 WHERE id=?",
                         (comment_id, task['id']))
    class AdversarialRandom:
        def shuffle(self, values):
            values[:] = [comments[2], comments[0], comments[1]]
    monkeypatch.setattr('automation_store.secrets.SystemRandom', AdversarialRandom)
    result = bank.store.assign_comments()
    assert result == {'assigned': 3, 'missing': 0, 'available': 0}
    allocated = [bank.store.get_task(task['id']) for task in tasks[1::2]]
    assert len({task['comment_id'] for task in allocated}) == 3
    assert all(task['comment_id'] != completed['comment_id']
               for task, completed in zip(allocated, [bank.store.get_task(task['id']) for task in tasks[::2]]))


def test_reopen_keeps_bank_usage_and_unfinished_assignments(bank):
    bank.import_text('RubUP один\nRubUP два')
    bank.store.add_videos(links())
    bank.store.assign_comments()
    restored = CommentBank(AutomationStore(bank.db))
    assert restored.list_comments() == bank.list_comments()
    assert restored.stats() == {'total': 2, 'used': 2, 'available': 0}
    assert restored.reset_usage()['protected'] == 2
