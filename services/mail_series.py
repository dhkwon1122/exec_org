"""정기 메일 모음 — 주간보고·월간 실적처럼 주기적으로 받는 메일을 한 모음으로 쌓아 두고 지난 것과 함께 본다.

모음 = 이름 + 보낸 사람에 들어 있는 글자 + 제목에 들어 있는 글자 (둘 중 하나 이상, '*' 는 아무 글자).
- 회신·전달(RE:/FW:)은 원래 보고가 아니라 모으지 않는다.
- 새 메일을 받으면 assign() 이 맞는 모음에 넣는다. file_away 면 AI 처리 없이 '처리 완료'로 둔다 (확인 필요에 안 쌓임).
- 모음을 만들거나 조건을 바꾸면 지난 메일도 다시 모은다 (refill).
- 잘못 모인 메일은 '모음에서 빼기'(exclude) — 메일은 그대로 두고, 다시 모을 때도 넣지 않는다.
"""
import io
import re

from sqlalchemy import func, or_, select, update

from services import db, llm, mail_threads
from services.db import InboundMail, MailSeries

FILED_BY = '정기 메일 모음'
TREND_LIMIT = 6  # 흐름 요약에 넣는 최근 메일 수


def _pattern(text: str) -> re.Pattern | None:
    text = (text or '').strip()
    if not text:
        return None
    return re.compile('.*'.join(re.escape(p) for p in text.split('*')), re.IGNORECASE)


def matches(series: dict, subject: str, sender: str) -> bool:
    if mail_threads.is_reply(subject or ''):
        return False
    subj, snd = _pattern(series['subject_pattern']), _pattern(series['sender_pattern'])
    if subj is None and snd is None:
        return False
    return (subj is None or bool(subj.search(subject or ''))) and (snd is None or bool(snd.search(sender or '')))


def _ids(text: str | None) -> set[int]:
    return {int(t) for t in (text or '').split(',') if t.strip().isdigit()}


def _dict(x: MailSeries, count: int = 0, latest: str = '') -> dict:
    return {'id': x.id, 'name': x.name, 'sender_pattern': x.sender_pattern or '',
            'subject_pattern': x.subject_pattern or '', 'file_away': bool(x.file_away), 'count': count,
            'latest': latest or '', 'excluded': sorted(_ids(x.excluded))}


def list_series() -> list[dict]:
    """모음 목록 — 쌓인 메일 수와 가장 최근 메일 날짜, 최근 메일이 온 순."""
    with db.SessionLocal() as s:
        stats = {sid: (n, latest) for sid, n, latest in s.execute(
            select(InboundMail.series_id, func.count(), func.max(InboundMail.mail_date))
            .where(InboundMail.series_id.is_not(None)).group_by(InboundMail.series_id))}
        rows = [_dict(x, *stats.get(x.id, (0, ''))) for x in s.scalars(select(MailSeries).order_by(MailSeries.id))]
    return sorted(rows, key=lambda r: (r['latest'], r['id']), reverse=True)


def get(series_id: int | None) -> dict | None:
    return next((r for r in list_series() if r['id'] == series_id), None) if series_id else None


def _clean(values: dict) -> dict:
    out = {'name': str(values.get('name') or '').strip()[:100],
           'sender_pattern': str(values.get('sender_pattern') or '').strip()[:300],
           'subject_pattern': str(values.get('subject_pattern') or '').strip()[:300],
           'file_away': bool(values.get('file_away'))}
    if not out['name']:
        raise ValueError('모음 이름을 적으세요.')
    if not out['sender_pattern'].strip('* ') and not out['subject_pattern'].strip('* '):
        raise ValueError('보낸 사람이나 제목에 들어 있는 글자를 하나 이상 적으세요.')
    return out


def preview_count(values: dict) -> int:
    """이 조건이면 지금까지 받은 메일 중 몇 통이 모이는지 (저장 전 확인용)."""
    series = {'sender_pattern': values.get('sender_pattern') or '', 'subject_pattern': values.get('subject_pattern') or ''}
    with db.SessionLocal() as s:
        return sum(matches(series, subj, snd) for subj, snd in s.execute(
            select(InboundMail.subject, InboundMail.sender).where(InboundMail.status != 'skipped')))


