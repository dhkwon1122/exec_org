"""People in SAIT(Researcher-board) 인사 정보에서 임원 기본 정보 불러오기.

Researcher-board 는 헤드카운트 원천을 정제해 같은 PostgreSQL(DATABASE_URL — 로그인용 app_users 와
같은 DB)의 researchers 테이블에 적재한다. 그 position(직급) 컬럼은 원본 영문 임원 표기를
상무/부사장/사장/고문 으로 바꿔 둔 값이고, Master·Fellow 처럼 매핑 없는 직급은 원본 영문 그대로라,
이 값으로 임원만 골라 이름·직급·소속을 가져온다(대소문자·앞뒤 공백 무시). researchers 테이블은 **읽기만** 한다.

- 바로 바꾸지 않고 먼저 **제안**한다(plan_sync) — 사람이 고른 것만 반영(apply_sync).
- 이미 불러온 임원(source_id = 사번)의 이름·직위·소속이 People in SAIT 값과 다르면 덮어쓸지 제안한다.
  덮어쓰지 않고 유지한 값은 기억해 두고 다음에는 기본 해제로 제안한다.
  이 앱에서만 쓰는 별칭·메모·추적 여부는 건드리지 않는다.
- 직접 등록해 둔 임원과 이름이 같으면(사번 미연결) 그 사람에게 사번을 연결한다 — 같은 이름이 둘 이상이면
  잘못 합칠 수 있으므로 새로 추가한다.
- 새로 추가하는 임원에게는 '성+직위' 별칭(예: 김부사장, 김 부사장, 영문 직급은 김마스터·김 Master)을 넣어 원문 자동 연결이 바로 되게
  한다. 같은 성·직위의 임원이 여럿이면 누구인지 모호하므로 넣지 않는다.
- 과거 임원(월별 이력 researchers_history 에서 한 번이라도 임원 직급이었던 사람, 지금은 아님)도 '추적 중지' 상태로
  추가할 수 있게 제안한다 — 목록·자동 연결에서는 빠지고, 그 사람의 기록·경력은 이어 볼 수 있다.
- 사번이 연결된 모든 임원의 경력 이력(직급·소속이 바뀐 달, 인사발령 hr_orders)을 반영할 때마다 person_career 에
  쌓는다. 원천에서 사라져도 지우지 않는다(누적).
- People in SAIT 최신 인원 목록에 더 이상 없는 임원(퇴임·이동 등)은 지우거나 추적 중지하지 않고
  결과에 알려만 준다 — 기록은 그대로 남아야 하므로 판단은 사람이 한다.
"""
import json
import os
from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import MetaData, Table, create_engine, func, select, text

from services import db, users
from services.db import Person, PersonCareer

EXEC_POSITIONS_DEFAULT = '사장,부사장,상무,고문,Master,Fellow'
# 영문 그대로 적재되는 직급의 한글 호칭 — '성+호칭' 별칭(김마스터)을 만들 때 쓴다.
POSITION_READINGS = {'master': ['마스터'], 'fellow': ['펠로우', '펠로']}


def exec_positions() -> list[str]:
    return [p.strip() for p in os.environ.get('EXEC_ORG_POSITIONS', EXEC_POSITIONS_DEFAULT).split(',') if p.strip()]


def _canonical_positions() -> dict[str, str]:
    """소문자 직급 → 설정에 적힌 표기 (Researcher-board 값이 'MASTER'·' Fellow' 여도 같은 직급으로)."""
    out = {}
    for p in exec_positions():
        out.setdefault(p.lower(), p)
    return out


class SyncError(RuntimeError):
    """People in SAIT DB 미설정/연결 실패/researchers 테이블 없음."""


def is_available() -> bool:
    return bool(users.user_db_url())


@contextmanager
def _people_db():
    url = users.user_db_url()
    if not url:
        raise SyncError('People in SAIT DB(DATABASE_URL)가 설정되지 않았습니다.')
    connect_args = {'connect_timeout': 5} if url.startswith('postgresql') else {}
    try:
        engine = create_engine(url, future=True, connect_args=connect_args)
    except Exception as e:  # noqa: BLE001 — 드라이버 미설치·URL 형식 오류
        raise SyncError(f'People in SAIT DB 에 연결할 수 없습니다: {type(e).__name__}: {e}') from e
    try:
        yield engine
    except SyncError:
        raise
    except Exception as e:  # noqa: BLE001
        raise SyncError(f'People in SAIT DB 조회 실패: {type(e).__name__}') from e
    finally:
        engine.dispose()


