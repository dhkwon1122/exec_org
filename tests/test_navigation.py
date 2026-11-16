"""메뉴 구성 — '검색·자료' 한 메뉴(이력 검색·참고자료·컨플루언스)와 업무 화면의 '내 업무' 보기."""
import json
from datetime import date, timedelta

import pytest

from services import auth, db, store, users


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'UPLOAD_DIR', str(tmp_path / 'uploads'))
    for name in ('USER_DATABASE_URL', 'DATABASE_URL', 'EXEC_ORG_ROLES', 'SSO_HOST'):
        monkeypatch.delenv(name, raising=False)
    db.configure('sqlite:///' + str(tmp_path / 'test.db'))
    users.reset()
    yield
    users.reset()


@pytest.fixture
def client():
    import app as app_module
    app_module.server.config['TESTING'] = True
    users.create_local_user('kim', 'password123', '김철수', role='executive_org')
    c = app_module.server.test_client()
    c.post('/login', data={'user_id': 'kim', 'password': 'password123'})
    return c


def _text(out) -> str:
    return json.dumps(out, default=lambda o: o.to_plurality_json() if hasattr(o, 'to_plurality_json') else str(o),
                      ensure_ascii=False)


def test_old_todos_address_opens_my_view(client):
    r = client.get('/todos')
    assert r.status_code == 302 and r.headers['Location'].endswith('/?view=mine')


def test_find_menu_is_active_on_all_three_pages():
    import app
    from components import ui
    assert [label for label, *_ in app._NAV].count('검색·자료') == 1
    assert not {'이력 검색', '참고자료', '컨플루언스', '내 업무'} & {label for label, *_ in app._NAV}
    assert all(app._mark_find_nav(p) for p in ('/search', '/references', '/confluence'))
    assert not app._mark_find_nav('/') and not app._mark_find_nav(None)
    tabs = _text(ui.find_tabs('/references'))
    assert all(label in tabs for label, *_ in ui.FIND_TABS)


def test_my_view_groups_only_my_tasks_by_due_date(monkeypatch):
    import app  # noqa: F401 — 페이지 등록
    from pages import home
    monkeypatch.setattr(auth, 'current_user', lambda: {'user_id': 'kim', 'name': '김철수'})
    monkeypatch.setattr(auth, 'current_name', lambda: '김철수')
    me = [{'name': '김철수', 'user_id': 'kim'}]
    today = date.today()
    t = store.create_task('2027 예산 편성', assignees=me, due_date=(today - timedelta(days=1)).isoformat())
    store.create_task('임원 출장 준비', assignees=me, due_date=(today + timedelta(days=3)).isoformat(),
                      parent_id=t['id'])
    store.create_task('남의 업무', assignees=[{'name': '박영수', 'user_id': 'park'}])

    groups, _opts, _tasks, assignee = home.render_mine('mine', False, 0, 0, '', [])
    text = _text(groups)
    assert '지난 마감' in text and '7일 이내' in text and '남의 업무' not in text
    assert assignee == ['u:kim']  # 새 업무의 담당자 기본값은 나
    found = _text(home.render_mine('mine', False, 0, 0, '출장', [])[0])
    assert '출장' in found and '지난 마감' not in found  # 검색어를 따른다 (예산 편성은 빠짐)
    assert '담당인 업무가 없습니다' in _text(home.render_mine('mine', False, 0, 0, '', ['보류'])[0])
    assert home.render_mine('list', False, 0, 0, '', [])[0] is home.no_update  # 다른 보기에서는 그리지 않음

    hide = {'display': 'none'}
    styles = home.switch_view('mine')
    assert styles[3] == hide and styles[4] == {}  # 목록 대신 내 업무
    styles = home.switch_view('list')
    assert styles[3] == {} and styles[4] == hide


def test_six_primary_sections_and_secondary_routes():
    import app
    from components import ui
    assert [label for label, *_ in app._NAV] == ['업무','사람','메일','캘린더','검색·자료','통합 보고서']
    assert app.section_active('/task/1')[0]
    assert app.section_active('/followups')[0]
    assert app.section_active('/people/1')[1]
    assert app.section_active('/leaders')[1]
    assert app.section_active('/outbox')[2]
    assert app.section_active('/glossary')[-1]
    assert app._mark_find_nav('/bookmarks')
    assert '내 업무' in _text(ui.work_tabs('mine'))
    assert '리더급 현황' in _text(ui.people_tabs('/leaders'))


