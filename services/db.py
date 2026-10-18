"""DB 연결과 테이블 정의.

EXEC_ORG_DATABASE_URL 환경변수의 DB 를 쓴다 — Docker 운영은 docker-compose.yml 의 이 앱 전용
PostgreSQL(exec-db). 없으면(개발·테스트) 프로젝트 안의 SQLite 파일(data/exec_org.db). (DATABASE_URL 은 tea-meeting-agenda 처럼 People in SAIT
사용자 DB 를 가리키므로 업무 기록 DB 로는 쓰지 않는다 — services/users.py) 테이블은 앱 시작 시 없으면 자동 생성한다.

테이블 구성
- tasks          : 업무 (상위 업무 parent_id 로 계층 구성 가능)
- history        : 업무별 이력 (메모/회의/결정/피드백/상태변경/연결 등 시간순 기록).
                   content 는 사용자가 올린 원문 그대로, title/summary/keywords 는
                   LLM 이 만든(또는 사용자가 고친) 검색용 정리본
- references_    : 업무별 참고자료 (링크/파일/메모). history_id 가 있으면 그 이력에 붙은 관련 자료
- task_links     : 업무 ↔ 업무 연결 (관련/선행/후속/파생)
- people         : 임원 (이름·직위·소속·별칭). 동향 기록의 대상
- history_people : 이력 ↔ 임원 연결 (한 이력에 여러 임원). 업무 없이 임원에만 연결된 이력(동향)도 있다
- todos          : 할 일 (업무에 속하거나 단독). 이력 원문에서 AI 가 뽑은 제안을 추가할 수 있음
"""
import logging
import os
import time
from datetime import datetime

from dotenv import load_dotenv
from sqlalchemy import (
    Boolean, Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine, event, inspect,
    text,
)
from sqlalchemy import MetaData
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

load_dotenv()

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, 'data')
UPLOAD_DIR = os.path.join(DATA_DIR, 'uploads')

Base = declarative_base()


def _now():
    return datetime.now().replace(microsecond=0)


class Task(Base):
    __tablename__ = 'tasks'

    id = Column(Integer, primary_key=True)
    title = Column(String(300), nullable=False)
    description = Column(Text, default='')
    status = Column(String(20), nullable=False, default='진행중')
    owner = Column(String(100), default='')
    tags = Column(String(500), default='')  # 쉼표 구분
    parent_id = Column(Integer, ForeignKey('tasks.id', ondelete='SET NULL'), nullable=True)
    start_date = Column(String(10), default='')  # YYYY-MM-DD
    due_date = Column(String(10), default='')
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    history = relationship('HistoryEntry', back_populates='task', cascade='all, delete-orphan',
                           passive_deletes=True)
    references = relationship('Reference', back_populates='task', cascade='all, delete-orphan',
                              passive_deletes=True)


class HistoryEntry(Base):
    __tablename__ = 'history'

    id = Column(Integer, primary_key=True)
    # 업무에 속한 이력이면 업무 id. 임원 동향처럼 업무 없이 임원에만 연결된 이력은 None
    task_id = Column(Integer, ForeignKey('tasks.id', ondelete='CASCADE'), nullable=True, index=True)
    entry_type = Column(String(20), nullable=False, default='메모')
    content = Column(Text, nullable=False)  # 원문 (가공하지 않고 그대로 보존)
    author = Column(String(100), default='')
    occurred_at = Column(DateTime, default=_now)  # 실제 일이 있었던 시점(사용자가 지정 가능)
    created_at = Column(DateTime, default=_now)
    # 검색용 정리본 — LLM 이 채우고 사용자가 고칠 수 있다
    title = Column(String(300), default='')
    summary = Column(Text, default='')
    keywords = Column(String(1000), default='')  # 쉼표 구분
    ai_status = Column(String(10), default='')  # '' (요청 안 함) / pending / done / failed
    ai_error = Column(Text, default='')
    ai_todos = Column(Text, default='')  # AI 가 원문에서 뽑은 할 일 제안 (JSON 배열)

    task = relationship('Task', back_populates='history')


