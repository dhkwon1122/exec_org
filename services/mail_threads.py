"""메일 스레드 — RE:/FW: 로 이어진 메일을 한 묶음(thread_key)으로.

묶는 순서 (먼저 맞는 것):
  1) 메일 머리글 References / In-Reply-To 에 적힌 메일이 이미 받은 메일에 있으면 그 메일의 스레드
  2) 이 메일을 가리키는(References 에 이 Message-ID 가 있는) 메일이 먼저 와 있으면 그 스레드
  3) 답장·전달(RE:/FW:/회신/전달 머리말이나 위 머리글이 있음)이면 머리말을 뗀 제목이 같은 최근 메일의 스레드
  4) References 의 첫 메일(스레드 맨 처음) → In-Reply-To → 자기 Message-ID → 메일 번호
제목만 같은 새 메일(머리말·머리글 없음 — 예: 매주 오는 '주간 보고')은 따로 둔다.
보낸 메일 중 받은 메일에 회신한 것(outbound_mails.related_kind='mail')도 스레드에 함께 보인다.
"""
import logging
import re
import threading
from datetime import date

from sqlalchemy import func, select, update

from services import db
from services.db import InboundMail, OutboundMail

SUBJ_PREFIX = re.compile(r'^\s*((re|fw|fwd|회신|전달|답장)\s*[:：]\s*|\[[^\]]{0,20}\]\s*)+', re.I)
_REPLY = re.compile(r'^\s*(\[[^\]]{0,20}\]\s*)*(re|fw|fwd|회신|전달|답장)\s*[:：]', re.I)
_ID = re.compile(r'<[^<>\s]+>')
SUBJECT_DAYS = 120  # 제목으로 묶을 때 이 날수보다 멀리 떨어진 메일은 다른 스레드로
log = logging.getLogger(__name__)


def norm_subject(subject: str) -> str:
    return re.sub(r'\s+', ' ', SUBJ_PREFIX.sub('', subject or '')).strip().lower()


def is_reply(subject: str) -> bool:
    return bool(_REPLY.match(subject or ''))


def parse_ids(text: str) -> list[str]:
    """머리글의 <메일 id> 들 → 소문자 목록 (순서 유지, 중복 없음)."""
    return list(dict.fromkeys(x.lower() for x in _ID.findall(text or '')))


def _own_id(m: InboundMail) -> str:
    ids = parse_ids(m.message_id or '')
    return ids[0] if ids else ''


def _near(a: str, b: str) -> bool:
    try:
        return abs((date.fromisoformat(a) - date.fromisoformat(b)).days) <= SUBJECT_DAYS
    except ValueError:
        return True  # 날짜를 모르면 제목만으로


_COLS = (InboundMail.id, InboundMail.message_id, InboundMail.references_hdr, InboundMail.in_reply_to,
         InboundMail.subject, InboundMail.mail_date, InboundMail.thread_key)


class _Index:
    """스레드가 정해진 메일들로 만든 찾아보기 — 메일마다 DB 를 다시 뒤지지 않게 한 번에 읽어 메모리에서."""

    def __init__(self):
        self.by_mid: dict[str, str] = {}   # 메일 id → 스레드
        self.by_ref: dict[str, str] = {}   # 머리글에 적힌 메일 id → 그 머리글을 가진 메일의 스레드
        self.by_subj: dict[str, list[tuple[str, str]]] = {}  # 머리말 뗀 제목 → [(메일 날짜, 스레드)] 받은 순

    def add(self, r, key: str) -> None:
        own = parse_ids(r.message_id or '')
        if own:
            self.by_mid.setdefault(own[0], key)
        for x in parse_ids(f'{r.references_hdr or ""} {r.in_reply_to or ""}'):
            self.by_ref.setdefault(x, key)
        core = norm_subject(r.subject)
        if core:
            self.by_subj.setdefault(core, []).append((r.mail_date or '', key))

    def decide(self, r) -> str:
        refs, irt = parse_ids(r.references_hdr or ''), parse_ids(r.in_reply_to or '')
        linked = list(dict.fromkeys(refs + irt))
        for x in linked:  # 1) 가리키는 메일이 와 있음 — 스레드 맨 처음에 가까운 것부터
            if x in self.by_mid:
                return self.by_mid[x]
        own = parse_ids(r.message_id or '')
        own = own[0] if own else ''
        if own and own in self.by_ref:  # 2) 답장이 먼저 와 있음
            return self.by_ref[own]
        core = norm_subject(r.subject)
        if core and (linked or is_reply(r.subject)):  # 3) 답장·전달 — 같은 제목의 가장 최근 메일
            for d, key in reversed(self.by_subj.get(core, [])):
                if _near(d, r.mail_date or ''):
                    return key
        if linked:  # 4)
            return 'mid:' + (refs[0] if refs else irt[0])
        return ('mid:' + own if own else f'mail:{r.id}')[:300]


