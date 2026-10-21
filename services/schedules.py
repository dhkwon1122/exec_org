"""원장님 일정 — 저장·조회, 비서실 일정 메일을 AI 로 정리해 가져오기.

흐름: 메일 붙여넣기/파일 → schedule_imports 에 원문 저장(status=pending) → 백그라운드에서 AI 가 일정 목록으로
정리(done/failed) → 화면에서 확인(신규·변경·동일) → 반영하면 schedules 에 들어간다.
같은 날짜·같은 제목의 일정이 이미 있으면 새로 만들지 않고 시간·장소 등을 고친다(수정된 주간 일정 메일 대응).
LLM 이 없거나 실패하면 날짜·시간이 보이는 줄을 규칙으로 읽는다.
"""
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

from sqlalchemy import select

from services import db, llm
from services.db import Schedule, ScheduleImport

CATEGORIES = ['회의', '보고', '면담', '행사', '외부', '출장', '기타']
FIELDS = ('date', 'start_time', 'end_time', 'title', 'location', 'attendees', 'category', 'details')

_executor = ThreadPoolExecutor(max_workers=int(os.environ.get('LLM_MAX_CONCURRENT', '2')),
                               thread_name_prefix='schedule')


# ── 일정 ──────────────────────────────────────────────────────────────

def _dict(s: Schedule) -> dict:
    return {'id': s.id, **{f: getattr(s, f) or '' for f in FIELDS}, 'source': s.source or '',
            'import_id': s.import_id, 'updated_at': s.updated_at.strftime('%Y-%m-%d %H:%M') if s.updated_at else ''}


def norm_time(v) -> str:
    """'9:00' / '0900' / '오후 2시' / '14시 30분' → 'HH:MM'. 못 읽으면 ''."""
    s = str(v or '').strip()
    if not s:
        return ''
    pm = '오후' in s or 'pm' in s.lower()
    am = '오전' in s or 'am' in s.lower()
    m = re.search(r'(\d{1,2})\s*[:;시.]\s*(\d{2})?', s) or re.search(r'^(\d{2})(\d{2})$', s)
    if not m:
        return ''
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    if pm and h < 12:
        h += 12
    if am and h == 12:
        h = 0
    return f'{h:02d}:{mi:02d}' if 0 <= h < 24 and 0 <= mi < 60 else ''


def clean_event(e: dict) -> dict | None:
    """AI·화면·규칙에서 온 일정 한 건을 저장 형식으로. 날짜·제목이 없으면 None."""
    d = str(e.get('date') or '').strip()[:10].replace('.', '-').replace('/', '-')
    try:
        d = date.fromisoformat(d).isoformat()
    except ValueError:
        return None
    title = re.sub(r'\s+', ' ', str(e.get('title') or '')).strip()[:300]
    if not title:
        return None
    start, end = norm_time(e.get('start_time') or e.get('start')), norm_time(e.get('end_time') or e.get('end'))
    if start and end and end <= start:
        end = ''
    cat = str(e.get('category') or '').strip()
    return {'date': d, 'start_time': start, 'end_time': end if start else '', 'title': title,
            'location': str(e.get('location') or '').strip()[:300],
            'attendees': str(e.get('attendees') or '').strip(), 'category': cat if cat in CATEGORIES else
            ('기타' if cat else ''), 'details': str(e.get('details') or '').strip()}


def list_range(date_from: str, date_to: str) -> list[dict]:
    """기간 안의 일정 — 날짜, 시작 시각(종일·미정이 먼저) 순."""
    with db.SessionLocal() as s:
        rows = s.scalars(select(Schedule).where(Schedule.date >= date_from, Schedule.date <= date_to))
        out = [_dict(x) for x in rows]
    return sorted(out, key=lambda x: (x['date'], x['start_time'], x['title']))


def get(sid: int) -> dict | None:
    with db.SessionLocal() as s:
        x = s.get(Schedule, sid)
        return _dict(x) if x else None


def save(data: dict, sid: int | None = None, by: str = '') -> dict:
    ev = clean_event(data)
    if ev is None:
        raise ValueError('날짜와 일정 제목을 입력하세요.')
    with db.SessionLocal() as s:
        x = s.get(Schedule, sid) if sid else None
        if sid and x is None:
            raise ValueError('일정을 찾을 수 없습니다.')
        if x is None:
            x = Schedule(source='manual', created_by=by)
            s.add(x)
        for f in FIELDS:
            setattr(x, f, ev[f])
        s.commit()
        return _dict(x)


def delete(sid: int) -> None:
    with db.SessionLocal() as s:
        x = s.get(Schedule, sid)
        if x:
            s.delete(x)
            s.commit()


