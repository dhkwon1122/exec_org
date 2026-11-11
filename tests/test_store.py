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


def test_history_about_reference_and_linked_task():
    a, b = store.create_task('조직개편'), store.create_task('임원 인사')
    h0 = store.add_history(a['id'], '조직도 초안 공유', '메모')
    ref = store.add_reference(a['id'], 'link', title='조직도 v2', url='https://conf/1', history_id=h0['id'])
    h1 = store.add_history(a['id'], 'v2 검토 의견: 센터 통합안 보류', '피드백', ref_ids=[ref['id']])
    assert [r['id'] for r in h1['refs']] == [ref['id']]  # 이 기록이 다룬 자료로 붙음
    assert store.reference_history_counts(a['id']) == {ref['id']: 2}
    with pytest.raises(ValueError):
        store.add_history(a['id'], 'x', ref_ids=[9999])
    store.link_tasks(a['id'], b['id'], '관련')
    assert store.shared_history_count(a['id'], b['id']) == 0
    store.add_history(a['id'], '두 업무 일정 맞추기로 함', '회의', also_task_ids=[b['id']])
    assert store.shared_history_count(a['id'], b['id']) == 1 == store.shared_history_count(b['id'], a['id'])
    store.delete_reference(ref['id'])  # 자료를 지우면 연결도 사라짐
    assert store.get_history(h1['id'])['refs'] == []


def test_task_tree_can_hide_completed_tasks():
    a = store.create_task('상위 A')
    done = store.create_task('끝난 하위', parent_id=a['id'], status='완료')
    store.create_task('남은 일', parent_id=done['id'])
    b = store.create_task('끝난 상위 B', status='완료')
    store.create_task('B 아래 남은 일', parent_id=b['id'])
    titles = lambda **kw: [(r['task']['title'], r['level']) for r in store.task_tree(depth=99, **kw)]
    assert ('끝난 하위', 1) in titles() and ('끝난 상위 B', 0) in titles()
    shown = titles(hide_done=True)
    assert ('끝난 하위', 1) not in shown and ('끝난 상위 B', 0) not in shown
    assert ('남은 일', 1) in shown and ('B 아래 남은 일', 0) in shown  # 남은 일은 한 단계 올려 보인다
    a_row = next(r for r in store.task_tree(depth=99, hide_done=True) if r['task']['id'] == a['id'])
    assert a_row['n_children'] == 1 and a_row['n_done'] == 1  # 진행률은 숨긴 것까지


def test_link_tasks_as_parent_or_child():
    a = store.create_task('조직 개편')
    b = store.create_task('조직도 초안')
    c = store.create_task('직제 규정 개정')
    store.set_hierarchy(b['id'], a['id'], 'parent', '나')  # b 의 상위 = a
    store.set_hierarchy(b['id'], c['id'], 'child', '나')  # c 를 b 아래로
    assert store.get_task(b['id'])['parent_id'] == a['id'] and store.get_task(c['id'])['parent_id'] == b['id']
    with pytest.raises(ValueError, match='하위 업무를 상위'):
        store.set_hierarchy(a['id'], c['id'], 'parent')  # 손자를 상위로 — 고리
    with pytest.raises(ValueError, match='이미'):
        store.set_hierarchy(a['id'], b['id'], 'child')
    with pytest.raises(ValueError):
        store.set_hierarchy(a['id'], a['id'], 'child')
    assert any('하위 업무로 묶음' in h['content'] for h in store.list_history(a['id']))
    store.detach_parent(b['id'], '나')
    assert store.get_task(b['id'])['parent_id'] is None and store.get_task(c['id'])['parent_id'] == b['id']
    assert {r['task']['id'] for r in store.task_tree(depth=99) if r['level'] == 0} == {a['id'], b['id']}


def test_gantt_spans_and_chart():
    from datetime import date
    from components import gantt
    today = date(2026, 11, 2)
    base = {'id': 1, 'title': 'x', 'status': '진행중', 'start_date': '', 'due_date': '', 'done_at': '',
            'created_at': '2026-10-01 09:00'}
    assert gantt.span({**base, 'start_date': '2026-10-10', 'due_date': '2026-10-20'}, today) == \
        (date(2026, 10, 10), date(2026, 10, 20), True)
    assert gantt.span(base, today) == (date(2026, 10, 1), today, False)  # 마감 미정 — 오늘까지
    assert gantt.span({**base, 'status': '완료', 'done_at': '2026-10-15 10:00'}, today)[1] == date(2026, 10, 15)
    rows = [{'task': {**base, 'due_date': '2026-10-20'}, 'level': 0, 'n_children': 1, 'n_done': 0},
            {'task': {**base, 'id': 2, 'title': 'y'}, 'level': 1, 'n_children': 0, 'n_done': 0}]
    html_ = str(gantt.chart(rows, today))
    assert "className='gantt-bar doing late'" in html_ and "className='gantt-bar doing open'" in html_
    assert "'26.10'" in html_ and "gantt-today-label" in html_  # 달 눈금, 오늘
    assert gantt.chart([], today) is None


