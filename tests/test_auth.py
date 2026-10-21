import pytest
from sqlalchemy import create_engine, text
from werkzeug.security import generate_password_hash

from services import auth, db, users


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'UPLOAD_DIR', str(tmp_path / 'uploads'))
    monkeypatch.delenv('USER_DATABASE_URL', raising=False)
    monkeypatch.delenv('DATABASE_URL', raising=False)
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


def _login(client, user_id, password, **headers):
    """로그인 POST 후 리다이렉트를 끝까지 따라간 최종 응답과 마지막 경로."""
    r = client.post('/login', data={'user_id': user_id, 'password': password}, headers=headers,
                    follow_redirects=True)
    return r, r.request.path + ('?' + r.request.query_string.decode() if r.request.query_string else '')


def _ok(result):
    r, path = result
    return path == '/' and r.status_code == 200


def test_everything_requires_login(client):
    assert client.get('/').status_code == 302
    assert client.get('/search').headers['Location'].endswith('/login')
    for path in ('/bookmarks', '/trash', '/history/1'):
        assert client.get(path).headers['Location'].endswith('/login')
    assert client.get('/files/1').status_code == 302
    assert client.post('/_dash-update-component', json={}).status_code == 401
    assert client.get('/_dash-layout').status_code == 401
    assert client.get('/login').status_code == 200


def test_only_executive_org_role_can_log_in(client, monkeypatch):
    users.create_local_user('kim', 'password123', '김철수', role='executive_org')
    users.create_local_user('park', 'password123', '박영수', role='recruit')
    r, path = _login(client, 'park', 'password123')
    assert 'error=role' in path and '임원조직 담당자만' in r.get_data(as_text=True)

    r, path = _login(client, 'kim', 'wrong-password')
    assert 'error=invalid' in path and '올바르지 않습니다' in r.get_data(as_text=True)
    assert _ok(_login(client, 'kim', 'password123'))
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
    assert _ok(_login(client, 'park', 'password123'))


def test_login_survives_people_in_sait_cookie_on_same_host(client):
    """같은 서버(포트만 다름)의 People in SAIT 가 'session' 쿠키를 써도 로그인 토큰이 덮이지 않는다."""
    users.create_local_user('kim', 'password123', '김철수', role='executive_org')
    client.get('/login')
    client.set_cookie('session', 'people-in-sait-session-value')  # 다른 탭에서 People in SAIT 사용
    assert _ok(_login(client, 'kim', 'password123'))
    assert client.get_cookie('session').value == 'people-in-sait-session-value'  # 그쪽 로그인도 그대로
    assert client.get_cookie('exec_org_session') is not None


def test_session_cookie_secure_follows_request_scheme(client, monkeypatch):
    """http 로 접속하면 Secure 없이(로그인 가능), https 면 Secure. .env 의 SESSION_COOKIE_SECURE 와 무관."""
    monkeypatch.setenv('SESSION_COOKIE_SECURE', 'true')
    users.create_local_user('kim', 'password123', '김철수', role='executive_org')
    assert 'Set-Cookie' not in client.get('/login').headers  # 로그인 전에는 쿠키를 쓰지 않는다
    r = client.post('/login', data={'user_id': 'kim', 'password': 'password123'})
    assert 'exec_org_session=' in r.headers['Set-Cookie'] and 'Secure' not in r.headers['Set-Cookie']
    r = client.post('/login', data={'user_id': 'kim', 'password': 'password123'}, base_url='https://localhost')
    assert 'Secure' in r.headers['Set-Cookie']


def test_cross_site_login_rejected_and_logout(client):
    users.create_local_user('kim', 'password123', '김철수', role='executive_org')
    r, path = _login(client, 'kim', 'password123', Origin='http://evil.example')
    assert 'error=origin' in path and client.get('/').status_code == 302
    # 같은 주소에서 보낸 폼(Origin/Referer 일치)과 헤더 없는 요청은 통과
    assert _ok(_login(client, 'kim', 'password123', Origin='http://localhost'))
    client.get('/logout')
    assert _ok(_login(client, 'kim', 'password123', Referer='http://localhost/login'))
    client.get('/logout')
    _login(client, 'kim', 'password123')
    client.get('/logout')
    assert client.get('/').status_code == 302


def test_throttle_locks_after_repeated_failures(client):
    users.create_local_user('kim', 'password123', '김철수', role='executive_org')
    for _ in range(5):
        _login(client, 'kim', 'nope-nope')
    r, path = _login(client, 'kim', 'password123')
    assert 'error=locked' in path and '잠시 막혔습니다' in r.get_data(as_text=True)


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
    monkeypatch.setenv('DATABASE_URL', f'sqlite:///{path}')  # tea-meeting-agenda 와 같은 변수명
    users.reset()
    assert users.is_shared()
    assert users.get_user('lee')['name'] == '이영희'
    with pytest.raises(users.UserDBError):
        users.create_local_user('x', 'password123', 'x')

    assert _ok(_login(client, 'lee', 'secret-pass'))
    client.get('/logout')
    r, path = _login(client, 'temp', 'secret-pass')
    assert '임시 비밀번호' in r.get_data(as_text=True)
    with eng.begin() as c:
        assert c.execute(text('SELECT COUNT(*) FROM app_users')).scalar() == 2


def test_cookie_not_stored_is_reported(client):
    """로그인은 성공했는데 브라우저가 쿠키를 버린 경우 원인을 알려준다."""
    users.create_local_user('kim', 'password123', '김철수', role='executive_org')
    r = client.post('/login', data={'user_id': 'kim', 'password': 'password123'})
    assert r.headers['Location'] == '/login/check'
    client.delete_cookie('exec_org_session')  # 브라우저가 쿠키를 저장하지 않은 상황
    r = client.get('/login/check', follow_redirects=True)
    assert '쿠키를 저장하지 않았습니다' in r.get_data(as_text=True)


def test_auth_check_script(capsys):
    import importlib.util, os
    spec = importlib.util.spec_from_file_location('auth_check', os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts', 'auth_check.py'))
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    users.create_local_user('kim', 'password123', '김철수', role='recruit')
    assert mod.main(['kim', 'password123']) == 1
    assert "역할 'recruit'" in capsys.readouterr().out
    assert mod.main(['kim', 'nope']) == 1
    assert '비밀번호가 일치하지 않습니다' in capsys.readouterr().out
