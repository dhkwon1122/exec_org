"""DB 연결과 테이블 정의.

기본은 프로젝트 안의 SQLite 파일(data/exec_org.db)이고, DATABASE_URL 환경변수가
있으면 그 DB(PostgreSQL 등)를 쓴다. 테이블은 앱 시작 시 없으면 자동 생성한다.

테이블 구성
- tasks          : 업무 (상위 업무 parent_id 로 계층 구성 가능)
- history        : 업무별 이력 (메모/회의/결정/진행/이슈/상태변경/연결 등 시간순 기록)
- references_    : 업무별 참고자료 (링크/파일/메모)
- task_links     : 업무 ↔ 업무 연결 (관련/선행/후속/파생)
"""
import os
from datetime import datetime

from dotenv import load_dotenv
from sqlalchemy import (
    Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine, event,
)
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
    task_id = Column(Integer, ForeignKey('tasks.id', ondelete='CASCADE'), nullable=False, index=True)
    entry_type = Column(String(20), nullable=False, default='메모')
    content = Column(Text, nullable=False)
    author = Column(String(100), default='')
    occurred_at = Column(DateTime, default=_now)  # 실제 일이 있었던 시점(사용자가 지정 가능)
    created_at = Column(DateTime, default=_now)

    task = relationship('Task', back_populates='history')


class Reference(Base):
    __tablename__ = 'references_'

    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, ForeignKey('tasks.id', ondelete='CASCADE'), nullable=False, index=True)
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


def make_engine(url: str | None = None):
    url = url or os.environ.get('DATABASE_URL', '').strip()
    if not url:
        os.makedirs(DATA_DIR, exist_ok=True)
        url = 'sqlite:///' + os.path.join(DATA_DIR, 'exec_org.db')
    engine = create_engine(url, future=True)
    if engine.dialect.name == 'sqlite':
        # SQLite 는 외래키 제약(ON DELETE CASCADE)이 기본 꺼져 있다
        @event.listens_for(engine, 'connect')
        def _fk_on(dbapi_conn, _):
            dbapi_conn.execute('PRAGMA foreign_keys=ON')
    Base.metadata.create_all(engine)
    return engine


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def configure(url: str):
    """테스트 등에서 다른 DB 로 전환할 때 사용."""
    global engine
    engine = make_engine(url)
    SessionLocal.configure(bind=engine)
