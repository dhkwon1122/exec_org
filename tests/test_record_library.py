"""기록 보존·복원 및 사용자별 북마크의 회귀 검증."""
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import select, text

from services import db, records, store
from services.db import HistoryBookmark, HistoryEntry, HistoryRevision


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'UPLOAD_DIR', str(tmp_path / 'uploads'))
    db.configure('sqlite:///' + str(tmp_path / 'records.db'))


def record():
    task = store.create_task('분기 보고')
    person = store.create_person('김임원')
    h = store.add_history(task['id'], '원문 보존', title='첫 제목', person_ids=[person['id']], author='작성자')
    return task, person, h


def test_bookmarks_are_personal_and_survive_trash_restore():
    t, p, h = record()
    assert records.toggle_bookmark(h['id'], 'alice') is True
    assert records.bookmark_ids('bob') == set()
    assert [r['id'] for r in store.search_history(bookmark_user='alice')] == [h['id']]
    assert store.search_history(bookmark_user='bob') == []
    store.delete_history(h['id'], actor='삭제자')
    assert records.bookmark_ids('alice') == set()
    assert store.search_history(bookmark_user='alice') == []
    records.restore('history', h['id'])
    assert records.bookmark_ids('alice') == {h['id']}
    assert records.toggle_bookmark(h['id'], 'alice') is False
    with pytest.raises(ValueError):
        records.toggle_bookmark(h['id'], '')


def test_record_trash_hides_search_counts_keywords_calendar_and_downloads():
    t, p, h = record()
    store.update_history(h['id'], keywords='검증키워드', occurred_at='2026-10-21T09:00')
    ref = store.add_reference(t['id'], 'file', history_id=h['id'], file_name='근거.txt', file_bytes=b'keep')
    store.delete_history(h['id'], actor='삭제자')
    assert store.get_history(h['id']) is None
    assert store.search_history('원문') == []
    assert ('검증키워드', 1) not in store.keyword_stats()
    assert store.get_reference(ref['id']) is None
    assert store.list_references() == []
    assert store.list_people()[0]['count'] == 0
    assert store.calendar_month(2026, 10).get('2026-10-21', {}).get('history', 0) == 0
    trash = records.list_trash('첫 제목')
    assert len(trash) == 1 and trash[0]['by'] == '삭제자'
    assert store.upload_abspath(ref['file_path'])
    records.restore('history', h['id'])
    assert store.get_history(h['id'])['refs'][0]['id'] == ref['id']
    assert store.list_people()[0]['count'] == 1


def test_task_restore_preserves_children_links_and_only_grouped_records():
    t, p, h = record()
    child = store.create_task('하위', parent_id=t['id'])
    other = store.create_task('연결')
    store.link_tasks(t['id'], other['id'])
    separately_deleted = store.add_history(t['id'], '개별 삭제')
    store.delete_history(separately_deleted['id'])
    store.delete_task(t['id'], actor='삭제자')
    assert store.get_task(child['id'])['parent_id'] is None
    assert store.list_history(t['id']) == []
    assert store.get_history(h['id']) is None
    with pytest.raises(ValueError, match='업무를 먼저 복원'):
        records.restore('history', separately_deleted['id'])
    rows = records.list_trash()
    assert {(r['kind'], r['id']) for r in rows} == {('task', t['id']), ('history', separately_deleted['id'])}
    records.restore('task', t['id'])
    assert store.get_task(child['id'])['parent_id'] == t['id']
    assert store.get_history(h['id'])
    assert store.get_history(separately_deleted['id']) is None
    assert store.linked_tasks(other['id'])[0]['id'] == t['id']


