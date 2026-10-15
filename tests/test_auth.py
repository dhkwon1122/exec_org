import re

import pytest
from sqlalchemy import create_engine, text
from werkzeug.security import generate_password_hash

from services import auth, db, users


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'UPLOAD_DIR', str(tmp_path / 'uploads'))
    monkeypatch.delenv('USER_DATABASE_URL', raising=False)
    monkeypatch.delenv('EXEC_ORG_ROLES', raising=False)
    db.configure('sqlite:///' + str(tmp_path / 'test.db'))
    users.reset()
    auth._failures.clear()
    yield
    users.reset()


@pytest.fixture
def client():
    import app as app_module
    app_module.server.config['TESTING'] = True
    return app_module.server.test_client()


def _login(client, user_id, password):
    page = client.get('/login').get_data(as_text=True)
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    return client.post('/login', data={'user_id': user_id, 'password': password, 'csrf': csrf})


def test_everything_requires_login(client):
    assert client.get('/').status_code == 302
    assert client.get('/search').headers['Location'].endswith('/login')
    assert client.get('/files/1').status_code == 302
    assert client.post('/_dash-update-component', json={}).status_code == 401
    assert client.get('/_dash-layout').status_code == 401
    assert client.get('/login').status_code == 200


def test_only_executive_org_role_can_log_in(client, monkeypatch):
    users.create_local_user('kim', 'password123', '김철수', role='executive_org')
    users.create_local_user('park', 'password123', '박영수', role='recruit')
    r = _login(client, 'park', 'password123')
    assert r.status_code == 401 and '임원조직 담당자만' in r.get_data(as_text=True)

    assert _login(client, 'kim', 'wrong-password').status_code == 401
    r = _login(client, 'kim', 'password123')
    assert r.status_code == 302 and r.headers['Location'] == '/'
    page = client.get('/')
    assert page.status_code == 200 and page.headers['Cache-Control'] == 'no-store'
    assert client.get('/_dash-layout').status_code == 200
    assert [m['user_id'] for m in auth.list_members()] == ['kim']

    # 사용자 DB 에서 역할이 바뀌면 이미 로그인한 세션도 바로 막힌다
    with db.engine.begin() as c:
        c.execute(text("UPDATE app_users SET role = 'recruit' WHERE user_id = 'kim'"))
    assert client.get('/').status_code == 302

    # 허용 역할은 환경변수로 바꿀 수 있다
    monkeypatch.setenv('EXEC_ORG_ROLES', 'executive_org, recruit')
    assert _login(client, 'park', 'password123').status_code == 302


def test_login_survives_people_in_sait_cookie_on_same_host(client):
    """같은 서버(포트만 다름)의 People in SAIT 가 'session' 쿠키를 써도 로그인 토큰이 덮이지 않는다."""
    users.create_local_user('kim', 'password123', '김철수', role='executive_org')
    page = client.get('/login').get_data(as_text=True)
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    client.set_cookie('session', 'people-in-sait-session-value')  # 다른 탭에서 People in SAIT 사용
    r = client.post('/login', data={'user_id': 'kim', 'password': 'password123', 'csrf': csrf})
    assert r.status_code == 302
    assert client.get_cookie('session').value == 'people-in-sait-session-value'  # 그쪽 로그인도 그대로
    assert client.get_cookie('exec_org_session') is not None


def test_csrf_and_logout(client):
    users.create_local_user('kim', 'password123', '김철수', role='executive_org')
    client.get('/login')
    r = client.post('/login', data={'user_id': 'kim', 'password': 'password123', 'csrf': 'forged'})
    assert r.status_code == 400
    _login(client, 'kim', 'password123')
    client.get('/logout')
    assert client.get('/').status_code == 302


def test_throttle_locks_after_repeated_failures(client):
    users.create_local_user('kim', 'password123', '김철수', role='executive_org')
    for _ in range(5):
        _login(client, 'kim', 'nope-nope')
    r = _login(client, 'kim', 'password123')
    assert r.status_code == 401 and '잠시 막혔습니다' in r.get_data(as_text=True)


def test_shared_user_db_is_read_only(tmp_path, monkeypatch, client):
    """People in SAIT 의 app_users(추가 컬럼 포함)를 그대로 읽고, 절대 쓰지 않는다."""
    path = tmp_path / 'people.db'
    eng = create_engine(f'sqlite:///{path}')
    with eng.begin() as c:
        c.execute(text('CREATE TABLE app_users (user_id VARCHAR PRIMARY KEY, password_hash VARCHAR NOT NULL,'
                       ' display_name VARCHAR NOT NULL, role VARCHAR NOT NULL, email VARCHAR,'
                       ' must_change_password BOOLEAN NOT NULL DEFAULT 0, is_admin BOOLEAN NOT NULL DEFAULT 0,'
                       ' perm_view_evaluation BOOLEAN, created_at DATETIME NOT NULL)'))
        c.execute(text("INSERT INTO app_users VALUES ('lee', :h, '이영희', 'executive_org', 'lee@x', 0, 0, NULL,"
                       " '2026-01-01'), ('temp', :h, '임시', 'executive_org', NULL, 1, 0, NULL, '2026-01-01')"),
                  {'h': generate_password_hash('secret-pass')})
    monkeypatch.setenv('USER_DATABASE_URL', f'sqlite:///{path}')
    users.reset()
    assert users.is_shared()
    assert users.get_user('lee')['name'] == '이영희'
    with pytest.raises(users.UserDBError):
        users.create_local_user('x', 'password123', 'x')

    assert _login(client, 'lee', 'secret-pass').status_code == 302
    client.get('/logout')
    r = _login(client, 'temp', 'secret-pass')
    assert r.status_code == 401 and '임시 비밀번호' in r.get_data(as_text=True)
    with eng.begin() as c:
        assert c.execute(text('SELECT COUNT(*) FROM app_users')).scalar() == 2
