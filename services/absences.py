"""임원 부재 일정 — 휴가·출장·교육 등으로 자리를 비우는 기간 (캘린더의 '임원 부재' 탭, 임원 화면).

항목: 임원, 종류, 시작일, 종료일, 부재 시간대(선택 — '09:00~12:00', '오후' 등), 행선지(place), 일정 내용(content), 메모.

메일에서 뽑기: extract() — AI(없거나 실패하면 규칙)로 위 항목을 뽑고,
이름·별칭으로 등록된 임원을 찾는다. preview() 로 신규/이미 있음/겹침/임원 모름을 보여 주고 apply_rows() 로 넣는다.
"""
import re
from datetime import date, timedelta

from sqlalchemy import select

from services import db, llm
from services.db import Absence, Person

KINDS = ['휴가', '출장', '해외출장', '교육', '외부 일정', '병가', '기타']
_TIME = re.compile(r'(?:오전|오후)?\s*\d{1,2}\s*[:;시]\s*\d{0,2}\s*분?')


def norm_time_range(v: str) -> str:
    """부재 시간대 — '9:00-12:00' / '14시~18시' → '09:00~12:00', 시각 하나면 '14:00~', 글자('오후' 등)는 그대로."""
    from services.schedules import norm_time
    v = re.sub(r'\s+', ' ', str(v or '')).strip()[:50]
    times = [t for t in (norm_time(m.group(0)) for m in _TIME.finditer(v)) if t]
    if len(times) >= 2:
        return f'{times[0]}~{times[1]}'
    if len(times) == 1:
        return f'{times[0]}~'
    return v


def _dict(a: Absence, p: Person | None) -> dict:
    return {'id': a.id, 'source_mail_id': a.source_mail_id, 'person_id': a.person_id, 'person_name': p.name if p else '',
            'position': (p.position or '') if p else '', 'kind': a.kind or '기타', 'start_date': a.start_date,
            'end_date': a.end_date, 'time_range': a.time_range or '', 'place': a.place or '',
            'content': a.content or '', 'note': a.note or '', 'created_by': a.created_by or '',
            'updated_at': a.updated_at.strftime('%Y-%m-%d %H:%M') if a.updated_at else ''}


def _check(data: dict) -> dict:
    try:
        start = date.fromisoformat(str(data.get('start_date') or '')[:10])
    except ValueError:
        raise ValueError('시작일을 입력하세요.') from None
    end_raw = str(data.get('end_date') or '')[:10]
    try:
        end = date.fromisoformat(end_raw) if end_raw else start
    except ValueError:
        raise ValueError('종료일 형식이 올바르지 않습니다.') from None
    if end < start:
        raise ValueError('종료일이 시작일보다 빠릅니다.')
    kind = data.get('kind') or '휴가'
    if kind not in KINDS:
        raise ValueError(f'알 수 없는 부재 종류: {kind}')
    return {'kind': kind, 'start_date': start.isoformat(), 'end_date': end.isoformat(),
            'time_range': norm_time_range(data.get('time_range') or ''),
            'place': str(data.get('place') or '').strip()[:300], 'content': str(data.get('content') or '').strip(),
            'note': str(data.get('note') or '').strip()}


def save(data: dict, person_ids: list[int] | None = None, aid: int | None = None, by: str = '',
         source_mail_id: int | None = None, *, session=None) -> list[dict]:
    """새로 만들 때는 person_ids 마다 한 건씩, 고칠 때(aid)는 그 한 건. 저장한 부재 목록을 돌려준다."""
    vals = _check(data)
    with db.transaction(session) as s:
        if aid is not None:
            a = s.get(Absence, aid)
            if a is None:
                raise ValueError('부재 일정을 찾을 수 없습니다.')
            for k, v in vals.items():
                setattr(a, k, v)
            rows = [a]
        else:
            ids = list(dict.fromkeys(int(p) for p in (person_ids or [])))
            if not ids:
                raise ValueError('임원을 고르세요.')
            if len(s.scalars(select(Person.id).where(Person.id.in_(ids))).all()) != len(ids):
                raise ValueError('임원을 찾을 수 없습니다.')
            rows = [Absence(person_id=pid, created_by=by, source_mail_id=source_mail_id, **vals) for pid in ids]
            s.add_all(rows)
        s.flush()
        return [_dict(a, s.get(Person, a.person_id)) for a in rows]


