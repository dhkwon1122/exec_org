"""업무 이력·참고자료 관리 시스템 (exec_org).

실행:  python app.py   → http://localhost:8050
운영:  gunicorn app:server -b 0.0.0.0:8050 --workers 2

모든 화면·데이터·파일은 임원조직 담당자로 로그인해야 열린다(services/auth.py).
"""
import os
import secrets
import time
from datetime import timedelta
from html import escape

import dash
import dash_bootstrap_components as dbc
import flask
from dash import dcc, html
from flask.sessions import SecureCookieSessionInterface

import components.todo_ui  # noqa: F401  할 일 체크/삭제 공용 콜백 등록
from services import auth, store, users
from services.db import DATA_DIR


def _secret_key() -> str:
    """세션 서명 키. FLASK_SECRET_KEY 가 없으면 data/.flask_secret_key 에 한 번 만들어 재사용한다
    (gunicorn 워커가 여럿이어도 같은 키를 쓰도록 파일에 고정, O_EXCL 로 한 워커만 생성)."""
    env = os.environ.get('FLASK_SECRET_KEY', '').strip()
    if env:
        return env
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, '.flask_secret_key')
    for _ in range(50):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            with open(path) as f:
                key = f.read().strip()
            if key:
                return key
            time.sleep(0.1)  # 다른 워커가 막 만들고 아직 쓰는 중
            continue
        key = secrets.token_hex(32)
        with os.fdopen(fd, 'w') as f:
            f.write(key)
        return key
    raise RuntimeError('세션 키 파일을 읽을 수 없습니다: ' + path)


app = dash.Dash(
    __name__,
    use_pages=True,
    external_stylesheets=[dbc.themes.BOOTSTRAP, dbc.icons.BOOTSTRAP],
    suppress_callback_exceptions=True,
    title='업무 기록',
)
server = app.server

# 리버스 프록시(deploy/nginx.conf)가 정확히 N홉 앞에 있을 때만 TRUSTED_PROXY_COUNT=N 으로 켠다 —
# 로그인 시도 제한이 실제 클라이언트 IP 를 구분하게 된다. 프록시 없이 직접 노출된 환경에서 켜면
# 클라이언트가 X-Forwarded-For 를 위조할 수 있으므로 기본은 꺼둔다 (Researcher-board 와 같은 방식).
_TRUSTED_PROXY_COUNT = int(os.environ.get('TRUSTED_PROXY_COUNT', '0'))
if _TRUSTED_PROXY_COUNT > 0:
    from werkzeug.middleware.proxy_fix import ProxyFix
    server.wsgi_app = ProxyFix(server.wsgi_app, x_for=_TRUSTED_PROXY_COUNT, x_proto=_TRUSTED_PROXY_COUNT,
                               x_host=_TRUSTED_PROXY_COUNT)
server.secret_key = _secret_key()


class _RequestAwareSession(SecureCookieSessionInterface):
    """세션 쿠키의 Secure 속성을 실제 접속 방식으로 정한다 — https 면 Secure, http 면 일반 쿠키.
    SESSION_COOKIE_SECURE=true 고정값은 http 로 접속하면 브라우저가 쿠키를 버려 로그인이 아예 안 된다
    (People in SAIT 의 .env 를 복사하면 이 값이 딸려 오기 쉽다). 그래서 설정값 대신 요청을 본다.
    리버스 프록시 뒤라면 TRUSTED_PROXY_COUNT 로 X-Forwarded-Proto 를 반영한다."""

    def get_cookie_secure(self, app):
        return flask.has_request_context() and flask.request.is_secure


server.session_interface = _RequestAwareSession()
server.config.update(
    PERMANENT_SESSION_LIFETIME=timedelta(hours=int(os.environ.get('SESSION_LIFETIME_HOURS', '8'))),
    # 같은 서버의 People in SAIT(8501)도 Flask 기본 쿠키 이름 'session' 을 쓴다. 브라우저 쿠키는 포트를
    # 구분하지 않아 이름이 같으면 서로 덮어써 로그인 토큰이 사라진다("페이지가 만료되었습니다") —
    # 이 앱만의 이름을 쓴다.
    SESSION_COOKIE_NAME=os.environ.get('SESSION_COOKIE_NAME', 'exec_org_session'),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    MAX_CONTENT_LENGTH=int(os.environ.get('MAX_CONTENT_LENGTH', str(80 * 1024 * 1024))),
)

# 세션이 만료된 상태에서 화면을 조작하면(401) 로그인 화면으로 보낸다
app.index_string = app.index_string.replace('</head>', '''<script>
(function(){var f=window.fetch;window.fetch=function(){return f.apply(this,arguments).then(function(r){
if(r.status===401){window.location.href='/login';}return r;});};})();
</script></head>''')

_PUBLIC_PREFIXES = ('/login', '/healthz', '/_dash-component-suites/', '/_favicon.ico', '/assets/')


@server.route('/healthz')
def healthz():
    # 컨테이너 헬스체크용 — 내용 없이 상태만
    return 'ok'


@server.before_request
def _require_login():
    path = flask.request.path
    if path.startswith(_PUBLIC_PREFIXES):
        return None
    if auth.current_user() is None:
        flask.session.pop('user', None)  # 역할이 바뀐 경우 세션도 정리
        if path.startswith('/_dash-') or flask.request.method != 'GET':
            return flask.Response('로그인이 필요합니다.', status=401)
        return flask.redirect('/login')
    return None