def search(query: str, limit: int = 200) -> list[dict]:
    """제목·장소·참석자·세부 사항에서 찾는다 (최근 일정이 위)."""
    terms = [t for t in (query or '').split() if t]
    with db.SessionLocal() as s:
        stmt = select(Schedule)
        for t in terms:
            like = f'%{t}%'
            stmt = stmt.where(Schedule.title.like(like) | Schedule.location.like(like) |
                              Schedule.attendees.like(like) | Schedule.details.like(like))
        rows = s.scalars(stmt.order_by(Schedule.date.desc(), Schedule.start_time.desc()).limit(limit))
        return [_dict(x) for x in rows]


# ── 메일 → 일정 (AI · 규칙) ───────────────────────────────────────────

PARSE_SYSTEM = f"""너는 원장님 비서실의 일정 담당자다. 비서가 보낸 일정 메일(표가 '칸 | 칸 | 칸' 한 줄로 바뀌어 있을 수 있음)을 읽고
원장님 일정을 한 건씩 뽑는다.
규칙:
- 날짜는 YYYY-MM-DD. 메일에 연도가 없으면 메일 보낸 날짜의 연도로(12월 메일에 1월 일정이면 다음 해). '오늘·내일·다음주 월요일'은 메일 날짜 기준.
- 시간은 24시간 HH:MM. 종료 시간을 모르면 빈 문자열, 시간이 없는 일정(종일·미정)은 둘 다 빈 문자열.
- 표에서 날짜 칸이 비어 있거나(병합된 칸) 아예 빠져 시간으로 시작하는 행은 바로 위 일정과 같은 날짜다.
  그런 행을 메일 보낸 날짜로 넣지 않는다. 메일 보낸 날짜는 연도·'다음주' 같은 표현을 풀 때만 쓴다.
- title 은 일정 이름만 짧게, location 은 장소, attendees 는 참석자(쉼표로), details 는 그 밖의 내용(준비물·비고·담당 등).
- category 는 {', '.join(CATEGORIES)} 중 하나.
- 취소·연기된 일정은 details 에 '취소' 또는 '연기'를 적고 그대로 넣는다. 일정이 아닌 인사말·서명은 무시한다.
- 입력에 없는 내용은 만들지 않는다.
반드시 JSON 만 출력: {{"events": [{{"date": "", "start_time": "", "end_time": "", "title": "", "location": "",
"attendees": "", "category": "", "details": ""}}]}}"""


def ai_parse(text: str, mail_date: str) -> list[dict]:
    today = mail_date or date.today().isoformat()
    wd = '월화수목금토일'[date.fromisoformat(today).weekday()]
    raw = llm.chat(PARSE_SYSTEM, f'메일 보낸 날짜: {today} ({wd})\n\n메일 본문:\n{text[:llm.MAX_INPUT_CHARS]}')
    events = [clean_event(e) for e in llm._extract_json(raw).get('events') or [] if isinstance(e, dict)]
    return [e for e in events if e]


_DATE_PATTERNS = [
    re.compile(r'(?P<y>20\d{2})\s*[.\-/년]\s*(?P<m>\d{1,2})\s*[.\-/월]\s*(?P<d>\d{1,2})\s*일?'),
    re.compile(r'(?<![\d:])(?P<m>\d{1,2})\s*월\s*(?P<d>\d{1,2})\s*일'),
    re.compile(r'(?<![\d:.])(?P<m>\d{1,2})\s*[/.]\s*(?P<d>\d{1,2})(?![\d:])(?:\s*\([월화수목금토일]\))?'),
]
# 메일 머리 줄(보낸 날짜·제목·받는 사람…)은 일정이 아니다
_HEADER_LINE = re.compile(r'^\s*(보낸\s*(날짜|사람)|받는\s*사람|날짜|제목|참조|숨은\s*참조|From|To|Cc|Sent|Date|Subject)\s*:', re.I)
_TIME_RANGE = re.compile(r'((?:오전|오후)?\s*\d{1,2}\s*[:;시]\s*\d{0,2}\s*분?)\s*(?:[~\-–]\s*((?:오전|오후)?\s*\d{1,2}\s*[:;시]\s*\d{0,2}\s*분?)?)?')
# 줄 맨 앞(빈 칸 '|' 뒤 포함)이 시간이면 일정 행 — 병합된 날짜 칸이 빠진 행인지 볼 때 쓴다
_ROW_STARTS_WITH_TIME = re.compile(r'^\s*(?:\|\s*)*(?:오전|오후)?\s*\d{1,2}\s*[:;시]')


def _find_date(line: str, base: date) -> tuple[str, str] | None:
    for pat in _DATE_PATTERNS:
        m = pat.search(line)
        if not m:
            continue
        y = int(m.group('y')) if 'y' in pat.groupindex and m.group('y') else base.year
        mo, d = int(m.group('m')), int(m.group('d'))
        if 'y' not in pat.groupindex or not m.group('y'):
            if base.month == 12 and mo == 1:
                y += 1
            elif base.month == 1 and mo == 12:
                y -= 1
        try:
            return date(y, mo, d).isoformat(), line[:m.start()] + ' ' + line[m.end():]
        except ValueError:
            continue
    return None


