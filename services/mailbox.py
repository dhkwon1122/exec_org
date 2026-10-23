"""받은 메일 자동 처리 — POP3 로 새 메일을 가져와 일정·업무로 반영한다.

흐름 (메일 한 통):
  1) 분류 규칙(mail_rules: 보낸 사람/제목/본문에 글자 포함 → 일정·업무·무시)에 맞으면 그 동작.
     규칙이 '자동'이면 확인 없이 바로 반영(status=auto), 아니면 추천만(review).
  2) 맞는 규칙이 없으면 AI 가 '일정 / 업무 / 둘 다 / 부재 / 무시'로 나누고 업무 후보를 뽑아 추천(review).
     일정이면 누구 일정인지(원장님·피플팀장)도 정한다 — schedule_owner().
     LLM 이 없으면 일정표가 보이면 일정, 아니면 제목을 업무 후보로.
  3) '받은 메일' 화면에서 추천을 고쳐 반영하거나 무시한다.
일정은 캘린더의 일정 가져오기(schedules)와 같은 길로 정리·반영하고, 업무는 하위 업무로 만들면서 메일 원문을
그 업무의 '요청' 기록으로 남긴다. POP3 고유 번호(UIDL)로 같은 메일을 두 번 가져오지 않는다.
"""
import fcntl
import hashlib
import json
import logging
import os
import poplib
import re
import threading
import time
from datetime import date, datetime, timedelta
from email.utils import parseaddr

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError

from services import absences, db, doc_summary, doc_versions, llm, mail_text, schedules, store
from services.db import InboundMail, MailRule, Task

log = logging.getLogger(__name__)
ACTIONS = {'schedule': '원장님 일정', 'schedule_people': '피플팀장 일정', 'task': '업무', 'absence': '임원 부재',
           'ignore': '무시'}
RULE_OWNER = {'schedule': 'director', 'schedule_people': 'people'}  # 일정 규칙 → 누구 일정
FIELDS = {'sender': '보낸 사람', 'subject': '제목', 'body': '본문'}
STATUS = {'auto': '자동 처리', 'review': '확인 필요', 'done': '처리 완료', 'ignored': '무시', 'failed': '실패',
          'skipped': '건너뜀'}
MAIL_AUTHOR = '메일 자동'


def _env(name, default=''):
    return os.environ.get(f'MAIL_{name}', default).strip()


def is_configured() -> bool:
    return bool(_env('POP3_HOST') and _env('POP3_USER'))


def settings() -> dict:
    ssl = _env('POP3_SSL', '1') not in ('0', 'false', 'no')
    return {'host': _env('POP3_HOST'), 'port': int(_env('POP3_PORT', '995' if ssl else '110')), 'ssl': ssl,
            'user': _env('POP3_USER'), 'password': _env('POP3_PASSWORD'),
            'minutes': max(1, int(_env('POLL_MINUTES', '5'))), 'since_days': int(_env('SINCE_DAYS', '7')),
            'max_per_run': int(_env('MAX_PER_RUN', '50')),
            'max_attachment_mb': int(_env('MAX_ATTACHMENT_MB', '30')),
            'delete': _env('DELETE_FROM_SERVER', '0') in ('1', 'true', 'yes')}


# ── 규칙 ──────────────────────────────────────────────────────────────

def _rule_dict(r: MailRule) -> dict:
    return {'id': r.id, 'name': r.name or '', 'field': r.field, 'pattern': r.pattern, 'action': r.action,
            'auto': bool(r.auto), 'parent_task_id': r.parent_task_id, 'sort': r.sort or 0, 'active': bool(r.active)}


def list_rules() -> list[dict]:
    with db.SessionLocal() as s:
        return [_rule_dict(r) for r in s.scalars(select(MailRule).order_by(MailRule.sort, MailRule.id))]


def save_rule(data: dict, rule_id: int | None = None) -> dict:
    pattern = str(data.get('pattern') or '').strip()
    if not pattern:
        raise ValueError('찾을 글자를 입력하세요.')
    if data.get('field') not in FIELDS or data.get('action') not in ACTIONS:
        raise ValueError('규칙 항목이 올바르지 않습니다.')
    with db.SessionLocal() as s:
        r = s.get(MailRule, rule_id) if rule_id else MailRule()
        if r is None:
            raise ValueError('규칙을 찾을 수 없습니다.')
        r.name = str(data.get('name') or '').strip()[:100] or f"{FIELDS[data['field']]}에 '{pattern}'"
        r.field, r.pattern, r.action = data['field'], pattern[:300], data['action']
        r.auto = True if data['action'] == 'ignore' else bool(data.get('auto'))  # 무시는 늘 바로
        r.parent_task_id = data.get('parent_task_id') or None
        r.active = bool(data.get('active', True))
        if not rule_id:
            r.sort = len(list(s.scalars(select(MailRule.id))))
            s.add(r)
        s.commit()
        return _rule_dict(r)


def delete_rule(rule_id: int) -> None:
    with db.SessionLocal() as s:
        r = s.get(MailRule, rule_id)
        if r:
            s.delete(r)
            s.commit()


def match_rule(subject: str, sender: str, body: str) -> dict | None:
    """위에서부터 처음 맞는 규칙 (대소문자 무시, 글자 포함)."""
    hay = {'sender': sender or '', 'subject': subject or '', 'body': body or ''}
    for r in list_rules():
        if r['active'] and r['pattern'].lower() in hay[r['field']].lower():
            return r
    return None


# ── 메일 ──────────────────────────────────────────────────────────────

def _mail_dict(m: InboundMail) -> dict:
    try:
        sug = json.loads(m.suggestion or '{}')
    except ValueError:
        sug = {}
    return {'id': m.id, 'subject': m.subject or '(제목 없음)', 'sender': m.sender or '', 'mail_date': m.mail_date or '',
            'body': m.body or '', 'status': m.status, 'kind': m.kind or '', 'rule_id': m.rule_id,
            'reason': m.reason or '', 'suggestion': sug, 'schedule_import_id': m.schedule_import_id,
            'task_ids': [int(x) for x in (m.task_ids or '').split(',') if x.strip().isdigit()],
            'error': m.error or '', 'received_at': m.received_at.strftime('%Y-%m-%d %H:%M') if m.received_at else '',
            'handled_at': m.handled_at.strftime('%Y-%m-%d %H:%M') if m.handled_at else '',
            'handled_by': m.handled_by or ''}


def list_mails(status: list[str] | None = None, limit: int = 200) -> list[dict]:
    with db.SessionLocal() as s:
        stmt = select(InboundMail).where(InboundMail.status != 'skipped')
        if status:
            stmt = stmt.where(InboundMail.status.in_(status))
        return [_mail_dict(m) for m in s.scalars(stmt.order_by(InboundMail.id.desc()).limit(limit))]