def _table(engine, name: str) -> Table | None:
    try:
        return Table(name, MetaData(), autoload_with=engine)
    except Exception:  # noqa: BLE001 — 없는 테이블(예전 Researcher-board 적재본)
        return None


def _rows(engine, stmt) -> list[dict]:
    with engine.begin() as conn:
        if engine.dialect.name == 'postgresql':
            conn.execute(text('SET LOCAL statement_timeout = 15000'))
        return [dict(r._mapping) for r in conn.execute(stmt)]


def _cols(t: Table, *names: str) -> list:
    return [t.c[c] for c in names if c in t.c]


def _s(v) -> str:
    return str(v if v is not None else '').strip()


def _ym(r: dict) -> tuple[int, int]:
    try:
        return int(float(_s(r.get('valid_year')))), int(float(_s(r.get('valid_month'))))
    except ValueError:
        return 0, 0


def _is_exec(t: Table, canon: dict):
    return func.lower(func.trim(t.c.position)).in_(list(canon))


def _clean(v) -> str:
    v = _s(v)
    if v.lower() in ('nan', 'none', 'nat'):
        return ''
    return v[:-2] if v.endswith('.0') and v[:-2].isdigit() else v


def _person_row(r: dict, canon: dict) -> dict:
    pos = _s(r.get('position'))
    return {'source_id': _s(r.get('researcher_id')), 'name': _s(r.get('name')),
            'position': canon.get(pos.lower(), pos), 'org': _s(r.get('department') or r.get('org_code')),
            'employment_status': _clean(r.get('employment_status')), 'birth_year': _clean(r.get('birth_year'))[:4],
            'hire_date': _clean(r.get('hire_date'))[:10], 'promotion_date': _clean(r.get('promotion_date'))[:10],
            'education': ''}


def _sort_execs(execs, canon):
    order = list(canon.values())
    return sorted(execs, key=lambda x: (order.index(x['position']) if x['position'] in order else 99, x['name']))


def _fetch(engine) -> tuple[list[dict], list[dict]]:
    """(현재 임원, 과거 임원). 과거 임원 = 월별 이력(researchers_history)에서 한 번이라도 임원 직급이었거나
    researchers 에 지난 시점(is_current=N)으로 남은 임원 중 지금 임원 목록에 없는 사람 — 마지막 임원 시점 값으로."""
    t = _table(engine, 'researchers')
    if t is None:
        raise SyncError('People in SAIT DB 에서 researchers 테이블을 읽을 수 없습니다 '
                        '(Researcher-board 의 load_to_db 적재 여부 확인)')
    need = {'researcher_id', 'name', 'position'}
    if not need <= set(t.c.keys()):
        raise SyncError(f'researchers 테이블에 필요한 컬럼이 없습니다: {sorted(need - set(t.c.keys()))}')
    canon = _canonical_positions()
    base = ('researcher_id', 'name', 'position', 'department', 'org_code', 'employment_status', 'birth_year',
            'hire_date', 'promotion_date')
    rows = _rows(engine, select(*_cols(t, *base, 'is_current', 'valid_year', 'valid_month'))
                 .where(_is_exec(t, canon)))
    current, past = {}, {}
    for r in rows:
        e = _person_row(r, canon)
        if not e['source_id'] or not e['name']:
            continue
        if _s(r.get('is_current')) == 'N':
            past[e['source_id']] = {**e, 'until': '%04d-%02d' % _ym(r) if _ym(r)[0] else ''}
        else:
            current[e['source_id']] = e  # 같은 사번이 여러 행이면 마지막 값

    h = _table(engine, 'researchers_history')
    if h is not None and {'researcher_id', 'position', 'valid_year', 'valid_month'} <= set(h.c.keys()):
        for r in sorted(_rows(engine, select(*_cols(h, *base, 'valid_year', 'valid_month')).where(_is_exec(h, canon))),
                        key=_ym):  # 오래된 달부터 → 마지막(가장 최근) 임원 시점이 남는다
            e = _person_row(r, canon)
            if e['source_id'] and e['source_id'] not in current and (e['name'] or e['source_id'] in past):
                e['name'] = e['name'] or past[e['source_id']]['name']
                past[e['source_id']] = {**e, 'until': '%04d-%02d' % _ym(r) if _ym(r)[0] else ''}
    for sid in current:
        past.pop(sid, None)
    edu = _fetch_education(engine, [*current, *past])
    for e in [*current.values(), *past.values()]:
        e['education'] = edu.get(e['source_id'], '')
    return _sort_execs(current.values(), canon), _sort_execs(past.values(), canon)


