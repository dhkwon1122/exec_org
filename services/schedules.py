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
from difflib import SequenceMatcher

from sqlalchemy import func, select

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


def delete_many(ids) -> int:
    """고른 일정을 한꺼번에 지운다. 지운 건수."""
    ids = [int(i) for i in ids or []]
    if not ids:
        return 0
    with db.SessionLocal() as s:
        rows = list(s.scalars(select(Schedule).where(Schedule.id.in_(ids))))
        for x in rows:
            s.delete(x)
        s.commit()
        return len(rows)


def delete_by_import(import_id: int) -> int:
    """이 메일로 들어간(또는 마지막으로 고쳐진) 일정을 모두 지우고 메일을 '반영 전'으로 돌린다 — 잘못 정리된 메일을
    지운 뒤 다시 정리·반영할 때. 지운 건수."""
    with db.SessionLocal() as s:
        rows = list(s.scalars(select(Schedule).where(Schedule.import_id == import_id)))
        for x in rows:
            s.delete(x)
        imp = s.get(ScheduleImport, import_id)
        if imp is not None:
            imp.applied_at = None
        s.commit()
        return len(rows)


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
- '26.10.21(수)' 처럼 두 자리 연도는 2026년. 날짜 줄 아래 '08;00-10;00 회의명' 줄이 일정 하나이고, 이어지는
  '* 참석자 : …' 줄은 attendees, '장소 : …' 또는 '본관동 7층 대회의실' 같은 장소 줄은 location 이다.