def get_mail(mail_id: int) -> dict | None:
    with db.SessionLocal() as s:
        m = s.get(InboundMail, mail_id)
        return _mail_dict(m) if m else None


def counts() -> dict:
    with db.SessionLocal() as s:
        return dict(s.execute(select(InboundMail.status, func.count()).group_by(InboundMail.status)).all())


def _update(mail_id: int, *, session=None, **fields):
    with db.transaction(session) as s:
        m = s.get(InboundMail, mail_id)
        for k, v in fields.items():
            setattr(m, k, json.dumps(v, ensure_ascii=False) if k == 'suggestion' else v)
        s.flush()


def sender_name(sender: str) -> str:
    name, addr = parseaddr(sender or '')
    return name or addr or (sender or '')


# ── 분류 ──────────────────────────────────────────────────────────────

CLASSIFY_SYSTEM = """너는 임원조직 담당자의 메일을 정리하는 비서다. 받은 메일을 읽고 무엇으로 처리할지 정한다.
- schedule: 원장님(기관장) 또는 피플팀장의 일정을 알리거나 바꾸는 메일 (비서실 주간 일정, 회의·행사 일시 안내 등)
- task: 담당자가 해야 할 일이 있는 메일 (요청·지시·자료 제출·검토·회신 요청 등)
- both: 둘 다
- absence: 임원의 휴가·출장·교육·병가 등 부재(자리 비움)를 알리는 메일 (부재 현황, 휴가·출장 신청·안내)
- ignore: 공지·광고·자동 발송·참고용이라 할 일이 없는 메일
task 나 both 면 해야 할 일을 tasks 에 넣는다(제목은 동사로 끝나는 한 문장, 기한은 YYYY-MM-DD — '금요일까지' 같은
상대 표현은 메일 날짜 기준, 없으면 빈 문자열, 담당자는 메일에 이름이 있을 때만, 여러 명이면 쉼표로).
schedule 이나 both 면 owner 에 누구 일정인지 넣는다: 원장님이면 director, 피플팀장이면 people (아니면 빈 문자열).
반드시 JSON 만 출력: {"kind": "schedule|task|both|absence|ignore", "owner": "director|people|", "reason": "한 줄 근거",
"tasks": [{"title": "", "due": "", "assignee": ""}]}"""


def _clean_tasks(raw) -> list[dict]:
    out = []
    for t in raw or []:
        if not isinstance(t, dict) or not str(t.get('title') or '').strip():
            continue
        due = str(t.get('due') or '').strip()[:10]
        try:
            due = date.fromisoformat(due).isoformat() if due else ''
        except ValueError:
            due = ''
        out.append({'title': str(t['title']).strip()[:300], 'due': due, 'assignee': str(t.get('assignee') or '')[:100]})
    return out[:10]


def ai_classify(subject: str, sender: str, mail_date: str, body: str) -> dict:
    raw = llm.chat(CLASSIFY_SYSTEM, f'메일 날짜: {mail_date or date.today().isoformat()}\n보낸 사람: {sender}\n'
                                    f'제목: {subject}\n\n본문:\n{body[:llm.MAX_INPUT_CHARS]}')
    data = llm._extract_json(raw)
    kind = str(data.get('kind') or '').strip().lower()
    if kind not in ('schedule', 'task', 'both', 'absence', 'ignore'):
        raise llm.LLMError(f'알 수 없는 분류: {kind}')
    owner = str(data.get('owner') or '').strip().lower()
    return {'kind': kind, 'reason': str(data.get('reason') or '')[:300], 'tasks': _clean_tasks(data.get('tasks')),
            'owner': owner if owner in schedules.OWNERS else ''}


def guess(subject: str, mail_date: str, body: str) -> dict:
    """LLM 없이 — 일정표로 읽히면 일정, 아니면 제목을 업무 후보로."""
    if schedules.table_parse(body, mail_date) or schedules.block_parse(body, mail_date) or \
            len(schedules.rule_parse(body, mail_date)) >= 2:
        return {'kind': 'schedule', 'reason': '본문에서 일정표를 찾음', 'tasks': []}
    if absences.rule_extract(body, mail_date, subject):
        return {'kind': 'absence', 'reason': '임원 이름과 휴가·출장 등 부재 낱말·날짜가 있음', 'tasks': []}
    return {'kind': 'task', 'reason': 'LLM 미설정 — 제목을 업무 후보로', 'tasks': [{'title': subject[:300], 'due': '',
                                                                                  'assignee': ''}]}


# ── 누구 일정인지 ─────────────────────────────────────────────────────
# 1) 제목에 '피플팀장'·'원장' 같은 낱말 2) 보낸 사람이 그 사람(비서) 주소 3) AI 판단 4) 본문에 한쪽 낱말만 있음
# 5) 그래도 모르면 원장님. 낱말·주소는 .env 로 더할 수 있다 (MAIL_PEOPLE_SCHEDULE_KEYWORDS 등).

_OWNER_WORDS = {'people': ['피플팀장', '피플 팀장', 'people팀장', 'people 팀장', '피플팀 팀장'],
                'director': ['원장님', '원장 ', '원장실', '기관장']}
_OWNER_ENV = {'people': 'PEOPLE_SCHEDULE', 'director': 'DIRECTOR_SCHEDULE'}


def _owner_words(owner: str) -> list[str]:
    extra = [w.strip() for w in _env(f'{_OWNER_ENV[owner]}_KEYWORDS').split(',') if w.strip()]
    return [w.lower() for w in _OWNER_WORDS[owner] + extra]


def _owner_senders(owner: str) -> list[str]:
    return [w.strip().lower() for w in _env(f'{_OWNER_ENV[owner]}_SENDERS').split(',') if w.strip()]


def schedule_owner(subject: str, sender: str, body: str, ai_owner: str = '') -> tuple[str, str]:
    """일정 메일이 누구 일정인지 → (owner, 근거)."""
    subj, frm, text = (subject or '').lower(), (sender or '').lower(), (body or '').lower()
    hits = {o: [w for w in _owner_words(o) if w in subj] for o in _OWNER_WORDS}
    if bool(hits['people']) != bool(hits['director']):
        o = 'people' if hits['people'] else 'director'
        return o, f"제목에 '{hits[o][0].strip()}'"
    for o in ('people', 'director'):
        if any(s in frm for s in _owner_senders(o)):
            return o, '보낸 사람'
    if ai_owner in schedules.OWNERS:
        return ai_owner, 'AI 판단'
    counts = {o: sum(text.count(w) for w in _owner_words(o)) for o in _OWNER_WORDS}
    if counts['people'] and not counts['director']:
        return 'people', '본문에 피플팀장'
    return schedules.DEFAULT_OWNER, '기본(원장님)'