class Reference(Base):
    __tablename__ = 'references_'

    id = Column(Integer, primary_key=True)
    # 업무 없는 이력(임원 동향)에 붙은 자료는 None
    task_id = Column(Integer, ForeignKey('tasks.id', ondelete='CASCADE'), nullable=True, index=True)
    # 특정 이력에 붙은 자료면 그 이력 id (이력을 지워도 자료는 업무 참고자료로 남는다)
    history_id = Column(Integer, ForeignKey('history.id', ondelete='SET NULL'), nullable=True, index=True)
    kind = Column(String(10), nullable=False)  # link / file / note
    title = Column(String(300), nullable=False)
    url = Column(Text, default='')
    file_name = Column(String(300), default='')  # 원래 파일명
    file_path = Column(String(500), default='')  # UPLOAD_DIR 기준 저장 경로
    note = Column(Text, default='')
    created_at = Column(DateTime, default=_now)

    task = relationship('Task', back_populates='references')


class TaskLink(Base):
    __tablename__ = 'task_links'
    __table_args__ = (UniqueConstraint('src_id', 'dst_id', name='uq_task_link'),)

    id = Column(Integer, primary_key=True)
    src_id = Column(Integer, ForeignKey('tasks.id', ondelete='CASCADE'), nullable=False, index=True)
    dst_id = Column(Integer, ForeignKey('tasks.id', ondelete='CASCADE'), nullable=False, index=True)
    relation = Column(String(20), nullable=False, default='관련')
    note = Column(Text, default='')
    created_at = Column(DateTime, default=_now)


class Person(Base):
    """임원. aliases 는 원문에서 이 사람을 부르는 다른 이름들(쉼표 구분, 예: '김부사장, 김 부사장, CTO').
    이름이나 별칭이 원문에 나오면 그 이력이 이 사람에게 자동으로 연결된다."""
    __tablename__ = 'people'

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    position = Column(String(100), default='')  # 직위
    org = Column(String(200), default='')  # 소속
    aliases = Column(String(500), default='')
    note = Column(Text, default='')
    active = Column(Boolean, nullable=False, default=True)  # 퇴임 등으로 더 이상 추적하지 않으면 False
    # People in SAIT(researchers 테이블)에서 불러온 임원이면 그 사번(researcher_id). 직접 등록한 임원은 ''
    source_id = Column(String(50), default='', index=True)
    synced_at = Column(DateTime, nullable=True)  # 마지막으로 People in SAIT 정보로 갱신한 시각
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class HistoryPerson(Base):
    __tablename__ = 'history_people'

    history_id = Column(Integer, ForeignKey('history.id', ondelete='CASCADE'), primary_key=True)
    person_id = Column(Integer, ForeignKey('people.id', ondelete='CASCADE'), primary_key=True, index=True)


class Todo(Base):
    __tablename__ = 'todos'

    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, ForeignKey('tasks.id', ondelete='CASCADE'), nullable=True, index=True)
    title = Column(String(500), nullable=False)
    due_date = Column(String(10), default='')  # YYYY-MM-DD, 비우면 날짜 없음
    done = Column(Boolean, nullable=False, default=False)
    done_at = Column(DateTime, nullable=True)
    assignee_id = Column(String(100), default='')  # 사용자 DB 의 user_id
    assignee_name = Column(String(100), default='')
    created_by = Column(String(100), default='')
    source_history_id = Column(Integer, ForeignKey('history.id', ondelete='SET NULL'), nullable=True)
    created_at = Column(DateTime, default=_now)


def make_engine(url: str | None = None):
    url = url or os.environ.get('EXEC_ORG_DATABASE_URL', '').strip()
    if not url:
        os.makedirs(DATA_DIR, exist_ok=True)
        url = 'sqlite:///' + os.path.join(DATA_DIR, 'exec_org.db')
    if url.startswith('sqlite'):
        # gunicorn 워커 여러 개 + AI 정리 스레드가 같은 파일에 쓰므로 잠금을 기다리게 한다
        engine = create_engine(url, future=True, connect_args={'timeout': 30})

        @event.listens_for(engine, 'connect')
        def _sqlite_pragmas(dbapi_conn, _):
            dbapi_conn.execute('PRAGMA foreign_keys=ON')  # ON DELETE CASCADE 가 기본 꺼져 있다
            dbapi_conn.execute('PRAGMA journal_mode=WAL')  # 읽기가 쓰기를 막지 않게
    else:
        engine = create_engine(url, future=True, pool_pre_ping=True)
    # 워커들이 동시에 떠서 같은 테이블을 동시에 만들려다 한쪽이 실패할 수 있어 몇 번 재시도한다
    for attempt in range(5):
        try:
            Base.metadata.create_all(engine)
            _add_missing_columns(engine)
            _relax_not_null(engine)
            break
        except Exception:  # noqa: BLE001
            if attempt == 4:
                raise
            time.sleep(0.5 * (attempt + 1))
    return engine