def save(values: dict, series_id: int | None = None, by: str = '') -> dict:
    data = _clean(values)
    with db.transaction() as s:
        if series_id:
            x = s.get(MailSeries, series_id)
            if x is None:
                raise ValueError('모음을 찾을 수 없습니다.')
        else:
            x = MailSeries(created_by=by)
            s.add(x)
        for k, v in data.items():
            setattr(x, k, v)
        s.flush()
        sid = x.id
    refill(sid)
    return get(sid)


def delete(series_id: int) -> None:
    """모음만 지운다 — 메일은 받은 메일에 그대로 남는다."""
    with db.transaction() as s:
        s.execute(update(InboundMail).where(InboundMail.series_id == series_id).values(series_id=None))
        x = s.get(MailSeries, series_id)
        if x is not None:
            s.delete(x)


def refill(series_id: int) -> int:
    """이 모음의 조건으로 지난 메일을 다시 모은다 (다른 모음에 든 메일은 그대로). 모인 수."""
    series = get(series_id)
    if series is None:
        return 0
    with db.transaction() as s:
        s.execute(update(InboundMail).where(InboundMail.series_id == series_id).values(series_id=None))
        ids = [mid for mid, subj, snd in s.execute(
            select(InboundMail.id, InboundMail.subject, InboundMail.sender).where(
                InboundMail.status != 'skipped', InboundMail.series_id.is_(None)))
            if matches(series, subj, snd) and mid not in series['excluded']]
        if ids:
            s.execute(update(InboundMail).where(InboundMail.id.in_(ids)).values(series_id=series_id))
    return len(ids)


def assign(mail_id: int) -> dict | None:
    """새 메일 한 통을 맞는 모음에 넣는다 → 그 모음 (없으면 None).
    모음이 file_away 면 '처리 완료'로 바로 둔다 — 이때는 AI 처리를 하지 않는다(mailbox.fetch_new)."""
    from datetime import datetime
    series = list_series()
    with db.transaction() as s:
        m = s.get(InboundMail, mail_id)
        if m is None or m.status == 'skipped':
            return None
        found = next((r for r in series if matches(r, m.subject, m.sender)), None)
        if found is None:
            return None
        m.series_id = found['id']
        if found['file_away'] and m.status in ('review', 'failed', None, ''):
            m.status, m.reason = 'done', f"정기 메일 모음: {found['name']}"
            m.handled_at, m.handled_by = datetime.now().replace(microsecond=0), FILED_BY
    return found


def exclude(series_id: int, mail_ids: list[int]) -> int:
    """잘못 모인 메일을 모음에서 뺀다 (메일은 받은 메일에 남음). 모음이 바로 '처리 완료'로 둔 메일은
    '확인 필요'로 돌려 놓는다 — 모음 때문에 AI 처리를 건너뛰었으니 받은 메일에서 다시 보게. 뺀 수."""
    ids = {int(i) for i in mail_ids or []}
    with db.transaction() as s:
        x = s.get(MailSeries, series_id)
        if x is None:
            raise ValueError('모음을 찾을 수 없습니다.')
        mails_ = list(s.scalars(select(InboundMail).where(InboundMail.id.in_(ids or [0]),
                                                          InboundMail.series_id == series_id)))
        for m in mails_:
            m.series_id = None
            if m.handled_by == FILED_BY and m.status == 'done':
                m.status, m.handled_at, m.handled_by = 'review', None, ''
                m.reason = f'정기 메일 모음({x.name})에서 뺌 — 확인 필요'
        x.excluded = ','.join(str(i) for i in sorted(_ids(x.excluded) | {m.id for m in mails_}))
    return len(mails_)


def include_again(series_id: int) -> int:
    """뺀 메일을 모두 다시 받아들인다 — 조건에 맞으면 다시 모인다. 다시 모인 수."""
    with db.transaction() as s:
        x = s.get(MailSeries, series_id)
        if x is None:
            raise ValueError('모음을 찾을 수 없습니다.')
        x.excluded = ''
    return refill(series_id)