def get(aid: int) -> dict | None:
    with db.SessionLocal() as s:
        a = s.get(Absence, aid)
        return _dict(a, s.get(Person, a.person_id)) if a else None


def delete(aid: int) -> None:
    with db.SessionLocal() as s:
        a = s.get(Absence, aid)
        if a:
            s.delete(a)
            s.commit()


def list_range(date_from: str, date_to: str, person_id: int | None = None, kinds: list[str] | None = None,
               query: str = '') -> list[dict]:
    """기간과 겹치는 부재 — 시작일, 이름 순. query: 이름·행선지·일정 내용·메모."""
    with db.SessionLocal() as s:
        stmt = (select(Absence, Person).join(Person, Person.id == Absence.person_id)
                .where(Absence.start_date <= date_to, Absence.end_date >= date_from))
        if person_id is not None:
            stmt = stmt.where(Absence.person_id == person_id)
        if kinds:
            stmt = stmt.where(Absence.kind.in_(kinds))
        rows = [_dict(a, p) for a, p in s.execute(stmt)]
    q = (query or '').strip().lower()
    if q:
        rows = [r for r in rows if q in ' '.join([r['person_name'], r['place'], r['content'], r['note'],
                                                  r['kind']]).lower()]
    return sorted(rows, key=lambda r: (r['start_date'], r['person_name'], r['id']))


def on_day(day: str, rows: list[dict] | None = None) -> list[dict]:
    rows = rows if rows is not None else list_range(day, day)
    return [r for r in rows if r['start_date'] <= day <= r['end_date']]


def days(r: dict) -> int:
    """부재 일수 (주말 포함 달력 일수)."""
    return (date.fromisoformat(r['end_date']) - date.fromisoformat(r['start_date'])).days + 1


def overlaps(person_id: int, start: str, end: str, exclude: int | None = None) -> list[dict]:
    """같은 임원의 겹치는 부재 (등록할 때 경고용)."""
    return [r for r in list_range(start, end or start, person_id=person_id) if r['id'] != exclude]


def for_person(person_id: int, limit: int = 20) -> list[dict]:
    """임원 화면용 — 지금·앞으로의 부재 먼저, 그다음 최근 지난 부재."""
    today = date.today().isoformat()
    rows = list_range('0000-01-01', '9999-12-31', person_id=person_id)
    upcoming = [r for r in rows if r['end_date'] >= today]
    past = sorted([r for r in rows if r['end_date'] < today], key=lambda r: r['start_date'], reverse=True)
    return (upcoming + past)[:limit]


def week_of(d: date) -> tuple[str, str]:
    start = d - timedelta(days=d.weekday())
    return start.isoformat(), (start + timedelta(days=6)).isoformat()


# ── 메일에서 뽑기 ─────────────────────────────────────────────────────

EXTRACT_SYSTEM = f"""너는 임원조직 담당자의 비서다. 메일(휴가·출장·교육·외부 일정 안내, 부재 현황표 등)을 읽고
임원이 자리를 비우는 일정을 사람마다 한 건씩 뽑는다.
- person: 임원 이름(직위 빼고 이름만). 여러 명이 같이 가면 사람마다 따로.
- kind: {', '.join(KINDS)} 중 하나 (연차·휴가·리프레시 → 휴가, 국내 출장 → 출장, 국외·해외 → 해외출장).
- start_date, end_date: YYYY-MM-DD. 연도가 없으면 메일 날짜의 연도, '다음주 월요일' 같은 표현은 메일 날짜 기준.
  하루면 둘 다 같은 날. '10/26~30' 은 10월 26일부터 30일까지.
- time_range: 하루 중 일부만 비우면 그 시간대('09:00~12:00', '오후' 등), 종일이면 빈 문자열.
- place: 행선지(출장지·교육 기관·방문처 등), content: 일정 내용(무슨 일로 비우는지 — 학회 참석, 고객사 방문 등),
  note: 그 밖의 내용(연락처·대결자 등).
- 부재가 아닌 내용(회의 일정·업무 요청 등)은 뽑지 않는다. 입력에 없는 내용은 만들지 않는다.
반드시 JSON 만 출력: {{"absences": [{{"person": "", "kind": "", "start_date": "", "end_date": "", "time_range": "",
"place": "", "content": "", "note": ""}}]}}"""

