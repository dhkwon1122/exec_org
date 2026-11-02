"""AD SSO(ADFS) 로그인 — 실제 ADFS 대신 테스트용 RSA 키로 id_token 을 서명한다."""
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from sqlalchemy import create_engine, text
from werkzeug.security import generate_password_hash

from services import db, sso, users

CLIENT_ID = 'exec-org-client'
ISSUER = 'https://sso.example.com/adfs'


def _key_and_cert():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'ADFS Signing')])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=30))
            .sign(key, hashes.SHA256()))
    return key, cert


KEY, CERT = _key_and_cert()
OTHER_KEY, _ = _key_and_cert()


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'UPLOAD_DIR', str(tmp_path / 'uploads'))
    for name in ('USER_DATABASE_URL', 'DATABASE_URL', 'EXEC_ORG_ROLES', 'SSO_PASSWORD_LOGIN', 'SSO_JWKS_URL',
                 'SSO_NEW_USER_ROLE', 'SSO_REDIRECT_URI', 'SSO_CLAIM_LOGIN_ID'):
        monkeypatch.delenv(name, raising=False)
    cert_path = tmp_path / 'adfs-signing.crt'
    cert_path.write_bytes(CERT.public_bytes(serialization.Encoding.PEM))
    monkeypatch.setenv('SSO_HOST', 'sso.example.com')
    monkeypatch.setenv('SSO_CLIENT_ID', CLIENT_ID)
    monkeypatch.setenv('SSO_ISSUER', ISSUER)
    monkeypatch.setenv('SSO_CERT_FILE', str(cert_path))
    db.configure('sqlite:///' + str(tmp_path / 'test.db'))
    users.reset()
    sso._jwks.update(keys={}, fetched=0.0)
    yield
    users.reset()


@pytest.fixture
def client():
    import app as app_module
    app_module.server.config['TESTING'] = True
    return app_module.server.test_client()


def _start(client) -> str:
    """/auth/sso → ADFS 로 가는 주소를 확인하고 nonce 를 돌려준다."""
    r = client.get('/auth/sso')
    assert r.status_code == 302
    url = urlparse(r.headers['Location'])
    assert (url.scheme, url.netloc, url.path) == ('https', 'sso.example.com', '/adfs/oauth2/authorize/')
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert q['client_id'] == CLIENT_ID and q['redirect_uri'] == 'http://localhost/auth/sso/callback'
    assert q['response_mode'] == 'form_post' and q['response_type'] == 'code id_token'
    assert q['scope'] == 'openid profile' and len(q['nonce']) == 36
    return q['nonce']


def _token(nonce, key=KEY, exp_in=300, **claims):
    now = int(time.time())
    payload = {'iss': ISSUER, 'aud': CLIENT_ID, 'iat': now, 'exp': now + exp_in, 'nonce': nonce,
               'loginid': 'kim', 'username': '김철수', 'deptname': '임원조직팀', **claims}
    return jwt.encode(payload, key, algorithm='RS256', headers={'kid': 'k1'})


def _callback(client, token):
    r = client.post('/auth/sso/callback', data={'id_token': token, 'code': 'ignored'}, follow_redirects=True)
    return r, r.request.path + ('?' + r.request.query_string.decode() if r.request.query_string else '')


def test_sso_login_creates_existing_session(client):
    users.create_local_user('kim', 'password123', '김철수', role='executive_org')
    assert 'AD 계정으로 로그인' in client.get('/login').get_data(as_text=True)
    nonce = _start(client)
    r, path = _callback(client, _token(nonce, loginid='KIM'))  # AD 는 대소문자를 가리지 않는다
    assert path == '/' and r.status_code == 200
    with client.session_transaction() as s:
        assert s['user'] == {'user_id': 'kim', 'name': '김철수', 'dept': '임원조직팀', 'via': 'sso'}
    assert client.get('/_dash-layout').status_code == 200
    # nonce 는 한 번만 쓴다 — 같은 토큰을 다시 보내도 안 된다
    client.get('/logout')
    _, path = _callback(client, _token(nonce))
    assert 'error=sso_nonce' in path and client.get('/').status_code == 302


@pytest.mark.parametrize('kwargs, code', [
    ({'exp_in': -600}, 'sso_expired'),
    ({'key': OTHER_KEY}, 'sso_failed'),  # 다른 키로 서명
    ({'aud': 'someone-else'}, 'sso_failed'),
    ({'iss': 'https://evil.example/adfs'}, 'sso_failed'),
    ({'nonce': 'other-nonce'}, 'sso_nonce'),
    ({'loginid': ''}, 'sso_claims'),
])
def test_invalid_tokens_rejected(client, kwargs, code):
    users.create_local_user('kim', 'password123', '김철수', role='executive_org')
    nonce = _start(client)
    key = kwargs.pop('key', KEY)
    exp_in = kwargs.pop('exp_in', 300)
    _, path = _callback(client, _token(kwargs.pop('nonce', nonce), key=key, exp_in=exp_in, **kwargs))
    assert f'error={code}' in path
    assert client.get('/').status_code == 302