- category 는 {', '.join(CATEGORIES)} 중 하나.
- 취소·연기된 일정은 details 에 '취소' 또는 '연기'를 적고 그대로 넣는다. 일정이 아닌 인사말·서명은 무시한다.
- 입력에 없는 내용은 만들지 않는다.
반드시 JSON 만 출력: {{"events": [{{"date": "", "start_time": "", "end_time": "", "title": "", "location": "",
"attendees": "", "category": "", "details": ""}}]}}"""


def _known_events_hint(mail_date: str, limit: int = 80) -> str:
    """이미 등록된(사람이 정제한) 메일 날짜 전후 일정 — AI 가 같은 일정의 제목·장소 표기를 그대로 맞추도록 참고로 준다."""
    try:
        d = date.fromisoformat(mail_date)
    except ValueError:
        return ''
    rows = list_range((d - timedelta(days=7)).isoformat(), (d + timedelta(days=45)).isoformat())[:limit]
    if not rows:
        return ''
    lines = [f"- {r['date']} {r['start_time'] or '종일'}{'-' + r['end_time'] if r['end_time'] else ''} | {r['title']}"
             f" | {r['location']} | {r['attendees'][:60]}" for r in rows]
    return ('\n\n참고 — 이미 등록된 일정 (사람이 확인한 값). 메일의 일정이 이 중 하나와 같은 일정이면 title·location·'
            'attendees 표기를 이것과 똑같이 맞춘다. 메일에 없는 일정은 만들지 않는다:\n' + '\n'.join(lines))


def ai_parse(text: str, mail_date: str) -> list[dict]:
    today = mail_date or date.today().isoformat()
    wd = '월화수목금토일'[date.fromisoformat(today).weekday()]
    raw = llm.chat(PARSE_SYSTEM, f'메일 보낸 날짜: {today} ({wd})\n\n메일 본문:\n{text[:llm.MAX_INPUT_CHARS]}'
                   + _known_events_hint(today))
    events = [clean_event(e) for e in llm._extract_json(raw).get('events') or [] if isinstance(e, dict)]
    return [e for e in events if e]


_DATE_PATTERNS = [
    re.compile(r'(?P<y>20\d{2})\s*[.\-/년]\s*(?P<m>\d{1,2})\s*[.\-/월]\s*(?P<d>\d{1,2})\s*일?'),
    # 두 자리 연도 '26.10.21(수)' / '26-10-21' / '26/10/21'
    re.compile(r'(?<![\d:.])(?P<yy>\d{2})\s*[.\-/]\s*(?P<m>\d{1,2})\s*[.\-/]\s*(?P<d>\d{1,2})(?![\d:])\.?'
               r'(?:\s*\([월화수목금토일]\))?'),
    re.compile(r'(?<![\d:])(?P<m>\d{1,2})\s*월\s*(?P<d>\d{1,2})\s*일'),
    re.compile(r'(?<![\d:.])(?P<m>\d{1,2})\s*[/.]\s*(?P<d>\d{1,2})(?![\d:])(?:\s*\([월화수목금토일]\))?'),
]
# 메일 머리 줄(보낸 날짜·제목·받는 사람…)은 일정이 아니다
_HEADER_LINE = re.compile(r'^\s*(보낸\s*(날짜|사람)|받는\s*사람|날짜|제목|참조|숨은\s*참조|From|To|Cc|Sent|Date|Subject)\s*:', re.I)
_TIME_RANGE = re.compile(r'((?:오전|오후)?\s*\d{1,2}\s*[:;시]\s*\d{0,2}\s*분?)\s*(?:[~\-–]\s*((?:오전|오후)?\s*\d{1,2}\s*[:;시]\s*\d{0,2}\s*분?)?)?')
# 줄 맨 앞(빈 칸 '|' 뒤 포함)이 시간이면 일정 행 — 병합된 날짜 칸이 빠진 행인지 볼 때 쓴다
_ROW_STARTS_WITH_TIME = re.compile(r'^\s*(?:\|\s*)*(?:오전|오후)?\s*\d{1,2}\s*[:;시]')


def _find_date(line: str, base: date) -> tuple[str, str] | None:
    """줄에서 처음 보이는 날짜 → (YYYY-MM-DD, 날짜를 뺀 나머지). 날짜가 안 되는 숫자 조합(26.10 의 26월 등)은 건너뛴다."""
    for pat in _DATE_PATTERNS:
        for m in pat.finditer(line):
            g = m.groupdict()
            mo, d = int(g['m']), int(g['d'])
            if g.get('y'):
                y = int(g['y'])
            elif g.get('yy'):
                y = 2000 + int(g['yy'])
            else:
                y = base.year
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


# ── 표 제목 행 기준 읽기 ──────────────────────────────────────────────
# 비서실 일정표는 제목 행(일자 | 시간 | 내용 | 장소 | 참석 …)이 있다. 칸을 순서로 짐작하면 날짜 칸 형식을 못 알아볼 때
# 한 칸씩 밀려 '일정 이름에 날짜, 장소에 내용'이 들어가므로, 제목 행으로 열의 뜻을 정하고 그대로 읽는다.
HEADER_WORDS = {
    'datetime': ['일시', '일정일시', '일자/시간', '날짜/시간', '일자및시간'],
    'date': ['일자', '날짜', '일', '월일', '월/일', '일자(요일)', 'date'],
    'weekday': ['요일'],
    'time': ['시간', '시각', 'time', '시간대'],
    'start': ['시작', '시작시간', '시작시각'],
    'end': ['종료', '종료시간', '종료시각'],
    'title': ['내용', '일정', '제목', '행사', '행사명', '회의명', '일정명', '업무', '주요일정', '주요내용', '건명', '안건',
              '일정내용', '회의', '주요행사'],
    'location': ['장소', '위치', '회의실', '장소/위치'],
    'attendees': ['참석', '참석자', '배석', '배석자', '대상', '참석대상', '참석범위', '동행', '수행', '참석인원'],
    'category': ['구분', '분류', '유형', '종류', '일정구분'],
    'details': ['비고', '세부', '세부내용', '세부사항', '준비', '준비사항', '준비물', '담당', '담당자', '메모', '주관',
                '기타', '참고', '복장', '보고자', '주관부서'],
}
_HEADER_KEY = {re.sub(r'[\s()/·.]', '', w).lower(): k for k, ws in HEADER_WORDS.items() for w in ws}


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.split('|')]


def _header_kinds(line: str) -> list[str] | None:
    """제목 행이면 열마다 뜻('date','time','title',…, 모르면 '') 목록, 아니면 None."""
    if '|' not in line:
        return None
    kinds = [_HEADER_KEY.get(re.sub(r'[\s()/·.]', '', c).lower(), '') for c in _cells(line)]
    known = [k for k in kinds if k]
    if len(known) >= 2 and ('title' in kinds) and ({'date', 'datetime', 'time', 'start'} & set(kinds)):
        return kinds
    return None


_DAY_ONLY = re.compile(r'^\s*(?P<d>\d{1,2})\s*일?\s*(?:\(?[월화수목금토일]\)?)?\s*$')


def parse_date_cell(cell: str, base: date, prev: str | None) -> str | None:
    """날짜 칸 → YYYY-MM-DD. '10/21(화)', '10.21', '10월 21일', '2026-10-21' 외에 '21일(화)', '21(화)' 처럼
    일만 있으면 앞 행(없으면 메일)의 달로, 날이 줄면 다음 달로."""
    cell = (cell or '').replace(' / ', ' ')
    found = _find_date(cell, base)
    if found:
        return found[0]
    m = _DAY_ONLY.match(cell)
    if not m:
        return None
    ref = date.fromisoformat(prev) if prev else base
    d, y, mo = int(m.group('d')), ref.year, ref.month
    if prev and d < ref.day:
        y, mo = (y + 1, 1) if mo == 12 else (y, mo + 1)
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return None


def table_parse(text: str, mail_date: str) -> list[dict]:
    """제목 행이 있는 표만 읽는다(없으면 []). 날짜 칸이 비면(병합) 위 행의 날짜, 시간이 없으면 종일."""
    base = date.fromisoformat(mail_date) if mail_date else date.today()
    out, kinds, cur = [], None, None
    for line in (text or '').splitlines():
        if not line.strip():
            continue
        head = _header_kinds(line)
        if head:
            kinds, cur = head, None
            continue
        if kinds is None:
            continue
        if '|' not in line:
            kinds = None  # 표가 끝났다
            continue
        cells = _cells(line)
        if len(cells) < len(kinds) and _ROW_STARTS_WITH_TIME.match(line) and kinds[0] in ('date', 'datetime'):
            cells = [''] * (len(kinds) - len(cells)) + cells  # 병합된 날짜 칸이 빠진 행 → 앞을 비운다
        cells += [''] * (len(kinds) - len(cells))
        col, extra = {}, []
        for k, c in zip(kinds, cells):
            if not c or k == 'weekday':
                continue
            if not k:
                extra.append(c)  # 모르는 열은 세부 사항으로
            elif k in col:
                col[k] += ' / ' + c  # 같은 뜻의 열이 둘이면 이어 붙인다
            else:
                col[k] = c
        start = end = ''
        if 'datetime' in col:  # '10/21(화) 09:00~10:00' 한 칸
            found = _find_date(col['datetime'], base)
            d = found[0] if found else parse_date_cell(col['datetime'], base, cur)
            if d:
                cur = d
            t = _TIME_RANGE.search(found[1] if found else col['datetime'])
            if t and norm_time(t.group(1)):
                start, end = t.group(1), t.group(2) or ''
        if 'date' in col:
            d = parse_date_cell(col['date'], base, cur)
            if d:
                cur = d
        if 'time' in col:
            t = _TIME_RANGE.search(col['time'])
            if t and norm_time(t.group(1)):
                start, end = t.group(1), t.group(2) or ''
        start = col.get('start', start)
        end = col.get('end', end)
        title = col.get('title', '')
        if not cur or not title:
            continue
        details = ' / '.join(x for x in [col.get('details', ''), *extra] if x)
        ev = clean_event({'date': cur, 'start_time': start, 'end_time': end, 'title': title,
                          'location': col.get('location', ''), 'attendees': col.get('attendees', ''),
                          'category': col.get('category', ''), 'details': details})
        if ev:
            out.append(ev)
    return out


# ── 블록 형식 읽기 ────────────────────────────────────────────────────
# 비서실 메일이 표 대신(또는 표 한 칸 안에) 이렇게 적힌 경우:
#     26.10.21(수)
#      08;00-10;00 주간 임원 간담회
#     * 참석자 : 플랫폼장(7), staff팀장(2)
#     본관동 7층 대회의실
# 날짜 줄 → 그 아래 '시간 + 일정' 줄마다 일정 하나 → 이어지는 '항목 : 값' 줄과 장소처럼 보이는 줄을 그 일정에 붙인다.
_BULLET = re.compile(r'^[\s*\-•ㆍ·○●◦▶▷■□◆◇※>]+')
_KV = re.compile(r'^(?P<k>[^:：]{1,12}?)\s*[:：]\s*(?P<v>.*)$')
_KEY_FIELD = {'참석': 'attendees', '참석자': 'attendees', '배석': 'attendees', '배석자': 'attendees', '대상': 'attendees',
              '참석대상': 'attendees', '동행': 'attendees', '수행': 'attendees', '참석범위': 'attendees',
              '장소': 'location', '위치': 'location', '회의실': 'location', '장소/위치': 'location',
              '구분': 'category', '일정': 'title', '제목': 'title', '내용': 'title', '행사': 'title', '회의': 'title'}
_PLACE = re.compile(r'(\d+\s*층|회의실|접견실|원장실|대회의실|강당|홀|룸|Room|본관|별관|동\b|관\b|센터|호텔|청사|연구소|캠퍼스|'
                    r'사옥|빌딩|타워|\bB\d|\bR\d|온라인|화상|Zoom|Teams|Webex)', re.I)
# 맺음말·서명 — 여기서 일정 블록이 끝난다
_CLOSING = re.compile(r'^(감사합니다|감사드립니다|고맙습니다|이상입니다|수고하세요|수고하십시오|.{0,20}(드림|올림)\.?$|'
                      r'Best regards|Regards|Thanks|Thank you|-- ?$)', re.I)
_TIME_AT_START = re.compile(r'^\s*(?:\[|\()?\s*((?:오전|오후)?\s*\d{1,2}\s*[:;시]\s*\d{0,2}\s*분?)'
                            r'\s*(?:[~\-–]\s*((?:오전|오후)?\s*\d{1,2}\s*[:;시]\s*\d{0,2}\s*분?)?)?\s*(?:\]|\))?\s*(.*)$')


def _logical_lines(text: str) -> list[str]:
    """표 칸(' | ')과 칸 안 줄바꿈(' / ')도 줄로 나눈다 — 블록이 표 한 칸에 들어 있는 메일도 같은 방식으로."""
    out = []
    for line in (text or '').splitlines():
        for part in re.split(r'\s+\|\s+|\s*\|\s*$|^\s*\|\s*|\s+/\s+', line):
            if part.strip():
                out.append(part.strip())
    return out


_TIME_ONLY = re.compile(r'^(?:오전|오후)?\s*\d{1,2}\s*[:;시]\s*\d{0,2}\s*분?\s*(?:[~\-–]\s*((?:오전|오후)?\s*\d{1,2}'
                        r'\s*[:;시]\s*\d{0,2}\s*분?)?)?$')


def is_column_table(text: str) -> bool:
    """시간만 든 칸이 있는 표(열마다 뜻이 따로인 표)인지 — 그런 표는 블록으로 읽지 않고 AI·규칙에 맡긴다."""
    for line in (text or '').splitlines():
        if '|' in line and any(_TIME_ONLY.match(c.strip()) for c in line.split('|')):
            return True
    return False


def block_parse(text: str, mail_date: str) -> list[dict]:
    base = date.fromisoformat(mail_date) if mail_date else date.today()
    out, cur_date, ev = [], None, None

    def close():
        nonlocal ev
        if ev and ev.get('title'):
            e = clean_event(ev)
            if e:
                out.append(e)
        ev = None

    for raw in _logical_lines(text):
        if _HEADER_LINE.match(raw):
            continue
        line = _BULLET.sub('', raw).strip()
        if not line:
            continue
        if _CLOSING.match(line):
            close()
            cur_date = None
            continue
        found = _find_date(line, base)
        if found:
            close()
            cur_date, line = found[0], found[1].strip(' ,-~|')
            if not line or re.fullmatch(r'[(（]?[월화수목금토일][)）]?', line):
                continue
        t = _TIME_AT_START.match(line)
        if t and cur_date and norm_time(t.group(1)):
            close()
            title = t.group(3).strip(' -:|')
            ev = {'date': cur_date, 'start_time': t.group(1), 'end_time': t.group(2) or '', 'title': title,
                  'location': '', 'attendees': '', 'details': ''}
            continue
        if ev is None:
            if cur_date and re.search(r'종일|하루', line):  # '종일 해외 출장'
                close()
                ev = {'date': cur_date, 'title': re.sub(r'\s*(하루\s*)?종일\s*', ' ', line).strip(), 'location': '',
                      'attendees': '', 'details': ''}
            continue
        kv = _KV.match(line)
        if kv and not re.match(r'^\d', kv.group('k')):
            key = re.sub(r'\s+', '', kv.group('k'))
            field = _KEY_FIELD.get(key)
            val = kv.group('v').strip()
            if field and not ev.get(field):
                ev[field] = val
            else:
                ev['details'] = (ev['details'] + ' / ' if ev['details'] else '') + f'{kv.group("k").strip()}: {val}'
        elif not ev['title']:
            ev['title'] = line
        elif not ev['location'] and _PLACE.search(line):
            ev['location'] = line
        else:
            ev['details'] = (ev['details'] + ' / ' if ev['details'] else '') + line
    close()
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
    try:
        events = table_parse(text, mail_date)  # 제목 행이 있는 표는 열 이름대로 그대로 읽는다 (AI 보다 정확)
        if events:
            error = '표의 제목 행(일자·시간·내용·장소…) 기준으로 읽었습니다.'
        elif not is_column_table(text):
            events = block_parse(text, mail_date)  # 날짜 줄 / 시간+일정 줄 / 참석자·장소 줄 형식
            if events:
                error = '날짜 · 시간+일정 · 참석자/장소 줄 형식으로 읽었습니다.'
    except Exception:  # noqa: BLE001
        events = []
    text = fill_dates(text, mail_date)
    try:
        if events:
            pass
        elif llm.is_configured():
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
    """최근 메일부터. n_events = 이 메일로 들어간(또는 마지막으로 고쳐진) 일정 수."""
    with db.SessionLocal() as s:
        counts = dict(s.execute(select(Schedule.import_id, func.count()).where(Schedule.import_id.is_not(None))
                                .group_by(Schedule.import_id)).all())
        rows = s.scalars(select(ScheduleImport).order_by(ScheduleImport.id.desc()).limit(limit))
        return [{**_import_dict(x), 'n_events': counts.get(x.id, 0)} for x in rows]


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


LABELS = {'date': '날짜', 'start_time': '시작', 'end_time': '종료', 'title': '일정', 'location': '장소',
          'attendees': '참석자', 'category': '구분', 'details': '세부'}
_TEXT_FIELDS = ('title', 'location', 'attendees', 'details')
# 날짜·시간·요일 글자만 있는 값 — 칸이 밀려 날짜 줄이 제목 등에 들어간 경우
_ONLY_DATE_TIME = re.compile(r'^[\s\d.\-/:;~()년월일시분오전후월화수목금토일*·,]*$')


def is_date_only(v: str) -> bool:
    v = (v or '').strip()
    return bool(v) and bool(_ONLY_DATE_TIME.match(v)) and bool(re.search(r'\d', v))


def _similar(a: str, b: str) -> float:
    a, b = _norm_title(a), _norm_title(b)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _blob(ev: dict) -> str:
    return _norm_title(' '.join(ev.get(f) or '' for f in _TEXT_FIELDS))


def _digits(t: str) -> list[str]:
    return re.findall(r'\d+', t or '')


def _match(s, ev: dict, taken: set | None = None) -> tuple[Schedule | None, str]:
    """이미 있는 같은 일정 찾기 → (일정, 어떻게 찾았나). 칸이 밀려 제목이 달라도 같은 날의 기존 일정으로 알아보도록:
    1) 날짜·제목 일치 2) 기존 제목이 새 일정의 어느 칸엔가 들어 있음 3) 제목이 비슷함 4) 같은 시작 시간에 제목도 꽤 비슷.
    제목 속 숫자가 다르면('일정 1' / '일정 2') 다른 일정. taken: 이번 메일의 다른 줄이 이미 차지한 일정 id."""
    all_rows = list(s.scalars(select(Schedule).where(Schedule.date == ev['date'])))
    key = _norm_title(ev['title'])
    for x in all_rows:
        if _norm_title(x.title) == key:
            return x, 'title'
    rows = [x for x in all_rows if x.id not in (taken or set())]
    blob, best, how, score = _blob(ev), None, '', 0.0
    for x in rows:
        xt = _norm_title(x.title)
        if _digits(x.title) and _digits(ev['title']) and _digits(x.title) != _digits(ev['title']):
            continue
        sim = _similar(x.title, ev['title'])
        same_time = bool(ev['start_time']) and ev['start_time'] == (x.start_time or '')
        if len(xt) >= 3 and xt in blob:
            cand, cand_how = 1.0 + (0.5 if same_time or not ev['start_time'] else 0), 'contains'
        elif sim >= 0.7 and min(len(xt), len(key)) >= 4:
            cand, cand_how = sim + (0.3 if same_time else 0), 'similar'
        elif same_time and (sim >= 0.35 or is_date_only(ev['title']) or not key):
            cand, cand_how = 0.5 + sim, 'time'
        else:
            continue
        if cand > score:
            best, how, score = x, cand_how, cand
    return best, how


def verify_changes(x: Schedule, ev: dict, how: str = 'title') -> tuple[list[str], list[dict]]:
    """기존 일정 x 와 새로 읽은 ev 를 칸마다 비교 → (정말 바뀐 칸, 무시한 칸 [{'field', 'value', 'reason'}]).
    빈 칸은 '모름'이라 건드리지 않는다. 칸이 밀려 다른 칸 값이 들어온 것, 날짜·시간 글자뿐인 것, 기존 값에 이미
    들어 있는 것, 띄어쓰기·기호만 다른 것은 바뀐 것으로 보지 않는다."""
    changed, ignored = [], []
    old_vals = {f: (getattr(x, f) or '') for f in FIELDS}
    for f in FIELDS:
        new, old = ev.get(f) or '', old_vals[f]
        if not new or new == old or f == 'date':
            continue
        if f in ('start_time', 'end_time'):
            changed.append(f)  # 시간이 바뀌는 건 실제 변경 (칸이 밀려도 시간 칸엔 시간만 들어온다)
            continue
        if f == 'category':
            if old:
                ignored.append({'field': f, 'value': new, 'reason': '구분은 기존 값 유지'})
            else:
                changed.append(f)
            continue
        n_new, n_old = _norm_title(new), _norm_title(old)
        if n_new == n_old:
            continue  # 띄어쓰기·기호만 다름
        if is_date_only(new):
            ignored.append({'field': f, 'value': new, 'reason': '날짜·시간 글자뿐 (칸 밀림)'})
            continue
        other = next((g for g in _TEXT_FIELDS if g != f and old_vals[g] and len(n_new) >= 2 and
                      (n_new == _norm_title(old_vals[g]) or n_new in _norm_title(old_vals[g]) or
                       (len(_norm_title(old_vals[g])) >= 3 and _norm_title(old_vals[g]) in n_new
                        and not (n_old and n_old in n_new)))), None)
        if other:
            ignored.append({'field': f, 'value': new, 'reason': f'기존 {LABELS[other]} 값이 잘못 들어옴'})
            continue
        if n_old and n_new in n_old:
            ignored.append({'field': f, 'value': new, 'reason': '기존 값에 이미 들어 있음'})
            continue
        if n_old and _similar(new, old) >= 0.85:
            ignored.append({'field': f, 'value': new, 'reason': '표기만 조금 다름'})
            continue
        if f == 'title' and how != 'title':
            ignored.append({'field': f, 'value': new, 'reason': '같은 일정으로 보여 기존 제목 유지'})
            continue
        changed.append(f)
    return changed, ignored


def _ignored_note(ignored: list[dict]) -> str:
    return ' · '.join(f"{LABELS[i['field']]} '{i['value'][:20]}' 무시({i['reason']})" for i in ignored)


def preview(events: list[dict]) -> list[dict]:
    """일정마다 상태 — new(신규) / changed(정말 바뀐 칸) / same(이미 같은 내용) / check(칸이 밀린 듯, 확인 필요).
    기존 일정과 같은 일정이면, 잘못 읽은 것으로 보이는 칸은 기존 값으로 되돌려 보여 준다(그대로 반영해도 안전).
    'match_id' 는 대응하는 기존 일정, 'note' 는 검증 내용."""
    out, taken = [], set()
    with db.SessionLocal() as s:
        for e in events:
            ev = clean_event(e)
            if ev is None:
                continue
            x, how = _match(s, ev, taken)
            if x is None:
                bad = is_date_only(ev['title'])
                out.append({**ev, 'state': 'check' if bad else 'new', 'changed': [], 'match_id': None,
                            'note': '제목이 날짜·시간뿐 — 칸이 밀린 것 같습니다. 고친 뒤 반영하세요.' if bad else ''})
                continue
            taken.add(x.id)
            changed, ignored = verify_changes(x, ev, how)
            row = {**ev}
            for i in ignored:
                row[i['field']] = getattr(x, i['field']) or ''
            for f in FIELDS:  # 새로 읽은 값이 비어 있는 칸은 기존 값 그대로
                if not row[f]:
                    row[f] = getattr(x, f) or ''
            found = {'title': '', 'contains': '기존 일정 제목이 들어 있어 같은 일정으로 봄',
                     'similar': f"비슷한 제목의 기존 일정 '{x.title[:30]}'", 'time': f"같은 시간의 기존 일정 '{x.title[:30]}'"}
            note = ' · '.join(n for n in [found.get(how, ''), _ignored_note(ignored)] if n)
            out.append({**row, 'state': 'changed' if changed else 'same', 'changed': changed, 'match_id': x.id,
                        'note': note})
    return out


def apply_events(events: list[dict], import_id: int | None = None, by: str = '') -> dict:
    """일정 목록을 반영. 'match_id' 가 있는 줄(미리보기에서 사람이 확인한 줄)은 그 일정을 줄 내용대로 고치고,
    없는 줄은 같은 일정을 찾아 정말 바뀐 칸만 고친다(verify_changes) — 같은 일정이 없으면 새로 만든다.
    제목이 날짜·시간뿐인 새 일정(칸이 밀린 줄)은 넣지 않는다(skipped)."""
    added = updated = same = skipped = 0
    taken = set()
    with db.SessionLocal() as s:
        for e in events:
            ev = clean_event(e)
            if ev is None:
                continue
            x = s.get(Schedule, int(e['match_id'])) if str(e.get('match_id') or '').isdigit() else None
            if x is not None:
                fields = [f for f in FIELDS if ev[f] and (getattr(x, f) or '') != ev[f]]
            else:
                x, how = _match(s, ev, taken)
                fields = verify_changes(x, ev, how)[0] if x is not None else []
            if x is not None:
                taken.add(x.id)
            if x is None:
                if is_date_only(ev['title']):
                    skipped += 1
                    continue
                s.add(Schedule(**ev, source='mail' if import_id else 'manual', import_id=import_id, created_by=by))
                s.flush()
                added += 1
                continue
            for f in fields:
                setattr(x, f, ev[f])
            if fields:
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
    return {'added': added, 'updated': updated, 'same': same, 'skipped': skipped}


def apply_all_pending(by: str = '') -> dict:
    """정리가 끝났는데 아직 반영하지 않은 메일을 보낸 날짜 순(옛 메일 먼저 → 최신 메일이 덮어씀)으로 모두 반영."""
    todo = [i for i in list_imports(limit=10000) if i['status'] == 'done' and not i['applied']]
    todo.sort(key=lambda i: (i['mail_date'] or '9999', i['id']))
    total = {'mails': 0, 'added': 0, 'updated': 0, 'same': 0, 'skipped': 0}
    for i in todo:
        r = apply_events(i['events'], i['id'], by)
        total['mails'] += 1
        for k in ('added', 'updated', 'same', 'skipped'):
            total[k] += r[k]
    return total


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())
