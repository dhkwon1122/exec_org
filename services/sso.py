"""AD SSO (ADFS, OpenID Connect 와 비슷한 흐름) 로그인.

1. /auth/sso → ADFS 로그인 주소(https://{SSO_HOST}/adfs/oauth2/authorize/)로 302
   (client_id, redirect_uri, response_mode=form_post, response_type=code id_token, scope=openid profile, nonce)
2. 로그인에 성공하면 ADFS 가 redirect_uri(/auth/sso/callback)로 id_token 을 POST 폼으로 보낸다
3. id_token 검증 — RS256 서명(SSO_CERT_FILE 인증서 또는 JWKS 엔드포인트의 공개키), exp 만료, aud(client_id),
   iss(SSO_ISSUER 를 적었을 때), nonce(1번에서 만든 값)
4. 클레임에서 로그인 ID·이름·부서명을 꺼낸다 (클레임 이름은 SSO_CLAIM_* 로 바꿀 수 있다)
5. 로그인 ID 로 사용자를 찾고(없으면 단독 실행 모드에서만 만든다) 기존 로그인 세션 생성(auth.start_session)을 그대로 쓴다

nonce 는 서명한 전용 쿠키에 담아 둔다. 콜백은 ADFS 에서 이 서버로 보내는 다른 사이트 POST 라
SameSite=Lax 인 세션 쿠키가 따라오지 않을 수 있기 때문이다 (https 면 SameSite=None; Secure).
"""
import hmac
import os
import threading
import time
import uuid
from urllib.parse import urlencode

import flask
import jwt
import requests
from cryptography import x509
from cryptography.hazmat.primitives.serialization import load_der_public_key, load_pem_public_key
from itsdangerous import BadSignature, URLSafeTimedSerializer

CALLBACK_PATH = '/auth/sso/callback'
_NONCE_MAX_AGE = 600  # ADFS 로그인 화면에 머무를 수 있는 시간(초)
_JWKS_TTL, _JWKS_MIN_REFETCH = 3600, 60

_jwks_lock = threading.Lock()
_jwks: dict = {'keys': {}, 'fetched': 0.0}
_cert_keys: dict = {}  # 경로 → (mtime, [공개키])


class SSOError(Exception):
    """SSO 로그인 실패. code 는 app.py 의 _LOGIN_ERRORS 키."""

    def __init__(self, code: str, detail: str = ''):
        super().__init__(detail or code)
        self.code = code


def _env(name: str, default: str = '') -> str:
    return (os.environ.get(name) or default).strip()


def enabled() -> bool:
    return bool(_env('SSO_HOST') and _env('SSO_CLIENT_ID'))


def password_login_allowed() -> bool:
    """SSO 를 켜면 아이디·비밀번호 로그인은 기본으로 끈다 (SSO_PASSWORD_LOGIN=1 이면 같이 쓴다)."""
    return not enabled() or _env('SSO_PASSWORD_LOGIN') == '1'


def redirect_uri() -> str:
    """ADFS 에 등록한 리디렉션 주소. 프록시 뒤에서는 SSO_REDIRECT_URI 로 정확히 적는 것이 안전하다."""
    return _env('SSO_REDIRECT_URI') or flask.request.url_root.rstrip('/') + CALLBACK_PATH


def _nonce_cookie_name() -> str:
    return flask.current_app.config.get('SESSION_COOKIE_NAME', 'session') + '_sso'


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(flask.current_app.secret_key, salt='sso-nonce')


def authorize_response() -> flask.Response:
    """1단계: nonce 를 쿠키에 남기고 ADFS 로그인 주소로 보낸다."""
    nonce = str(uuid.uuid4())
    params = {
        'client_id': _env('SSO_CLIENT_ID'),
        'redirect_uri': redirect_uri(),
        'response_mode': 'form_post',
        'response_type': 'code id_token',
        'scope': _env('SSO_SCOPE', 'openid profile'),
        'nonce': nonce,
    }
    if _env('SSO_RESOURCE'):  # ADFS 2016 에서 리소스 식별자를 따로 요구할 때
        params['resource'] = _env('SSO_RESOURCE')
    url = f"https://{_env('SSO_HOST')}/adfs/oauth2/authorize/?{urlencode(params)}"
    resp = flask.redirect(url)
    secure = flask.request.is_secure
    resp.set_cookie(_nonce_cookie_name(), _serializer().dumps(nonce), max_age=_NONCE_MAX_AGE, path='/auth/sso',
                    httponly=True, secure=secure, samesite='None' if secure else 'Lax')
    return resp


def clear_nonce(resp: flask.Response) -> flask.Response:
    resp.delete_cookie(_nonce_cookie_name(), path='/auth/sso')
    return resp


def _expected_nonce() -> str:
    raw = flask.request.cookies.get(_nonce_cookie_name(), '')
    if not raw:
        raise SSOError('sso_nonce', 'nonce 쿠키 없음 (만료 또는 브라우저가 쿠키를 보내지 않음)')
    try:
        return _serializer().loads(raw, max_age=_NONCE_MAX_AGE)
    except BadSignature as e:  # SignatureExpired 도 여기에 포함
        raise SSOError('sso_nonce', f'nonce 쿠키 검증 실패: {type(e).__name__}') from e


# ── 서명 공개키 ──