def test_gantt_scales_and_paging():
    from datetime import date
    from components import gantt
    today = date(2026, 11, 4)  # 수요일
    spans = [(date(2026, 1, 1), date(2026, 12, 31), True)]
    lo, hi = gantt.window(spans, today, 'week')
    assert lo == date(2026, 10, 26) and (hi - lo).days == 42 and lo.weekday() == 0  # 월요일부터 6주
    assert gantt.window(spans, today, 'week', 1)[0] == date(2026, 12, 7)  # 한 화면 뒤
    lo, hi = gantt.window(spans, today, 'month')
    assert lo == date(2026, 10, 5) and (hi - lo).days == 180
    assert gantt.window(spans, today, 'all')[0] == date(2025, 12, 29)  # 전체 — 업무 기간에 맞춤
    assert gantt.window([(date(2020, 1, 1), today, True)], today, 'all')[0] == date(2025, 11, 4)  # 최대 1년 전부터
    assert [t for _, t in gantt._ticks(date(2026, 10, 26), date(2026, 11, 10), 'week')] == ['10/26', '11/2', '11/9']
    base = {'id': 1, 'title': 'x', 'status': '진행중', 'start_date': '2026-01-05', 'due_date': '2026-02-01',
            'done_at': '', 'created_at': '2026-01-01 09:00'}
    html_ = str(gantt.chart([{'task': base, 'level': 0}], today, 'week'))
    assert 'outside' in html_ and "'오늘'" in html_  # 보이는 기간 밖 업무는 끝에 흐리게


def test_merge_tasks_moves_everything_and_deletes_source():
    from services.db import InboundMail, OutboundMail, TaskLink
    a = store.create_task('예산 편성', description='A 설명', tags='예산', owner='김', start_date='2026-03-01',
                          due_date='2026-05-01')
    b = store.create_task('예산안 작성', description='B 설명', tags='보고', owner='이', start_date='2026-04-01',
                          due_date='2026-04-30')
    c = store.create_task('관련 업무')
    child = store.create_task('세부', parent_id=a['id'])
    h1 = store.add_history(a['id'], 'A 기록', '메모')
    h2 = store.add_history(c['id'], '함께 걸친 기록', '메모', also_task_ids=[a['id'], b['id']])
    h3 = store.add_history(c['id'], 'A 에만 걸친 기록', '메모', also_task_ids=[a['id']])
    ref = store.add_reference(a['id'], 'link', title='자료', url='https://x')
    store.link_tasks(a['id'], c['id'])
    store.link_tasks(a['id'], b['id'])  # 합치면 자기 자신 연결 → 지움
    with db.SessionLocal() as s:
        s.add(InboundMail(uidl='u1', subject='메일', task_ids=f"{a['id']},{b['id']},1{a['id']}"))
        s.add(OutboundMail(subject='공유', related_kind='task', related_id=str(a['id'])))
        s.commit()
    r = store.merge_tasks(a['id'], b['id'], by='나')
    assert store.get_task(a['id']) is None
    t = store.get_task(b['id'])
    assert (t['start_date'], t['due_date']) == ('2026-03-01', '2026-05-01')
    assert t['tags'] == ['보고', '예산'] and 'A 설명' in t['description'] and t['description'].startswith('B 설명')
    assert {x['name'] for x in t['assignees']} == {'이', '김'}
    assert store.get_task(child['id'])['parent_id'] == b['id']
    hist_ids = {h['id'] for h in store.list_history(b['id'])}
    assert {h1['id'], h2['id'], h3['id']} <= hist_ids
    assert any('업무 합침' in h['content'] for h in store.list_history(b['id']))
    assert [x['id'] for x in store.references_for_task(b['id'])] == [ref['id']]
    with db.SessionLocal() as s:
        links = {(lk.src_id, lk.dst_id) for lk in s.query(TaskLink)}
        assert links == {(b['id'], c['id'])}
        m = s.query(InboundMail).one()
        assert m.task_ids == f"{b['id']},1{a['id']}"  # 다른 번호(1x)는 그대로
        assert s.query(OutboundMail).one().related_id == str(b['id'])
    assert r['children'] == 1 and r['references'] == 1 and r['mails'] == 1
    with pytest.raises(ValueError):
        store.merge_tasks(b['id'], b['id'])


def test_merge_into_own_subtask_lifts_it():
    parent = store.create_task('상위')
    a = store.create_task('A', parent_id=parent['id'])
    b = store.create_task('B', parent_id=a['id'])
    store.merge_tasks(a['id'], b['id'])
    assert store.get_task(b['id'])['parent_id'] == parent['id']