DEGREES = {'박사': '박', '석사': '석', '학사': '학', '전문대': '전', '고교': '고'}  # 높은 학위부터


def _fetch_education(engine, sids) -> dict[str, str]:
    """사번별 최종학력 한 줄 — Researcher-board 프로필과 같은 표기('박)○○대학교 전공', 고교는 전공 없이)."""
    t = _table(engine, 'education')
    if t is None or not {'researcher_id', 'degree'} <= set(t.c.keys()):
        return {}
    best: dict[str, tuple[int, dict]] = {}
    order = list(DEGREES)
    for part in _chunks(sorted(set(sids))):
        for r in _rows(engine, select(*_cols(t, 'researcher_id', 'degree', 'school', 'major'))
                       .where(t.c.researcher_id.in_(part))):
            deg = _s(r.get('degree'))
            if deg not in DEGREES:
                continue
            sid = _s(r['researcher_id'])
            if sid not in best or order.index(deg) < best[sid][0]:
                best[sid] = (order.index(deg), r)
    out = {}
    for sid, (i, r) in best.items():
        deg = order[i]
        school, major = _clean(r.get('school')) or '-', _clean(r.get('major')) or '-'
        out[sid] = f'{DEGREES[deg]}){school}' if deg == '고교' else f'{DEGREES[deg]}){school} {major}'
    return out


def _chunks(seq, n=500):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _fetch_careers(engine, sids) -> dict[str, list[dict]]:
    """사번별 경력 이력 항목 — 월별 이력에서 직급·소속이 바뀐 달, 인사발령 전부. 날짜순."""
    sids = sorted({s for s in sids if s})
    out: dict[str, list[dict]] = {s: [] for s in sids}
    if not sids:
        return out
    canon = _canonical_positions()
    h = _table(engine, 'researchers_history')
    if h is not None and {'researcher_id', 'position', 'valid_year', 'valid_month'} <= set(h.c.keys()):
        snaps: dict[str, list[dict]] = {}
        for part in _chunks(sids):
            for r in _rows(engine, select(*_cols(h, 'researcher_id', 'position', 'department', 'org_code',
                                                'valid_year', 'valid_month')).where(h.c.researcher_id.in_(part))):
                if _ym(r)[0]:
                    snaps.setdefault(_s(r['researcher_id']), []).append(r)
        for sid, rs in snaps.items():
            prev = None
            for r in sorted(rs, key=_ym):
                pos = _s(r.get('position'))
                cur = (canon.get(pos.lower(), pos), _s(r.get('department') or r.get('org_code')))
                if cur != prev:
                    ym = '%04d-%02d' % _ym(r)
                    out[sid].append({'key': f'snap:{ym}', 'kind': '직급·소속', 'date': ym, 'position': cur[0],
                                     'org': cur[1], 'title': '', 'assignment': ''})
                prev = cur
    o = _table(engine, 'hr_orders')
    if o is not None and {'researcher_id', 'order_date'} <= set(o.c.keys()):
        for part in _chunks(sids):
            for r in _rows(engine, select(*_cols(o, 'researcher_id', 'order_date', 'order_name', 'order_dep',
                                                'order_cl', 'order_assignment')).where(o.c.researcher_id.in_(part))):
                d, name, dep = _s(r.get('order_date'))[:10], _s(r.get('order_name')), _s(r.get('order_dep'))
                if not d:
                    continue
                out.setdefault(_s(r['researcher_id']), []).append({
                    'key': f'order:{d}:{name}:{dep}'[:300], 'kind': '발령', 'date': d,
                    'position': _s(r.get('order_cl')), 'org': dep, 'title': name,
                    'assignment': _s(r.get('order_assignment'))})
    for v in out.values():
        v.sort(key=lambda c: (c['date'], c['kind']))
    return out