def task_candidates(subject: str, mail_date: str, body: str) -> list[dict]:
    """규칙으로 '업무'가 정해진 메일의 업무 후보 — AI 가 있으면 뽑고, 없거나 못 찾으면 제목 하나."""
    if llm.is_configured():
        try:
            tasks = ai_classify(subject, '', mail_date, body)['tasks']
            if tasks:
                return tasks
        except Exception:  # noqa: BLE001
            pass
    return [{'title': subject[:300] or '메일 확인', 'due': '', 'assignee': ''}]


# ── 상위 업무 추천 ────────────────────────────────────────────────────
# 메일에 업무가 있으면 어느 업무 아래에 둘지 추천한다.
#   1) 같은 메일 스레드(RE:/FW:)로 전에 만든 업무가 있으면 그 업무의 상위 업무 (가장 확실)
#   2) 진행 중인 업무의 제목·태그·설명·기록 키워드가 메일 제목/본문에 얼마나 나오는지 점수
#   3) LLM 이 있으면 점수 상위 후보 중에서 AI 가 고른다 (근거 포함)
_SUBJ_PREFIX = re.compile(r'^\s*((re|fw|fwd|회신|전달|답장)\s*[:：]\s*|\[[^\]]{0,20}\]\s*)+', re.I)
_STOP = {'요청', '관련', '자료', '회신', '검토', '부탁', '드립니다', '합니다', '안내', '공유', '확인', '송부', '제출', '건의',
         '업무', '진행', '보고', '협조', '문의', '전달', '참고', '사항', '내용', '일정', '회의', '이번', '다음', '해당', '대한'}


def clean_subject(subject: str) -> str:
    """RE:/FW:/[머리말] 을 뗀 제목 — 메일로 업무를 만들 때 기본 업무 이름."""
    return re.sub(r'\s+', ' ', _SUBJ_PREFIX.sub('', subject or '')).strip()


def norm_subject(subject: str) -> str:
    return re.sub(r'\s+', ' ', _SUBJ_PREFIX.sub('', subject or '')).strip().lower()


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in re.findall(r'[가-힣A-Za-z0-9]{2,}', text or '') if t not in _STOP and not t.isdigit()}


def _thread_parent(subject: str, exclude_id: int | None = None) -> tuple[int | None, str]:
    """같은 제목(RE:/FW: 등 머리 뺀)의 이전 메일로 만든 업무 → 그 상위 업무(없으면 그 업무)."""
    key = norm_subject(subject)
    if not key:
        return None, ''
    with db.SessionLocal() as s:
        rows = list(s.scalars(select(InboundMail).where(InboundMail.task_ids != '', InboundMail.task_ids.is_not(None))
                              .order_by(InboundMail.id.desc())))
    for m in rows:
        if m.id == exclude_id or norm_subject(m.subject) != key:
            continue
        t = store.get_task(int(m.task_ids.split(',')[0]))
        if t is None:
            continue
        return t['parent_id'] or t['id'], f"같은 메일 스레드 — 이전 메일 '{m.subject[:40]}'의 업무"
    return None, ''


def _scored_candidates(subject: str, body: str, limit: int = 15) -> list[dict]:
    """진행 중인 업무별 점수 — 업무의 낱말이 메일 제목에 나오면 3배, 업무 제목의 낱말이면 2배, 긴 낱말일수록 더."""
    subj, text = (subject or '').lower(), f'{subject}\n{body}'.lower()
    out = []
    for t in store.list_tasks(status=['예정', '진행중', '보류']):
        title_toks = _tokens(t['title'])
        extra = _tokens(' '.join(t['tags']) + ' ' + t['description'][:300])
        for k, _ in store.keyword_stats(10, task_id=t['id']):
            extra |= _tokens(k)
        score, hits = 0.0, []
        for tok in title_toks | extra:
            if tok in text:
                score += (3 if tok in subj else 1) * min(len(tok), 6) / 2 * (2 if tok in title_toks else 1)
                hits.append(tok)
        if score:
            out.append({'id': t['id'], 'title': t['title'], 'score': round(score, 1), 'hits': sorted(hits)[:6]})
    return sorted(out, key=lambda c: -c['score'])[:limit]


PARENT_SYSTEM = """너는 업무 담당자의 비서다. 받은 메일에서 나온 할 일을 기존 업무 중 어디 아래(하위 업무)에 둘지 고른다.
목록에서 메일 주제와 가장 직접 관련된 업무 하나를 고르고, 마땅한 게 없으면 null.
반드시 JSON 만 출력: {"parent_id": 12 또는 null, "reason": "한 줄 근거"}"""


def ai_pick_parent(subject: str, body: str, tasks: list[dict], cands: list[dict]) -> tuple[int | None, str]:
    lines = '\n'.join(f"[{c['id']}] {c['title']}" for c in cands)
    todo = '\n'.join(f"- {t['title']}" for t in tasks) or '-'
    raw = llm.chat(PARENT_SYSTEM, f'메일 제목: {subject}\n메일에서 나온 할 일:\n{todo}\n\n메일 본문(앞부분):\n'
                                  f'{body[:3000]}\n\n업무 목록:\n{lines}')
    data = llm._extract_json(raw)
    try:
        pid = int(data.get('parent_id')) if data.get('parent_id') not in (None, '', 'null') else None
    except (TypeError, ValueError):
        pid = None
    if pid is not None and pid not in {c['id'] for c in cands}:
        pid = None  # 목록에 없는 번호는 버림
    return pid, str(data.get('reason') or '')[:200]


def suggest_parent(subject: str, body: str, tasks: list[dict], exclude_id: int | None = None) -> dict:
    """→ {'parent_task_id', 'parent_reason', 'parent_candidates': [{id,title,score,hits}]}. 추천이 없으면 id None."""
    cands = _scored_candidates(subject, body)
    pid, why = _thread_parent(subject, exclude_id)
    if pid is None and cands and llm.is_configured():
        try:
            pid, why = ai_pick_parent(subject, body, tasks, cands)
            why = f'AI: {why}' if pid else ''
        except Exception:  # noqa: BLE001
            pid, why = None, ''
    if pid is None and cands and cands[0]['score'] >= 4 and (
            len(cands) == 1 or cands[0]['score'] >= cands[1]['score'] * 1.5):  # 뚜렷이 앞선 후보만
        pid, why = cands[0]['id'], f"메일에 업무 낱말이 나옴: {', '.join(cands[0]['hits'])}"
    if pid is not None and pid not in {c['id'] for c in cands}:
        t = store.get_task(pid)
        if t:
            cands.insert(0, {'id': pid, 'title': t['title'], 'score': 0, 'hits': []})
    return {'parent_task_id': pid, 'parent_reason': why, 'parent_candidates': cands[:4]}


# ── 반영 ──────────────────────────────────────────────────────────────