def fill_dates(text: str, mail_date: str) -> str:
    """날짜 칸이 병합돼 아래 행에 날짜가 없는 표(시간으로 시작하는 행)에 위 행의 날짜를 채운다.

        10/21(화) | 00:00 ~ | 일정 1          10/21(화) | 00:00 ~ | 일정 1
                  | 01:00 ~ | 일정 2    →    2026-10-21 | 01:00 ~ | 일정 2
    메일 머리 줄(보낸 날짜·제목)의 날짜는 쓰지 않고, 표가 아닌 문장이 나오면 이어 쓰기를 멈춘다."""
    base = date.fromisoformat(mail_date) if mail_date else date.today()
    cur, out = None, []
    for line in (text or '').splitlines():
        if not line.strip() or _HEADER_LINE.match(line):
            out.append(line)
            continue
        found = _find_date(line, base)
        if found:
            cur = found[0]
        elif cur and _ROW_STARTS_WITH_TIME.match(line):
            stripped = line.lstrip()
            line = f'{cur} ' + (stripped if stripped.startswith('|') else '| ' + stripped)
        elif '|' not in line:
            cur = None  # 표 밖 문장 — 다음 표는 그 표의 날짜부터
        out.append(line)
    return '\n'.join(out)


def rule_parse(text: str, mail_date: str) -> list[dict]:
    """규칙 초안 — 날짜가 나온 뒤 시간이 있는 줄을 일정으로. 표 행이면 칸을 제목/장소/참석자로 나눈다."""
    base = date.fromisoformat(mail_date) if mail_date else date.today()
    cur, out = None, []
    for line in (text or '').splitlines():
        if not line.strip() or _HEADER_LINE.match(line):
            continue
        found = _find_date(line, base)
        rest = line
        if found:
            cur, rest = found
        elif '|' not in line and not _ROW_STARTS_WITH_TIME.match(line):
            cur = None  # 표 밖 문장 — 앞 표의 날짜를 이어 쓰지 않는다
            continue
        t = _TIME_RANGE.search(rest)
        all_day = not (t and norm_time(t.group(1))) and re.search(r'종일|하루', rest)
        if not cur or not (all_day or (t and norm_time(t.group(1)))):
            continue
        if all_day:
            rest = re.sub(r'종일|하루\s*종일', ' ', rest)
        else:
            rest = (rest[:t.start()] + ' ' + rest[t.end():]).strip()
        cells = [c.strip() for c in re.split(r'\s*\|\s*', rest) if c.strip() and not re.fullmatch(r'[(월화수목금토일)]+', c.strip())]
        if not cells:
            continue
        ev = clean_event({'date': cur, 'start_time': '' if all_day else t.group(1),
                          'end_time': '' if all_day else t.group(2) or '', 'title': cells[0],
                          'location': cells[1] if len(cells) > 1 else '',
                          'attendees': cells[2] if len(cells) > 2 else '',
                          'details': ' / '.join(cells[3:])})
        if ev:
            out.append(ev)
    return out


# ── 가져오기 ──────────────────────────────────────────────────────────

def _import_dict(x: ScheduleImport) -> dict:
    try:
        events = json.loads(x.events or '[]')
    except ValueError:
        events = []
    return {'id': x.id, 'name': x.name or '', 'mail_date': x.mail_date or '', 'status': x.status or '',
            'events': events, 'error': x.error or '', 'applied': x.applied_at is not None,
            'applied_at': x.applied_at.strftime('%Y-%m-%d %H:%M') if x.applied_at else '',
            'created_at': x.created_at.strftime('%Y-%m-%d %H:%M') if x.created_at else '',
            'raw_text': x.raw_text or ''}


def create_import(name: str, mail_date: str, text: str, by: str = '') -> int:
    with db.SessionLocal() as s:
        x = ScheduleImport(name=(name or '')[:300], mail_date=mail_date or '', raw_text=text or '',
                           status='pending', created_by=by)
        s.add(x)
        s.commit()
        return x.id


