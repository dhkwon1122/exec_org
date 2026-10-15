from datetime import date, timedelta

import pytest

from services import db, store


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'UPLOAD_DIR', str(tmp_path / 'uploads'))
    db.configure('sqlite:///' + str(tmp_path / 'test.db'))
    yield


def test_todo_lifecycle_and_history_log():
    t = store.create_task('장비 도입')
    td = store.create_todo('B사 견적 받기', task_id=t['id'], due_date='2026-10-20', assignee_id='kim',
                           assignee_name='김철수')
    assert td['task_title'] == '장비 도입'
    assert store.set_todo_done(td['id'], True, by='김철수') is True
    assert store.set_todo_done(td['id'], True) is False  # 같은 값이면 아무 일 없음
    assert any(h['entry_type'] == '할일' and 'B사 견적 받기' in h['content'] for h in store.list_history(t['id']))
    assert store.list_todos(task_id=t['id'])[0]['done']
    with pytest.raises(ValueError):
        store.create_todo('x', due_date='10/20')
    with pytest.raises(ValueError):
        store.create_todo('  ')


def test_todo_sorting_and_filters():
    today = date.today()
    store.create_todo('날짜 없음')
    late = store.create_todo('지난 것', due_date=(today - timedelta(days=2)).isoformat(), assignee_id='me')
    store.create_todo('내일', due_date=(today + timedelta(days=1)).isoformat(), assignee_id='me')
    done = store.create_todo('끝난 것', due_date=today.isoformat())
    store.set_todo_done(done['id'], True)
    titles = [t['title'] for t in store.list_todos()]
    assert titles == ['지난 것', '내일', '날짜 없음', '끝난 것']
    assert store.list_todos(task_id=None)[0]['overdue']
    assert [t['title'] for t in store.list_todos(assignee_id='me')] == ['지난 것', '내일']
    assert all(not t['done'] for t in store.list_todos(done=False))
    store.delete_todo(late['id'])
    assert len(store.list_todos(assignee_id='me')) == 1


def test_todos_cascade_with_task():
    t = store.create_task('a')
    store.create_todo('x', task_id=t['id'])
    store.create_todo('단독')
    store.delete_task(t['id'])
    assert [x['title'] for x in store.list_todos()] == ['단독']


def test_ai_todo_suggestions_roundtrip():
    t = store.create_task('a')
    h = store.add_history(t['id'], '원문')
    store.set_history_ai(h['id'], 'done', todos=[{'title': '보고서 수정', 'due': '2026-10-24', 'assignee': ''}])
    assert store.get_history(h['id'])['ai_todos'][0]['title'] == '보고서 수정'
    store.create_todo('보고서 수정', task_id=t['id'], source_history_id=h['id'])
    assert store.todo_titles_for_history(h['id']) == {'보고서 수정'}


def test_calendar_month():
    t = store.create_task('a', start_date='2026-10-01', due_date='2026-10-31')
    store.create_todo('할 일', due_date='2026-10-15', task_id=t['id'])
    store.create_todo('다음 달', due_date='2026-11-02')
    store.add_history(t['id'], '회의', '회의', occurred_at='2026-10-15T14:00')
    days = store.calendar_month(2026, 10)
    assert [x['title'] for x in days['2026-10-15']['todos']] == ['할 일']
    assert days['2026-10-15']['history'] == 1  # 자동 이력(업무 생성)은 세지 않음
    assert days['2026-10-31']['tasks_due'][0]['title'] == 'a'
    assert days['2026-10-01']['tasks_start'][0]['title'] == 'a'
    assert '2026-11-02' not in days
