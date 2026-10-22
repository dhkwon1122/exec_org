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
- person_career  : 임원 경력 이력 (People in SAIT 직급·소속 변천, 인사발령 — 누적)
- person_highlights  : 임원 주요 이력 (발령이력을 정리·정제한 핵심 경력)
- person_evaluations : 임원 평가 이력 (연도·종류별 — 임원종합평가, MBO 등)
- person_attributes  : 임원 추가 항목 (엑셀 업로드로 생긴 이름-값)
- schedules / schedule_imports : 원장님 일정 / 비서실 일정 메일(원문·AI 정리 결과)
- inbound_mails / mail_rules : POP3 로 받은 메일과 분류 규칙 (일정·업무 자동 반영 / 확인 추천)
- history_people : 이력 ↔ 임원 연결 (한 이력에 여러 임원). 업무 없이 임원에만 연결된 이력(동향)도 있다
- todos          : 할 일 (업무에 속하거나 단독). 이력 원문에서 AI 가 뽑은 제안을 추가할 수 있음
"""
import logging
import os
import time
from contextlib import contextmanager
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
    # 예전 '할 일'을 업무로 합치며 들어온 칸 — 담당자(사용자 DB user_id, 이름은 owner), 완료 시각,
    # 이 업무를 만든 사람, AI 가 이 업무를 제안한 이력(메모)
    assignee_id = Column(String(100), default='')
    done_at = Column(DateTime, nullable=True)
    created_by = Column(String(100), default='')
    source_history_id = Column(Integer, ForeignKey('history.id', ondelete='SET NULL', use_alter=True,
                                                   name='fk_tasks_source_history'), nullable=True)
    source_mail_id = Column(Integer, nullable=True, index=True)  # 이 업무를 만든 받은 메일 (inbound_mails.id)
    source_mail_key = Column(String(64), default='')  # 생성 당시 후보의 식별값 — 제목을 고쳐도 중복 반영 방지
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    history = relationship('HistoryEntry', back_populates='task', foreign_keys='HistoryEntry.task_id', cascade='all, delete-orphan',
                           passive_deletes=True)
    references = relationship('Reference', back_populates='task', cascade='all, delete-orphan',
                              passive_deletes=True)
    assignee_rows = relationship('TaskAssignee', order_by='TaskAssignee.pos', cascade='all, delete-orphan',
                                 passive_deletes=True, lazy='selectin')


class TaskAssignee(Base):
    """업무 담당자 (여러 명). user_id 는 접속 가능한 사용자면 그 id, 이름만 적은 담당자면 ''.
    tasks.owner / tasks.assignee_id 에는 화면·예전 코드용으로 이름을 이은 글과 첫 user_id 를 같이 둔다."""
    __tablename__ = 'task_assignees'

    task_id = Column(Integer, ForeignKey('tasks.id', ondelete='CASCADE'), primary_key=True)
    name = Column(String(100), primary_key=True)
    user_id = Column(String(100), default='', index=True)
    pos = Column(Integer, default=0)


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
    ai_token = Column(String(32), default='')  # 사용자 수정·새 요청마다 변경해 오래된 AI 응답을 차단
    ai_reference_date = Column(String(10), default='')  # 상대 기한을 해석할 때 사용한 기록 기준일

    task = relationship('Task', back_populates='history', foreign_keys=[task_id])


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
    # People in SAIT 인사 정보 (불러오기 때 비어 있으면 채우고, 다르면 덮어쓸지 제안). 나이·연차·근속은 화면에서 계산
    employment_status = Column(String(50), default='')  # 재직상태 (재직/휴직 등)
    birth_year = Column(String(10), default='')
    hire_date = Column(String(10), default='')  # 입사일(그룹입사일) YYYY-MM-DD
    promotion_date = Column(String(10), default='')  # 승격 기준일 — 연차 계산용
    education = Column(String(300), default='')  # 최종학력 '박)○○대학교 전공'
    # 'current' = 최근 불러오기 때 People in SAIT 현재 임원, 'former' = 퇴직·전출 등으로 빠진(과거) 임원,
    # '' = 직접 등록(사번 없음) — 임원 목록 기본 화면은 current 와 직접 등록한 추적 중 임원만 보인다
    hr_state = Column(String(10), default='')
    # 불러오기에서 덮어쓰지 않고 유지하기로 한 People in SAIT 값 (JSON {필드: 그때의 값}) — 다음 제안에서 기본 해제
    sync_skip = Column(Text, default='')
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class HistoryTask(Base):
    """한 이력이 여러 업무에 걸칠 때 — 이력의 업무(history.task_id) 말고 함께 걸친 다른 업무들.
    걸친 업무마다 그 업무의 이력 목록에 같이 보인다."""
    __tablename__ = 'history_tasks'

    history_id = Column(Integer, ForeignKey('history.id', ondelete='CASCADE'), primary_key=True)
    task_id = Column(Integer, ForeignKey('tasks.id', ondelete='CASCADE'), primary_key=True, index=True)


class HistoryReference(Base):
    """이력이 다룬 참고자료 — 참고자료 탭에서 '이 자료에 대한 기록'을 남길 때. 자료 하나에 기록이 여럿일 수 있다
    (자료가 처음 붙은 이력은 references_.history_id)."""
    __tablename__ = 'history_references'

    history_id = Column(Integer, ForeignKey('history.id', ondelete='CASCADE'), primary_key=True)
    reference_id = Column(Integer, ForeignKey('references_.id', ondelete='CASCADE'), primary_key=True, index=True)


class HistoryPerson(Base):
    __tablename__ = 'history_people'

    history_id = Column(Integer, ForeignKey('history.id', ondelete='CASCADE'), primary_key=True)
    person_id = Column(Integer, ForeignKey('people.id', ondelete='CASCADE'), primary_key=True, index=True)


class PersonCareer(Base):
    """임원 경력 이력 — People in SAIT 의 월별 인원 스냅샷(researchers_history)에서 직급·소속이 바뀐 시점과
    인사발령(hr_orders)을 불러올 때마다 쌓는다. 원천에서 사라져도 지우지 않는다(누적).
    source_key 로 같은 항목을 두 번 넣지 않는다 (snap:YYYY-MM / order:날짜:발령명:부서)."""
    __tablename__ = 'person_career'
    __table_args__ = (UniqueConstraint('person_id', 'source_key', name='uq_person_career_key'),)

    id = Column(Integer, primary_key=True)
    person_id = Column(Integer, ForeignKey('people.id', ondelete='CASCADE'), nullable=False, index=True)
    kind = Column(String(20), nullable=False)  # '직급·소속' | '발령'
    date = Column(String(10), nullable=False, default='')  # YYYY-MM 또는 YYYY-MM-DD
    position = Column(String(100), default='')
    org = Column(String(200), default='')
    title = Column(String(200), default='')  # 발령명
    assignment = Column(String(100), default='')  # 직책
    source_key = Column(String(300), nullable=False)
    created_at = Column(DateTime, default=_now)


class PersonHighlight(Base):
    """임원 주요 이력 — 발령이력·경력 이력을 사람이(또는 AI 초안을 고쳐) 정리한 핵심 경력 한 줄씩.
    source: manual(직접 입력·수정) / ai(AI 초안 그대로) / rule(규칙 초안) / excel(엑셀 일괄 업로드)."""
    __tablename__ = 'person_highlights'

    id = Column(Integer, primary_key=True)
    person_id = Column(Integer, ForeignKey('people.id', ondelete='CASCADE'), nullable=False, index=True)
    date = Column(String(10), default='')  # YYYY / YYYY.MM / YYYY.MM.DD
    text = Column(String(500), nullable=False)
    sort = Column(Integer, default=0)
    source = Column(String(20), default='manual')
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class PersonEvaluation(Base):
    """임원 평가 이력 — 해마다 임원종합평가·MBO 등. 종류(kind)는 자유 문자열이라 엑셀에서 새 평가가 오면 그대로 생긴다."""
    __tablename__ = 'person_evaluations'
    __table_args__ = (UniqueConstraint('person_id', 'year', 'kind', name='uq_person_eval'),)

    id = Column(Integer, primary_key=True)
    person_id = Column(Integer, ForeignKey('people.id', ondelete='CASCADE'), nullable=False, index=True)
    year = Column(Integer, nullable=False)
    kind = Column(String(50), nullable=False)
    grade = Column(String(50), default='')
    note = Column(Text, default='')
    source = Column(String(20), default='manual')
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class PersonAttribute(Base):
    """임원 추가 항목 — 엑셀 업로드에서 기존 칸과 맞지 않는 제목이 오면 이 이름으로 새 항목을 만들어 담는다."""
    __tablename__ = 'person_attributes'
    __table_args__ = (UniqueConstraint('person_id', 'name', name='uq_person_attr'),)

    id = Column(Integer, primary_key=True)
    person_id = Column(Integer, ForeignKey('people.id', ondelete='CASCADE'), nullable=False, index=True)
    name = Column(String(100), nullable=False)
    value = Column(Text, default='')
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class Schedule(Base):
    """원장님 일정 — 비서실 메일을 AI 가 정리해 넣거나 직접 입력. 지난 일정이 곧 원장님 일정 이력이다.
    날짜만 있고 시간이 없으면 종일 일정(start_time='')."""
    __tablename__ = 'schedules'

    id = Column(Integer, primary_key=True)
    date = Column(String(10), nullable=False, index=True)  # YYYY-MM-DD
    start_time = Column(String(5), default='')  # HH:MM ('' = 종일·시간 미정)
    end_time = Column(String(5), default='')
    title = Column(String(300), nullable=False)
    location = Column(String(300), default='')
    attendees = Column(Text, default='')
    category = Column(String(30), default='')  # 회의/보고/행사/외부/출장/면담/기타
    details = Column(Text, default='')  # 세부 사항·결과
    source = Column(String(20), default='manual')  # manual / mail
    import_id = Column(Integer, ForeignKey('schedule_imports.id', ondelete='SET NULL'), nullable=True)
    created_by = Column(String(100), default='')
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class Absence(Base):
    """임원 부재 — 휴가·출장·교육 등으로 자리를 비우는 기간. 한 사람당 한 건(여럿이 같이 가는 출장은 사람마다)."""
    __tablename__ = 'absences'

    id = Column(Integer, primary_key=True)
    person_id = Column(Integer, ForeignKey('people.id', ondelete='CASCADE'), nullable=False, index=True)
    kind = Column(String(20), default='휴가')
    start_date = Column(String(10), nullable=False, index=True)  # YYYY-MM-DD
    end_date = Column(String(10), nullable=False, index=True)
    time_range = Column(String(50), default='')  # 부재 시간대 (선택) '09:00~12:00' · '오후' — 비면 종일
    place = Column(String(300), default='')  # 행선지
    content = Column(Text, default='')  # 일정 내용
    note = Column(Text, default='')  # 메모
    # 예전 칸 — 더는 쓰지 않는다(반차는 시간대로, 대결자는 메모로 옮김: _migrate_absences)
    half = Column(String(10), default='')
    deputy = Column(String(100), default='')
    source_mail_id = Column(Integer, nullable=True, index=True)  # 메일에서 뽑아 등록했으면 그 메일 (inbound_mails.id)
    created_by = Column(String(100), default='')
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class ScheduleImport(Base):
    """일정 메일 한 통(붙여넣기·파일) — 원문을 그대로 두고 AI 가 일정 목록으로 정리한 결과를 담는다.
    사람이 확인하고 반영(applied)해야 schedules 에 들어간다."""
    __tablename__ = 'schedule_imports'

    id = Column(Integer, primary_key=True)
    name = Column(String(300), default='')  # 메일 제목 또는 파일 이름
    mail_date = Column(String(10), default='')  # 메일 보낸 날 (상대 날짜 '다음주 월요일' 해석 기준)
    raw_text = Column(Text, default='')
    status = Column(String(20), default='pending')  # pending / done / failed
    events = Column(Text, default='')  # JSON 목록
    error = Column(Text, default='')
    applied_at = Column(DateTime, nullable=True)
    created_by = Column(String(100), default='')
    created_at = Column(DateTime, default=_now)


class InboundMail(Base):
    """POP3 로 받아 온 메일 한 통 — 규칙에 맞으면 자동 반영, 아니면 AI 추천을 사람이 확인한다.
    uidl(POP3 고유 번호)로 같은 메일을 두 번 가져오지 않는다."""
    __tablename__ = 'inbound_mails'

    id = Column(Integer, primary_key=True)
    uidl = Column(String(200), nullable=False, unique=True)
    message_id = Column(String(300), default='')
    subject = Column(String(500), default='')
    sender = Column(String(300), default='')
    mail_date = Column(String(10), default='')
    body = Column(Text, default='')
    # skipped(오래돼 건너뜀) / auto(규칙으로 자동 반영) / review(확인 필요) / done(사람이 반영) / ignored(무시) / failed
    status = Column(String(20), default='review', index=True)
    kind = Column(String(20), default='')  # schedule / task / both / ignore — 규칙 또는 AI 판단
    rule_id = Column(Integer, ForeignKey('mail_rules.id', ondelete='SET NULL'), nullable=True)
    reason = Column(String(500), default='')  # 분류 근거 (규칙 이름 또는 AI 설명)
    suggestion = Column(Text, default='')  # JSON — AI 가 뽑은 업무 후보 등
    schedule_import_id = Column(Integer, ForeignKey('schedule_imports.id', ondelete='SET NULL'), nullable=True)
    task_ids = Column(String(500), default='')  # 만든 업무 id (쉼표)
    error = Column(Text, default='')
    received_at = Column(DateTime, default=_now)
    handled_at = Column(DateTime, nullable=True)
    handled_by = Column(String(100), default='')


class MailAttachment(Base):
    """받은 메일의 첨부 파일. doc_key = 파일 이름에서 날짜·_r1·_v2·최종 같은 꼬리표를 뗀 '같은 문서' 열쇠
    (services/doc_versions.py) — 같은 열쇠끼리 메일 날짜 순으로 판(버전)을 매긴다."""
    __tablename__ = 'mail_attachments'

    id = Column(Integer, primary_key=True)
    mail_id = Column(Integer, ForeignKey('inbound_mails.id', ondelete='CASCADE'), nullable=False, index=True)
    filename = Column(String(300), nullable=False)
    file_path = Column(String(500), default='')  # UPLOAD_DIR 기준
    size = Column(Integer, default=0)
    sha256 = Column(String(64), default='', index=True)
    doc_key = Column(String(300), default='', index=True)
    created_at = Column(DateTime, default=_now)


class OutboundMail(Base):
    """이 시스템에서 보낸 메일 기록 (services/outbox.py). related_kind/id: 무엇을 보냈나 — task / schedule /
    absence / mail(회신) 과 그 id(일정·부재는 기간 'YYYY-MM-DD~YYYY-MM-DD')."""
    __tablename__ = 'outbound_mails'

    id = Column(Integer, primary_key=True)
    to_addrs = Column(Text, default='')  # 쉼표
    cc_addrs = Column(Text, default='')
    subject = Column(String(500), default='')
    body_html = Column(Text, default='')
    attachments = Column(Text, default='')  # 첨부 파일 이름 (쉼표)
    status = Column(String(10), default='sent')  # sent / failed
    error = Column(Text, default='')
    related_kind = Column(String(20), default='', index=True)
    related_id = Column(String(50), default='', index=True)
    sent_by = Column(String(100), default='')
    created_at = Column(DateTime, default=_now)


class MailRule(Base):
    """받은 메일 분류 규칙 — 보낸 사람/제목/본문에 글자가 들어 있으면 그 동작. auto 면 확인 없이 바로 반영."""
    __tablename__ = 'mail_rules'

    id = Column(Integer, primary_key=True)
    name = Column(String(100), default='')
    field = Column(String(20), default='sender')  # sender / subject / body
    pattern = Column(String(300), nullable=False)
    action = Column(String(20), default='task')  # schedule / task / ignore
    auto = Column(Boolean, nullable=False, default=False)
    parent_task_id = Column(Integer, ForeignKey('tasks.id', ondelete='SET NULL'), nullable=True)  # 업무를 이 아래에
    sort = Column(Integer, default=0)
    active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=_now)


class Todo(Base):
    """예전 '할 일' — 지금은 업무(하위 업무)로 합쳐졌다. 예전 DB 에 남은 행은 시작할 때 업무로 옮기고 지운다
    (_merge_todos_into_tasks). 새로 쓰지 않는다."""
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
            _merge_todos_into_tasks(engine)
            _backfill_assignees(engine)
            _link_source_history(engine)
            _migrate_absences(engine)
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



def _migrate_absences(engine):
    """부재 칸 정리 이전의 기록 — 반차(half)는 부재 시간대로, 대결자(deputy)는 메모 끝에 '대결: …'로 옮긴다.
    옮긴 칸은 비우므로 여러 번 돌아도 같다."""
    with engine.begin() as conn:
        rows = conn.execute(text("SELECT id, half, deputy, time_range, note FROM absences "
                                 "WHERE (half IS NOT NULL AND half <> '') OR (deputy IS NOT NULL AND deputy <> '')")).all()
        for aid, half, deputy, time_range, note in rows:
            note = note or ''
            if deputy:
                note = (note + '\n' if note else '') + f'대결: {deputy}'
            conn.execute(text("UPDATE absences SET time_range = :t, note = :n, half = '', deputy = '' WHERE id = :i"),
                         {'t': time_range or half or '', 'n': note, 'i': aid})


def _link_source_history(engine):
    """AI 할 일로 만든 업무(tasks.source_history_id)에 그 기록이 걸려 있지 않으면 건다 — 예전에 만든 업무도
    새 업무 이력에서 원래 기록이 보이게. 이미 걸린 것은 건너뛰므로 여러 번 돌아도 같다."""
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO history_tasks (history_id, task_id) "
            "SELECT t.source_history_id, t.id FROM tasks t JOIN history h ON h.id = t.source_history_id "
            "WHERE (h.task_id IS NULL OR h.task_id <> t.id) AND NOT EXISTS ("
            "  SELECT 1 FROM history_tasks ht WHERE ht.history_id = t.source_history_id AND ht.task_id = t.id)"))


def _backfill_assignees(engine):
    """담당자를 여러 명 둘 수 있게 되기 전의 업무 — owner(이름)·assignee_id 를 task_assignees 로 옮긴다.
    담당자 행이 하나도 없는 업무만 채우므로 여러 번 돌아도 같다."""
    import re
    with engine.begin() as conn:
        rows = conn.execute(text(
            "SELECT id, owner, assignee_id FROM tasks WHERE owner IS NOT NULL AND owner <> '' "
            "AND id NOT IN (SELECT task_id FROM task_assignees)")).all()
        for tid, owner, uid in rows:
            names = list(dict.fromkeys(n.strip() for n in re.split(r'[,/;]', owner) if n.strip()))
            for i, name in enumerate(names):
                conn.execute(TaskAssignee.__table__.insert().values(
                    task_id=tid, name=name[:100], user_id=(uid or '') if i == 0 else '', pos=i))


def _merge_todos_into_tasks(engine):
    """예전 '할 일'(todos)을 업무로 옮긴다 — 업무에 속했던 할 일은 그 업무의 하위 업무, 단독 할 일은 최상위 업무.
    완료 여부 → 상태(완료/진행중), 담당자 이름 → owner. 옮긴 행은 todos 에서 지운다(한 트랜잭션)."""
    with engine.begin() as conn:
        rows = conn.execute(Todo.__table__.select().order_by(Todo.__table__.c.id)).mappings().all()
        for r in rows:
            conn.execute(Task.__table__.insert().values(
                title=r['title'], parent_id=r['task_id'], due_date=r['due_date'] or '',
                status='완료' if r['done'] else '진행중', done_at=r['done_at'], owner=r['assignee_name'] or '',
                assignee_id=r['assignee_id'] or '', created_by=r['created_by'] or '',
                source_history_id=r['source_history_id'], description='', tags='', start_date='',
                created_at=r['created_at'] or _now(), updated_at=_now()))
        if rows:
            conn.execute(text('DELETE FROM todos'))


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


@contextmanager
def transaction(session=None):
    """session 을 받으면 같은 트랜잭션에 참여하고, 없으면 성공 시 한 번만 commit 한다.
    파일처럼 DB 밖에서 만든 자원은 rollback_callbacks 에 정리 함수를 등록한다."""
    if session is not None:
        yield session
        return
    with SessionLocal() as s:
        try:
            yield s
            s.commit()
        except BaseException:
            s.rollback()
            for cleanup in reversed(s.info.pop('rollback_callbacks', [])):
                try:
                    cleanup()
                except OSError:
                    logging.getLogger(__name__).exception('롤백 후 파일 정리 실패')
            raise


def configure(url: str):
    """테스트 등에서 다른 DB 로 전환할 때 사용."""
    global engine
    engine = make_engine(url)
    SessionLocal.configure(bind=engine)