def test_unsigned_or_missing_token_and_no_nonce_cookie(client):
    nonce = _start(client)
    none_token = jwt.encode({'aud': CLIENT_ID, 'exp': int(time.time()) + 60, 'nonce': nonce, 'loginid': 'kim'},
                            None, algorithm='none')
    assert 'error=sso_failed' in _callback(client, none_token)[1]
    assert 'error=sso_failed' in _callback(client, '')[1]
    r = client.post('/auth/sso/callback', data={'error': 'access_denied'}, follow_redirects=True)
    assert 'error=sso_failed' in r.request.query_string.decode()
    client.delete_cookie('exec_org_session_sso', path='/auth/sso')
    assert 'error=sso_nonce' in _callback(client, _token(nonce))[1]


def test_role_still_required(client):
    users.create_local_user('park', 'password123', '박영수', role='recruit')
    _, path = _callback(client, _token(_start(client), loginid='park'))
    assert 'error=role' in path


def test_local_mode_creates_user_without_access(client, monkeypatch):
    _, path = _callback(client, _token(_start(client), loginid='new1', username='신입'))
    assert 'error=role' in path  # 만들되 역할이 없어 못 들어온다
    assert users.get_user('new1')['name'] == '신입'
    assert users.verify('new1', '') is None
    monkeypatch.setenv('SSO_NEW_USER_ROLE', 'executive_org')
    _, path = _callback(client, _token(_start(client), loginid='new2'))
    assert path == '/'


def test_shared_user_db_is_never_written(tmp_path, monkeypatch, client):
    path = tmp_path / 'people.db'
    eng = create_engine(f'sqlite:///{path}')
    with eng.begin() as c:
        c.execute(text('CREATE TABLE app_users (user_id VARCHAR PRIMARY KEY, password_hash VARCHAR NOT NULL,'
                       ' display_name VARCHAR NOT NULL, role VARCHAR NOT NULL, email VARCHAR,'
                       ' must_change_password BOOLEAN NOT NULL DEFAULT 0, is_admin BOOLEAN NOT NULL DEFAULT 0,'
                       ' created_at DATETIME NOT NULL)'))
        c.execute(text("INSERT INTO app_users VALUES ('lee', :h, '이영희', 'executive_org', NULL, 0, 0,"
                       " '2026-01-01')"), {'h': generate_password_hash('secret-pass')})
    monkeypatch.setenv('DATABASE_URL', f'sqlite:///{path}')
    users.reset()
    _, p = _callback(client, _token(_start(client), loginid='nobody'))
    assert 'error=sso_unknown' in p
    _, p = _callback(client, _token(_start(client), loginid='lee'))
    assert p == '/'
    with eng.begin() as c:
        assert c.execute(text('SELECT COUNT(*) FROM app_users')).scalar() == 1


def test_password_login_disabled_unless_allowed(client, monkeypatch):
    users.create_local_user('kim', 'password123', '김철수', role='executive_org')
    assert 'name="password"' not in client.get('/login').get_data(as_text=True)
    r = client.post('/login', data={'user_id': 'kim', 'password': 'password123'}, follow_redirects=True)
    assert 'error=sso_only' in r.request.query_string.decode()
    monkeypatch.setenv('SSO_PASSWORD_LOGIN', '1')
    assert 'name="password"' in client.get('/login').get_data(as_text=True)
    r = client.post('/login', data={'user_id': 'kim', 'password': 'password123'}, follow_redirects=True)
    assert r.request.path == '/'


def test_jwks_endpoint_and_https_cookie(client, monkeypatch):
    monkeypatch.delenv('SSO_CERT_FILE')
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key(), as_dict=True) | {'kid': 'k1', 'use': 'sig'}
    calls = []

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {'keys': [jwk]}

    monkeypatch.setattr(sso.requests, 'get', lambda url, **kw: calls.append(url) or _Resp())
    users.create_local_user('kim', 'password123', '김철수', role='executive_org')
    r = client.get('/auth/sso', base_url='https://localhost')
    assert 'SameSite=None' in r.headers['Set-Cookie'] and 'Secure' in r.headers['Set-Cookie']
    nonce = parse_qs(urlparse(r.headers['Location']).query)['nonce'][0]
    r = client.post('/auth/sso/callback', data={'id_token': _token(nonce)}, base_url='https://localhost')
    assert r.headers['Location'].endswith('/login/check')
    assert calls == ['https://sso.example.com/adfs/discovery/keys']


def test_sso_disabled_keeps_password_login(client, monkeypatch):
    monkeypatch.delenv('SSO_HOST')
    users.create_local_user('kim', 'password123', '김철수', role='executive_org')
    assert client.get('/auth/sso').headers['Location'].endswith('/login')
    page = client.get('/login').get_data(as_text=True)
    assert 'AD 계정' not in page and 'name="password"' in page