def _make_schedule_import(m: dict, owner: str = schedules.DEFAULT_OWNER) -> int:
    iid = schedules.create_import(m['subject'], m['mail_date'], m['body'], MAIL_AUTHOR, owner)
    schedules.parse_import(iid)
    return iid


def _lock_mail(s, mail_id: int) -> InboundMail:
    # 쓰기 잠금으로 워커 간 반영을 직렬화한다. SQLite 에서도 동작하게 UPDATE 를 먼저 한다.
    result = s.execute(update(InboundMail).where(InboundMail.id == mail_id).values(
        task_ids=InboundMail.task_ids).execution_options(synchronize_session=False))
    if not result.rowcount:
        raise ValueError('메일을 찾을 수 없습니다.')
    return s.get(InboundMail, mail_id, populate_existing=True)


def _task_key(title: str) -> str:
    return hashlib.sha256(' '.join(title.split()).casefold().encode('utf-8')).hexdigest()


def create_tasks(mail: dict, items: list[dict], parent_id: int | None, by: str, *, session=None) -> list[int]:
    """같은 메일에서 이미 만든 제목의 업무는 유지하고, 새 제목만 원문 기록과 함께 추가한다."""
    with db.transaction(session) as s:
        m = _lock_mail(s, mail['id'])
        linked = list(_mail_dict(m)['task_ids'])
        existing = {t.source_mail_key or _task_key(t.title) for t in s.scalars(select(Task).where(
            or_(Task.source_mail_id == m.id, Task.id.in_(linked))))}
        ids = []
        for it in items:
            title = str(it.get('title') or '').strip()
            if not title or _task_key(title) in existing:
                continue
            t = store.create_todo(title, task_id=parent_id or None, due_date=str(it.get('due') or '')[:10],
                                  assignee_name=str(it.get('assignee') or ''), created_by=by,
                                  source_mail_id=m.id, source_mail_key=_task_key(title), session=s)
            _mail_history(mail, t['id'], by, session=s)
            ids.append(t['id'])
            existing.add(_task_key(title))
        m.task_ids = ','.join(map(str, dict.fromkeys(linked + ids)))
        s.flush()
        return ids


def _mail_history(mail: dict, task_id: int, by: str, *, session=None) -> None:
    """메일 원문을 업무의 '요청' 기록으로."""
    content = (f"[받은 메일] {mail['subject']}\n보낸 사람: {mail['sender']}\n날짜: {mail['mail_date']}\n\n"
               f"{mail['body'][:20000]}")
    store.add_history(task_id, content, '요청', author=sender_name(mail['sender']) or by,
                      title=mail['subject'][:200], session=session)


def _manual_done(m: dict, by: str) -> dict:
    """사람이 업무를 직접 만들거나 연결하면 — 무시·실패했던 메일은 '반영함'으로 (확인 필요는 그대로 둬서
    남은 일정 추천 등을 마저 처리할 수 있게)."""
    if m['status'] in ('ignored', 'failed', 'skipped'):
        return {'status': 'done', 'handled_at': datetime.now().replace(microsecond=0), 'handled_by': by}
    return {}


def link_task(mail_id: int, task_id: int, by: str) -> bool:
    """가져온 메일을 이미 있는 업무에 연결 — 메일 원문을 그 업무의 '요청' 기록으로 남긴다.
    이미 연결돼 있으면 False."""
    with db.transaction() as s:
        m = _mail_dict(_lock_mail(s, mail_id))
        if s.get(Task, task_id) is None:
            raise ValueError('업무를 찾을 수 없습니다.')
        if task_id in m['task_ids']:
            return False
        _mail_history(m, task_id, by, session=s)
        _update(mail_id, task_ids=','.join(map(str, m['task_ids'] + [task_id])), session=s, **_manual_done(m, by))
        return True


def create_task_from_mail(mail_id: int, title: str, parent_id: int | None, by: str, due: str = '',
                          assignee: str = '') -> int:
    """가져온 메일로 업무를 직접 하나 만든다 (분류·상태와 상관없이)."""
    with db.transaction() as s:
        m = _mail_dict(_lock_mail(s, mail_id))
        title = (title or '').strip() or clean_subject(m['subject'])
        if not title:
            raise ValueError('업무 이름을 입력하세요.')
        if parent_id and s.get(Task, parent_id) is None:
            raise ValueError('상위 업무를 찾을 수 없습니다.')
        ids = create_tasks(m, [{'title': title, 'due': due or '', 'assignee': assignee or ''}], parent_id, by, session=s)
        _update(mail_id, session=s, **_manual_done(m, by))
        if ids:
            return ids[0]
        existing = s.scalars(select(Task).where(or_(Task.source_mail_id == mail_id, Task.id.in_(m['task_ids']))))
        return next(t.id for t in existing if (t.source_mail_key or _task_key(t.title)) == _task_key(title))


def unlink_task(mail_id: int, task_id: int) -> None:
    """메일과 업무의 연결만 끊는다 — 업무와 거기 남긴 기록은 그대로."""
    with db.transaction() as s:
        if s.get(InboundMail, mail_id) is None:
            return
        m = _mail_dict(_lock_mail(s, mail_id))
        _update(mail_id, task_ids=','.join(str(t) for t in m['task_ids'] if t != task_id), session=s)
        store.clear_source_mail(task_id, mail_id, session=s)


def process(mail_id: int) -> None:
    """새 메일 한 통 처리 — 규칙 → (자동 반영 | 추천), 규칙이 없으면 AI 추천. 예외는 failed 로 남긴다."""
    m = get_mail(mail_id)
    if m is None:
        return
    try:
        rule = match_rule(m['subject'], m['sender'], m['body'])
        if rule:
            _apply_rule(m, rule)
            return
        try:
            c = ai_classify(m['subject'], m['sender'], m['mail_date'], m['body']) if llm.is_configured() else \
                guess(m['subject'], m['mail_date'], m['body'])
        except Exception as e:  # noqa: BLE001
            c = {**guess(m['subject'], m['mail_date'], m['body']), 'reason': f'AI 분류 실패({str(e)[:100]}) — 추정'}
        iid, reason = None, c['reason']
        if c['kind'] in ('schedule', 'both'):
            owner, why = schedule_owner(m['subject'], m['sender'], m['body'], c.get('owner', ''))
            iid = _make_schedule_import(m, owner)
            reason = f"{reason} · {schedules.OWNERS[owner]} 일정({why})"
        sug = {'tasks': c['tasks']}
        if c['kind'] == 'absence':
            sug['absences'], sug['absence_note'] = absences.extract(m['body'], m['mail_date'], m['subject'])
        if c['kind'] in ('task', 'both') and c['tasks']:
            sug.update(suggest_parent(m['subject'], m['body'], c['tasks'], mail_id))
        _update(mail_id, status='review', kind=c['kind'], reason=reason[:500], suggestion=sug,
                schedule_import_id=iid)
    except Exception as e:  # noqa: BLE001
        log.exception('메일 처리 실패 %s', mail_id)
        _update(mail_id, status='failed', error=str(e)[:500])


