"""메일 보내기 — 받는 사람 정리, 첨부 모으기, 발송(services/mailer.py)과 보낸 메일 기록(outbound_mails).

화면마다 보낼 내용의 초안을 만든다(draft_*): 업무 공유, 원장님 일정, 임원 부재 현황, 받은 메일 회신.
초안 = {'subject', 'text'(고칠 수 있는 본문), 'table_html'(함께 넣을 표, 고치지 않음), 'to', 'attachments'
(고를 수 있는 첨부 [{'value': 'ref:ID'|'att:ID', 'label'}]), 'related_kind', 'related_id'}.
"""
import html
import re
from datetime import date

from email.utils import getaddresses
from sqlalchemy import select

from services import absences, db, doc_versions, mailer, schedules, store, users
from services.db import InboundMail, OutboundMail

_ADDR = re.compile(r'^[\w.+\-]+@[\w\-]+(\.[\w\-]+)+$')
_CELL = 'border:1px solid #d7d9df;padding:4px 8px;font-size:13px;vertical-align:top'
_HEAD = _CELL + ';background:#f3f4f6;font-weight:600'


def parse_addrs(text: str | list | None) -> tuple[list[str], list[str]]:
    """'홍길동 <a@x.com>, b@x.com; c@x' → (올바른 주소, 틀린 것). 같은 주소는 한 번만."""
    if isinstance(text, list):
        text = ','.join(t for t in text if t)
    good, bad = [], []
    # email.utils.getaddresses 는 빈 칸(끝 쉼표 등)이 하나라도 있으면 전체를 버려서 직접 나눈다
    for piece in re.split(r'[,;\n]', text or ''):
        m = re.search(r'<([^<>]+)>', piece)
        a = (m.group(1) if m else piece).strip().strip('"\'')
        if not a:
            continue
        (good if _ADDR.match(a) else bad).append(a)
    return list(dict.fromkeys(good)), bad


def self_address(user: dict | None) -> str:
    """받는 사람을 비우면 보낼 본인 주소 — 사용자 DB 의 메일, 없으면 로그인 ID@MAIL_DEFAULT_DOMAIN."""
    if not user:
        return ''
    try:
        u = users.get_user(user['user_id'])
    except Exception:  # noqa: BLE001
        u = None
    if u and u.get('email'):
        return u['email']
    import os
    return f"{user['user_id']}@{os.environ.get('MAIL_DEFAULT_DOMAIN', 'samsung.com').strip() or 'samsung.com'}"


def address_book(limit: int = 300) -> list[dict]:
    """받는 사람 고르기 — 사용자(메일 있는 사람), 전에 보낸 주소, 받은 메일의 보낸 사람."""
    seen: dict[str, str] = {}
    try:
        for u in users.list_users():
            if u.get('email'):
                seen.setdefault(u['email'], u.get('name') or u.get('display_name') or '')
    except Exception:  # noqa: BLE001 — 사용자 DB 에 못 붙어도 나머지로
        pass
    with db.SessionLocal() as s:
        for to, cc in s.execute(select(OutboundMail.to_addrs, OutboundMail.cc_addrs)
                                .order_by(OutboundMail.id.desc()).limit(200)):
            for a in parse_addrs(f'{to},{cc}')[0]:
                seen.setdefault(a, '')
        for (sender,) in s.execute(select(InboundMail.sender).where(InboundMail.sender != '')
                                   .order_by(InboundMail.id.desc()).limit(500)):
            for name, a in getaddresses([sender or '']):
                if _ADDR.match(a or ''):
                    seen.setdefault(a, name)
    return [{'label': f'{n} <{a}>' if n else a, 'value': a} for a, n in list(seen.items())[:limit]]


def text_to_html(text: str) -> str:
    return ('<div style="font-size:14px;line-height:1.6">'
            + html.escape(text or '').replace('\n', '<br>') + '</div>')


def _table(headers: list[str], rows: list[list[str]]) -> str:
    head = ''.join(f'<th style="{_HEAD}">{html.escape(h)}</th>' for h in headers)
    body = ''.join('<tr>' + ''.join(f'<td style="{_CELL}">{html.escape(str(c or ""))}</td>' for c in r) + '</tr>'
                   for r in rows)
    return f'<table style="border-collapse:collapse;margin:8px 0">{"<tr>" + head + "</tr>"}{body}</table>'


def _load_attachments(keys: list[str]) -> list[tuple[str, bytes]]:
    out = []
    for k in keys or []:
        kind, _, sid = str(k).partition(':')
        if not sid.isdigit():
            continue
        if kind == 'ref':
            r = store.get_reference(int(sid))
            path = store.upload_abspath(r['file_path']) if r and r['kind'] == 'file' else None
            name = r['file_name'] if r else ''
        elif kind == 'att':
            a = doc_versions.get(int(sid))
            path = store.upload_abspath(a['file_path']) if a else None
            name = a['filename'] if a else ''
        else:
            continue
        if not path:
            raise ValueError(f'첨부 파일을 찾을 수 없습니다: {k}')
        with open(path, 'rb') as f:
            out.append((name or f'file_{sid}', f.read()))
    return out