def test_shared_record_and_file_survive_primary_task_trash_and_purge():
    t, p, h = record()
    other = store.create_task('다른 업무')
    store.update_history(h['id'], task_ids=[t['id'], other['id']])
    ref = store.add_reference(t['id'], 'file', history_id=h['id'], file_name='공유.txt', file_bytes=b'shared')
    store.delete_task(t['id'])
    assert store.get_history(h['id'])['task_id'] == other['id']
    assert store.get_reference(ref['id'])['task_id'] == other['id']
    assert store.list_history(t['id']) == []
    store.update_history(h['id'], title='공유 기록 편집', task_ids=[other['id']])
    records.restore('task', t['id'])
    assert store.get_history(h['id'])['also_tasks'][0]['id'] == t['id']
    store.delete_task(t['id'])
    records.purge('task', t['id'])
    assert store.get_history(h['id'])['title'] == '공유 기록 편집'
    assert store.upload_abspath(ref['file_path'])


def test_purge_removes_versions_bookmarks_attachments_and_preserves_generated_task():
    t, p, h = record()
    records.toggle_bookmark(h['id'], 'alice')
    generated = store.create_todo('파생 업무', source_history_id=h['id'])
    ref = store.add_reference(t['id'], 'file', history_id=h['id'], file_name='a.txt', file_bytes=b'x')
    with pytest.raises(ValueError, match='휴지통'):
        records.purge('history', h['id'])
    store.delete_history(h['id'])
    records.purge('history', h['id'])
    assert records.revisions(h['id']) == []
    with db.SessionLocal() as s:
        assert s.get(HistoryBookmark, (h['id'], 'alice')) is None
    assert store.upload_abspath(ref['file_path']) is None
    assert store.get_task(generated['id'])['source_history_id'] is None


def test_task_purge_clears_generated_record_source_and_preserves_child():
    t, p, h = record()
    generated = store.create_todo('파생 업무', task_id=t['id'], source_history_id=h['id'])
    ref = store.add_reference(t['id'], 'file', history_id=h['id'], file_name='a.txt', file_bytes=b'x')
    records.toggle_bookmark(h['id'], 'alice')
    store.delete_task(t['id'])
    records.purge('task', t['id'])
    assert store.get_task(generated['id'])['parent_id'] is None
    assert store.get_task(generated['id'])['source_history_id'] is None
    assert records.revisions(h['id']) == [] and records.bookmark_ids('alice') == set()
    assert store.upload_abspath(ref['file_path']) is None


def test_versions_capture_actual_changes_and_restore_with_optimistic_lock():
    t, p, h = record()
    first = records.revisions(h['id'])[0]
    updated = store.update_history(h['id'], title='수정 제목', content='수정 원문', actor='편집자',
                                   actor_id='alice', expected_version=h['edit_version'])
    assert records.revisions(h['id'])[0]['actor'] == '편집자'
    before, after = records.revision_pair(h['id'], records.revisions(h['id'])[0]['id'])
    assert before['content'] == '원문 보존' and after['content'] == '수정 원문'
    with pytest.raises(ValueError, match='다시 열어'):
        records.restore_revision(h['id'], first['id'], h['edit_version'])
    restored = records.restore_revision(h['id'], first['id'], updated['edit_version'], actor='복원자')
    assert restored['content'] == h['content'] and restored['title'] == h['title']
    assert records.revisions(h['id'])[0]['action'] == '버전 복원'
    count = len(records.revisions(h['id']))
    store.update_history(h['id'], title=h['title'])
    assert len(records.revisions(h['id'])) == count


def test_legacy_history_first_edit_captures_baseline_and_transaction_rolls_back_revision():
    t, p, h = record()
    with db.transaction() as s:
        for r in s.scalars(select(HistoryRevision).where(HistoryRevision.history_id == h['id'])):
            s.delete(r)
    store.update_history(h['id'], title='첫 수정')
    baseline = records.revisions(h['id'])[-1]
    assert baseline['action'] == '기준 버전'
    assert records.revision_pair(h['id'], baseline['id'])[1]['title'] == '첫 제목'
    count = len(records.revisions(h['id']))
    with pytest.raises(ValueError):
        with db.transaction() as s:
            store.update_history(h['id'], title='롤백', session=s)
            raise ValueError('실패')
    assert len(records.revisions(h['id'])) == count
    assert store.get_history(h['id'])['title'] == '첫 수정'