_KIND_WORDS = [('해외출장', r'해외\s*출장|국외\s*출장|해외'), ('병가', r'병가|병원|입원'), ('교육', r'교육|연수|과정'),
               ('출장', r'출장'), ('휴가', r'휴가|연차|반차|리프레시|하계|동계|월차'), ('외부 일정', r'외부\s*일정|외근|외부\s*행사')]
_DEPUTY = re.compile(r'(?:대결자?|업무\s*대행|대행|직무\s*대리)\s*[:：]?\s*([가-힣A-Za-z]{2,10}(?:\s*(?:부사장|사장|전무|상무|'
                     r'마스터|Master|펠로우|Fellow|팀장|센터장|님))?)')
_HALF = re.compile(r'(오전|오후)\s*반차|반차\s*\(?(오전|오후)')
_TIME_RANGE = re.compile(r'((?:오전|오후)?\s*\d{1,2}\s*[:;시]\s*\d{0,2}\s*분?)\s*[~\-–]\s*((?:오전|오후)?\s*\d{1,2}\s*[:;시]\s*\d{0,2}\s*분?)')


def _clean_row(r: dict, base: date) -> dict | None:
    def d(v):
        v = str(v or '').strip()[:10].replace('.', '-').replace('/', '-')
        try:
            return date.fromisoformat(v).isoformat()
        except ValueError:
            return ''
    start = d(r.get('start_date'))
    if not start or not str(r.get('person') or '').strip():
        return None
    end = d(r.get('end_date')) or start
    if end < start:
        end = start
    kind = str(r.get('kind') or '').strip()
    kind = kind if kind in KINDS else ('휴가' if '휴가' in kind or '연차' in kind else '기타')
    return {'person': str(r['person']).strip()[:50], 'kind': kind, 'start_date': start, 'end_date': end,
            'time_range': norm_time_range(r.get('time_range') or ''), 'place': str(r.get('place') or '').strip()[:300],
            'content': str(r.get('content') or '').strip()[:500], 'note': str(r.get('note') or '').strip()[:500]}


def ai_extract(text: str, mail_date: str) -> list[dict]:
    base = _base(mail_date)
    names = ', '.join(p.name for p in _people())[:2000]
    raw = llm.chat(EXTRACT_SYSTEM, f"메일 날짜: {base.isoformat()} ({'월화수목금토일'[base.weekday()]})\n"
                                   f"등록된 임원 이름(참고): {names}\n\n메일:\n{text[:llm.MAX_INPUT_CHARS]}")
    rows = [_clean_row(r, base) for r in llm._extract_json(raw).get('absences') or [] if isinstance(r, dict)]
    return [r for r in rows if r]


def _base(mail_date: str) -> date:
    try:
        return date.fromisoformat(mail_date)
    except (TypeError, ValueError):
        return date.today()


def _people() -> list[Person]:
    with db.SessionLocal() as s:
        return list(s.scalars(select(Person).order_by(Person.active.desc(), Person.id)))


def _names(p: Person) -> list[str]:
    return [n for n in [p.name] + [a.strip() for a in (p.aliases or '').split(',')] if len(n) >= 2]