def send(to, subject: str, text: str = '', table_html: str = '', cc=None, attachments: list[str] | None = None,
         related_kind: str = '', related_id: str = '', by: str = '', default_to: str = '') -> dict:
    """메일 보내기 → 보낸 기록. 받는 사람이 비어 있으면 default_to(본인). 주소가 틀리면 ValueError,
    발송이 실패하면 기록을 'failed' 로 남기고 mailer.MailError."""
    to_list, bad = parse_addrs(to)
    cc_list, bad_cc = parse_addrs(cc)
    if bad or bad_cc:
        raise ValueError('메일 주소를 확인하세요: ' + ', '.join(bad + bad_cc))
    if not to_list and default_to:
        to_list = [default_to]
    if not to_list:
        raise ValueError('받는 사람을 입력하세요.')
    subject = (subject or '').strip()
    if not subject:
        raise ValueError('제목을 입력하세요.')
    files = _load_attachments(attachments or [])
    body = text_to_html(text) + (table_html or '')
    rec = OutboundMail(to_addrs=', '.join(to_list), cc_addrs=', '.join(cc_list), subject=subject[:500],
                       body_html=body, attachments=', '.join(n for n, _ in files), related_kind=related_kind or '',
                       related_id=str(related_id or ''), sent_by=by)
    err = None
    try:
        mailer.send_html_email(to_list, subject, body, attachments=files or None, cc=cc_list)
    except mailer.MailError as e:
        err = e
        rec.status, rec.error = 'failed', str(e)[:1000]
    with db.SessionLocal() as s:
        s.add(rec)
        s.commit()
        out = _dict(rec)
    if err:
        raise err
    return out


def _dict(m: OutboundMail) -> dict:
    return {'id': m.id, 'to': m.to_addrs or '', 'cc': m.cc_addrs or '', 'subject': m.subject or '',
            'body_html': m.body_html or '', 'attachments': m.attachments or '', 'status': m.status,
            'error': m.error or '', 'related_kind': m.related_kind or '', 'related_id': m.related_id or '',
            'sent_by': m.sent_by or '', 'created_at': m.created_at.strftime('%Y-%m-%d %H:%M') if m.created_at else ''}


def list_sent(limit: int = 200, related_kind: str = '', related_id: str = '') -> list[dict]:
    with db.SessionLocal() as s:
        stmt = select(OutboundMail)
        if related_kind:
            stmt = stmt.where(OutboundMail.related_kind == related_kind)
        if related_id:
            stmt = stmt.where(OutboundMail.related_id == str(related_id))
        return [_dict(m) for m in s.scalars(stmt.order_by(OutboundMail.id.desc()).limit(limit))]


def get_sent(mid: int) -> dict | None:
    with db.SessionLocal() as s:
        m = s.get(OutboundMail, mid)
        return _dict(m) if m else None


# ── 초안 ──────────────────────────────────────────────────────────────

def draft_task(task_id: int) -> dict:
    """업무 공유 — 상태·담당·기간·설명, 하위 업무, 최근 기록 요약. 첨부는 이 업무의 파일과 메일 첨부(최신 판)."""
    t = store.get_task(task_id)
    if t is None:
        raise ValueError('업무를 찾을 수 없습니다.')
    who = ', '.join(a['name'] for a in t['assignees']) or '-'
    period = ' ~ '.join(x for x in [t['start_date'], t['due_date']] if x) or '-'
    lines = [f"[업무] {t['title']}", f"상태: {t['status']} · 담당: {who} · 기간: {period}"]
    if t['description']:
        lines += ['', t['description']]
    subs = store.list_todos(task_id=task_id)
    if subs:
        lines += ['', f"하위 업무 ({sum(x['done'] for x in subs)}/{len(subs)} 완료)"]
        lines += [f"  {'☑' if x['done'] else '☐'} {x['title']}" + (f" (~{x['due_date']})" if x['due_date'] else '')
                  + (f" — {', '.join(a['name'] for a in x['assignees'])}" if x['assignees'] else '') for x in subs]
    hist = [h for h in store.list_history(task_id) if h['entry_type'] not in store.AUTO_ENTRY_TYPES][:5]
    if hist:
        lines += ['', '최근 기록']
        lines += [f"  · {h['occurred_at'][:10]} [{h['entry_type']}] {h['title'] or h['content'][:60]}"
                  + (f" — {h['summary'][:120]}" if h['summary'] else '') for h in hist]
    atts = [{'value': f"ref:{r['id']}", 'label': f"참고자료 · {r['file_name'] or r['title']}"}
            for r in store.list_references(task_id=task_id) if r['kind'] == 'file']
    atts += [{'value': f"att:{g['latest']['id']}", 'label': f"메일 첨부 · {g['latest']['filename']} "
              f"({g['latest']['version']}판, 최신)"} for g in doc_versions.for_task(task_id)]
    return {'subject': f"[업무 공유] {t['title']}", 'text': '\n'.join(lines), 'table_html': '', 'to': '',
            'attachments': atts, 'related_kind': 'task', 'related_id': str(task_id)}