def test_late_ai_cannot_change_trashed_or_restored_record():
    t, p, h = record()
    token = store.begin_history_ai(h['id'])
    store.delete_history(h['id'])
    store.set_history_ai(h['id'], 'done', title='늦은 AI', token=token)
    records.restore('history', h['id'])
    store.set_history_ai(h['id'], 'done', title='늦은 AI', token=token)
    assert store.get_history(h['id'])['title'] == '첫 제목'


def test_edit_open_before_trash_cannot_overwrite_after_restore():
    t, p, h = record()
    store.delete_history(h['id'])
    records.restore('history', h['id'])
    with pytest.raises(ValueError, match='다시 열어'):
        store.update_history(h['id'], title='오래된 창', expected_version=h['edit_version'])


def test_restoring_task_rejects_parent_cycle_created_while_hidden():
    parent = store.create_task('상위')
    archived = store.create_task('삭제 예정', parent_id=parent['id'])
    child = store.create_task('하위', parent_id=archived['id'])
    store.delete_task(archived['id'])
    store.update_task(parent['id'], parent_id=child['id'])
    with pytest.raises(ValueError, match='순환'):
        records.restore('task', archived['id'])
    assert store.get_task(archived['id']) is None


def test_revision_must_belong_to_record_and_deleted_context_cannot_be_restored():
    t, p, h = record()
    another = store.add_history(t['id'], '별도 기록')
    rid = records.revisions(h['id'])[0]['id']
    with pytest.raises(ValueError):
        records.restore_revision(another['id'], rid, 0)
    with db.transaction() as s:
        revision = s.get(HistoryRevision, rid)
        snap = json.loads(revision.snapshot)
        snap['task_ids'], snap['person_ids'] = [99999], []
        revision.snapshot = json.dumps(snap)
    with pytest.raises(ValueError, match='먼저 복원'):
        records.restore_revision(h['id'], rid, 0)


def test_trend_person_cannot_be_deleted_while_record_is_in_trash():
    p = store.create_person('김임원')
    h = store.add_history(None, '동향', person_ids=[p['id']])
    store.delete_history(h['id'])
    with pytest.raises(ValueError, match='동향'):
        store.delete_person(p['id'])
    records.restore('history', h['id'])
    assert store.get_history(h['id'])['people'][0]['id'] == p['id']


def test_concurrent_edits_allow_one_writer_and_bookmark_clicks_are_serialized():
    t, p, h = record()
    barrier = Barrier(2)

    def edit(title):
        barrier.wait(timeout=10)
        try:
            store.update_history(h['id'], title=title, expected_version=h['edit_version'])
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(edit, ['창 A', '창 B'])) == [False, True]
    assert len(records.revisions(h['id'])) == 2

    def click(_):
        barrier.wait(timeout=10)
        return records.toggle_bookmark(h['id'], 'alice')

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(click, range(2))) == [False, True]
    assert records.bookmark_ids('alice') == set()


def test_schema_upgrade_preserves_old_record_and_adds_library_fields():
    t, p, h = record()
    previous = db.engine
    with previous.begin() as conn:
        conn.execute(text('DROP TABLE history_bookmarks'))
        conn.execute(text('DROP TABLE history_revisions'))
        for name in ('edit_version', 'deleted_at', 'deleted_by', 'deleted_with_task'):
            conn.execute(text('ALTER TABLE history DROP COLUMN ' + name))
        for name in ('deleted_at', 'deleted_by'):
            conn.execute(text('ALTER TABLE tasks DROP COLUMN ' + name))
    migrated = db.make_engine(previous.url.render_as_string(hide_password=False))
    db.SessionLocal.configure(bind=migrated)
    try:
        got = store.get_history(h['id'])
        assert got['content'] == h['content'] and got['edit_version'] == 0
        store.update_history(h['id'], title='업그레이드 후 수정', expected_version=0)
        assert records.revisions(h['id'])[-1]['action'] == '기준 버전'
        assert records.toggle_bookmark(h['id'], 'alice')
        store.delete_task(t['id'])
        records.restore('task', t['id'])
        assert store.get_history(h['id'])['title'] == '업그레이드 후 수정'
    finally:
        db.SessionLocal.configure(bind=previous)
        migrated.dispose()
