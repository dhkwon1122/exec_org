"""임원 부재 일정 — 휴가·출장·교육 등으로 자리를 비우는 기간 (캘린더의 '임원 부재' 탭, 임원 화면)."""
from datetime import date, timedelta

from sqlalchemy import select

from services import db
from services.db import Absence, Person

KINDS = ['휴가', '출장', '해외출장', '교육', '외부 일정', '병가', '기타']
HALVES = ['', '오전', '오후']


def _dict(a: Absence, p: Person | None) -> dict:
    return {'id': a.id, 'person_id': a.person_id, 'person_name': p.name if p else '',
            'position': (p.position or '') if p else '', 'kind': a.kind or '기타', 'start_date': a.start_date,
            'end_date': a.end_date, 'half': a.half or '', 'place': a.place or '', 'deputy': a.deputy or '',
            'note': a.note or '', 'created_by': a.created_by or '',
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
    half = data.get('half') or ''
    if half not in HALVES:
        raise ValueError(f'알 수 없는 반차: {half}')
    if half and end != start:
        half = ''  # 반차는 하루짜리만
    return {'kind': kind, 'start_date': start.isoformat(), 'end_date': end.isoformat(), 'half': half,
            'place': str(data.get('place') or '').strip()[:300], 'deputy': str(data.get('deputy') or '').strip()[:100],
            'note': str(data.get('note') or '').strip()}


def save(data: dict, person_ids: list[int] | None = None, aid: int | None = None, by: str = '') -> list[dict]:
    """새로 만들 때는 person_ids 마다 한 건씩, 고칠 때(aid)는 그 한 건. 저장한 부재 목록을 돌려준다."""
    vals = _check(data)
    with db.SessionLocal() as s:
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
            rows = [Absence(person_id=pid, created_by=by, **vals) for pid in ids]
            s.add_all(rows)
        s.commit()
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
    """기간과 겹치는 부재 — 시작일, 이름 순. query: 이름·장소·대결자·메모."""
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
        rows = [r for r in rows if q in ' '.join([r['person_name'], r['place'], r['deputy'], r['note'],
                                                  r['kind']]).lower()]
    return sorted(rows, key=lambda r: (r['start_date'], r['person_name'], r['id']))


def on_day(day: str, rows: list[dict] | None = None) -> list[dict]:
    rows = rows if rows is not None else list_range(day, day)
    return [r for r in rows if r['start_date'] <= day <= r['end_date']]


def days(r: dict) -> float:
    """부재 일수 (반차 0.5, 주말 포함 달력 일수)."""
    if r['half']:
        return 0.5
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
