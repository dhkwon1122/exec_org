"""People in SAIT(Researcher-board)에서 리더급(조직 직책자) 불러오기 — 임원(services/people_sync.py) 말고.

리더 = team_refer 표(조직 단위별 1행, 조직장급 행에만 사번·직책이 채워져 있음)의 **현재** 직책자 중 임원 직급이
아닌 사람. 현재 = dep_id 별 가장 최근 스냅샷(valid_year·month·day), 그 행이 deleted='Y' 면 그 조직은 없음
(Researcher-board pipeline/rd_specialist_markdown.py _latest_current_rows 와 같은 기준). People in SAIT 표는 **읽기만**.

임원과 같은 people 표에 role='leader' 로 넣어 같은 화면(/people/<id>)·기록·경력·평가를 쓴다.
- 불러오기는 제안 없이 바로 반영한다 — 직책자 목록이 원천이라 사람이 고를 것이 없다.
  새 직책자는 추가, 있던 리더는 이름·직급·직책·조직·인사 정보를 원천 값으로 맞춘다.
- 직책자 목록에서 빠진 (불러온) 리더는 지우지 않고 '지난 리더'(hr_state='former')로 — 기록은 그대로 남는다.
- 직접 추가한 리더(hr_state='')는 직책자 목록에 없어도 그대로 두고, 사번이 있으면 인사 정보 빈 칸만 채운다.
- 이미 임원으로 있는 사번은 건너뛴다. 리더가 임원이 되면 임원 불러오기가 임원으로 옮긴다.
- 사번이 있는 리더의 경력·발령(임원과 같은 방식으로 누적), 평가(evaluations — 연봉등급·상/하반기업적),
  리더십진단(leadership — 연도·평가자 그룹별 역량 점수)을 함께 옮긴다.
"""
import json
import re
from datetime import datetime

from sqlalchemy import select

from services import db, people_sync, profile
from services.db import Person, PersonLeadership
from services.people_sync import (HR_FIELDS, SyncError, _canonical_positions, _chunks, _cols, _existing_keys,
                                  _fetch_careers, _fetch_education, _people_db, _rows, _s, _table, _ym, save_careers)

LEADERSHIP_DIMS = ['미래통찰', '성과창출', '몰입촉진', '인재육성', '자기관리', '저해행동']
EVAL_KINDS = {'salary_grade': '연봉등급', 'first_half_grade': '상반기업적', 'second_half_grade': '하반기업적'}
_EVAL_COL = re.compile(r'^(\d{4})_(salary_grade|first_half_grade|second_half_grade)$')
_LEVELS = ('dep_1st_name', 'dep_2nd_name', 'dep_3rd_name')

__all__ = ['SyncError', 'is_available', 'fetch_leaders', 'sync_leaders', 'LEADERSHIP_DIMS', 'EVAL_KINDS']


def is_available() -> bool:
    return people_sync.is_available()


def _date_key(r: dict) -> tuple:
    return tuple(_s(r.get(k)).zfill(n) for k, n in (('valid_year', 4), ('valid_month', 2), ('valid_day', 2)))


def _current_org_rows(engine) -> list[dict]:
    """team_refer 의 지금 조직들 — dep_id 별 가장 최근 행, 삭제(deleted='Y')된 조직은 뺀다."""
    t = _table(engine, 'team_refer')
    if t is None:
        raise SyncError('People in SAIT DB 에서 team_refer 테이블을 읽을 수 없습니다 (Researcher-board 의 조직 정보 적재 확인)')
    if not {'researcher_id', 'dep_id'} <= set(t.c.keys()):
        raise SyncError('team_refer 테이블에 researcher_id·dep_id 컬럼이 없습니다.')
    rows = _rows(engine, select(*_cols(t, 'dep_id', 'researcher_id', 'name', 'assignment_name', 'org_name_wd',
                                       'team_layer', 'deleted', 'valid_year', 'valid_month', 'valid_day', *_LEVELS)))
    if 'valid_year' not in t.c.keys():
        return [r for r in rows if _s(r.get('deleted')).upper() != 'Y']
    latest: dict[str, dict] = {}
    for r in rows:
        dep = _s(r.get('dep_id'))
        if dep and (dep not in latest or _date_key(r) > _date_key(latest[dep])):
            latest[dep] = r
    return [r for r in latest.values() if _s(r.get('deleted')).upper() != 'Y']


def _own_name(r: dict) -> str:
    """조직 자기 이름 — org_name_wd, 없으면 team_layer 단계의 이름 (Researcher-board own_level_name 과 같게)."""
    if _s(r.get('org_name_wd')):
        return _s(r['org_name_wd'])
    try:
        layer = int(_s(r.get('team_layer')) or 0)
    except ValueError:
        layer = 0
    if 1 <= layer <= len(_LEVELS) and _s(r.get(_LEVELS[layer - 1])):
        return _s(r[_LEVELS[layer - 1]])
    return next((_s(r.get(k)) for k in reversed(_LEVELS) if _s(r.get(k))), '')