def _index(s, exclude_id: int | None = None) -> tuple[_Index, list]:
    """→ (스레드가 정해진 메일의 찾아보기, 아직 안 정해진 메일 목록 — 오래된 것부터)."""
    idx, todo = _Index(), []
    for r in s.execute(select(*_COLS).where(InboundMail.status != 'skipped').order_by(InboundMail.id)):
        if r.id == exclude_id:
            continue
        if r.thread_key:
            idx.add(r, r.thread_key)
        else:
            todo.append(r)
    return idx, todo


def compute_key(s, m: InboundMail) -> str:
    idx, _ = _index(s, m.id)
    return idx.decide(m)[:300]


def assign(s, m: InboundMail) -> str:
    """스레드가 비어 있으면 정해 둔다 → thread_key."""
    if not m.thread_key:
        m.thread_key = compute_key(s, m)
        s.flush()
    return m.thread_key


_lock = threading.Lock()


def ensure_all(wait: bool = True) -> int:
    """아직 스레드가 없는 메일(새 메일·이전 버전에서 받은 메일)에 스레드를 정한다 — 오래된 것부터, 한 번에.
    wait=False 면 다른 곳에서 이미 하고 있을 때 기다리지 않고 0 (화면 그리기용)."""
    if not pending():
        return 0
    if not _lock.acquire(blocking=wait):
        return 0
    try:
        return _ensure_all()
    finally:
        _lock.release()


def _ensure_all() -> int:
    with db.SessionLocal() as s:
        idx, todo = _index(s)
        rows = []
        for r in todo:
            key = idx.decide(r)[:300]
            idx.add(r, key)
            rows.append({'id': r.id, 'thread_key': key})
        if rows:
            s.execute(update(InboundMail), rows)
            s.commit()
        return len(rows)


def pending() -> bool:
    """스레드가 아직 없는 메일이 있나."""
    with db.SessionLocal() as s:
        return s.scalar(select(InboundMail.id).where(func.coalesce(InboundMail.thread_key, '') == '',
                                                     InboundMail.status != 'skipped').limit(1)) is not None


def mail_ids(s, mail_id: int) -> list[int]:
    """같은 스레드의 받은 메일 id (자기 포함, 오래된 것부터)."""
    m = s.get(InboundMail, mail_id)
    if m is None:
        return []
    key = assign(s, m)
    return list(s.scalars(select(InboundMail.id).where(InboundMail.thread_key == key, InboundMail.status != 'skipped')
                          .order_by(InboundMail.id)))


def sizes(keys: list[str]) -> dict[str, int]:
    """스레드별 받은 메일 수."""
    keys = [k for k in set(keys) if k]
    if not keys:
        return {}
    with db.SessionLocal() as s:
        return dict(s.execute(select(InboundMail.thread_key, func.count()).where(
            InboundMail.thread_key.in_(keys), InboundMail.status != 'skipped').group_by(InboundMail.thread_key)).all())


def thread(mail_id: int) -> list[dict]:
    """스레드의 메일 — 받은 메일과 그 회신으로 보낸 메일을 시간 순으로.
    → [{'dir': 'in'|'out', 'id', 'subject', 'who', 'date', 'snippet', 'status', 'task_ids'}]"""
    with db.SessionLocal() as s:
        ids = mail_ids(s, mail_id)
        s.commit()
        if not ids:
            return []
        out = []
        for m in s.scalars(select(InboundMail).where(InboundMail.id.in_(ids))):
            out.append({'dir': 'in', 'id': m.id, 'subject': m.subject or '(제목 없음)', 'who': m.sender or '',
                        'date': m.received_at.strftime('%Y-%m-%d %H:%M') if m.received_at else (m.mail_date or ''),
                        'sort': (m.mail_date or '', m.received_at.isoformat() if m.received_at else '', m.id),
                        'snippet': re.sub(r'\s+', ' ', (m.body or ''))[:120], 'status': m.status,
                        'task_ids': [int(x) for x in (m.task_ids or '').split(',') if x.strip().isdigit()]})
        for o in s.scalars(select(OutboundMail).where(OutboundMail.related_kind == 'mail',
                                                      OutboundMail.related_id.in_([str(i) for i in ids]))):
            when = o.created_at
            out.append({'dir': 'out', 'id': o.id, 'subject': o.subject or '', 'who': o.to_addrs or '',
                        'date': when.strftime('%Y-%m-%d %H:%M') if when else '',
                        'sort': (when.date().isoformat() if when else '', when.isoformat() if when else '', 0),
                        'snippet': re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', o.body_html or ''))[:120],
                        'status': o.status, 'task_ids': []})
    out.sort(key=lambda x: x['sort'])
    for x in out:
        x.pop('sort')
    return out


def group(mails: list[dict]) -> list[list[dict]]:
    """메일 목록(최신 먼저)을 스레드별로 — 묶음 순서는 각 스레드의 가장 최근 메일 순, 묶음 안도 최신 먼저."""
    groups: dict[str, list[dict]] = {}
    for m in mails:
        groups.setdefault(m.get('thread_key') or f"mail:{m['id']}", []).append(m)
    return list(groups.values())
