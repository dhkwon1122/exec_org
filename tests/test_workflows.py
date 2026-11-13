"""초안·후속조치·통합 보고서의 데이터 보존 및 범위 검증."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from threading import Barrier

import pytest
from sqlalchemy import select

from services import db, drafts, followups, llm, records, reports, store
from services.db import Task


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'UPLOAD_DIR', str(tmp_path / 'uploads'))
    db.configure('sqlite:///' + str(tmp_path / 'workflows.db'))


def suggestion():
    task = store.create_task('보고 준비')
    h = store.add_history(task['id'], '보고서를 요청함', occurred_at='2026-11-10T09:00')
    todo = {'title': '보고서 작성', 'due': '2026-11-20', 'assignee': '김담당'}
    store.set_history_ai(h['id'], 'done', todos=[todo])
    return task, h, todo, followups.suggestion_key(todo)


def test_drafts_are_isolated_by_user_and_context_and_clear_requires_token():
    saved = drafts.save('a', 'task:1', {'body': '비공개 초안'})
    assert drafts.get('b', 'task:1') is None and drafts.get('a', 'task:2') is None
    with pytest.raises(ValueError):
        drafts.save('', 'task:1', {})
    with pytest.raises(ValueError):
        drafts.clear('a', 'task:1', 'stale')
    with pytest.raises(ValueError):
        drafts.save('a', 'task:1', {'body': '덮어쓰기'})
    current = drafts.save('a', 'task:1', {'body': '수정'}, saved['token'])
    with pytest.raises(ValueError):
        drafts.save('a', 'task:1', {'body': '오래된 창'}, saved['token'])
    drafts.clear('a', 'task:1', current['token'])
    assert drafts.get('a', 'task:1') is None


def test_draft_tokens_do_not_match_after_delete_and_recreate():
    old = drafts.save('a', 'home', {'body': '첫 초안'})
    drafts.clear('a', 'home', old['token'])
    new = drafts.save('a', 'home', {'body': '새 초안'})
    assert old['token'] != new['token']
    with pytest.raises(ValueError):
        drafts.clear('a', 'home', old['token'])
    assert drafts.get('a', 'home')['payload']['body'] == '새 초안'


def test_suggestion_decisions_are_personal_expire_and_restore():
    t, h, todo, key = suggestion()
    assert len(followups.suggestions('a')) == 1
    until = (date.today() + timedelta(days=2)).isoformat()
    followups.decide('a', h['id'], key, 'deferred', until)
    assert not followups.suggestions('a') and len(followups.suggestions('b')) == 1
    assert len(followups.suggestions('a', 'deferred')) == 1
    assert len(followups.suggestions('a', today=date.today() + timedelta(days=3))) == 1
    followups.decide('a', h['id'], key, 'excluded')
    assert len(followups.suggestions('a', 'excluded')) == 1
    followups.decide('a', h['id'], key, 'pending')
    assert len(followups.suggestions('a')) == 1
    with pytest.raises(ValueError):
        followups.decide('a', h['id'], key, 'deferred', date.today().isoformat())


def test_concurrent_conversion_creates_one_task_even_when_title_is_edited():
    t, h, todo, key = suggestion()
    gate = Barrier(2)

    def convert(_):
        gate.wait(timeout=10)
        return followups.convert(h['id'], key, '고친 업무 제목', parent_id=t['id'], by='김담당')

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(convert, range(2)))
    assert sum(r['created'] for r in results) == 1 and results[0]['id'] == results[1]['id']
    assert not followups.suggestions('a')
    store.update_task(results[0]['id'], title='다시 바꾼 제목')
    store.set_history_ai(h['id'], 'done', todos=[todo])  # AI 재정리로 원문의 task_id가 사라져도 중복 방지
    assert not followups.suggestions('a')
    assert followups.convert(h['id'], key, '보고서 작성')['created'] is False


def test_changed_or_trashed_suggestion_is_not_converted_and_failed_save_is_atomic():
    t, h, todo, key = suggestion()
    with pytest.raises(ValueError):
        followups.convert(h['id'], key, '', parent_id=t['id'])
    assert len(store.list_tasks()) == 1
    store.set_history_ai(h['id'], 'done', todos=[{'title': '새 제안'}])
    with pytest.raises(ValueError, match='변경'):
        followups.convert(h['id'], key, '보고서 작성')
    store.delete_history(h['id'])
    assert not followups.suggestions('a')
    with pytest.raises(ValueError, match='휴지통'):
        followups.convert(h['id'], key, '보고서 작성')


def test_existing_card_conversion_is_reused_and_trashed_task_is_not_duplicated():
    t, h, todo, key = suggestion()
    created = store.create_todo(todo['title'], source_history_id=h['id'])
    store.mark_ai_todo(h['id'], 0, created['id'])
    assert followups.convert(h['id'], key, '다른 제목')['id'] == created['id']
    store.delete_task(created['id'])
    with pytest.raises(ValueError, match='휴지통'):
        followups.convert(h['id'], key, '중복 금지')


def test_attention_excludes_archived_tasks_and_recent_shared_history_prevents_stale():
    old = datetime.now() - timedelta(days=30)
    a = store.create_task('정체 업무', owner='김담당')
    b = store.create_task('최근 활동', owner='이담당')
    with db.transaction() as s:
        for t in s.scalars(select(Task)):
            t.created_at = t.updated_at = old
    store.add_history(a['id'], '최근 작성', also_task_ids=[b['id']])
    rows = followups.attention_tasks()
    assert all('14일 이상 변경 없음' not in r['reasons'] for r in rows if r['id'] in (a['id'], b['id']))
    u = store.create_task('담당자 없음')
    assert any(r['id'] == u['id'] and '담당자 미지정' in r['reasons'] for r in followups.attention_tasks())
    store.delete_task(u['id'])
    assert all(r['id'] != u['id'] for r in followups.attention_tasks())


def test_report_filters_period_task_children_people_and_trash():
    a = store.create_task('범위 A')
    child = store.create_task('하위', parent_id=a['id'])
    other = store.create_task('범위 밖')
    person = store.create_person('김임원')
    h = store.add_history(child['id'], '기간 안 결정', '결정', occurred_at='2026-11-10T23:59:59', person_ids=[person['id']])
    store.add_history(child['id'], '다음 날', occurred_at='2026-11-11T00:00')
    store.add_history(other['id'], '다른 업무', occurred_at='2026-11-10T09:00', person_ids=[person['id']])
    data = reports.collect('2026-11-10', '2026-11-10', [a['id']], [person['id']])
    assert data['record_count'] == 1 and data['facts'][0]['key'] == f"H{h['id']}"
    text = reports.render(data, base_url='https://example.test')['text']
    assert '기간 안 결정' in text and f'https://example.test/history/{h["id"]}' in text
    assert '다음 날' not in text and '다른 업무' not in text
    assert reports.collect('2026-11-10', '2026-11-10', [a['id']], include_children=False)['record_count'] == 0
    store.delete_history(h['id'])
    assert reports.collect('2026-11-10', '2026-11-10', [a['id']], [person['id']])['record_count'] == 0


def test_report_completion_uses_actual_done_date_and_preserves_basic_draft_on_bad_ai(monkeypatch):
    t, h, todo, key = suggestion()
    store.set_todo_done(t['id'], True)
    with db.transaction() as s:
        s.get(Task, t['id']).done_at = datetime(2026, 11, 10, 12)
    data = reports.collect('2026-11-10', '2026-11-10', [t['id']])
    assert any(f['category'] == '완료 업무' for f in data['facts'])
    monkeypatch.setattr(llm, 'is_configured', lambda: True)
    monkeypatch.setattr(llm, 'chat', lambda *a: '{"sections":[{"name":"진행 사항","items":[{"text":"허구","sources":["H9999"]}]}]}')
    report = reports.render(data, ai=True)
    assert '기본 초안' in report['note'] and '허구' not in report['text']
    monkeypatch.setattr(llm, 'chat', lambda *a: '{"sections":[{"name":"진행 사항","items":[{"text":"보고 요청 확인","sources":["H'+str(h['id'])+'"]}]}]}')
    report = reports.render(data, ai=True)
    assert '보고 요청 확인' in report['text'] and f'/history/{h["id"]}' in report['text']


@pytest.mark.parametrize('first,last', [('2026-11-11','2026-11-10'), ('bad','2026-11-10'), ('2020-01-01','2026-01-01')])
def test_report_rejects_invalid_period(first, last):
    with pytest.raises(ValueError):
        reports.collect(first, last)


def test_new_summary_sections_are_restored_and_merge_preserves_trashed_records():
    t, h, todo, key = suggestion()
    store.update_history(h['id'], sections='[{"name":"결정","text":"원래 내용"}]')
    version = records.revisions(h['id'])[0]['id']
    changed = store.update_history(h['id'], sections='[]')
    records.restore_revision(h['id'], version, changed['edit_version'])
    assert store.get_history(h['id'])['sections'][0]['text'] == '원래 내용'
    store.delete_history(h['id'])
    dst = store.create_task('합친 업무')
    store.merge_tasks(t['id'], dst['id'])
    records.restore('history', h['id'])
    assert store.get_history(h['id'])['task_id'] == dst['id']


def test_merge_into_deep_descendant_keeps_valid_tree():
    root = store.create_task('A')
    child = store.create_task('B', parent_id=root['id'])
    leaf = store.create_task('C', parent_id=child['id'])
    store.merge_tasks(root['id'], leaf['id'])
    assert store.get_task(leaf['id'])['parent_id'] is None
    assert store.get_task(child['id'])['parent_id'] == leaf['id']
    assert {r['task']['id'] for r in store.task_tree(depth=99)} == {child['id'], leaf['id']}


def test_trash_restore_rebuilds_document_cache_and_invalidates_summary():
    from services import documents
    from services.db import TaskSummary
    t = store.create_task('문서 보존')
    h = store.add_history(t['id'], '기록')
    store.add_reference(t['id'], 'file', history_id=h['id'], file_name='운영계획.txt', file_bytes=b'text')
    assert documents.cached()[1] == 1

    with db.transaction() as s:
        s.add(TaskSummary(task_id=t['id'], data='{"overview":"예전 요약"}'))
    store.delete_history(h['id'])
    assert documents.cached()[1] == 0
    with db.SessionLocal() as s:
        assert s.scalar(select(TaskSummary).where(TaskSummary.task_id == t['id'])) is None
    records.restore('history', h['id'])
    assert documents.cached()[1] == 1


def test_task_history_filters_auto_before_pagination():
    import app  # noqa: F401 — 페이지 등록
    from pages import task_detail
    t = store.create_task('기록 표시')
    store.add_history(t['id'], '회의 메모', title='찾아야 할 기록', occurred_at='2020-01-01T09:00')
    for index in range(35):
        store.update_task(t['id'], status='보류' if index % 2 else '예정')
    rendered, maximum, _ = task_detail.render_history(None, None, None, 1, t['id'])
    assert '찾아야 할 기록' in str(rendered) and maximum == 1
    assert task_detail.render_history(None, None, None, 1, t['id'], True)[1] == 2
