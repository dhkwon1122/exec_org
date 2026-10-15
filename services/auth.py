"""로그인 세션과 접근 권한.

민감한 업무 기록이 있으므로 사용자 DB(app_users)의 역할(role)이 **임원조직 담당자
(executive_org)** 인 계정만 들어올 수 있다. 허용할 역할은 EXEC_ORG_ROLES 환경변수(쉼표
구분)로 바꿀 수 있다.

역할은 매 요청마다 사용자 DB 에서 다시 확인하므로, People in SAIT 에서 역할이 바뀌면
이미 로그인해 있던 사람도 다음 클릭부터 바로 막힌다.
"""
import os
import time

import flask

from services import users

# ── 로그인 시도 제한 (워커 프로세스별 메모리) ──
_FAIL_LIMIT, _FAIL_WINDOW, _LOCK_SECONDS = 5, 600, 600
_failures: dict[str, list[float]] = {}


def allowed_roles() -> set[str]:
    return {r.strip() for r in os.environ.get('EXEC_ORG_ROLES', 'executive_org').split(',') if r.strip()}


def is_allowed(user: dict | None) -> bool:
    return bool(user) and user['role'] in allowed_roles()


def list_members() -> list[dict]:
    """접속 가능한 사용자(허용 역할) 목록 — 할 일 담당자 선택 등에 쓴다."""
    try:
        return [u for u in users.list_users() if is_allowed(u)]
    except users.UserDBError:
        return []


# ── 로그인 ──

def _locked(key: str) -> bool:
    now = time.time()
    recent = [t for t in _failures.get(key, []) if now - t < max(_FAIL_WINDOW, _LOCK_SECONDS)]
    _failures[key] = recent
    return len(recent) >= _FAIL_LIMIT and now - recent[-1] < _LOCK_SECONDS


def login(user_id: str, password: str, client: str = '') -> tuple[dict | None, str]:
    """성공하면 (사용자, ''), 실패하면 (None, 안내 문구). 세션에 저장까지 한다."""
    user_id = (user_id or '').strip()
    key = f'{client}|{user_id.lower()}'
    if _locked(key):
        return None, '로그인 실패가 반복되어 잠시 막혔습니다. 10분 뒤 다시 시도하세요.'
    try:
        user = users.verify(user_id, password)
    except users.UserDBError as e:
        return None, str(e)
    if user is None:
        _failures.setdefault(key, []).append(time.time())
        return None, '아이디 또는 비밀번호가 올바르지 않습니다.'
    _failures.pop(key, None)
    if user['must_change_password']:
        return None, '임시 비밀번호 상태입니다. People in SAIT 에서 비밀번호를 먼저 바꾼 뒤 로그인하세요.'
    if not is_allowed(user):
        return None, '임원조직 담당자만 사용할 수 있는 시스템입니다.'
    flask.session.clear()
    flask.session.permanent = True
    flask.session['user'] = {'user_id': user['user_id'], 'name': user['name']}
    return user, ''


def logout() -> None:
    flask.session.clear()


def current_user() -> dict | None:
    """로그인했고 지금도 허용 역할인 사용자. 요청 안에서만 호출."""
    if not flask.has_request_context():
        return None
    u = flask.session.get('user')
    if not u:
        return None
    if 'db_user' not in flask.g:  # 요청당 한 번만 사용자 DB 조회
        try:
            flask.g.db_user = users.get_user(u['user_id'])
        except users.UserDBError:
            flask.g.db_user = None
    if not is_allowed(flask.g.db_user):
        return None
    return {**u, 'name': flask.g.db_user['name']}


def current_name() -> str:
    u = current_user()
    return u['name'] if u else ''