def fetch_executives() -> list[dict]:
    """researchers 테이블에서 현재 재직 중인 임원(직급이 EXEC_ORG_POSITIONS 중 하나) 목록."""
    with _people_db() as engine:
        return _fetch(engine)[0]


def fetch_past_executives() -> list[dict]:
    """지금은 임원 목록에 없지만 과거에 임원이었던 사람 (마지막 임원 시점 값, until=YYYY-MM)."""
    with _people_db() as engine:
        return _fetch(engine)[1]


def fetch_careers(sids) -> dict[str, list[dict]]:
    with _people_db() as engine:
        return _fetch_careers(engine, sids)


def _title_aliases(execs: list[dict]) -> dict[str, list[str]]:
    """'성+직위' 별칭(김부사장, 김 부사장 / 김마스터, 김 마스터, 김 Master).
    같은 성·직위가 둘 이상이면 모호하므로 만들지 않는다."""
    count: dict[tuple[str, str], int] = {}
    for e in execs:
        key = (e['name'][:1], e['position'])
        count[key] = count.get(key, 0) + 1
    out = {}
    for e in execs:
        surname, pos = e['name'][:1], e['position']
        if not pos or count[(surname, pos)] > 1:
            out[e['source_id']] = []
            continue
        readings = POSITION_READINGS.get(pos.lower())
        if readings:  # 영문 직급: 한글 호칭으로, 영문은 띄어 쓴 형태만 (김Master 는 쓰지 않음)
            out[e['source_id']] = [a for r in readings for a in (f'{surname}{r}', f'{surname} {r}')] + [f'{surname} {pos}']
        else:
            out[e['source_id']] = [f'{surname}{pos}', f'{surname} {pos}']
    return out


FIELDS = {'name': '이름', 'position': '직위', 'org': '소속'}
# 인사 정보 — 비어 있으면 제안 없이 채우고(auto), 값이 있는데 다르면 덮어쓸지 제안
HR_FIELDS = {'employment_status': '재직상태', 'birth_year': '출생연도', 'hire_date': '입사일',
             'promotion_date': '승격일', 'education': '최종학력'}
ALL_FIELDS = {**FIELDS, **HR_FIELDS}


def _skip_of(p: Person) -> dict:
    try:
        v = json.loads(p.sync_skip or '{}')
    except ValueError:
        return {}
    return v if isinstance(v, dict) else {}


def _existing_keys(s, people) -> dict[int, set[str]]:
    out: dict[int, set[str]] = {}
    for pid, key in s.execute(select(PersonCareer.person_id, PersonCareer.source_key)):
        out.setdefault(pid, set()).add(key)
    return out


