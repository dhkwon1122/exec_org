"""받은 메일 자동 처리 — POP3 로 새 메일을 가져와 일정·업무로 반영한다.

흐름 (메일 한 통):
  1) 분류 규칙(mail_rules: 보낸 사람/제목/본문에 글자 포함 → 일정·업무·무시)에 맞으면 그 동작.
     규칙이 '자동'이면 확인 없이 바로 반영(status=auto), 아니면 추천만(review).
  2) 맞는 규칙이 없으면 AI 가 '원장님 일정 / 업무 / 둘 다 / 무시'로 나누고 업무 후보를 뽑아 추천(review).
     LLM 이 없으면 일정표가 보이면 일정, 아니면 제목을 업무 후보로.
  3) '받은 메일' 화면에서 추천을 고쳐 반영하거나 무시한다.
일정은 원장님 일정 가져오기(schedules)와 같은 길로 정리·반영하고, 업무는 하위 업무로 만들면서 메일 원문을
그 업무의 '요청' 기록으로 남긴다. POP3 고유 번호(UIDL)로 같은 메일을 두 번 가져오지 않는다.
"""
import fcntl
import json
import logging
import os
import poplib
import re
import threading
import time
from datetime import date, datetime, timedelta
from email.utils import parseaddr

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from services import db, llm, mail_text, schedules, store
from services.db import InboundMail, MailRule