def _path(r: dict) -> str:
    parts = []
    for k in _LEVELS:
        v = _s(r.get(k))
        if v and v not in parts:
            parts.append(v)
    return ' > '.join(parts)


def _researchers(engine, sids) -> dict[str, dict]:
    """사번 → 지금 인사 정보 (researchers 의 현재 행, 여러 행이면 가장 최근 달)."""
    t = _table(engine, 'researchers')
    if t is None:
        raise SyncError('People in SAIT DB 에서 researchers 테이블을 읽을 수 없습니다')
    out: dict[str, dict] = {}
    base = ('researcher_id', 'name', 'position', 'department', 'employment_status', 'birth_year', 'hire_date',
            'promotion_date', 'is_current', 'valid_year', 'valid_month')
    for part in _chunks(sorted(sids)):
        for r in _rows(engine, select(*_cols(t, *base)).where(t.c.researcher_id.in_(part))):
            sid = _s(r.get('researcher_id'))
            rank = (_s(r.get('is_current')) != 'N', _ym(r))
            if sid and (sid not in out or rank >= out[sid]['_rank']):
                out[sid] = {**r, '_rank': rank}
    return out


def fetch_leaders(engine=None) -> list[dict]:
    """지금 조직 직책자 중 임원 직급이 아닌 사람 — 한 사람이 여러 조직을 맡으면 직책·조직을 쉼표로."""
    if engine is None:
        with _people_db() as e:
            return fetch_leaders(e)
    by_sid: dict[str, dict] = {}
    for r in _current_org_rows(engine):
        sid = _s(r.get('researcher_id'))
        if not sid:
            continue
        L = by_sid.setdefault(sid, {'source_id': sid, 'name': _s(r.get('name')), 'assignments': [], 'orgs': [],
                                    'paths': []})
        for key, val in (('assignments', _s(r.get('assignment_name'))), ('orgs', _own_name(r)), ('paths', _path(r))):
            if val and val not in L[key]:
                L[key].append(val)
    info = _researchers(engine, by_sid)
    edu = _fetch_education(engine, list(by_sid))
    canon = _canonical_positions()
    out = []
    for sid, L in by_sid.items():
        r = info.get(sid, {})
        pos = _s(r.get('position'))
        if pos.lower() in canon:  # 임원 — 임원 불러오기에서
            continue
        out.append({'source_id': sid, 'name': _s(r.get('name')) or L['name'], 'position': pos,
                    'assignment': ', '.join(L['assignments']), 'org': ', '.join(L['orgs']) or _s(r.get('department')),
                    'org_path': ' / '.join(L['paths']),
                    **{f: _s(r.get(f)) for f in HR_FIELDS if f != 'education'}, 'education': edu.get(sid, '')})
    return sorted(out, key=lambda x: (x['org_path'], x['name']))


def _evaluations(engine, sids) -> dict[str, list[tuple[int, str, str]]]:
    """사번 → [(연도, 종류, 등급)] — evaluations 의 넓은 표(2026_salary_grade 등)를 펼친다. 가장 최근 행 기준."""
    t = _table(engine, 'evaluations')
    if t is None or 'researcher_id' not in t.c.keys():
        return {}
    cols = [c for c in t.c.keys() if _EVAL_COL.match(c)]
    if not cols:
        return {}
    latest: dict[str, dict] = {}
    for part in _chunks(sorted(sids)):
        for r in _rows(engine, select(*_cols(t, 'researcher_id', 'is_current', 'valid_year', 'valid_month', *cols))
                       .where(t.c.researcher_id.in_(part))):
            sid = _s(r.get('researcher_id'))
            rank = (_s(r.get('is_current')) != 'N', _ym(r))
            if sid and (sid not in latest or rank >= latest[sid]['_rank']):
                latest[sid] = {**r, '_rank': rank}
    out: dict[str, list] = {}
    for sid, r in latest.items():
        for c in cols:
            v = _s(r.get(c))
            if v and v.lower() not in ('nan', 'none', '-'):
                m = _EVAL_COL.match(c)
                out.setdefault(sid, []).append((int(m.group(1)), EVAL_KINDS[m.group(2)], v[:50]))
    return out