def plan_sync() -> dict:
    """People in SAIT 값과 이 앱 임원 목록을 비교해 바꿀 것을 '제안'만 한다 (DB 는 건드리지 않음).

    items 의 각 제안은 key 로 골라 apply_sync 에 넘긴다.
    - add  : 새 (현재) 임원 추가
    - past : 과거 임원 추가 — 추적 중지 상태로 (목록·자동 연결에서는 빠지고 기록·경력은 이어 볼 수 있게)
    - link : 직접 등록해 둔 같은 이름(유일)에 사번 연결
    - set  : 이미 연결된 (현재) 임원의 이름·직위·소속이 People in SAIT 값과 다름 → 덮어쓸지.
             지난번에 유지하기로 한 값과 같으면 kept=True (기본 선택 해제)
    경력 이력(직급·소속 변천, 인사발령)은 제안 없이 반영 때 쌓인다 — career_new 는 이미 연결된 임원에게 새로 쌓일 건수.
    """
    with _people_db() as engine:
        execs, past = _fetch(engine)
        with db.SessionLocal() as s:
            people = list(s.scalars(select(Person)))
            existing = _existing_keys(s, people)
        linked = {p.source_id: p for p in people if p.source_id}
        careers = _fetch_careers(engine, linked)
    career_new = sum(1 for sid, p in linked.items() for c in careers.get(sid, [])
                     if c['key'] not in existing.get(p.id, set()))
    aliases = _title_aliases(execs)
    past_aliases = _title_aliases(execs + past)
    items, unchanged = [], 0
    unlinked_by_name: dict[str, list[Person]] = {}
    for p in people:
        if not p.source_id:
            unlinked_by_name.setdefault(p.name, []).append(p)
    for e, is_past in [(e, False) for e in execs] + [(e, True) for e in past]:
        sid = e['source_id']
        p = linked.get(sid)
        if p is None and len(unlinked_by_name.get(e['name'], [])) == 1:
            p = unlinked_by_name[e['name']][0]
            items.append({'key': f'link:{sid}', 'kind': 'link', 'source_id': sid, 'person_id': p.id,
                          'name': p.name, 'label': f"{p.name} — 직접 등록한 임원에 사번 {sid} 연결"
                                                   + (' (과거 임원)' if is_past else '')})
        if p is None:
            info = ' · '.join(x for x in (e['name'], e['position'], e['org']) if x)
            if is_past:
                items.append({'key': f'past:{sid}', 'kind': 'past', 'source_id': sid, 'name': e['name'],
                              'position': e['position'], 'org': e['org'], 'aliases': past_aliases[sid],
                              'hr': {f: e[f] for f in HR_FIELDS},
                              'label': info + (f" ({e['until']}까지)" if e.get('until') else '')})
            else:
                items.append({'key': f'add:{sid}', 'kind': 'add', 'source_id': sid, 'name': e['name'],
                              'position': e['position'], 'org': e['org'], 'aliases': aliases[sid],
                              'hr': {f: e[f] for f in HR_FIELDS}, 'label': info})
            continue
        skip = _skip_of(p)
        diffs = []
        for f in ALL_FIELDS:
            old, new = getattr(p, f) or '', e[f]
            if old == new or (f in HR_FIELDS and not new):
                continue
            if f in HR_FIELDS and not old:  # 빈 칸 채우기 — 제안 없이 반영
                items.append({'key': f'set:{sid}:{f}', 'kind': 'set', 'source_id': sid, 'person_id': p.id,
                              'name': p.name, 'field': f, 'old': old, 'new': new, 'auto': True,
                              'label': f"{p.name} {ALL_FIELDS[f]}: {new}"})
            elif not is_past:  # 과거 임원은 예전 값으로 덮어쓰지 않는다
                diffs.append(f)
        for f in diffs:
            old, new = getattr(p, f) or '', e[f]
            kept = skip.get(f) == new
            items.append({'key': f'set:{sid}:{f}', 'kind': 'set', 'source_id': sid, 'person_id': p.id,
                          'name': p.name, 'field': f, 'old': old, 'new': new, 'kept': kept,
                          'label': f"{p.name} {ALL_FIELDS[f]}: {old or '(없음)'} → {new or '(없음)'}"
                                   + (' (지난번에 유지함)' if kept else '')})
        if not diffs and p.source_id == sid and not is_past:
            unchanged += 1
    seen = {e['source_id'] for e in execs}
    missing = [f'{p.name} {p.position}'.strip() for p in people
               if p.source_id and p.source_id not in seen and p.active]
    return {'total': len(execs), 'past_total': len(past), 'items': items, 'unchanged': unchanged,
            'missing': missing, 'career_new': career_new, 'seen': sorted(seen)}


def default_keys(plan: dict) -> list[str]:
    """기본으로 고를 제안 — 지난번에 유지하기로 한 덮어쓰기만 빼고 전부."""
    return [i['key'] for i in plan['items'] if not i.get('kept')]


def proposals(plan: dict) -> list[dict]:
    """사람이 고를 제안만 (빈 칸 채우기 auto 는 빼고)."""
    return [i for i in plan['items'] if not i.get('auto')]


_KIND_ORDER = ('add', 'past', 'link', 'set')