def mails(series_id: int, query: str = '', limit: int = 30, offset: int = 0) -> tuple[list[dict], int]:
    """모음의 메일 — 최근 것이 위. query 는 제목·본문 글자 검색. → (메일들, 전체 수)."""
    with db.SessionLocal() as s:
        stmt = select(InboundMail).where(InboundMail.series_id == series_id)
        q = (query or '').strip()
        if q:
            like = f'%{q}%'
            stmt = stmt.where(or_(InboundMail.subject.ilike(like), InboundMail.body.ilike(like)))
        total = s.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        rows = s.scalars(stmt.order_by(InboundMail.mail_date.desc(), InboundMail.id.desc())
                         .offset(offset).limit(limit))
        return [{'id': m.id, 'subject': m.subject or '(제목 없음)', 'sender': m.sender or '',
                 'mail_date': m.mail_date or '', 'body': m.body or '', 'status': m.status} for m in rows], total


def suggest(mail: dict) -> dict:
    """메일 한 통으로 모음 조건을 미리 채운다 — 제목의 [머리말]이 있으면 그것, 없으면 숫자·날짜를 뺀 제목 앞부분.
    보낸 사람은 메일 주소."""
    subject = re.sub(r'^\s*((re|fw|fwd)\s*:\s*)+', '', mail.get('subject') or '', flags=re.IGNORECASE)
    tag = re.search(r'\[[^\]]{2,30}\]', subject)
    if tag:
        pattern = tag.group(0)
    else:
        words = re.split(r'\s*[\d./()~\-]+\s*', subject)
        pattern = max(words, key=len).strip() if words else subject
    addr = re.search(r'[\w.+-]+@[\w-]+(\.[\w-]+)+', mail.get('sender') or '')
    name = re.sub(r'[\[\]]', '', pattern).strip() or subject[:30]
    return {'name': name[:100], 'subject_pattern': pattern[:300], 'sender_pattern': addr.group(0) if addr else '',
            'file_away': True}


TREND_SYSTEM = """너는 임원조직 담당자의 비서다. 같은 정기 보고 메일(예: 주간보고)의 최근 여러 회차를 받아,
회차를 넘어 이어지는 흐름을 정리한다.
- 계속 다뤄지는 주제와 진행 상황의 변화, 새로 등장한 이슈, 사라지거나 끝난 이슈, 숫자(실적·일정)의 변화.
- 회차 날짜를 근거로 붙인다. 메일에 없는 내용은 만들지 않는다.
- 한국어 마크다운, 소제목 '계속되는 주제' / '새로 나온 것' / '끝났거나 빠진 것' / '눈여겨볼 숫자·일정'."""


def trend(series_id: int, n: int = TREND_LIMIT) -> str:
    """최근 n 통의 흐름 요약 (LLM 필요)."""
    if not llm.is_configured():
        raise ValueError('흐름 요약은 LLM 이 설정돼 있어야 합니다.')
    rows, _ = mails(series_id, limit=n)
    if len(rows) < 2:
        raise ValueError('흐름을 보려면 메일이 두 통 이상 있어야 합니다.')
    budget = max(llm.MAX_INPUT_CHARS // len(rows), 1000)
    text = '\n\n'.join(f"=== {r['mail_date']} · {r['subject']}\n{r['body'][:budget]}" for r in reversed(rows))
    return llm.chat(TREND_SYSTEM, text)


def export_xlsx(series_id: int) -> bytes:
    """모음의 메일 전체 → 엑셀 (날짜·제목·보낸 사람·본문)."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font
    series = get(series_id)
    if series is None:
        raise ValueError('모음을 찾을 수 없습니다.')
    rows, _ = mails(series_id, limit=100000)
    wb = Workbook()
    ws = wb.active
    ws.title = '메일'
    ws.append(['날짜', '제목', '보낸 사람', '본문'])
    for c in ws[1]:
        c.font = Font(bold=True)
    for r in rows:
        ws.append([r['mail_date'], r['subject'], r['sender'], r['body'][:32000]])
    for col, width in zip('ABCD', (12, 50, 30, 100)):
        ws.column_dimensions[col].width = width
    for row in ws.iter_rows(min_row=2, min_col=4, max_col=4):
        for c in row:
            c.alignment = Alignment(wrap_text=True, vertical='top')
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
