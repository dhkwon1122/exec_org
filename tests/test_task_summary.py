"""업무 AI 요약 — 하위 업무·이력·참고자료를 묶어, 바뀐 업무만 쉬는 시간에 다시 만든다."""
import json
import time
from datetime import date

import pytest

from services import db, llm, store, task_summary


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'UPLOAD_DIR', str(tmp_path / 'uploads'))
    monkeypatch.setattr(db, 'DATA_DIR', str(tmp_path))
    db.configure('sqlite:///' + str(tmp_path / 'test.db'))
    monkeypatch.setattr(llm, 'is_configured', lambda: True)
    calls = []

    def fake_chat(system, user, temperature=0.2):
        calls.append(user)
        return json.dumps({'overview': f'요약 {len(calls)}', 'progress': [{'date': '2026-10-27', 'what': '안건 확정'}],
                           'next': ['규정 개정'], 'references': '예산안 문서'}, ensure_ascii=False)
    monkeypatch.setattr(llm, 'chat', fake_chat)
    yield calls


def test_summary_covers_children_history_and_references(fresh_db):
    t = store.create_task('조직 개편', due_date='2026-12-31')
    kid = store.create_task('직제 규정 개정', parent_id=t['id'])
    store.add_history(t['id'], '안건 확정함', entry_type='회의', title='개편 안건 회의')
    store.add_history(kid['id'], '규정 초안 공유', title='규정 초안')
    store.add_reference(t['id'], 'note', title='참고 메모', note='노조 협의 필요')
    text = task_summary.compose(t['id'])
    assert '[하위 업무]' in text and '직제 규정 개정' in text
    assert '개편 안건 회의' in text and '규정 초안' in text  # 하위 업무의 이력도
    assert '참고 메모: 노조 협의 필요' in text and '마감 2026-12-31' in text
    s = task_summary.generate(t['id'])
    assert s['overview'] == '요약 1' and s['progress'] == [{'date': '2026-10-27', 'what': '안건 확정'}]
    assert s['next'] == ['규정 개정'] and s['references'] == '예산안 문서' and not s['stale']
    task_summary.generate(t['id'])
    assert len(fresh_db) == 1  # 넣을 글이 같으면 다시 묻지 않는다
    task_summary.generate(t['id'], force=True)
    assert len(fresh_db) == 2


def test_changes_make_summary_stale_and_worker_refreshes(fresh_db, monkeypatch):
    from services import doc_worker
    a = store.create_task('A')
    b = store.create_task('B')
    kid = store.create_task('A 하위', parent_id=a['id'])
    assert set(task_summary.pending(10)) == {a['id'], b['id'], kid['id']}
    for tid in (a['id'], b['id'], kid['id']):
        task_summary.generate(tid)
    assert task_summary.pending(10) == []
    time.sleep(1.1)  # 초 단위 시각
    store.add_history(kid['id'], '진행 상황 메모')
    assert set(task_summary.pending(10)) == {kid['id'], a['id']}  # 하위 업무의 변화는 상위로 올라간다
    assert task_summary.get(a['id'])['stale'] is True and task_summary.get(b['id'])['stale'] is False

    monkeypatch.setenv('DOC_SUMMARY_IDLE_MINUTES', '10')
    monkeypatch.setattr(doc_worker, '_fails', {})
    monkeypatch.setattr(doc_worker, '_pause_until', 0.0)
    n = len(fresh_db)
    assert doc_worker.step(10 ** 10) is True and doc_worker.step(10 ** 10 + 1) is True
    assert doc_worker.step(10 ** 10 + 2) is False and task_summary.pending(10) == []
    assert len(fresh_db) == n + 2

    def boom(*a, **k):
        raise llm.LLMError('LLM 서버에 연결할 수 없습니다')
    monkeypatch.setattr(llm, 'chat', boom)
    store.add_history(b['id'], '새 기록')
    time.sleep(0.01)
    with pytest.raises(llm.LLMError):
        task_summary.generate(b['id'], force=True)
    assert '연결' in task_summary.get(b['id'])['error']


def test_history_marks_for_gantt():
    from components import gantt
    t = store.create_task('A', start_date='2026-10-01', due_date='2026-11-30')
    other = store.create_task('B')
    store.add_history(t['id'], '회의 결과', entry_type='회의', title='1차 회의', occurred_at='2026-10-10 10:00')
    store.add_history(t['id'], '메모', occurred_at='2026-10-10 15:00')
    store.add_history(other['id'], '두 업무에 걸친 기록', occurred_at='2026-10-20 09:00', also_task_ids=[t['id']])
    marks = store.history_marks([t['id']])[t['id']]
    assert [(m['date'], m['n']) for m in marks] == [('2026-10-10', 2), ('2026-10-20', 1)]
    assert marks[0]['items'][0] == '[회의] 1차 회의'
    html_ = str(gantt.chart([{'task': store.get_task(t['id']), 'level': 0}], date(2026, 11, 2), 'all',
                            marks=store.history_marks([t['id']])))
    assert html_.count("className='gantt-dot many'") == 1 and html_.count("className='gantt-dot'") == 1
    assert "'type': 'gantt-task'" in html_