def apply_sync(plan: dict, keys) -> dict:
    """plan_sync 의 제안 중 keys 로 고른 것만 반영하고, 사번이 연결된 모든 임원의 경력 이력을 쌓는다.
    고르지 않은 덮어쓰기는 '유지'로 기억해 다음 제안에서 기본 해제한다.
    제안 이후 DB 가 바뀐 경우(같은 사번이 이미 있음 등)는 건너뛴다."""
    keys = set(keys or [])
    with db.SessionLocal() as s:
        sids = {p.source_id for p in s.scalars(select(Person)) if p.source_id}
    sids |= {str(i.get('source_id', '')) for i in plan.get('items', []) if i['key'] in keys}
    careers = fetch_careers(sids)  # People in SAIT 를 못 읽으면 아무것도 바꾸지 않고 SyncError
    now = datetime.now().replace(microsecond=0)
    added, past_added, linked, updated, kept, skipped = [], [], [], [], [], []
    changed_fields: dict[int, list[str]] = {}
    with db.SessionLocal() as s:
        sources = {p.source_id for p in s.scalars(select(Person)) if p.source_id}
        for it in sorted(plan.get('items', []), key=lambda i: _KIND_ORDER.index(i['kind'])):
            sid, chosen = str(it.get('source_id', '')), it['key'] in keys
            if it['kind'] in ('add', 'past'):
                if not chosen:
                    continue
                if sid in sources:
                    skipped.append(it['name'])
                    continue
                hr = {f: str(v) for f, v in (it.get('hr') or {}).items() if f in HR_FIELDS}
                s.add(Person(name=it['name'], position=it['position'], org=it['org'], source_id=sid,
                             aliases=', '.join(it['aliases']), synced_at=now, active=it['kind'] == 'add',
                             hr_state='current' if it['kind'] == 'add' else 'former', **hr))
                sources.add(sid)
                (added if it['kind'] == 'add' else past_added).append(it['name'])
            elif it['kind'] == 'link':
                if not chosen:
                    continue
                p = s.get(Person, it['person_id'])
                if p is None or p.source_id or sid in sources:
                    skipped.append(it['name'])
                    continue
                p.source_id, p.synced_at = sid, now
                sources.add(sid)
                linked.append(it['name'])
            elif it['kind'] == 'set' and it.get('field') in ALL_FIELDS:
                p = s.get(Person, it['person_id'])
                if p is None or p.source_id != sid:  # 연결을 고르지 않았거나 그 사이 바뀜
                    continue
                if it.get('auto'):
                    if not (getattr(p, it['field']) or ''):
                        setattr(p, it['field'], str(it['new']))
                    continue
                skip = _skip_of(p)
                if chosen:
                    if it['field'] == 'name' and not it['new']:
                        continue
                    setattr(p, it['field'], it['new'])
                    skip.pop(it['field'], None)
                    changed_fields.setdefault(p.id, []).append(ALL_FIELDS[it['field']])
                else:
                    skip[it['field']] = it['new']
                    kept.append(f"{p.name} {ALL_FIELDS[it['field']]}")
                p.sync_skip = json.dumps(skip, ensure_ascii=False) if skip else ''
        s.flush()
        people = list(s.scalars(select(Person)))
        existing = _existing_keys(s, people)
        seen = set(plan.get('seen', []))
        career_added = 0
        for p in people:
            if p.source_id and p.source_id in seen:
                p.synced_at = now  # People in SAIT 값과 대조를 마친 시각
            if p.source_id and seen:  # 현재 임원 목록에 있으면 재직, 빠졌으면 퇴직·전출
                p.hr_state = 'current' if p.source_id in seen else 'former'
            if p.id in changed_fields:
                updated.append(f"{p.name} ({', '.join(changed_fields[p.id])})")
            have = existing.get(p.id, set())
            for c in careers.get(p.source_id, []) if p.source_id else []:
                if c['key'] in have:
                    continue
                s.add(PersonCareer(person_id=p.id, kind=c['kind'], date=c['date'], position=c['position'],
                                   org=c['org'], title=c['title'], assignment=c['assignment'], source_key=c['key']))
                have.add(c['key'])
                career_added += 1
        s.commit()
    return {'added': added, 'past_added': past_added, 'linked': linked, 'updated': updated, 'kept': kept,
            'skipped': skipped, 'career_added': career_added}


def sync_people() -> dict:
    """제안을 모두(지난번에 유지한 것은 빼고) 반영 — scripts/sync_people.py --apply 용."""
    plan = plan_sync()
    r = apply_sync(plan, default_keys(plan))
    return {**r, 'total': plan['total'], 'unchanged': plan['unchanged'], 'missing': plan['missing']}