def _load_cert_file(path: str) -> list:
    """PEM(인증서·공개키, 여러 개 가능) 또는 DER(.cer) 파일의 공개키들."""
    mtime = os.path.getmtime(path)
    cached = _cert_keys.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    with open(path, 'rb') as f:
        data = f.read()
    keys = []
    if b'-----BEGIN' in data:
        if b'CERTIFICATE-----' in data:
            keys = [c.public_key() for c in x509.load_pem_x509_certificates(data)]
        else:
            keys = [load_pem_public_key(data)]
    else:
        try:
            keys = [x509.load_der_x509_certificate(data).public_key()]
        except ValueError:
            keys = [load_der_public_key(data)]
    _cert_keys[path] = (mtime, keys)
    return keys


def _jwks_url() -> str:
    return _env('SSO_JWKS_URL') or f"https://{_env('SSO_HOST')}/adfs/discovery/keys"


def _fetch_jwks() -> dict:
    verify = _env('SSO_CA_BUNDLE') or True
    r = requests.get(_jwks_url(), timeout=10, verify=verify)
    r.raise_for_status()
    keys = {}
    for k in r.json().get('keys', []):
        if k.get('kty') != 'RSA' or k.get('use', 'sig') != 'sig':
            continue
        key = jwt.PyJWK.from_dict(k, algorithm='RS256').key
        for kid in {k.get('kid'), k.get('x5t')} - {None}:
            keys[kid] = key
        keys.setdefault('', key)  # kid 없는 토큰용 — 첫 키
    return keys


def _jwks_keys(kid: str) -> list:
    """kid 에 맞는 JWKS 공개키. 모르는 kid 면(ADFS 인증서 교체) 한 번 다시 받아 온다."""
    with _jwks_lock:
        now = time.time()
        stale = now - _jwks['fetched'] > _JWKS_TTL
        unknown = kid not in _jwks['keys'] and now - _jwks['fetched'] > _JWKS_MIN_REFETCH
        if stale or unknown:
            try:
                _jwks['keys'] = _fetch_jwks()
                _jwks['fetched'] = now
            except Exception as e:  # noqa: BLE001
                if not _jwks['keys']:
                    raise SSOError('sso_keys', f'JWKS 조회 실패({_jwks_url()}): {type(e).__name__}: {e}') from e
                print(f'[sso] JWKS 다시 받기 실패 — 이전 키로 검증: {type(e).__name__}', flush=True)
        key = _jwks['keys'].get(kid)
        return [key] if key else []


def _signing_keys(header: dict) -> list:
    path = _env('SSO_CERT_FILE')
    if path:
        try:
            return _load_cert_file(path)
        except (OSError, ValueError) as e:
            raise SSOError('sso_keys', f'SSO_CERT_FILE 을 읽을 수 없음({path}): {type(e).__name__}') from e
    return _jwks_keys(header.get('kid') or header.get('x5t') or '')


# ── 토큰 검증 ──

def verify_id_token(token: str, nonce: str) -> dict:
    """3단계: 서명·만료·대상·발급자·nonce 를 확인한 클레임."""
    try:
        header = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError as e:
        raise SSOError('sso_failed', f'id_token 형식 오류: {e}') from e
    if header.get('alg') != 'RS256':
        raise SSOError('sso_failed', f"허용하지 않는 서명 방식: {header.get('alg')!r}")
    keys = _signing_keys(header)
    if not keys:
        raise SSOError('sso_keys', f"서명 키(kid={header.get('kid')!r})를 찾을 수 없음")
    issuer = _env('SSO_ISSUER') or None
    claims = None
    for key in keys:
        try:
            claims = jwt.decode(
                token, key, algorithms=['RS256'], audience=_env('SSO_CLIENT_ID'), issuer=issuer,
                leeway=int(_env('SSO_CLOCK_SKEW', '60')),
                options={'require': ['exp', 'aud'] + (['iss'] if issuer else [])})
            break
        except jwt.InvalidSignatureError:
            continue  # 인증서 파일에 여러 개가 있으면 다음 키
        except jwt.ExpiredSignatureError as e:
            raise SSOError('sso_expired', str(e)) from e
        except jwt.InvalidTokenError as e:
            raise SSOError('sso_failed', f'{type(e).__name__}: {e}') from e
    if claims is None:
        raise SSOError('sso_failed', '서명 검증 실패')
    if not hmac.compare_digest(str(claims.get('nonce', '')), nonce):
        raise SSOError('sso_nonce', 'nonce 불일치')
    return claims


def extract_identity(claims: dict) -> dict:
    """4단계: 로그인 ID·이름·부서명. 로그인 ID 가 없으면 실패."""
    def claim(env_name: str, default: str) -> str:
        v = claims.get(_env(env_name, default))
        if isinstance(v, list):
            v = v[0] if v else ''
        return str(v or '').strip()

    login_id = claim('SSO_CLAIM_LOGIN_ID', 'loginid')
    if not login_id:
        raise SSOError('sso_claims', f"로그인 ID 클레임({_env('SSO_CLAIM_LOGIN_ID', 'loginid')!r}) 없음 — "
                                     f'받은 클레임: {sorted(claims)}')
    if '\\' in login_id:  # DOMAIN\user 형태면 user 만
        login_id = login_id.rsplit('\\', 1)[1]
    return {'login_id': login_id, 'name': claim('SSO_CLAIM_USERNAME', 'username'),
            'dept': claim('SSO_CLAIM_DEPTNAME', 'deptname')}


def handle_callback() -> dict:
    """2~4단계: 콜백 POST 폼 → 검증된 사용자 신원."""
    form = flask.request.form
    if form.get('error'):
        raise SSOError('sso_failed', f"ADFS 오류: {form.get('error')} {form.get('error_description', '')}".strip())
    token = form.get('id_token', '')
    if not token:
        raise SSOError('sso_failed', 'id_token 이 없음')
    return extract_identity(verify_id_token(token, _expected_nonce()))