def _leadership(engine, sids) -> dict[str, list[dict]]:
    """사번 → [{'year', 'group', 'scores': {역량: 점수}}]."""
    t = _table(engine, 'leadership')
    if t is None or not {'researcher_id', 'year'} <= set(t.c.keys()):
        return {}
    dims = [d for d in LEADERSHIP_DIMS if d in t.c.keys()]
    out: dict[str, list] = {}
    for part in _chunks(sorted(sids)):
        for r in _rows(engine, select(*_cols(t, 'researcher_id', 'year', 'evaluator_group', *dims))
                       .where(t.c.researcher_id.in_(part))):
            try:
                year = int(float(_s(r.get('year'))))
            except ValueError:
                continue
            scores = {}
            for d in dims:
                try:
                    scores[d] = round(float(r.get(d)), 2)
                except (TypeError, ValueError):
                    continue
            if scores:
                out.setdefault(_s(r['researcher_id']), []).append(
                    {'year': year, 'group': _s(r.get('evaluator_group'))[:30], 'scores': scores})
    return out


def sync_leaders() -> dict:
    """직책자 목록대로 리더를 추가·갱신하고, 빠진 리더는 '지난 리더'로. 경력·평가·리더십진단도 옮긴다.
    People in SAIT 를 못 읽으면 아무것도 바꾸지 않고 SyncError."""
    with db.SessionLocal() as s:
        people = list(s.scalars(select(Person)))
    manual = {p.source_id for p in people if p.role == 'leader' and p.source_id}
    with _people_db() as engine:
        leaders = fetch_leaders(engine)
        sids = {L['source_id'] for L in leaders} | manual
        extra = _researchers(engine, manual - {L['source_id'] for L in leaders})
        extra_edu = _fetch_education(engine, list(extra))
        careers = _fetch_careers(engine, sids)
        evals = _evaluations(engine, sids)
        lead = _leadership(engine, sids)
    now = datetime.now().replace(microsecond=0)
    added, updated, former, skipped_exec = [], [], [], []
    with db.SessionLocal() as s:
        by_sid = {p.source_id: p for p in s.scalars(select(Person)) if p.source_id}
        seen = set()
        for L in leaders:
            sid = L['source_id']
            seen.add(sid)
            p = by_sid.get(sid)
            fields = {'name': L['name'], 'position': L['position'], 'org': L['org'], 'org_path': L['org_path'],
                      'assignment': L['assignment'], **{f: L[f] for f in HR_FIELDS}}
            if p is None:
                p = Person(source_id=sid, role='leader', hr_state='current', active=True, synced_at=now, aliases='',
                           **{k: v for k, v in fields.items() if v or k in ('position', 'org')})
                if not p.name:
                    continue
                s.add(p)
                by_sid[sid] = p
                added.append(p.name)
                continue
            if p.role != 'leader':  # 이미 임원
                skipped_exec.append(p.name)
                continue
            changed = [k for k, v in fields.items() if v and (getattr(p, k) or '') != v]
            for k in changed:
                setattr(p, k, fields[k])
            if p.hr_state != 'current':
                p.hr_state = 'current'
                changed.append('hr_state')
            p.synced_at = now
            if changed:
                updated.append(p.name)
        for sid, p in by_sid.items():
            if p.role != 'leader' or sid in seen:
                continue
            if p.hr_state == 'current':  # 불러온 리더가 직책자 목록에서 빠짐
                p.hr_state = 'former'
                former.append(p.name)
            r = extra.get(sid)
            if r:  # 직접 추가한 리더 — 빈 칸만 채운다
                vals = {'position': _s(r.get('position')), 'org': _s(r.get('department')),
                        **{f: _s(r.get(f)) for f in HR_FIELDS if f != 'education'}, 'education': extra_edu.get(sid, '')}
                for k, v in vals.items():
                    if v and not (getattr(p, k) or ''):
                        setattr(p, k, v)
                p.synced_at = now
        s.flush()
        existing = _existing_keys(s, None)
        career_added = eval_changed = lead_n = 0
        for sid, p in by_sid.items():
            if p.role != 'leader':
                continue
            career_added += save_careers(s, p, careers, existing)
            for year, kind, grade in evals.get(sid, []):
                eval_changed += profile.upsert_evaluation(s, p.id, year, kind, grade, source='sait')
            for x in lead.get(sid, []):
                row = s.scalar(select(PersonLeadership).where(PersonLeadership.person_id == p.id,
                                                              PersonLeadership.year == x['year'],
                                                              PersonLeadership.evaluator_group == x['group']))
                scores = json.dumps(x['scores'], ensure_ascii=False)
                if row is None:
                    s.add(PersonLeadership(person_id=p.id, year=x['year'], evaluator_group=x['group'], scores=scores))
                    lead_n += 1
                elif row.scores != scores:
                    row.scores = scores
                    lead_n += 1
        s.commit()
    return {'total': len(leaders), 'added': added, 'updated': updated, 'former': former,
            'skipped_exec': skipped_exec, 'career_added': career_added, 'eval_changed': eval_changed,
            'leadership_changed': lead_n}