@server.after_request
def _no_cache(resp):
    # 민감 정보가 담긴 화면·응답이 브라우저/프록시 캐시에 남지 않도록
    if not flask.request.path.startswith('/_dash-component-suites/'):
        resp.headers['Cache-Control'] = 'no-store'
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['X-Frame-Options'] = 'SAMEORIGIN'
    return resp


_LOGIN_HTML = '''<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>업무 기록 · 로그인</title>
<link rel="stylesheet" href="{css}"></head>
<body class="bg-light"><div class="container" style="max-width:380px;margin-top:12vh">
<div class="card shadow-sm"><div class="card-body p-4">
<h4 class="mb-1">업무 기록</h4><p class="text-muted small mb-4">{hint}</p>
{error}
<form method="post" action="/login" autocomplete="on">
<input type="hidden" name="csrf" value="{csrf}">
<div class="mb-3"><label class="form-label">아이디</label>
<input class="form-control" name="user_id" value="{user_id}" autofocus required></div>
<div class="mb-3"><label class="form-label">비밀번호</label>
<input class="form-control" type="password" name="password" required></div>
<button class="btn btn-primary w-100" type="submit">로그인</button>
</form></div></div></div></body></html>'''


def _login_page(error: str = '', user_id: str = '', status: int = 200):
    csrf = secrets.token_urlsafe(24)
    flask.session['csrf'] = csrf
    hint = ('People in SAIT 계정으로 로그인하세요 (임원조직 담당자 전용).' if users.is_shared()
            else '계정으로 로그인하세요 (임원조직 담당자 전용).')
    body = _LOGIN_HTML.format(
        css=dbc.themes.BOOTSTRAP, hint=hint, csrf=csrf, user_id=escape(user_id),
        error=f'<div class="alert alert-danger py-2 small">{escape(error)}</div>' if error else '')
    return flask.Response(body, status=status, mimetype='text/html')


@server.route('/login', methods=['GET', 'POST'])
def login():
    if flask.request.method == 'GET':
        if auth.current_user():
            return flask.redirect('/')
        return _login_page()
    form = flask.request.form
    expected = flask.session.pop('csrf', None)
    if not expected:
        # 로그인 화면을 열 때 준 세션 쿠키가 돌아오지 않았다
        return _login_page('로그인 화면이 만료되었습니다. 다시 로그인하세요. 계속되면 브라우저 쿠키 차단 설정을 확인하세요.',
                           status=400)
    if not secrets.compare_digest(expected, form.get('csrf', '')):
        return _login_page('페이지가 만료되었습니다. 다시 시도하세요.', status=400)
    user, error = auth.login(form.get('user_id', ''), form.get('password', ''),
                             client=flask.request.remote_addr or '')
    if user is None:
        return _login_page(error, form.get('user_id', ''), status=401)
    return flask.redirect('/')


@server.route('/logout')
def logout():
    auth.logout()
    return flask.redirect('/login')


@server.route('/files/<int:ref_id>')
def download_file(ref_id: int):
    ref = store.get_reference(ref_id)
    path = store.upload_abspath(ref['file_path']) if ref and ref['kind'] == 'file' else None
    if not path:
        flask.abort(404)
    return flask.send_file(path, as_attachment=True, download_name=ref['file_name'] or os.path.basename(path))


def _navbar():
    user = auth.current_user() or {}
    links = [
        dbc.NavItem(dbc.NavLink('업무', href='/', active='exact')),
        dbc.NavItem(dbc.NavLink('할 일', href='/todos', active='exact')),
        dbc.NavItem(dbc.NavLink('캘린더', href='/calendar', active='exact')),
        dbc.NavItem(dbc.NavLink('이력 검색', href='/search', active='exact')),
        dbc.NavItem(dbc.NavLink('참고자료', href='/references', active='exact')),
    ]
    return dbc.Navbar(
        dbc.Container([
            dbc.NavbarBrand([html.I(className='bi bi-journal-text me-2'), '업무 기록'], href='/'),
            dbc.NavbarToggler(id='nav-toggler'),
            dbc.Collapse([
                dbc.Nav(links, navbar=True, className='me-auto'),
                html.Div([
                    html.Span([html.I(className='bi bi-person-circle me-1'), user.get('name', '')],
                              className='text-light small me-3'),
                    html.A('로그아웃', href='/logout', className='btn btn-outline-light btn-sm'),
                ], className='d-flex align-items-center'),
            ], id='nav-collapse', navbar=True, is_open=False),
        ], fluid=True),
        color='dark', dark=True, className='mb-3',
    )


def serve_layout():
    # 요청마다 다시 만들어 로그인한 사용자 이름을 보여준다
    return html.Div([
        dcc.Store(id='todo-changed', data=0),  # 할 일 체크/추가/삭제 시 증가 (components/todo_ui.py)
        _navbar(),
        dbc.Container(dash.page_container, fluid=True, className='pb-5'),
    ])


app.layout = serve_layout


@app.callback(dash.Output('nav-collapse', 'is_open'), dash.Input('nav-toggler', 'n_clicks'),
              dash.State('nav-collapse', 'is_open'), prevent_initial_call=True)
def _toggle_nav(_, is_open):
    return not is_open


if __name__ == '__main__':
    app.run(debug=os.environ.get('DASH_DEBUG', '1') == '1', host='0.0.0.0', port=int(os.environ.get('PORT', 8050)))
