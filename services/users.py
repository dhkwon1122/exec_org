"""사용자 DB — 계정(아이디·비밀번호 해시·이름·역할) 조회.

운영: DATABASE_URL(또는 USER_DATABASE_URL)에 People in SAIT(Researcher-board)가 쓰는 PostgreSQL 을
지정하면 그 DB 의 app_users 테이블을 **읽기 전용**으로 사용한다. 같은 계정·비밀번호로
로그인하며, 계정 생성·비밀번호 변경은 People in SAIT 에서 한다. 이 테이블은 다른
시스템도 함께 쓰므로 여기서는 절대 스키마를 만들거나 바꾸거나 쓰지 않는다.

tea-meeting-agenda 와 같은 이름(DATABASE_URL)이라 그쪽 .env 값을 그대로 쓰면 된다.

개발/단독 실행: 둘 다 없으면 이 앱 DB 안에 같은 모양의 app_users 를
만들어 쓴다 (scripts/create_user.py 로 계정 생성).

비밀번호 해시는 Researcher-board 와 같은 werkzeug(generate/check_password_hash).
"""
import os
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, MetaData, String, Table, create_engine, select, text
from werkzeug.security import check_password_hash, generate_password_hash

from services import db

# 존재하지 않는 아이디로 로그인할 때도 해시 검증 시간을 똑같이 써서, 응답 시간으로
# 아이디 존재 여부를 알아낼 수 없게 한다
_DUMMY_HASH = generate_password_hash('dummy-password-for-timing')
_STATEMENT_TIMEOUT_MS = 8000  # 공유 DB 의 잠금 때문에 화면이 멈추지 않도록 (PostgreSQL)

_local_meta = MetaData()
_local_users = Table(
    'app_users', _local_meta,
    Column('user_id', String, primary_key=True),
    Column('password_hash', String, nullable=False),
    Column('display_name', String, nullable=False),
    Column('role', String, nullable=False, default=''),
    Column('email', String),
    Column('must_change_password', Boolean, nullable=False, default=False),
    Column('is_admin', Boolean, nullable=False, default=False),
    Column('created_at', DateTime(timezone=True), nullable=False, default=datetime.now),
)

_engine = None
_table = None


class UserDBError(RuntimeError):
    """사용자 DB 연결/조회 실패."""


def user_db_url() -> str:
    return (os.environ.get('USER_DATABASE_URL') or os.environ.get('DATABASE_URL') or '').strip()


def is_shared() -> bool:
    return bool(user_db_url())


def _get():
    global _engine, _table
    if _engine is None:
        url = user_db_url()
        if url:
            connect_args = {'connect_timeout': 5} if url.startswith('postgresql') else {}
            _engine = create_engine(url, future=True, pool_pre_ping=True, connect_args=connect_args)
            try:
                _table = Table('app_users', MetaData(), autoload_with=_engine)  # 읽기만, 생성 안 함
            except Exception as e:  # noqa: BLE001
                _engine = None
                raise UserDBError(f'사용자 DB(app_users)에 연결할 수 없습니다: {type(e).__name__}') from e
        else:
            _engine = db.engine
            _local_meta.create_all(_engine)
            _table = _local_users
    return _engine, _table


def reset():
    """테스트·설정 변경용: 다음 호출 때 다시 연결한다."""
    global _engine, _table
    _engine = _table = None


def _execute(stmt):
    engine, _ = _get()
    try:
        with engine.begin() as conn:
            if engine.dialect.name == 'postgresql':
                conn.execute(text(f'SET LOCAL statement_timeout = {_STATEMENT_TIMEOUT_MS}'))
            return [dict(r._mapping) for r in conn.execute(stmt)]
    except Exception as e:  # noqa: BLE001
        raise UserDBError(f'사용자 DB 조회 실패: {type(e).__name__}') from e


def _public(row: dict) -> dict:
    return {
        'user_id': row['user_id'], 'name': row.get('display_name') or row['user_id'],
        'role': row.get('role') or '', 'email': row.get('email') or '',
        'must_change_password': bool(row.get('must_change_password')),
    }


def get_user(user_id: str) -> dict | None:
    _, t = _get()
    rows = _execute(select(t).where(t.c.user_id == user_id))
    return _public(rows[0]) if rows else None


def list_users() -> list[dict]:
    _, t = _get()
    cols = [c for c in (t.c.user_id, t.c.display_name, t.c.role, t.c.email)]
    return [_public(r) for r in _execute(select(*cols).order_by(t.c.display_name))]


def verify(user_id: str, password: str) -> dict | None:
    """아이디·비밀번호가 맞으면 사용자 정보, 아니면 None."""
    user_id = (user_id or '').strip()
    if not user_id or not password:
        return None
    _, t = _get()
    rows = _execute(select(t).where(t.c.user_id == user_id))
    if not rows:
        check_password_hash(_DUMMY_HASH, password)
        return None
    if not check_password_hash(rows[0]['password_hash'], password):
        return None
    return _public(rows[0])


def create_local_user(user_id: str, password: str, display_name: str, role: str = '', email: str = '') -> None:
    """단독 실행 모드 전용. 공유 사용자 DB 에는 절대 쓰지 않는다."""
    if is_shared():
        raise UserDBError('공유 사용자 DB 모드에서는 계정을 만들 수 없습니다. People in SAIT 에서 만드세요.')
    if len(password or '') < 8:
        raise ValueError('비밀번호는 8자 이상이어야 합니다.')
    engine, t = _get()
    with engine.begin() as conn:
        conn.execute(t.insert().values(user_id=user_id, password_hash=generate_password_hash(password),
                                       display_name=display_name or user_id, role=role, email=email,
                                       must_change_password=False, is_admin=False, created_at=datetime.now()))