def test_old_drafts_are_available_only_to_owner_and_migrate_links():
    from services import drafts
    drafts.save('kim','task:2',{'h-content':'초안', 'h-also':[3,2], 'h-people':[1], 'h-type':'회의'})
    drafts.save('park','qr',{'qr-content':'다른 사람의 초안'})
    drafts.save('kim','report',{'rp-text':'보고서 초안'})
    rows = drafts.legacy_records('kim')
    assert [r['context'] for r in rows] == ['task:2']
    migrated = drafts.migrate_record_payload(rows[0]['context'], rows[0]['payload'])
    assert migrated['rc-tasks'] == [2,3] and migrated['rc-people'] == [1]
    assert migrated['rc-content'] == '초안'
    assert drafts.get('kim','task:2')  # 원본을 임의 삭제하지 않는다


def test_unified_search_deduplicates_and_preserves_match_reasons():
    import app  # noqa: F401
    from pages.search import merge_results
    rows = merge_results([{'id':1}], [{'id':2},{'id':1}], [{'id':2,'why':['같은 사람: 김임원']},{'id':3}])
    assert [r['id'] for r in rows] == [1,2,3]
    assert rows[0]['match_reasons'] == ['검색 조건 일치','뜻이 비슷함']
    assert rows[1]['match_reasons'] == ['뜻이 비슷함','연결된 기록','같은 사람: 김임원']
    scored = merge_results([{'id':1}], [{'id':1, 'score':0.82}], [{'id':2, 'score':3.5, 'why':[]}])
    assert scored[0]['similarity'] == 0.82  # 직접 일치로 먼저 들어와도 유사도는 남긴다
    assert 'similarity' not in scored[1]  # 연결 점수는 유사도가 아님


def test_broken_old_draft_does_not_hide_the_others():
    from services import drafts
    drafts.save('kim','task:2',{'h-content':'정상 초안'})
    drafts.save('kim','qr',{'qr-content':'깨질 초안'})
    with db.transaction() as s:
        s.get(db.UserDraft, ('kim','qr')).payload = '{깨진 json'
    assert [r['context'] for r in drafts.legacy_records('kim')] == ['task:2']


def test_common_composer_keeps_context_options_with_values_and_preserves_draft(monkeypatch):
    from types import SimpleNamespace
    import app  # noqa: F401
    from components import record_compose as compose
    task = store.create_task('연결 업무')
    person = store.create_person('김임원')
    ref = store.add_reference(task['id'], 'link', title='선택한 자료', url='https://example.com')
    trigger = dict(type='record-compose', task=task['id'], other=0, person=person['id'], ref=ref['id'], context='test')
    monkeypatch.setattr(compose, 'ctx', SimpleNamespace(triggered=[{'value':1}], triggered_id=trigger))
    values = ['', '메모', None, [], '', False, [], [], None, None, 0, 0, 0]
    out = compose.act([1],None,None,*values)
    assert len(out)==21 and out[1]==[task['id']] and out[2]==[person['id']] and out[3]==[ref['id']]
    assert task['id'] in {option['value'] for option in out[16]}
    assert ref['id'] in {option['value'] for option in out[18]}
    values[0]='이미 작성 중인 내용'
    other = store.create_task('이전 업무')
    values[6] = [other['id']]  # 초안은 이전 업무에서 쓰던 것
    kept = compose.act([1],None,None,*values)
    assert len(kept)==21 and kept[5] is compose.no_update  # 본문은 그대로
    assert kept[1]==[other['id'], task['id']] and kept[2]==[person['id']] and kept[3]==[ref['id']]
    assert '덧붙였습니다' in kept[4]
    values[6] = [other['id'], task['id']]; values[3] = [person['id']]; values[7] = [ref['id']]
    again = compose.act([1],None,None,*values)  # 이미 들어 있는 대상이면 그대로
    assert again[1]==[other['id'], task['id']] and '덧붙였습니다' not in again[4]
    values[3] = []; values[6] = []; values[7] = []
    monkeypatch.setattr(compose, 'ctx', SimpleNamespace(triggered=[{'value':1}], triggered_id='rc-add'))
    values[0]=''
    invalid = compose.act([],None,1,*values)
    assert len(invalid)==21 and invalid[11] is True and invalid[20] is compose.no_update
    values[0]='새 기록';values[6]=[task['id']];values[7]=[ref['id']]
    monkeypatch.setattr(auth,'current_name',lambda:'검토 담당')
    saved = compose.act([],None,1,*values)
    assert len(saved)==21 and saved[0] is False and saved[12:15]==(1,1,1)
    assert saved[20] is True and '저장 완료' in _text(saved[19]) and f"/task/{task['id']}" in _text(saved[19])
    assert store.list_history(task['id'])[0]['content']=='새 기록'