def _apply_rule(m: dict, rule: dict) -> None:
    reason = f"규칙: {rule['name']}"
    if rule['action'] == 'ignore':
        _update(m['id'], status='ignored', kind='ignore', rule_id=rule['id'], reason=reason,
                handled_at=datetime.now().replace(microsecond=0), handled_by=MAIL_AUTHOR)
        return
    if rule['action'] in RULE_OWNER:
        iid = _make_schedule_import(m, RULE_OWNER[rule['action']])
        imp = schedules.get_import(iid)
        if rule['auto'] and imp['status'] == 'done':
            r = schedules.apply_events(imp['events'], iid, MAIL_AUTHOR)
            _update(m['id'], status='auto', kind='schedule', rule_id=rule['id'], schedule_import_id=iid,
                    reason=f"{reason} — 새 일정 {r['added']}건, 바뀐 일정 {r['updated']}건",
                    handled_at=datetime.now().replace(microsecond=0), handled_by=MAIL_AUTHOR)
        else:
            _update(m['id'], status='review', kind='schedule', rule_id=rule['id'], schedule_import_id=iid,
                    reason=reason + ('' if imp['status'] == 'done' else f" — 일정 정리 실패: {imp['error']}"))
        return
    if rule['action'] == 'absence':
        rows, how = absences.extract(m['body'], m['mail_date'], m['subject'])
        sug = {'tasks': [], 'absences': rows, 'absence_note': how}
        states = {r['state'] for r in absences.preview(rows)}
        if rule['auto'] and rows and states <= {'new', 'same'}:  # 임원을 다 찾았고 겹치는 부재가 없을 때만 자동
            r = absences.apply_rows(rows, MAIL_AUTHOR, m['id'])
            _update(m['id'], status='auto', kind='absence', rule_id=rule['id'], suggestion=sug,
                    reason=f"{reason} — 임원 부재 {r['added']}건 (이미 있음 {r['same']}건)",
                    handled_at=datetime.now().replace(microsecond=0), handled_by=MAIL_AUTHOR)
        else:
            why = '' if rows else ' — 부재를 찾지 못함'
            if rows and not states <= {'new', 'same'}:
                why = ' — 임원을 못 찾았거나 겹치는 부재가 있어 확인 필요'
            _update(m['id'], status='review', kind='absence', rule_id=rule['id'], suggestion=sug, reason=reason + why)
        return
    tasks = task_candidates(m['subject'], m['mail_date'], m['body'])
    sug = {'tasks': tasks}
    if not rule['parent_task_id']:  # 규칙에 상위 업무가 없으면 추천 (자동 반영은 같은 스레드일 때만 그대로 씀)
        sug.update(suggest_parent(m['subject'], m['body'], tasks, m['id']))
    if rule['auto']:
        thread = sug.get('parent_reason', '').startswith('같은 메일 스레드')
        ids = create_tasks(m, tasks, rule['parent_task_id'] or (sug.get('parent_task_id') if thread else None),
                           MAIL_AUTHOR)
        _update(m['id'], status='auto', kind='task', rule_id=rule['id'], reason=f'{reason} — 업무 {len(ids)}건',
                suggestion=sug,
                handled_at=datetime.now().replace(microsecond=0), handled_by=MAIL_AUTHOR)
    else:
        if rule['parent_task_id']:
            sug['parent_task_id'] = rule['parent_task_id']
        _update(m['id'], status='review', kind='task', rule_id=rule['id'], reason=reason, suggestion=sug)


def apply_review(mail_id: int, tasks: list[dict], parent_id: int | None, schedule_rows: list[dict] | None,
                 by: str, absence_rows: list[dict] | None = None) -> dict:
    """확인 화면에서 반영 — 고친 업무 후보로 업무를 만들고, 고친 일정 표·임원 부재 표를 반영한다."""
    with db.transaction() as s:
        m = _mail_dict(_lock_mail(s, mail_id))
        r = {'added': 0, 'updated': 0, 'same': 0}
        if m['status'] in ('done', 'auto', 'ignored', 'skipped'):
            return {'tasks': 0, 'existing_tasks': len(m['task_ids']), 'absences': 0, **r}
        ids = create_tasks(m, tasks or [], parent_id, by, session=s)
        if schedule_rows:
            r = schedules.apply_events(schedule_rows, m['schedule_import_id'], by, session=s)
        ab = absences.apply_rows(absence_rows, by, mail_id, session=s) if absence_rows else {'added': 0}
        _update(mail_id, status='done', handled_at=datetime.now().replace(microsecond=0), handled_by=by, session=s)
        return {'tasks': len(ids), 'existing_tasks': len(m['task_ids']), **r, 'absences': ab['added']}


# ── AI 에게 맡기기 (사람이 고르는 분석) ───────────────────────────────

AI_ACTIONS = {'schedule': '일정 정리 (원장님·피플팀장)', 'absence': '임원 부재 추출', 'tasks': '할 일(업무) 추출',
              'person': '임원 정보 변경 찾기', 'summary': '요약·키워드', 'docs': '첨부 문서 요약·태그'}
LLM_ACTIONS = {'person', 'summary', 'docs'}  # LLM 이 없으면 고를 수 없는 분석


def recommend_actions(m: dict) -> list[tuple[str, str]]:
    """이 메일에 맞을 분석 추천 — [(동작, 이유)]. 받을 때의 AI 분류와 본문 모양(일정표, 임원 이름+부재 낱말)으로.
    AI 를 다시 부르지 않는다(화면을 열 때마다 돌기 때문)."""
    out: dict[str, str] = {}
    kind = m['kind']
    why = f"분류: {m['reason'][:60]}" if m['reason'] else '분류'
    if kind in ('schedule', 'both'):
        out['schedule'] = why
    if kind in ('task', 'both'):
        out['tasks'] = why
    if kind == 'absence':
        out['absence'] = why
    body, md = m['body'], m['mail_date']
    if 'schedule' not in out and (schedules.table_parse(body, md) or schedules.block_parse(body, md)):
        out['schedule'] = '본문에 일정표가 있음'
    if 'absence' not in out and absences.rule_extract(body, md, m['subject']):
        out['absence'] = '임원 이름과 휴가·출장 등 부재 낱말·날짜가 있음'
    if len(body) > 1500 and llm.is_configured():
        out.setdefault('summary', '본문이 길어 요약이 도움이 됨')
    if llm.is_configured():
        n = doc_summary.pending_count(m['id'])
        if n:
            out['docs'] = f'아직 요약하지 않은 첨부 문서 {n}개'
    return list(out.items())


