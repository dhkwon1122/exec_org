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


def test_subtasks_are_tasks_and_survive_parent_delete():
    t = store.create_task('부모')
    sub = store.create_todo('하위', task_id=t['id'], due_date='2026-10-20', assignee_name='김철수')
    store.create_todo('단독')
    task = store.get_task(sub['id'])  # 할 일 = 업무
    assert (task['parent_id'], task['status'], task['owner'], task['due_date']) == (t['id'], '진행중', '김철수',
                                                                                   '2026-10-20')
    assert [c['title'] for c in store.child_tasks(t['id'])] == ['하위']
    assert any(h['content'] == '하위 업무 추가: 하위' for h in store.list_history(t['id']))
    store.update_task(sub['id'], status='완료')  # 업무 화면에서 완료해도 체크된다
    assert store.list_todos(task_id=t['id'])[0]['done'] and store.get_task(sub['id'])['done_at']
    store.delete_task(t['id'])  # 부모를 지워도 하위 업무는 최상위로 남는다
    assert store.get_task(sub['id'])['parent_id'] is None


def test_quick_delete_only_for_plain_items():
    t = store.create_task('부모')
    sub = store.create_todo('하위', task_id=t['id'])
    with pytest.raises(ValueError, match='업무 화면'):
        store.delete_todo(t['id'])  # 하위 업무가 있는 업무
    store.add_history(sub['id'], '진행 메모', '메모')
    with pytest.raises(ValueError):
        store.delete_todo(sub['id'])  # 직접 쓴 기록이 있는 업무
    plain = store.create_todo('그냥 체크')
    store.delete_todo(plain['id'])
    assert store.get_task(plain['id']) is None


def test_parent_cycle_rejected():
    a = store.create_task('A')
    b = store.create_todo('B', task_id=a['id'])
    c = store.create_todo('C', task_id=b['id'])
    with pytest.raises(ValueError, match='하위 업무를 상위'):
        store.update_task(a['id'], parent_id=c['id'])


def test_my_tasks_by_id_or_name():
    store.create_task('이름만 담당', owner='김철수')
    store.create_todo('아이디 담당', assignee_id='kim', assignee_name='김철수')
    store.create_todo('남의 것', assignee_id='lee', assignee_name='이영희')
    assert {t['title'] for t in store.list_todos(assignee_id='kim', assignee_name='김철수')} == {'이름만 담당',
                                                                                               '아이디 담당'}


def test_old_todos_move_into_tasks(tmp_path):
    from sqlalchemy import create_engine, text
    path = tmp_path / 'old.db'
    db.configure('sqlite:///' + str(path))
    t = store.create_task('예전 업무')
    h = store.add_history(t['id'], '원문', '메모')
    with create_engine(f'sqlite:///{path}').begin() as c:  # 예전 버전이 남긴 할 일 2건
        c.execute(text("INSERT INTO todos (task_id, title, due_date, done, done_at, assignee_id, assignee_name, "
                       "created_by, source_history_id, created_at) VALUES "
                       "(:t, '견적 받기', '2026-10-20', 1, '2026-10-19 10:00:00', 'kim', '김철수', '권', :h, "
                       "'2026-10-18 09:00:00'), (NULL, '단독 할 일', '', 0, NULL, '', '', '', NULL, "
                       "'2026-10-18 09:00:00')"), {'t': t['id'], 'h': h['id']})
    db.configure('sqlite:///' + str(path))  # 다시 시작 → 업무로 옮김
    subs = store.list_todos(task_id=t['id'])
    assert [(x['title'], x['done'], x['assignee_name'], x['due_date']) for x in subs] == [
        ('견적 받기', True, '김철수', '2026-10-20')]
    assert subs[0]['done_at'] == '2026-10-19 10:00' and store.todo_titles_for_history(h['id']) == {'견적 받기'}
    assert any(x['title'] == '단독 할 일' and x['task_id'] is None for x in store.list_todos())
    with create_engine(f'sqlite:///{path}').connect() as c:
        assert c.execute(text('SELECT COUNT(*) FROM todos')).scalar() == 0
    db.configure('sqlite:///' + str(path))  # 한 번만 옮긴다
    assert len(store.list_todos(task_id=t['id'])) == 1


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
    assert [x['title'] for x in days['2026-10-31']['todos']] == ['a']  # 마감인 업무도 같은 체크 목록
    assert days['2026-10-01']['tasks_start'][0]['title'] == 'a'
    assert '2026-11-02' not in days


def test_task_tree_depth_and_status_filter():
    a = store.create_task('A')
    b = store.create_todo('B', task_id=a['id'])
    c = store.create_todo('C', task_id=b['id'])
    store.create_todo('D', task_id=a['id'])
    store.create_task('E')
    flat = lambda rows: [(r['task']['title'], r['level'], r['n_hidden']) for r in rows]  # noqa: E731
    assert flat(store.task_tree(depth=0)) == [('E', 0, 0), ('A', 0, 2)]
    assert flat(store.task_tree(depth=1)) == [('E', 0, 0), ('A', 0, 0), ('B', 1, 1), ('D', 1, 0)]
    assert flat(store.task_tree(depth=99)) == [('E', 0, 0), ('A', 0, 0), ('B', 1, 0), ('C', 2, 0), ('D', 1, 0)]
    store.set_todo_done(c['id'], True)
    rows = store.task_tree(depth=99)
    assert next(r for r in rows if r['task']['title'] == 'B')['n_done'] == 1
    # 진행중만: 완료된 C 는 빠짐
    assert [r['task']['title'] for r in store.task_tree(['진행중'], depth=99)] == ['E', 'A', 'B', 'D']
