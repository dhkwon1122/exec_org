import pytest

from services import db, store


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'UPLOAD_DIR', str(tmp_path / 'uploads'))
    db.configure('sqlite:///' + str(tmp_path / 'test.db'))
    yield


def test_create_and_status_change_logs_history():
    t = store.create_task('보고서 작성', tags='보고, 분기')
    assert t['tags'] == ['보고', '분기']
    store.update_task(t['id'], author='kim', status='완료')
    hist = store.list_history(t['id'])
    assert [h['entry_type'] for h in hist] == ['상태변경', '상태변경']
    assert '진행중 → 완료' in hist[0]['content']


def test_validation():
    with pytest.raises(ValueError):
        store.create_task('  ')
    t = store.create_task('a')
    with pytest.raises(ValueError):
        store.add_history(t['id'], '')
    with pytest.raises(ValueError):
        store.update_task(t['id'], parent_id=t['id'])


def test_history_occurred_at_order():
    t = store.create_task('a')
    store.add_history(t['id'], '과거 회의', '회의', occurred_at='2020-01-01T10:00')
    store.add_history(t['id'], '오늘 결정', '결정')
    contents = [h['content'] for h in store.list_history(t['id'])]
    assert contents[-1] == '과거 회의'
    assert contents[0] in ('오늘 결정', '업무 생성 (상태: 진행중)')


def test_references_link_file_note_and_delete():
    t = store.create_task('a')
    link = store.add_reference(t['id'], 'link', url='example.com/doc')
    assert link['url'] == 'https://example.com/doc'
    f = store.add_reference(t['id'], 'file', file_name='../../evil name.txt', file_bytes=b'hi')
    path = store.upload_abspath(f['file_path'])
    assert path and open(path, 'rb').read() == b'hi'
    assert '..' not in f['file_path']
    store.add_reference(t['id'], 'note', note='첫 줄 제목\n본문')
    refs = store.list_references(task_id=t['id'])
    assert {r['kind'] for r in refs} == {'link', 'file', 'note'}
    assert any(r['title'] == '첫 줄 제목' for r in refs)
    store.delete_reference(f['id'])
    assert store.upload_abspath(f['file_path']) is None
    # 자료 추가는 이력에도 남는다
    assert sum(h['entry_type'] == '자료' for h in store.list_history(t['id'])) == 3


def test_upload_path_traversal_blocked():
    assert store.upload_abspath('../../etc/passwd') is None


def test_links_are_bidirectional_with_inverse_relation():
    a = store.create_task('설계')
    b = store.create_task('구현')
    store.link_tasks(a['id'], b['id'], '선행', note='설계 끝나야 구현')
    assert store.linked_tasks(a['id'])[0]['relation'] == '선행'
    assert store.linked_tasks(b['id'])[0]['relation'] == '후속'
    with pytest.raises(ValueError):
        store.link_tasks(b['id'], a['id'])
    assert any(h['entry_type'] == '연결' for h in store.list_history(b['id']))
    store.unlink_tasks(store.linked_tasks(a['id'])[0]['link_id'])
    assert store.linked_tasks(b['id']) == []


def test_search_covers_history_and_references():
    a = store.create_task('업무A')
    b = store.create_task('업무B')
    store.add_history(a['id'], '예산 협의 완료')
    store.add_reference(b['id'], 'note', note='계약서 초안 위치')
    assert [t['id'] for t in store.list_tasks('예산')] == [a['id']]
    assert [t['id'] for t in store.list_tasks('계약서')] == [b['id']]
    assert store.list_tasks(status='완료') == []


def test_delete_task_cascades():
    a = store.create_task('a')
    b = store.create_task('b', parent_id=a['id'])
    f = store.add_reference(a['id'], 'file', file_name='x.txt', file_bytes=b'x')
    store.link_tasks(a['id'], b['id'])
    store.delete_task(a['id'])
    assert store.get_task(a['id']) is None
    assert store.get_task(b['id'])['parent_id'] is None
    assert store.linked_tasks(b['id']) == []
    assert store.upload_abspath(f['file_path']) is None


def test_history_spanning_several_tasks():
    a, b, c = store.create_task('조직개편'), store.create_task('임원 인사'), store.create_task('예산')
    h = store.add_history(a['id'], '조직개편안 확정에 따라 임원 인사 발령 준비', '회의', also_task_ids=[b['id'], a['id']],
                          keywords=['발령'])
    assert h['task_id'] == a['id'] and [t['id'] for t in h['also_tasks']] == [b['id']]
    mine = lambda tid: [x['id'] for x in store.list_history(tid) if x['entry_type'] not in store.AUTO_ENTRY_TYPES]  # noqa: E731
    assert mine(b['id']) == [h['id']]  # 걸친 업무 이력에도 보인다
    assert store.search_history(task_id=b['id'])[0]['id'] == h['id']
    assert store.search_history('인사')[0]['id'] == h['id']  # 걸친 업무 제목으로도 찾는다
    assert b['id'] in [t['id'] for t in store.list_tasks('확정')]
    assert ('발령', 1) in store.keyword_stats(task_id=b['id'])
    # 수정 창: 걸친 업무 전체를 다시 고른다 — 원래 업무를 빼면 남은 첫 업무가 이력의 업무
    store.add_reference(a['id'], 'note', title='메모', note='메모', history_id=h['id'])
    h = store.update_history(h['id'], task_ids=[b['id'], c['id']])
    assert h['task_id'] == b['id'] and [t['id'] for t in h['also_tasks']] == [c['id']]
    assert h['refs'][0]['task_id'] == b['id'] and mine(a['id']) == []
    # 이 업무에서 빼기
    store.unlink_history_task(h['id'], c['id'])
    assert store.get_history(h['id'])['also_tasks'] == [] and mine(c['id']) == []
    with pytest.raises(ValueError):
        store.unlink_history_task(h['id'], b['id'])  # 하나만 남으면 빼지 못함 (삭제를 써야)
    with pytest.raises(ValueError):
        store.update_history(h['id'], task_ids=[])  # 임원도 업무도 없는 기록은 안 됨
    with pytest.raises(ValueError):
        store.add_history(a['id'], 'x', also_task_ids=[9999])


def test_deleting_main_task_keeps_shared_history():
    a, b = store.create_task('A'), store.create_task('B')
    h = store.add_history(None, '두 업무에 걸친 기록', also_task_ids=[a['id'], b['id']])
    assert h['task_id'] == a['id']  # 업무 없이 걸친 업무만 주면 첫 업무가 이력의 업무
    only_a = store.add_history(a['id'], 'A 만의 기록')
    store.delete_task(a['id'])
    moved = store.get_history(h['id'])
    assert moved['task_id'] == b['id'] and moved['also_tasks'] == []
    assert store.get_history(only_a['id']) is None