def run_actions(mail_id: int, actions: list[str], by: str = '') -> dict:
    """고른 분석을 돌려 추천으로 남긴다(바로 반영하지 않음 — 화면에서 확인 후 반영).
    schedule: 일정 정리(표) / absence: 임원 부재 표 / tasks: 업무 후보 + 상위 업무 추천 / summary: 제목·요약·키워드·할 일 /
    docs: 첨부 문서 요약·태그(첨부 기록에 바로 남는다 — 이것만 고르면 메일 상태는 그대로).
    결과 {동작: 건수}. 메일은 '확인 필요'로 돌려 반영 버튼을 쓸 수 있게 한다."""
    m = get_mail(mail_id)
    if m is None:
        raise ValueError('메일을 찾을 수 없습니다.')
    actions = [a for a in actions or [] if a in AI_ACTIONS]
    if not actions:
        raise ValueError('맡길 일을 고르세요.')
    sug, done, fields = dict(m['suggestion']), {}, {}
    if 'schedule' in actions:
        old = schedules.get_import(m['schedule_import_id']) if m['schedule_import_id'] else None
        owner = old['owner'] if old else schedule_owner(m['subject'], m['sender'], m['body'])[0]
        iid = _make_schedule_import(m, owner)
        imp = schedules.get_import(iid)
        fields['schedule_import_id'] = iid
        done['schedule'] = len(imp['events']) if imp else 0
    if 'absence' in actions:
        sug['absences'], sug['absence_note'] = absences.extract(m['body'], m['mail_date'], m['subject'])
        done['absence'] = len(sug['absences'])
    if 'tasks' in actions:
        tasks = task_candidates(m['subject'], m['mail_date'], m['body'])
        sug['tasks'] = tasks
        sug['requested_tasks'] = True
        sug.update(suggest_parent(m['subject'], m['body'], tasks, mail_id))
        done['tasks'] = len(tasks)
    if 'person' in actions:
        if not llm.is_configured():
            raise ValueError('임원 정보 변경 찾기는 LLM 이 설정돼 있어야 합니다.')
        sug['person_updates'] = ai_person_updates(m)
        done['person'] = len(sug['person_updates'])
    if 'summary' in actions:
        if not llm.is_configured():
            raise ValueError('요약은 LLM 이 설정돼 있어야 합니다.')
        r = llm.analyze_record(f"제목: {m['subject']}\n보낸 사람: {m['sender']}\n\n{m['body']}", entry_type='요청')
        sug['summary'] = {k: r[k] for k in ('title', 'summary', 'keywords', 'todos')}
        if r['todos'] and 'tasks' not in actions:  # 요약에서 찾은 할 일도 업무 후보로
            sug['tasks'] = r['todos']
            sug['requested_tasks'] = True
        done['summary'] = 1
    if 'docs' in actions:
        if not llm.is_configured():
            raise ValueError('첨부 문서 요약은 LLM 이 설정돼 있어야 합니다.')
        r = doc_summary.analyze_mail(mail_id)
        done['docs'] = r['done']
        if r['skipped']:
            sug['docs_skipped'] = [f'{name} — {why}' for name, why in r['skipped']]
    sug['requested'] = actions
    reason = 'AI 분석 요청: ' + ', '.join(AI_ACTIONS[a] for a in actions)
    if actions == ['docs']:
        _update(mail_id, suggestion=sug)
    else:
        _update(mail_id, suggestion=sug, status='review', reason=reason[:500], handled_at=None, handled_by='',
                **fields)
    return done


# ── 항목별 처리 (추천을 하나씩 처리하고, 끝나면 '처리 완료') ──────────────
# suggestion['done'] = {'tasks'|'schedule'|'absence'|'trend'|'person': {'n': 건수, 'at': 시각, 'by': 사람}}

HANDLED = {'tasks': '업무로 등록', 'schedule': '일정에 반영', 'absence': '부재 일정에 등록', 'trend': '임원 동향으로 기록',
           'person': '임원 정보에 반영'}


def _mark(mail_id: int, key: str, n: int, by: str, **extra) -> None:
    m = get_mail(mail_id)
    sug = dict(m['suggestion'])
    done = dict(sug.get('done') or {})
    prev = done.get(key, {}).get('n', 0)
    done[key] = {'n': prev + n, 'at': datetime.now().strftime('%Y-%m-%d %H:%M'), 'by': by}
    sug['done'] = done
    sug.update(extra)
    _update(mail_id, suggestion=sug)


def add_tasks(mail_id: int, items: list[dict], parent_id: int | None, by: str) -> list[int]:
    """업무 후보 표 → 업무로 등록 (메일 상태는 그대로 — 다 처리했으면 '처리 완료')."""
    m = get_mail(mail_id)
    if m is None:
        raise ValueError('메일을 찾을 수 없습니다.')
    if not any(str(it.get('title') or '').strip() for it in items or []):
        raise ValueError('등록할 업무가 없습니다 — 표에 업무 이름을 적으세요.')
    ids = create_tasks(m, items or [], parent_id, by)  # 같은 메일에서 이미 만든 제목은 건너뛴다
    if not ids:
        raise ValueError('표의 업무는 이 메일로 이미 등록했습니다.')
    _mark(mail_id, 'tasks', len(ids), by)
    return ids


def set_schedule_owner(mail_id: int, owner: str) -> bool:
    """메일의 일정이 누구 일정인지 바꾼다 (반영 전에만). 바뀌었으면 True."""
    m = get_mail(mail_id)
    if m is None or not m['schedule_import_id']:
        raise ValueError('이 메일에는 정리된 일정이 없습니다.')
    owner = schedules.owner_of(owner)
    imp = schedules.get_import(m['schedule_import_id'])
    if imp is None or imp['owner'] == owner:
        return False
    if imp['applied'] or (m['suggestion'].get('done') or {}).get('schedule'):
        raise ValueError(f"이미 {schedules.OWNERS[imp['owner']]} 일정에 반영했습니다 — 캘린더에서 지우고 다시 반영하세요.")
    schedules.set_import_owner(imp['id'], owner)
    return True


def apply_schedule(mail_id: int, rows: list[dict], by: str) -> dict:
    """일정 표 → 그 사람(원장님·피플팀장) 일정에 반영 (기존 일정과 대조해 정말 바뀐 칸만)."""
    m = get_mail(mail_id)
    if m is None:
        raise ValueError('메일을 찾을 수 없습니다.')
    r = schedules.apply_events(rows or [], m['schedule_import_id'], by)
    _mark(mail_id, 'schedule', r['added'] + r['updated'], by)
    return r