def parse_import(import_id: int) -> None:
    """동기 처리 — AI(없거나 실패하면 규칙)로 일정 목록을 만들어 저장한다. 예외는 밖으로 내보내지 않는다."""
    with db.SessionLocal() as s:
        x = s.get(ScheduleImport, import_id)
        if x is None:
            return
        text, mail_date = x.raw_text, x.mail_date
    status, error, events = 'done', '', []
    text = fill_dates(text, mail_date)
    try:
        if llm.is_configured():
            try:
                events = ai_parse(text, mail_date)
            except Exception as e:  # noqa: BLE001
                error = f'AI 정리 실패({str(e)[:200]}) — 규칙으로 읽었습니다.'
                events = rule_parse(text, mail_date)
        else:
            events = rule_parse(text, mail_date)
            error = 'LLM 미설정 — 날짜·시간이 보이는 줄만 규칙으로 읽었습니다.'
        if not events:
            status = 'failed'
            error = (error + ' ' if error else '') + '일정을 찾지 못했습니다.'
    except Exception as e:  # noqa: BLE001
        status, error = 'failed', str(e)[:500]
    with db.SessionLocal() as s:
        x = s.get(ScheduleImport, import_id)
        if x is not None:
            x.status, x.error, x.events = status, error, json.dumps(events, ensure_ascii=False)
            s.commit()


def submit_import(import_id: int, background: bool = True) -> None:
    if background:
        _executor.submit(parse_import, import_id)
    else:
        parse_import(import_id)


def reparse(import_id: int) -> None:
    with db.SessionLocal() as s:
        x = s.get(ScheduleImport, import_id)
        if x is None:
            return
        x.status, x.error = 'pending', ''
        s.commit()
    submit_import(import_id)


def list_imports(limit: int = 100) -> list[dict]:
    with db.SessionLocal() as s:
        rows = s.scalars(select(ScheduleImport).order_by(ScheduleImport.id.desc()).limit(limit))
        return [_import_dict(x) for x in rows]


def get_import(import_id: int) -> dict | None:
    with db.SessionLocal() as s:
        x = s.get(ScheduleImport, import_id)
        return _import_dict(x) if x else None


def delete_import(import_id: int) -> None:
    with db.SessionLocal() as s:
        x = s.get(ScheduleImport, import_id)
        if x:
            s.delete(x)
            s.commit()


def _norm_title(t: str) -> str:
    return re.sub(r'[\s\W_]+', '', (t or '').lower())


def _match(s, ev: dict) -> Schedule | None:
    """같은 날짜·같은 제목(공백·기호 무시)의 일정."""
    key = _norm_title(ev['title'])
    for x in s.scalars(select(Schedule).where(Schedule.date == ev['date'])):
        if _norm_title(x.title) == key:
            return x
    return None


def preview(events: list[dict]) -> list[dict]:
    """일정마다 상태 — new(신규) / changed(바뀐 칸 목록) / same(이미 같은 내용)."""
    out = []
    with db.SessionLocal() as s:
        for e in events:
            ev = clean_event(e)
            if ev is None:
                continue
            x = _match(s, ev)
            if x is None:
                out.append({**ev, 'state': 'new', 'changed': []})
                continue
            changed = [f for f in FIELDS if ev[f] and (getattr(x, f) or '') != ev[f]]
            out.append({**ev, 'state': 'changed' if changed else 'same', 'changed': changed, 'id': x.id})
    return out


def apply_events(events: list[dict], import_id: int | None = None, by: str = '') -> dict:
    """일정 목록을 반영 — 같은 날짜·제목이 있으면 값이 있는 칸만 고치고, 없으면 새로 만든다."""
    added = updated = same = 0
    with db.SessionLocal() as s:
        for e in events:
            ev = clean_event(e)
            if ev is None:
                continue
            x = _match(s, ev)
            if x is None:
                s.add(Schedule(**ev, source='mail' if import_id else 'manual', import_id=import_id, created_by=by))
                s.flush()
                added += 1
                continue
            changed = False
            for f in FIELDS:
                if ev[f] and (getattr(x, f) or '') != ev[f]:
                    setattr(x, f, ev[f])
                    changed = True
            if changed:
                x.import_id = import_id or x.import_id
                updated += 1
            else:
                same += 1
        if import_id:
            imp = s.get(ScheduleImport, import_id)
            if imp is not None:
                imp.applied_at = datetime.now().replace(microsecond=0)
                imp.events = json.dumps([clean_event(e) for e in events if clean_event(e)], ensure_ascii=False)
        s.commit()
    return {'added': added, 'updated': updated, 'same': same}


def apply_all_pending(by: str = '') -> dict:
    """정리가 끝났는데 아직 반영하지 않은 메일을 보낸 날짜 순(옛 메일 먼저 → 최신 메일이 덮어씀)으로 모두 반영."""
    todo = [i for i in list_imports(limit=10000) if i['status'] == 'done' and not i['applied']]
    todo.sort(key=lambda i: (i['mail_date'] or '9999', i['id']))
    total = {'mails': 0, 'added': 0, 'updated': 0, 'same': 0}
    for i in todo:
        r = apply_events(i['events'], i['id'], by)
        total['mails'] += 1
        for k in ('added', 'updated', 'same'):
            total[k] += r[k]
    return total


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())