def rule_extract(text: str, mail_date: str, subject: str = '') -> list[dict]:
    """LLM 없이 — 등록된 임원 이름이 나오는 줄에서 부재 종류 낱말과 날짜(범위)를 읽는다."""
    from services.schedules import _find_date  # 날짜 읽기는 원장님 일정과 같은 규칙
    base = _base(mail_date)
    people = _people()
    out = []
    subj_kind = next((k for k, pat in _KIND_WORDS if re.search(pat, subject or '')), '')
    for line in (text or '').splitlines():
        deputy = ''
        dm = _DEPUTY.search(line)
        if dm:  # '(대결: 박민수 상무)' — 대결자는 부재가 아니다
            deputy = dm.group(1).strip()
            line = re.sub(r'\(\s*\)', '', line[:dm.start()] + line[dm.end():]).rstrip()
        who = [p for p in people if any(n in line for n in _names(p))]
        if not who:
            continue
        kind = next((k for k, pat in _KIND_WORDS if re.search(pat, line)), subj_kind)
        found = _find_date(line, base)
        if not kind or not found:
            continue
        start, rest = found
        end = start
        tail = line[line.find(start[5:7].lstrip('0')):]  # 시작 날짜 뒤에 '~30' 같은 끝 날짜가 붙었는지
        second = _find_date(rest, base)
        if second and second[0] >= start:
            end = second[0]
        else:
            m = re.search(r'[~\-–]\s*(?:(\d{1,2})\s*[./월]\s*)?(\d{1,2})\s*일?(?!\d)', tail)
            if m:
                try:
                    s = date.fromisoformat(start)
                    e = date(s.year, int(m.group(1) or s.month), int(m.group(2)))
                    end = e.isoformat() if e >= s else start
                except ValueError:
                    pass
        h = _HALF.search(line)
        tr = _TIME_RANGE.search(line)
        time_range = (h.group(1) or h.group(2)) if h else (f'{tr.group(1)}~{tr.group(2)}' if tr else '')
        place = re.search(r'\(([^()]{2,40})\)\s*$', line.strip())  # 줄 끝 괄호 '(미국)' 은 행선지로
        for p in who:
            out.append({'person': p.name, 'kind': kind, 'start_date': start, 'end_date': end,
                        'time_range': norm_time_range(time_range), 'place': place.group(1).strip() if place else '',
                        'content': line.strip()[:200], 'note': f'대결: {deputy}' if deputy else ''})
    return out


def extract(text: str, mail_date: str, subject: str = '') -> tuple[list[dict], str]:
    """메일 → (부재 후보, 어떻게 읽었나). AI 가 있으면 AI, 없거나 실패하면 규칙."""
    if llm.is_configured():
        try:
            return ai_extract(f'제목: {subject}\n\n{text}' if subject else text, mail_date), 'AI 로 읽었습니다.'
        except Exception as e:  # noqa: BLE001
            return rule_extract(text, mail_date, subject), f'AI 실패({str(e)[:80]}) — 규칙으로 읽었습니다.'
    return rule_extract(text, mail_date, subject), 'LLM 미설정 — 임원 이름·날짜가 있는 줄을 규칙으로 읽었습니다.'


def match_person(name: str) -> tuple[int | None, str]:
    """이름(직위가 붙어 있어도) → (임원 id, 찾은 이름). 같은 이름이 여럿이면 추적 중인 임원 먼저."""
    name = (name or '').strip()
    if not name:
        return None, ''
    people = _people()
    for p in people:  # 이름이 정확히 같거나
        if name == p.name or name in _names(p):
            return p.id, p.name
    for p in people:  # 적힌 글자 안에 이름·별칭이 들어 있으면 ('김영수 부사장')
        if any(n in name for n in _names(p)):
            return p.id, p.name
    return None, ''


def preview(rows: list[dict]) -> list[dict]:
    """후보마다 상태 — new(신규) / same(이미 등록됨) / overlap(그 기간에 다른 부재가 있음) / unknown(임원 모름).
    상태는 표의 '상태' 칸 글자와 줄 색으로만 보여 준다."""
    out = []
    for r in rows:
        r = {**r}
        pid, pname = match_person(r.get('person', ''))
        r['person_id'] = pid
        if pid is None:
            r['state'] = 'unknown'
            out.append(r)
            continue
        others = overlaps(pid, r['start_date'], r['end_date'])
        key = (r['kind'], r['start_date'], r['end_date'], norm_time_range(r.get('time_range') or ''))
        if any((o['kind'], o['start_date'], o['end_date'], o['time_range']) == key for o in others):
            r['state'] = 'same'
        else:
            r['state'] = 'overlap' if others else 'new'
        out.append(r)
    return out


def apply_rows(rows: list[dict], by: str = '', source_mail_id: int | None = None, *, session=None) -> dict:
    """미리보기 줄을 등록 — 이미 같은 부재가 있으면 건너뛰고, 임원을 못 찾은 줄도 건너뛴다."""
    added = same = unknown = 0
    with db.transaction(session) as s:
        for r in preview([x for x in (_clean_row(x, date.today()) for x in rows) if x]):
            if r['state'] == 'unknown':
                unknown += 1
                continue
            if r['state'] == 'same':
                same += 1
                continue
            save(r, person_ids=[r['person_id']], by=by, source_mail_id=source_mail_id, session=s)
            added += 1
    return {'added': added, 'same': same, 'unknown': unknown}