def draft_schedule(date_from: str, date_to: str, owner: str = schedules.DEFAULT_OWNER) -> dict:
    """원장님(또는 피플팀장) 일정 — 기간의 일정을 표로."""
    rows = schedules.list_range(date_from, date_to, owner)
    who = schedules.OWNERS[schedules.owner_of(owner)]
    wd = '월화수목금토일'
    table = _table(['날짜', '시간', '일정', '장소', '참석자', '세부'], [
        [f"{r['date'][5:].replace('-', '/')}({wd[date.fromisoformat(r['date']).weekday()]})"
         + (f"~{r['end_date'][5:].replace('-', '/')}({wd[date.fromisoformat(r['end_date']).weekday()]})"
            if r['end_date'] else ''),
         (r['start_time'] + ('~' + r['end_time'] if r['end_time'] else '')) or '종일', r['title'], r['location'],
         r['attendees'], r['details']] for r in rows]) if rows else '<p>등록된 일정이 없습니다.</p>'
    span = f"{date_from[5:].replace('-', '/')}~{date_to[5:].replace('-', '/')}"
    return {'subject': f'[{who} 일정] {span}', 'text': f'{who} 일정({span}) 공유드립니다.', 'table_html': table,
            'to': '', 'attachments': [], 'related_kind': 'schedule', 'related_id': f'{date_from}~{date_to}'}


def draft_confluence(updates: list[dict], digest: str = '') -> dict:
    """컨플루언스 변경 소식 — AI 브리핑(있으면)과 바뀐 페이지 표 (services/confluence_watch.py)."""
    table = _table(['페이지', '공간', '판', '수정', '바뀐 점'], [
        [u['title'], u['space'], f"{u['version']}판", f"{u['modified_by']} {u['modified_at']}",
         u['summary'] or (f"+{u['added']} / -{u['removed']}줄" if u['diff'] else '새 페이지')] for u in updates])
    links = '\n'.join(f"- {u['title']}: {u['link']}" for u in updates if u['link'])
    day = date.today().strftime('%m/%d')
    return {'subject': f'[컨플루언스 변경] {day} — {len(updates)}건',
            'text': (digest.strip() + '\n\n' if digest else f'지켜보는 컨플루언스 페이지 {len(updates)}건이 바뀌었습니다.\n\n')
                    + (('페이지 링크\n' + links) if links else ''),
            'table_html': table, 'to': '', 'attachments': [], 'related_kind': 'confluence', 'related_id': ''}


def draft_absence(date_from: str, date_to: str) -> dict:
    """임원 부재 현황 — 기간과 겹치는 부재를 표로."""
    rows = absences.list_range(date_from, date_to)
    table = _table(['임원', '종류', '기간', '시간대', '행선지', '일정 내용', '메모'], [
        [f"{r['person_name']} {r['position']}".strip(), r['kind'],
         r['start_date'][5:].replace('-', '/') + ('' if r['start_date'] == r['end_date'] else
                                                  ' ~ ' + r['end_date'][5:].replace('-', '/')),
         r['time_range'], r['place'], r['content'], r['note']] for r in rows]) if rows else '<p>부재 중인 임원이 없습니다.</p>'
    span = f"{date_from[5:].replace('-', '/')}~{date_to[5:].replace('-', '/')}"
    return {'subject': f'[임원 부재 현황] {span}', 'text': f'임원 부재 현황({span}) 공유드립니다.',
            'table_html': table, 'to': '', 'attachments': [], 'related_kind': 'absence',
            'related_id': f'{date_from}~{date_to}'}


def draft_reply(mail_id: int) -> dict:
    """받은 메일 회신 — 받는 사람은 보낸 사람, 본문 아래 원문 인용."""
    with db.SessionLocal() as s:
        m = s.get(InboundMail, mail_id)
        if m is None:
            raise ValueError('메일을 찾을 수 없습니다.')
        subject, sender, mail_date, body = m.subject or '', m.sender or '', m.mail_date or '', m.body or ''
    quoted = '\n'.join('> ' + line for line in body[:5000].splitlines())
    to = parse_addrs(sender)[0]
    return {'subject': subject if re.match(r'(?i)^\s*re\s*:', subject) else f'RE: {subject}',
            'text': f'\n\n-----원본 메일-----\n보낸 사람: {sender}\n날짜: {mail_date}\n제목: {subject}\n\n{quoted}',
            'table_html': '', 'to': ', '.join(to),
            'attachments': [{'value': f"att:{f['id']}", 'label': f"원문 첨부 · {f['filename']}"}
                            for g in doc_versions.for_mails([mail_id]) for f in g['files']],
            'related_kind': 'mail', 'related_id': str(mail_id)}