def record_trend(mail_id: int, person_ids: list[int], by: str, task_id: int | None = None) -> dict:
    """메일을 임원 동향으로 기록 — 고른 임원(과 업무)에 연결된 '동향' 이력. AI 가 있으면 요약·키워드를 붙인다."""
    from services import enrich
    m = get_mail(mail_id)
    if m is None:
        raise ValueError('메일을 찾을 수 없습니다.')
    if not person_ids:
        raise ValueError('동향을 남길 임원을 고르세요.')
    content = (f"[받은 메일] {m['subject']}\n보낸 사람: {m['sender']}\n날짜: {m['mail_date']}\n\n"
               f"{m['body'][:20000]}")
    h = store.add_history(task_id or None, content, '동향', author=sender_name(m['sender']) or by,
                          occurred_at=f"{m['mail_date']} 09:00" if m['mail_date'] else None,
                          title=m['subject'][:200], person_ids=person_ids)
    enrich.submit(h['id'])
    _mark(mail_id, 'trend', 1, by, trend_history_id=h['id'])
    return h


PERSON_FIELDS = {'직위': 'position', '소속': 'org', '별칭': 'aliases', '메모': 'note'}

PERSON_SYSTEM = """너는 임원 정보를 관리하는 비서다. 메일에서 임원 정보가 바뀌거나 새로 알게 된 사실만 뽑는다
(승진·보임·전보로 바뀐 직위나 소속, 새 직책, 겸직, 수상, 학위, 연락처 등).
- person: 임원 이름(직위 빼고). field: 직위 / 소속 / 별칭 / 메모 중 하나, 그 밖이면 짧은 항목 이름(예: 겸직, 수상, 학위).
- value: 바뀐 뒤의 값(메모면 한 줄로 남길 내용). reason: 메일에서 근거가 된 구절(짧게).
- 메일에 없는 내용은 만들지 않는다. 임원 정보와 상관없는 내용(회의 일정, 업무 요청)은 뽑지 않는다.
반드시 JSON 만 출력: {"updates": [{"person": "", "field": "", "value": "", "reason": ""}]}"""


def ai_person_updates(m: dict) -> list[dict]:
    names = ', '.join(f"{p['name']}({p['position']}, {p['org']})" for p in store.list_people())[:3000]
    raw = llm.chat(PERSON_SYSTEM, f"등록된 임원(이름(직위, 소속)): {names}\n\n메일 날짜: {m['mail_date']}\n"
                                  f"제목: {m['subject']}\n\n본문:\n{m['body'][:llm.MAX_INPUT_CHARS]}")
    out = []
    for u in llm._extract_json(raw).get('updates') or []:
        if isinstance(u, dict) and str(u.get('person') or '').strip() and str(u.get('value') or '').strip():
            out.append({'person': str(u['person']).strip()[:50], 'field': str(u.get('field') or '메모').strip()[:40],
                        'value': str(u['value']).strip()[:500], 'reason': str(u.get('reason') or '').strip()[:200]})
    return out[:20]


def person_update_rows(rows: list[dict]) -> list[dict]:
    """임원 정보 변경 표 — 임원을 찾고 지금 값을 붙인다."""
    from services import profile
    out = []
    for r in rows or []:
        pid, pname = absences.match_person(r.get('person', ''))
        field = (r.get('field') or '메모').strip()
        current = ''
        if pid:
            p = store.get_person(pid)
            if field in PERSON_FIELDS:
                v = p.get(PERSON_FIELDS[field])
                current = ', '.join(v) if isinstance(v, list) else (v or '')
            else:
                current = next((a['value'] for a in profile.list_attributes(pid) if a['name'] == field), '')
        out.append({'person': r.get('person', ''), 'field': field, 'current': current[:200],
                    'value': r.get('value', ''), 'reason': r.get('reason', ''),
                    'state': 'unknown' if pid is None else ('same' if current == (r.get('value') or '') else 'new'),
                    'person_id': pid or ''})
    return out


def apply_person_updates(mail_id: int, rows: list[dict], by: str) -> dict:
    """임원 정보에 반영 — 직위·소속은 바꾸고, 별칭은 더하고, 메모는 끝에 덧붙이고(날짜·메일 제목과 함께),
    그 밖의 항목은 '추가 항목'으로. 임원을 못 찾은 줄·값이 같은 줄은 건너뛴다."""
    from services import profile
    m = get_mail(mail_id)
    if m is None:
        raise ValueError('메일을 찾을 수 없습니다.')
    changed = skipped = 0
    for r in person_update_rows(rows):
        value = str(r['value'] or '').strip()
        if not r['person_id'] or not value or r['state'] == 'same':
            skipped += 1
            continue
        pid, field = int(r['person_id']), r['field']
        if field in ('직위', '소속'):
            store.update_person(pid, **{PERSON_FIELDS[field]: value})
        elif field == '별칭':
            p = store.get_person(pid)
            store.update_person(pid, aliases=list(dict.fromkeys(p['aliases'] + [value])))
        elif field == '메모':
            p = store.get_person(pid)
            line = f"[{m['mail_date']}] {value} (메일: {m['subject'][:60]})"
            store.update_person(pid, note=(p['note'] + '\n' if p['note'] else '') + line)
        else:
            with db.SessionLocal() as s:
                profile.upsert_attribute(s, pid, field[:100], value)
                s.commit()
        changed += 1
    if changed:
        _mark(mail_id, 'person', changed, by)
    return {'changed': changed, 'skipped': skipped}


def complete(mail_id: int, by: str) -> None:
    """처리 완료 — '확인 필요'에서 빠진다."""
    _update(mail_id, status='done', handled_at=datetime.now().replace(microsecond=0), handled_by=by)


def pending_items(m: dict) -> list[str]:
    """추천은 있는데 아직 처리하지 않은 항목 이름 (처리 완료 전에 알려 주기)."""
    sug, done = m['suggestion'], (m['suggestion'].get('done') or {})
    has = {'tasks': bool(sug.get('tasks')) and (m['kind'] in ('task', 'both') or sug.get('requested_tasks')),
           'schedule': bool(m['schedule_import_id']), 'absence': bool(sug.get('absences')),
           'person': bool(sug.get('person_updates'))}
    return [HANDLED[k] for k, v in has.items() if v and k not in done]


def extract_absences(mail_id: int) -> list[dict]:
    """어떤 메일이든 — 임원 부재를 뽑아 추천으로 남긴다 (받은 메일 화면의 '임원 부재 찾기')."""
    m = get_mail(mail_id)
    if m is None:
        raise ValueError('메일을 찾을 수 없습니다.')
    rows, how = absences.extract(m['body'], m['mail_date'], m['subject'])
    _update(mail_id, suggestion={**m['suggestion'], 'absences': rows, 'absence_note': how})
    return rows


def apply_absences(mail_id: int, rows: list[dict], by: str) -> dict:
    """부재 표 → 부재 일정에 등록 (메일 상태는 그대로 — 다 처리했으면 '처리 완료')."""
    m = get_mail(mail_id)
    if m is None:
        raise ValueError('메일을 찾을 수 없습니다.')
    r = absences.apply_rows(rows or [], by, mail_id)
    _mark(mail_id, 'absence', r['added'], by)
    return r


