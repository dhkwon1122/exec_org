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
