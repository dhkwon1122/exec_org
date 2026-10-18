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
- People in SAIT 최신 인원 목록에 더 이상 없는 임원(퇴임·이동 등)은 지우거나 추적 중지하지 않고
  결과에 알려만 준다 — 기록은 그대로 남아야 하므로 판단은 사람이 한다.
"""
import json
import os
from datetime import datetime

from sqlalchemy import MetaData, Table, create_engine, func, select, text

from services import db, users
from services.db import Person

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


def fetch_executives() -> list[dict]:
    """researchers 테이블에서 현재 재직 중인 임원(직급이 EXEC_ORG_POSITIONS 중 하나) 목록."""
    url = users.user_db_url()
    if not url:
        raise SyncError('People in SAIT DB(DATABASE_URL)가 설정되지 않았습니다.')
    connect_args = {'connect_timeout': 5} if url.startswith('postgresql') else {}
    try:
        engine = create_engine(url, future=True, connect_args=connect_args)
    except Exception as e:  # noqa: BLE001 — 드라이버 미설치·URL 형식 오류
        raise SyncError(f'People in SAIT DB 에 연결할 수 없습니다: {type(e).__name__}: {e}') from e
    try:
        try:
            t = Table('researchers', MetaData(), autoload_with=engine)
        except Exception as e:  # noqa: BLE001
            raise SyncError('People in SAIT DB 에서 researchers 테이블을 읽을 수 없습니다 '
                            f'(Researcher-board 의 load_to_db 적재 여부 확인): {type(e).__name__}') from e
        need = {'researcher_id', 'name', 'position'}
        if not need <= set(t.c.keys()):
            raise SyncError(f'researchers 테이블에 필요한 컬럼이 없습니다: {sorted(need - set(t.c.keys()))}')
        cols = [t.c[c] for c in ('researcher_id', 'name', 'position', 'department', 'org_code',
                                 'employment_status', 'is_current') if c in t.c]
        canon = _canonical_positions()
        stmt = select(*cols).where(func.lower(func.trim(t.c.position)).in_(list(canon)))
        if 'is_current' in t.c:  # 과거 시점 행(N)은 빼고 최신 인원만
            stmt = stmt.where((t.c.is_current != 'N') | t.c.is_current.is_(None))
        with engine.begin() as conn:
            if engine.dialect.name == 'postgresql':
                conn.execute(text('SET LOCAL statement_timeout = 8000'))
            rows = [dict(r._mapping) for r in conn.execute(stmt)]
    except SyncError:
        raise
    except Exception as e:  # noqa: BLE001
        raise SyncError(f'People in SAIT DB 조회 실패: {type(e).__name__}') from e
    finally:
        engine.dispose()

    out = {}
    for r in rows:
        rid = str(r.get('researcher_id') or '').strip()
        name = str(r.get('name') or '').strip()
        if not rid or not name:
            continue
        out[rid] = {  # 같은 사번이 여러 행이면 마지막 값
            'source_id': rid, 'name': name,
            'position': canon.get(str(r.get('position') or '').strip().lower(), str(r.get('position') or '').strip()),
            'org': str(r.get('department') or r.get('org_code') or '').strip(),
            'employment_status': str(r.get('employment_status') or '').strip(),
        }
    order = list(canon.values())
    return sorted(out.values(), key=lambda x: (order.index(x['position']) if x['position'] in order else 99,
                                               x['name']))


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


def _skip_of(p: Person) -> dict:
    try:
        v = json.loads(p.sync_skip or '{}')
    except ValueError:
        return {}
    return v if isinstance(v, dict) else {}


def plan_sync() -> dict:
    """People in SAIT 값과 이 앱 임원 목록을 비교해 바꿀 것을 '제안'만 한다 (DB 는 건드리지 않음).

    items 의 각 제안은 key 로 골라 apply_sync 에 넘긴다.
    - add  : 새 임원 추가
    - link : 직접 등록해 둔 같은 이름(유일)에 사번 연결
    - set  : 이미 연결된 임원의 이름·직위·소속이 People in SAIT 값과 다름 → 덮어쓸지.
             지난번에 유지하기로 한 값과 같으면 kept=True (기본 선택 해제)
    """
    execs = fetch_executives()
    aliases = _title_aliases(execs)
    items, unchanged = [], 0
    with db.SessionLocal() as s:
        people = list(s.scalars(select(Person)))
    by_source = {p.source_id: p for p in people if p.source_id}
    unlinked_by_name: dict[str, list[Person]] = {}
    for p in people:
        if not p.source_id:
            unlinked_by_name.setdefault(p.name, []).append(p)
    for e in execs:
        sid = e['source_id']
        p = by_source.get(sid)
        if p is None and len(unlinked_by_name.get(e['name'], [])) == 1:
            p = unlinked_by_name[e['name']][0]
            items.append({'key': f'link:{sid}', 'kind': 'link', 'source_id': sid, 'person_id': p.id,
                          'name': p.name, 'label': f"{p.name} — 직접 등록한 임원에 사번 {sid} 연결"})
        if p is None:
            items.append({'key': f'add:{sid}', 'kind': 'add', 'source_id': sid, 'name': e['name'],
                          'position': e['position'], 'org': e['org'], 'aliases': aliases[sid],
                          'label': ' · '.join(x for x in (e['name'], e['position'], e['org']) if x)})
            continue
        skip = _skip_of(p)
        diffs = [f for f in FIELDS if (getattr(p, f) or '') != e[f]]
        for f in diffs:
            old, new = getattr(p, f) or '', e[f]
            kept = skip.get(f) == new
            items.append({'key': f'set:{sid}:{f}', 'kind': 'set', 'source_id': sid, 'person_id': p.id,
                          'name': p.name, 'field': f, 'old': old, 'new': new, 'kept': kept,
                          'label': f"{p.name} {FIELDS[f]}: {old or '(없음)'} → {new or '(없음)'}"
                                   + (' (지난번에 유지함)' if kept else '')})
        if not diffs and p.source_id == sid:
            unchanged += 1
    seen = {e['source_id'] for e in execs}
    missing = [f'{p.name} {p.position}'.strip() for p in people
               if p.source_id and p.source_id not in seen and p.active]
    return {'total': len(execs), 'items': items, 'unchanged': unchanged, 'missing': missing,
            'seen': sorted(seen)}


def default_keys(plan: dict) -> list[str]:
    """기본으로 고를 제안 — 지난번에 유지하기로 한 덮어쓰기만 빼고 전부."""
    return [i['key'] for i in plan['items'] if not i.get('kept')]


def apply_sync(plan: dict, keys) -> dict:
    """plan_sync 의 제안 중 keys 로 고른 것만 반영한다. 고르지 않은 덮어쓰기는 '유지'로 기억해
    다음 제안에서 기본 해제한다. 제안 이후 DB 가 바뀐 경우(같은 사번이 이미 있음 등)는 건너뛴다."""
    keys = set(keys or [])
    now = datetime.now().replace(microsecond=0)
    added, linked, updated, kept, skipped = [], [], [], [], []
    changed_fields: dict[int, list[str]] = {}
    with db.SessionLocal() as s:
        sources = {p.source_id for p in s.scalars(select(Person)) if p.source_id}
        for it in sorted(plan.get('items', []), key=lambda i: ('add', 'link', 'set').index(i['kind'])):
            sid, chosen = str(it.get('source_id', '')), it['key'] in keys
            if it['kind'] == 'add':
                if not chosen:
                    continue
                if sid in sources:
                    skipped.append(it['name'])
                    continue
                s.add(Person(name=it['name'], position=it['position'], org=it['org'], source_id=sid,
                             aliases=', '.join(it['aliases']), synced_at=now))
                sources.add(sid)
                added.append(it['name'])
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
            elif it['kind'] == 'set' and it.get('field') in FIELDS:
                p = s.get(Person, it['person_id'])
                if p is None or p.source_id != sid:  # 연결을 고르지 않았거나 그 사이 바뀜
                    continue
                skip = _skip_of(p)
                if chosen:
                    if it['field'] == 'name' and not it['new']:
                        continue
                    setattr(p, it['field'], it['new'])
                    skip.pop(it['field'], None)
                    changed_fields.setdefault(p.id, []).append(FIELDS[it['field']])
                else:
                    skip[it['field']] = it['new']
                    kept.append(f"{p.name} {FIELDS[it['field']]}")
                p.sync_skip = json.dumps(skip, ensure_ascii=False) if skip else ''
        seen = set(plan.get('seen', []))
        for p in s.scalars(select(Person)):
            if p.source_id and p.source_id in seen:
                p.synced_at = now  # People in SAIT 값과 대조를 마친 시각
            if p.id in changed_fields:
                updated.append(f"{p.name} ({', '.join(changed_fields[p.id])})")
        s.commit()
    return {'added': added, 'linked': linked, 'updated': updated, 'kept': kept, 'skipped': skipped}


def sync_people() -> dict:
    """제안을 모두(지난번에 유지한 것은 빼고) 반영 — scripts/sync_people.py --apply 용."""
    plan = plan_sync()
    r = apply_sync(plan, default_keys(plan))
    return {**r, 'total': plan['total'], 'unchanged': plan['unchanged'], 'missing': plan['missing']}