def ignore(mail_id: int, by: str) -> None:
    _update(mail_id, status='ignored', handled_at=datetime.now().replace(microsecond=0), handled_by=by)


def reopen(mail_id: int) -> None:
    _update(mail_id, status='review', handled_at=None, handled_by='')


# ── POP3 가져오기 ─────────────────────────────────────────────────────

def _connect(cfg: dict):
    cls = poplib.POP3_SSL if cfg['ssl'] else poplib.POP3
    conn = cls(cfg['host'], cfg['port'], timeout=30)
    conn.user(cfg['user'])
    conn.pass_(cfg['password'])
    return conn


def fetch_new(connect=None, process_now: bool = True) -> dict:
    """새 메일을 가져와 처리한다 → {'new','skipped','processed'}. 서버의 메일은 기본으로 지우지 않는다."""
    cfg = settings()
    conn = (connect or _connect)(cfg)
    new_ids, skipped = [], 0
    try:
        _, listing, _ = conn.uidl()
        pairs = []
        for line in listing:
            num, uid = line.decode('utf-8', 'replace').split(None, 1)
            pairs.append((int(num), uid.strip()))
        # 이전 버전이 한도 초과분에 남긴 빈 skipped 행은 실제로 읽은 메일이 아니다.
        deferred_conditions = (InboundMail.status == 'skipped', func.coalesce(InboundMail.subject, '') == '',
                               func.coalesce(InboundMail.mail_date, '') == '')
        with db.SessionLocal() as s:
            known = set(s.scalars(select(InboundMail.uidl)))
            deferred = dict(s.execute(select(InboundMail.uidl, InboundMail.id).where(*deferred_conditions)).all())
        todo = [(n, u) for n, u in pairs if u not in known or u in deferred]
        since = (date.today() - timedelta(days=cfg['since_days'])).isoformat() if cfg['since_days'] > 0 else ''
        for num, uid in todo[-cfg['max_per_run']:] if cfg['max_per_run'] > 0 else todo:  # 가장 최근 것들
            _, lines, _ = conn.retr(num)
            raw = b'\r\n'.join(lines)
            parsed = mail_text.from_bytes(raw)
            old = since and parsed['mail_date'] and parsed['mail_date'] < since
            with db.SessionLocal() as s:
                fields = dict(message_id=parsed['message_id'][:300], subject=parsed['name'][:500],
                              sender=parsed['sender'][:300], mail_date=parsed['mail_date'],
                              body='' if old else parsed['text'], status='skipped' if old else 'review')
                if uid in deferred:
                    mid = deferred[uid]
                    result = s.execute(update(InboundMail).where(InboundMail.id == mid, *deferred_conditions)
                                       .values(**fields))
                    if not result.rowcount:  # 다른 워커가 이미 복원한 경우
                        s.rollback()
                        continue
                else:
                    m = InboundMail(uidl=uid, **fields)
                    s.add(m)
                try:
                    s.commit()
                except IntegrityError:  # 다른 프로세스가 방금 같은 메일을 넣었다
                    s.rollback()
                    continue
                if uid not in deferred:
                    mid = m.id
            if old:
                skipped += 1
            else:
                new_ids.append(mid)
                try:  # 첨부 파일 저장 — 문서별 판(버전) 묶기는 services/doc_versions.py
                    if doc_versions.save(mid, mail_text.attachments(raw, cfg['max_attachment_mb'] * 1024 * 1024)):
                        doc_summary.read_mail(mid)  # 글자만 뽑아 둔다 — AI 요약은 'AI 에게 맡기기'에서
                except Exception:  # noqa: BLE001 — 첨부 때문에 메일을 놓치지 않게
                    log.exception('첨부 저장 실패 %s', mid)
            if cfg['delete']:
                conn.dele(num)
        # 수집 한도를 넘은 UIDL 은 저장하지 않는다. 다음 실행에서 이어 받고,
        # MAIL_SINCE_DAYS 보다 오래됐는지는 실제 메일 날짜를 읽은 뒤 판단한다.
    finally:
        try:
            conn.quit()
        except Exception:  # noqa: BLE001
            pass
    if process_now:
        for mid in new_ids:
            process(mid)
    return {'new': len(new_ids), 'skipped': skipped, 'processed': len(new_ids) if process_now else 0}


# ── 주기 실행 ─────────────────────────────────────────────────────────

_STATUS_FILE = os.path.join(db.DATA_DIR, '.mail_poll_status.json')
_started = False


def last_run() -> dict:
    try:
        with open(_STATUS_FILE, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def run_once() -> dict:
    """한 번 가져오기 — 결과를 상태 파일에 남긴다 (화면의 '마지막 확인'). 주기 실행과 '지금 가져오기'가 겹치지 않게
    파일 잠금으로 한 번에 하나만 돈다(겹치면 busy)."""
    at = datetime.now().strftime('%Y-%m-%d %H:%M')
    os.makedirs(db.DATA_DIR, exist_ok=True)
    with open(os.path.join(db.DATA_DIR, '.mail_fetch.lock'), 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return {'at': at, 'ok': True, 'busy': True, 'new': 0, 'skipped': 0, 'processed': 0}
        try:
            r = fetch_new()
            st = {'at': at, 'ok': True, **r}
        except Exception as e:  # noqa: BLE001
            log.warning('메일 가져오기 실패: %s', e)
            st = {'at': at, 'ok': False, 'error': f'{type(e).__name__}: {str(e)[:300]}'}
    try:
        os.makedirs(db.DATA_DIR, exist_ok=True)
        with open(_STATUS_FILE, 'w', encoding='utf-8') as f:
            json.dump(st, f, ensure_ascii=False)
    except OSError:
        pass
    return st


def start_poller() -> bool:
    """MAIL_POP3_* 가 설정돼 있으면 백그라운드에서 주기적으로 가져온다. gunicorn 워커가 여럿이어도
    파일 잠금을 잡은 프로세스 하나만 돈다. 시작했으면 True."""
    global _started
    if _started or not is_configured():
        return False
    try:
        os.makedirs(db.DATA_DIR, exist_ok=True)
        lock = open(os.path.join(db.DATA_DIR, '.mail_poll.lock'), 'w')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False  # 다른 워커가 이미 돌고 있다
    _started = True

    def loop():
        time.sleep(10)  # 앱이 다 뜬 뒤
        while True:
            run_once()
            time.sleep(settings()['minutes'] * 60)

    threading.Thread(target=loop, name='mail-poller', daemon=True).start()
    lock_holder.append(lock)  # 프로세스가 사는 동안 잠금 유지
    return True


lock_holder: list = []
