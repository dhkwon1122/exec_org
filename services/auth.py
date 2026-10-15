"""로그인 세션과 접근 권한.

계정은 사용자 DB(services/users.py)에 있지만, 그 계정이 있다고 이 시스템을 쓸 수
있는 것은 아니다. 민감한 업무 기록이 있으므로 **exec_members 에 등록된 사람만** 들어올 수
있다 (허용 목록 방식).

  admin  : 업무 기록 전체 + 사용자 접근 권한 관리
  member : 업무 기록 전체

EXEC_ORG_ADMINS 환경변수(쉼표 구분 user_id)에 적힌 사람은 항상 admin 이다 — 처음
설치할 때 첫 관리자를 정하는 용도. 권한은 매 요청마다 다시 확인하므로, 접근을 회수하면
이미 로그인해 있던 사람도 다음 클릭부터 바로 막힌다.
"""
import os
import time

import flask
from sqlalchemy import select

from services import db, users
from services.db import ExecMember

ROLES = {'admin': '관리자', 'member': '사용자'}

# ── 로그인 시도 제한 (워커 프로세스별 메모리) ──
_FAIL_LIMIT, _FAIL_WINDOW, _LOCK_SECONDS = 5, 600, 600
_failures: dict[str, list[float]] = {}


def _bootstrap_admins() -> set[str]:
    return {u.strip() for u in os.environ.get('EXEC_ORG_ADMINS', '').split(',') if u.strip()}


def get_role(user_id: str) -> str | None:
    if not user_id:
        return None
    if user_id in _bootstrap_admins():
        return 'admin'
    with db.SessionLocal() as s:
        m = s.get(ExecMember, user_id)
        return m.role if m else None


def list_members() -> list[dict]:
    with db.SessionLocal() as s:
        rows = list(s.scalars(select(ExecMember).order_by(ExecMember.created_at)))
    try:
        names = {u['user_id']: u for u in users.list_users()}
    except users.UserDBError:
        names = {}
    out = [{'user_id': m.user_id, 'role': m.role, 'granted_by': m.granted_by,
            'created_at': m.created_at.strftime('%Y-%m-%d') if m.created_at else '',
            'name': names.get(m.user_id, {}).get('name', m.user_id), 'bootstrap': False,
            'in_user_db': m.user_id in names} for m in rows]
    listed = {m['user_id'] for m in out}
    for uid in sorted(_bootstrap_admins() - listed):
        out.append({'user_id': uid, 'role': 'admin', 'granted_by': 'EXEC_ORG_ADMINS', 'created_at': '',
                    'name': names.get(uid, {}).get('name', uid), 'bootstrap': True, 'in_user_db': uid in names})
    return out


def grant(user_id: str, role: str, granted_by: str) -> None:
    if role not in ROLES:
        raise ValueError(f'알 수 없는 권한: {role}')
    if users.get_user(user_id) is None:
        raise ValueError('사용자 DB 에 없는 아이디입니다.')
    with db.SessionLocal() as s:
        m = s.get(ExecMember, user_id)
        if m:
            m.role = role
        else:
            s.add(ExecMember(user_id=user_id, role=role, granted_by=granted_by))
        s.commit()


def revoke(user_id: str, by: str) -> None:
    if user_id == by:
        raise ValueError('자기 자신의 권한은 회수할 수 없습니다.')
    if user_id in _bootstrap_admins():
        raise ValueError('EXEC_ORG_ADMINS 로 지정된 관리자는 환경변수에서 빼야 합니다.')
    with db.SessionLocal() as s:
        m = s.get(ExecMember, user_id)
        if m:
            s.delete(m)
            s.commit()


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
    role = get_role(user['user_id'])
    if role is None:
        return None, '이 시스템을 사용할 권한이 없습니다. 관리자에게 접근 권한을 요청하세요.'
    flask.session.clear()
    flask.session.permanent = True
    flask.session['user'] = {'user_id': user['user_id'], 'name': user['name']}
    return {**user, 'access': role}, ''


def logout() -> None:
    flask.session.clear()


def current_user() -> dict | None:
    """로그인했고 지금도 권한이 있는 사용자. 요청 안에서만 호출."""
    if not flask.has_request_context():
        return None
    u = flask.session.get('user')
    if not u:
        return None
    if 'access' not in flask.g:
        flask.g.access = get_role(u['user_id'])
    if flask.g.access is None:
        return None
    return {**u, 'access': flask.g.access}


def current_name() -> str:
    u = current_user()
    return u['name'] if u else ''


def is_admin() -> bool:
    u = current_user()
    return bool(u and u['access'] == 'admin')