log = logging.getLogger(__name__)
ACTIONS = {'schedule': '원장님 일정', 'task': '업무', 'ignore': '무시'}
FIELDS = {'sender': '보낸 사람', 'subject': '제목', 'body': '본문'}
STATUS = {'auto': '자동 반영', 'review': '확인 필요', 'done': '반영함', 'ignored': '무시', 'failed': '실패',
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


def _update(mail_id: int, **fields):
    with db.SessionLocal() as s:
        m = s.get(InboundMail, mail_id)
        for k, v in fields.items():
            setattr(m, k, json.dumps(v, ensure_ascii=False) if k == 'suggestion' else v)
        s.commit()


def sender_name(sender: str) -> str:
    name, addr = parseaddr(sender or '')
    return name or addr or (sender or '')


# ── 분류 ──────────────────────────────────────────────────────────────

CLASSIFY_SYSTEM = """너는 임원조직 담당자의 메일을 정리하는 비서다. 받은 메일을 읽고 무엇으로 처리할지 정한다.
- schedule: 원장님(기관장) 일정을 알리거나 바꾸는 메일 (비서실 주간 일정, 회의·행사 일시 안내 등)
- task: 담당자가 해야 할 일이 있는 메일 (요청·지시·자료 제출·검토·회신 요청 등)
- both: 둘 다
- ignore: 공지·광고·자동 발송·참고용이라 할 일이 없는 메일
task 나 both 면 해야 할 일을 tasks 에 넣는다(제목은 동사로 끝나는 한 문장, 기한은 YYYY-MM-DD — '금요일까지' 같은
상대 표현은 메일 날짜 기준, 없으면 빈 문자열, 담당자는 메일에 이름이 있을 때만, 여러 명이면 쉼표로).
반드시 JSON 만 출력: {"kind": "schedule|task|both|ignore", "reason": "한 줄 근거",
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
    if kind not in ('schedule', 'task', 'both', 'ignore'):
        raise llm.LLMError(f'알 수 없는 분류: {kind}')
    return {'kind': kind, 'reason': str(data.get('reason') or '')[:300], 'tasks': _clean_tasks(data.get('tasks'))}


def guess(subject: str, mail_date: str, body: str) -> dict:
    """LLM 없이 — 일정표로 읽히면 일정, 아니면 제목을 업무 후보로."""
    if schedules.table_parse(body, mail_date) or schedules.block_parse(body, mail_date) or \
            len(schedules.rule_parse(body, mail_date)) >= 2:
        return {'kind': 'schedule', 'reason': '본문에서 일정표를 찾음', 'tasks': []}
    return {'kind': 'task', 'reason': 'LLM 미설정 — 제목을 업무 후보로', 'tasks': [{'title': subject[:300], 'due': '',
                                                                                  'assignee': ''}]}


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

def _make_schedule_import(m: dict) -> int:
    iid = schedules.create_import(m['subject'], m['mail_date'], m['body'], MAIL_AUTHOR)
    schedules.parse_import(iid)
    return iid


def create_tasks(mail: dict, items: list[dict], parent_id: int | None, by: str) -> list[int]:
    """업무 후보 → 업무(상위 업무가 있으면 그 하위). 메일 원문은 각 업무의 '요청' 기록으로 남긴다."""
    ids = []
    for it in items:
        title = str(it.get('title') or '').strip()
        if not title:
            continue
        t = store.create_todo(title, task_id=parent_id or None, due_date=str(it.get('due') or '')[:10],
                              assignee_name=str(it.get('assignee') or ''), created_by=by,
                              source_mail_id=mail.get('id'))
        content = (f"[받은 메일] {mail['subject']}\n보낸 사람: {mail['sender']}\n날짜: {mail['mail_date']}\n\n"
                   f"{mail['body'][:20000]}")
        store.add_history(t['id'], content, '요청', author=sender_name(mail['sender']) or by,
                          title=mail['subject'][:200])
        ids.append(t['id'])
    return ids


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
        iid = _make_schedule_import(m) if c['kind'] in ('schedule', 'both') else None
        sug = {'tasks': c['tasks']}
        if c['kind'] in ('task', 'both') and c['tasks']:
            sug.update(suggest_parent(m['subject'], m['body'], c['tasks'], mail_id))
        _update(mail_id, status='review', kind=c['kind'], reason=c['reason'], suggestion=sug,
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
    if rule['action'] == 'schedule':
        iid = _make_schedule_import(m)
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
    tasks = task_candidates(m['subject'], m['mail_date'], m['body'])
    sug = {'tasks': tasks}
    if not rule['parent_task_id']:  # 규칙에 상위 업무가 없으면 추천 (자동 반영은 같은 스레드일 때만 그대로 씀)
        sug.update(suggest_parent(m['subject'], m['body'], tasks, m['id']))
    if rule['auto']:
        thread = sug.get('parent_reason', '').startswith('같은 메일 스레드')
        ids = create_tasks(m, tasks, rule['parent_task_id'] or (sug.get('parent_task_id') if thread else None),
                           MAIL_AUTHOR)
        _update(m['id'], status='auto', kind='task', rule_id=rule['id'], reason=f'{reason} — 업무 {len(ids)}건',
                suggestion=sug, task_ids=','.join(map(str, ids)),
                handled_at=datetime.now().replace(microsecond=0), handled_by=MAIL_AUTHOR)
    else:
        if rule['parent_task_id']:
            sug['parent_task_id'] = rule['parent_task_id']
        _update(m['id'], status='review', kind='task', rule_id=rule['id'], reason=reason, suggestion=sug)


def apply_review(mail_id: int, tasks: list[dict], parent_id: int | None, schedule_rows: list[dict] | None,
                 by: str) -> dict:
    """확인 화면에서 반영 — 고친 업무 후보로 업무를 만들고, 고친 일정 표를 반영한다."""
    m = get_mail(mail_id)
    if m is None:
        raise ValueError('메일을 찾을 수 없습니다.')
    ids = create_tasks(m, tasks or [], parent_id, by)
    r = {'added': 0, 'updated': 0, 'same': 0}
    if schedule_rows:
        r = schedules.apply_events(schedule_rows, m['schedule_import_id'], by)
    _update(mail_id, status='done', task_ids=','.join(map(str, m['task_ids'] + ids)),
            handled_at=datetime.now().replace(microsecond=0), handled_by=by)
    return {'tasks': len(ids), **r}


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
        with db.SessionLocal() as s:
            known = set(s.scalars(select(InboundMail.uidl)))
        todo = [(n, u) for n, u in pairs if u not in known]
        since = (date.today() - timedelta(days=cfg['since_days'])).isoformat() if cfg['since_days'] > 0 else ''
        for num, uid in todo[-cfg['max_per_run']:] if cfg['max_per_run'] > 0 else todo:  # 가장 최근 것들
            _, lines, _ = conn.retr(num)
            parsed = mail_text.from_bytes(b'\r\n'.join(lines))
            old = since and parsed['mail_date'] and parsed['mail_date'] < since
            with db.SessionLocal() as s:
                m = InboundMail(uidl=uid, message_id=parsed['message_id'][:300], subject=parsed['name'][:500],
                                sender=parsed['sender'][:300], mail_date=parsed['mail_date'],
                                body='' if old else parsed['text'], status='skipped' if old else 'review')
                s.add(m)
                try:
                    s.commit()
                except IntegrityError:  # 다른 프로세스가 방금 같은 메일을 넣었다
                    s.rollback()
                    continue
                mid = m.id
            if old:
                skipped += 1
            else:
                new_ids.append(mid)
            if cfg['delete']:
                conn.dele(num)
        # 처음 연결할 때 너무 오래된 메일까지 받지 않도록, 이번에 못 받은(최대 건수 밖) 옛 메일은 건너뜀으로 기록
        for num, uid in (todo[:-cfg['max_per_run']] if cfg['max_per_run'] > 0 else []):
            with db.SessionLocal() as s:
                s.add(InboundMail(uidl=uid, status='skipped'))
                try:
                    s.commit()
                    skipped += 1
                except IntegrityError:
                    s.rollback()
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