def _add_missing_columns(engine):
    """create_all 은 이미 있는 테이블에 새 컬럼을 추가하지 않는다 — 이전 버전으로
    만든 DB 에 나중에 추가된 컬럼(예: history.summary)을 ALTER TABLE 로 붙인다."""
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            existing = {c['name'] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                coltype = col.type.compile(dialect=engine.dialect)
                if isinstance(col.type, (String, Text)):
                    default = " DEFAULT ''"
                elif isinstance(col.type, Boolean):
                    default = ' DEFAULT FALSE'
                else:
                    default = ''
                conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN {col.name} {coltype}{default}'))



# 예전 버전에서 NOT NULL 이던 컬럼 중 지금은 비워둘 수 있게 된 것 (임원 동향은 업무가 없다)
_NULLABLE_NOW = [('history', 'task_id'), ('references_', 'task_id')]


def _relax_not_null(engine):
    """예전 DB 의 NOT NULL 제약을 푼다. PostgreSQL 은 ALTER 한 줄, SQLite 는 ALTER 로 못 바꾸므로
    SQLite 공식 절차대로 새 테이블을 만들어 옮긴 뒤 이름을 바꾼다(외래키 검사를 잠시 끄고, 데이터·id 그대로)."""
    insp = inspect(engine)
    for table_name, col_name in _NULLABLE_NOW:
        cols = {c['name']: c for c in insp.get_columns(table_name)}
        if col_name not in cols or cols[col_name]['nullable']:
            continue
        if engine.dialect.name == 'postgresql':
            with engine.begin() as conn:
                conn.execute(text(f'ALTER TABLE {table_name} ALTER COLUMN {col_name} DROP NOT NULL'))
            continue
        if engine.dialect.name != 'sqlite':
            continue
        table = Base.metadata.tables[table_name]
        tmp_name = f'{table_name}__rebuild'
        # 외래키가 가리키는 다른 테이블들도 같은 MetaData 에 있어야 CREATE 문을 만들 수 있다
        target = MetaData()
        for other in Base.metadata.sorted_tables:
            if other.name != table_name:
                other.to_metadata(target)
        tmp = table.to_metadata(target, name=tmp_name)
        for idx in list(tmp.indexes):  # 인덱스 이름은 DB 전체에서 유일 — 옛 테이블을 지운 뒤 다시 만든다
            tmp.indexes.discard(idx)
        shared = [c for c in table.columns.keys() if c in cols]
        col_list = ', '.join(shared)
        with engine.connect() as conn:
            # 이 PRAGMA 는 트랜잭션 밖에서만 먹는다 — 바로 commit 해 트랜잭션을 닫고 시작
            conn.exec_driver_sql('PRAGMA foreign_keys=OFF')
            conn.commit()
            try:
                with conn.begin():
                    conn.execute(text(f'DROP TABLE IF EXISTS {tmp_name}'))
                    tmp.create(conn)
                    conn.execute(text(f'INSERT INTO {tmp_name} ({col_list}) SELECT {col_list} FROM {table_name}'))
                    conn.execute(text(f'DROP TABLE {table_name}'))
                    conn.execute(text(f'ALTER TABLE {tmp_name} RENAME TO {table_name}'))
                    for idx in table.indexes:
                        idx.create(conn)
            finally:
                conn.exec_driver_sql('PRAGMA foreign_keys=ON')
                conn.commit()


def _legacy_sqlite_notice(engine):
    """PostgreSQL 로 옮겼는데 예전 SQLite 기록이 남아 있고 새 DB 는 비어 있으면, 옮기는 방법을 로그로 알린다."""
    legacy = os.path.join(DATA_DIR, 'exec_org.db')
    if engine.dialect.name == 'sqlite' or not os.path.exists(legacy):
        return
    try:
        with engine.connect() as conn:
            empty = all(conn.execute(text(f'SELECT COUNT(*) FROM {t}')).scalar() == 0 for t in ('history', 'people'))
    except Exception:  # noqa: BLE001
        return
    if empty:
        logging.getLogger(__name__).warning(
            '업무 기록 DB(PostgreSQL)가 비어 있고 예전 SQLite 기록(data/exec_org.db)이 있습니다. '
            '옮기려면: docker compose exec app python scripts/migrate_to_postgres.py')


engine = make_engine()
_legacy_sqlite_notice(engine)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def configure(url: str):
    """테스트 등에서 다른 DB 로 전환할 때 사용."""
    global engine
    engine = make_engine(url)
    SessionLocal.configure(bind=engine)
