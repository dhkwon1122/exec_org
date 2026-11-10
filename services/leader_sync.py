"""People in SAIT(Researcher-board)에서 리더급 불러오기 — 리더급 현황(services/leaders.py STATUS_COLUMNS)을 채운다.

리더급 = 지금 조직 직책자(team_refer 의 조직장급 행 — 보직장) + R.Master(CL 이 LEADER_RMASTER_POSITIONS) 중
임원이 아닌 사람. 임원(services/people_sync.py)은 따로 불러오지만 리더급 현황에는 직책구분 '임원/Master' 로 함께 나오므로
현황 칸은 임원에게도 채운다. 현재 조직 = dep_id 별 가장 최근 스냅샷(valid_year·month·day), 그 행이 deleted='Y' 면 그
조직은 없음 (Researcher-board pipeline/rd_specialist_markdown.py _latest_current_rows 와 같은 기준). People in SAIT 표는
**읽기만**.

현황 칸
- Platform·그룹·과제/파트명 = 조직 1·2·3단계 이름 (보직장은 맡은 조직, 아니면 자기 소속 org_code 의 조직 — upper_dep_id 로
  올라가며), 구분 = 그 조직의 구분(work_type), 부서명·CL·Knox ID = researchers
- 직책구분 = 임원/Master(EXEC_ORG_POSITIONS) → R.Master → 보직장 → 일반 순으로 처음 맞는 것
- 보직장·플랫폼장/팀장·그룹장/PL·PM/파트장 = 'O' (맡은 조직의 단계 1·2·3)
- 과제명 = 지금 투입 중인 과제(tasks — 시작일 ≤ 오늘 ≤ 해제일, 투입률 높은 순)
- 보직 수행시작일 = 맡은 조직의 스냅샷 이력에서 지금까지 이어서 직책자였던 첫 날짜
- 과제구분 등 불러올 수 없는 칸은 화면에서 고친다 (leader_manual — 불러오기를 다시 해도 유지)

임원과 같은 people 표에 role='leader' 로 넣어 같은 화면(/people/<id>)·기록·경력·평가를 쓴다.
- 불러오기는 제안 없이 바로 반영한다. 새 리더는 추가, 있던 리더는 이름·직급·직책·조직·인사 정보를 원천 값으로 맞춘다.
- 리더급에서 빠진 (불러온) 리더는 지우지 않고 '지난 리더'(hr_state='former')로 — 기록은 그대로 남는다.
- 직접 추가한 리더(hr_state='')는 그대로 두고, 사번이 있으면 인사 정보 빈 칸과 현황 칸을 채운다.
- 사번이 있는 리더의 경력·발령(임원과 같은 방식으로 누적), 평가(evaluations — 연봉등급·상/하반기업적),
  리더십진단(leadership — 연도·평가자 그룹별 역량 점수)을 함께 옮긴다.
"""
import json
import os
import re
from datetime import date, datetime

from sqlalchemy import select

from services import db, people_sync, profile
from services.db import LeaderExclusion, Person, PersonLeadership
from services.people_sync import (HR_FIELDS, SyncError, _canonical_positions, _chunks, _cols, _existing_keys,
                                  _fetch_careers, _fetch_education, _people_db, _rows, _s, _table, _ym, save_careers)

LEADERSHIP_DIMS = ['미래통찰', '성과창출', '몰입촉진', '인재육성', '자기관리', '저해행동']
EVAL_KINDS = {'salary_grade': '연봉등급', 'first_half_grade': '상반기업적', 'second_half_grade': '하반기업적'}
_EVAL_COL = re.compile(r'^(\d{4})_(salary_grade|first_half_grade|second_half_grade)$')
_LEVELS = ('dep_1st_name', 'dep_2nd_name', 'dep_3rd_name')
LAYER_HEADS = {1: '플랫폼장/팀장', 2: '그룹장/PL', 3: 'PM/파트장'}
# 팀/리더 참조의 직책명으로 어느 칸인지 — 조직 단계보다 먼저 본다 (PL·PM 은 과제 단계 조직에 붙기도 하고,
# PM 은 직무프로필 '(M)'으로 채워진다 — Researcher-board pipeline/team_refer_intake.py). 앞 규칙이 우선
_TITLE_HEADS = [(re.compile(r'(?<![A-Za-z])PM(?![A-Za-z])|파트장', re.I), 3),
                (re.compile(r'(?<![A-Za-z])PL(?![A-Za-z])|그룹장', re.I), 2),
                (re.compile(r'플랫폼장|팀장|센터장|본부장'), 1)]


def head_layer(assignment: str, layer: int) -> int:
    """직책명이 PM·파트장이면 3, PL·그룹장이면 2, 플랫폼장·팀장·센터장·본부장이면 1, 그 밖에는 조직 단계."""
    for rx, n in _TITLE_HEADS:
        if rx.search(assignment or ''):
            return n
    return layer
KINDS = ('임원/Master', 'R.Master', '보직장', '일반')  # 직책구분 (앞이 우선)

__all__ = ['SyncError', 'is_available', 'fetch_leaders', 'sync_leaders', 'LEADERSHIP_DIMS', 'EVAL_KINDS',
           'LAYER_HEADS', 'KINDS', 'rmaster_positions']


def is_available() -> bool:
    return people_sync.is_available()


def rmaster_positions() -> list[str]:
    return [p.strip() for p in os.environ.get('LEADER_RMASTER_POSITIONS', 'R.Master').split(',') if p.strip()]


def _norm_pos(v: str) -> str:
    return re.sub(r'[\s.]', '', v or '').lower()


def _date_key(r: dict) -> tuple:
    return tuple(_s(r.get(k)).zfill(n) for k, n in (('valid_year', 4), ('valid_month', 2), ('valid_day', 2)))


def _iso(key: tuple) -> str:
    y, m, d = key
    return f'{y}-{m}-{d}' if y.strip('0') and m.strip('0') else (f'{y}-{m}' if y.strip('0') else '')


class _Orgs:
    """team_refer — 지금 조직(dep_id 별 최신 행, 삭제 제외)과 dep_id 별 스냅샷 이력."""

    def __init__(self, engine):
        t = _table(engine, 'team_refer')
        if t is None:
            raise SyncError('People in SAIT DB 에서 team_refer 테이블을 읽을 수 없습니다 (Researcher-board 의 조직 정보 적재 확인)')
        if not {'researcher_id', 'dep_id'} <= set(t.c.keys()):
            raise SyncError('team_refer 테이블에 researcher_id·dep_id 컬럼이 없습니다.')
        rows = _rows(engine, select(*_cols(t, 'dep_id', 'upper_dep_id', 'researcher_id', 'name', 'assignment_name',
                                           'org_name_wd', 'work_type', 'team_layer', 'deleted', 'valid_year',
                                           'valid_month', 'valid_day', *_LEVELS)))
        self.history: dict[str, list[dict]] = {}
        for r in rows:
            dep = _s(r.get('dep_id'))
            if dep:
                self.history.setdefault(dep, []).append(r)
        for v in self.history.values():
            v.sort(key=_date_key)
        self.nodes = {dep: v[-1] for dep, v in self.history.items() if _s(v[-1].get('deleted')).upper() != 'Y'}
        self.by_name = {}
        for dep, r in self.nodes.items():
            if _s(r.get('org_name_wd')):
                self.by_name.setdefault(_s(r['org_name_wd']), dep)

    def layer(self, dep: str) -> int:
        r = self.nodes.get(dep, {})
        try:
            n = int(float(_s(r.get('team_layer')) or 0))
        except ValueError:
            n = 0
        if not n:  # 단계가 비어 있으면 위로 몇 번 올라가는지로
            n, cur, seen = 1, _s(r.get('upper_dep_id')), set()
            while cur in self.nodes and cur not in seen:
                seen.add(cur)
                n += 1
                cur = _s(self.nodes[cur].get('upper_dep_id'))
        return n

    def name(self, dep: str) -> str:
        return _own_name(self.nodes.get(dep, {}))

    def levels(self, dep: str) -> dict[int, str]:
        """{단계: 이름} — upper_dep_id 로 올라가며. 위 조직 행이 없으면 그 행의 단계별 이름 칸으로."""
        out, cur, seen = {}, dep, set()
        while cur in self.nodes and cur not in seen:
            seen.add(cur)
            out.setdefault(self.layer(cur), self.name(cur))
            cur = _s(self.nodes[cur].get('upper_dep_id'))
        r = self.nodes.get(dep, {})
        for i, k in enumerate(_LEVELS, start=1):
            if i not in out and _s(r.get(k)) and i < self.layer(dep):
                out[i] = _s(r[k])
        return out

    def path(self, dep: str) -> str:
        lv = self.levels(dep)
        return ' > '.join(lv[k] for k in sorted(lv) if lv[k])

    def since(self, dep: str, sid: str) -> str:
        """이 조직의 스냅샷을 최근부터 거슬러 올라가며 sid 가 계속 직책자였던 첫 날짜."""
        first = None
        for r in reversed(self.history.get(dep, [])):
            if _s(r.get('researcher_id')) != sid:
                break
            first = r
        return _iso(_date_key(first)) if first else ''

    def holders(self) -> dict[str, list[str]]:
        """사번 → 맡은 지금 조직 dep_id (단계 낮은 = 큰 조직 먼저)."""
        out: dict[str, list[str]] = {}
        for dep, r in self.nodes.items():
            sid = _s(r.get('researcher_id'))
            if sid:
                out.setdefault(sid, []).append(dep)
        for v in out.values():
            v.sort(key=lambda d: (self.layer(d), self.name(d)))
        return out


def _own_name(r: dict) -> str:
    """조직 자기 이름 — org_name_wd, 없으면 team_layer 단계의 이름 (Researcher-board own_level_name 과 같게)."""
    if _s(r.get('org_name_wd')):
        return _s(r['org_name_wd'])
    try:
        layer = int(float(_s(r.get('team_layer')) or 0))
    except ValueError:
        layer = 0
    if 1 <= layer <= len(_LEVELS) and _s(r.get(_LEVELS[layer - 1])):
        return _s(r[_LEVELS[layer - 1]])
    return next((_s(r.get(k)) for k in reversed(_LEVELS) if _s(r.get(k))), '')


_RCOLS = ('researcher_id', 'name', 'position', 'department', 'org_code', 'knox_id', 'employment_status', 'birth_year',
          'hire_date', 'promotion_date', 'is_current', 'valid_year', 'valid_month')


def _researchers(engine, sids=None) -> dict[str, dict]:
    """사번 → 지금 인사 정보 (researchers 의 현재 행, 여러 행이면 가장 최근 달). sids 가 None 이면 모두."""
    t = _table(engine, 'researchers')
    if t is None:
        raise SyncError('People in SAIT DB 에서 researchers 테이블을 읽을 수 없습니다')
    out: dict[str, dict] = {}
    parts = [None] if sids is None else list(_chunks(sorted(sids)))
    for part in parts:
        stmt = select(*_cols(t, *_RCOLS))
        if part is not None:
            stmt = stmt.where(t.c.researcher_id.in_(part))
        for r in _rows(engine, stmt):
            sid = _s(r.get('researcher_id'))
            rank = (_s(r.get('is_current')) != 'N', _ym(r))
            if sid and (sid not in out or rank >= out[sid]['_rank']):
                out[sid] = {**r, '_rank': rank}
    return out


def _tasks(engine, sids) -> dict[str, str]:
    """사번 → 지금 투입 중인 과제명 (투입률 높은 순, 쉼표)."""
    t = _table(engine, 'tasks')
    if t is None or not {'researcher_id', 'task_name'} <= set(t.c.keys()):
        return {}
    today = date.today().isoformat()
    found: dict[str, list[tuple]] = {}
    for part in _chunks(sorted(sids)):
        for r in _rows(engine, select(*_cols(t, 'researcher_id', 'task_name', 'the_task_name', 'start_date', 'end_date',
                                             'input_rate')).where(t.c.researcher_id.in_(part))):
            start, end = _s(r.get('start_date'))[:10], _s(r.get('end_date'))[:10]
            if (start and start > today) or (end and end < today):
                continue
            name = _s(r.get('the_task_name')) or _s(r.get('task_name'))
            try:
                rate = float(_s(r.get('input_rate')) or 0)
            except ValueError:
                rate = 0
            if name:
                found.setdefault(_s(r['researcher_id']), []).append((-rate, name))
    return {sid: ', '.join(dict.fromkeys(n for _, n in sorted(v))) for sid, v in found.items()}


def _status(sid: str, r: dict, orgs: _Orgs, deps: list[str], kind: str, task: str) -> dict:
    """리더급 현황 칸 (services/leaders.py STATUS_COLUMNS 이름 그대로).
    Platform·그룹·부서명은 사번으로 읽은 그 사람의 소속(researchers 의 org_code → 조직 1·2단계, department) —
    맡은 조직이 아니라 (Researcher-board 의 org_code_label_maps 와 같은 기준). 소속 조직을 못 찾으면 맡은 조직으로.
    과제/파트명·구분은 맡은 조직(없으면 소속)."""
    own = orgs.by_name.get(_s(r.get('org_code')), '')
    home = deps[0] if deps else own
    lv = orgs.levels(home) if home else {}
    own_lv = orgs.levels(own) if own else lv
    layers = {head_layer(_s(orgs.nodes[d].get('assignment_name')), orgs.layer(d)) for d in deps}
    node = orgs.nodes.get(home, {})
    return {
        '사원번호': sid, 'Platform': own_lv.get(1, ''), '그룹': own_lv.get(2, ''), '부서명': _s(r.get('department')),
        '과제/파트명': lv.get(3, ''), '성명': _s(r.get('name')), 'CL': _s(r.get('position')),
        'Knox ID': _s(r.get('knox_id')), '구분': _s(node.get('work_type')), '직책구분': kind,
        '보직장': 'O' if deps else '', **{h: 'O' if n in layers else '' for n, h in LAYER_HEADS.items()},
        '과제명': task, '보직 수행시작일': min((orgs.since(d, sid) for d in deps if orgs.since(d, sid)), default=''),
    }


def fetch_leaders(engine=None) -> list[dict]:
    """리더급(보직장 + R.Master) 전원 — 임원 포함. 각 항목에 status(현황 칸)와 exec(임원 여부)."""
    if engine is None:
        with _people_db() as e:
            return fetch_leaders(e)
    orgs = _Orgs(engine)
    holders = orgs.holders()
    people = _researchers(engine)
    rm = {_norm_pos(p) for p in rmaster_positions()}
    canon = _canonical_positions()
    sids = set(holders) | {sid for sid, r in people.items()
                           if _norm_pos(_s(r.get('position'))) in rm and _s(r.get('is_current')) != 'N'}
    tasks = _tasks(engine, sids)
    edu = _fetch_education(engine, list(sids))
    out = []
    for sid in sids:
        r = people.get(sid, {})
        pos = _s(r.get('position'))
        deps = holders.get(sid, [])
        is_exec = pos.lower() in canon
        kind = KINDS[0] if is_exec else KINDS[1] if _norm_pos(pos) in rm else KINDS[2] if deps else KINDS[3]
        name = _s(r.get('name')) or _s(orgs.nodes[deps[0]].get('name')) if deps else _s(r.get('name'))
        out.append({'source_id': sid, 'name': name, 'position': pos, 'exec': is_exec,
                    'assignment': ', '.join(dict.fromkeys(_s(orgs.nodes[d].get('assignment_name')) for d in deps
                                                          if _s(orgs.nodes[d].get('assignment_name')))),
                    'org': ', '.join(orgs.name(d) for d in deps) or _s(r.get('department')),
                    'org_path': ' / '.join(orgs.path(d) for d in deps)
                                or (orgs.path(orgs.by_name[_s(r.get('org_code'))])
                                    if _s(r.get('org_code')) in orgs.by_name else ''),
                    **{f: _s(r.get(f)) for f in HR_FIELDS if f != 'education'}, 'education': edu.get(sid, ''),
                    'status': {**_status(sid, r, orgs, deps, kind, tasks.get(sid, '')), '성명': name}})
    return sorted(out, key=lambda x: (x['org_path'], x['name']))


def refresh_status(sids) -> int:
    """이 사번들의 리더급 현황 칸만 People in SAIT 에서 바로 다시 읽어 넣는다 (직접 추가·엑셀 업로드 뒤 —
    다음 불러오기를 기다리지 않게). People in SAIT 를 못 읽으면 0 (화면은 다음 불러오기 때 채워짐)."""
    sids = {str(x).strip() for x in sids or [] if str(x or '').strip()}
    if not sids or not is_available():
        return 0
    try:
        with _people_db() as engine:
            st = _status_for(engine, sids)
    except SyncError:
        return 0
    n = 0
    with db.SessionLocal() as s:
        for p in s.scalars(select(Person).where(Person.source_id.in_(list(st)))):
            p.leader_info = json.dumps(st[p.source_id], ensure_ascii=False)
            n += 1
        s.commit()
    return n


def _status_for(engine, sids) -> dict[str, dict]:
    """리더급이 아닌 사번(임원·직접 추가한 사람)의 현황 칸."""
    if not sids:
        return {}
    orgs = _Orgs(engine)
    holders = orgs.holders()
    people = _researchers(engine, sids)
    tasks = _tasks(engine, sids)
    canon = _canonical_positions()
    rm = {_norm_pos(p) for p in rmaster_positions()}
    out = {}
    for sid in sids:
        r = people.get(sid)
        if not r:
            continue
        pos, deps = _s(r.get('position')), holders.get(sid, [])
        kind = KINDS[0] if pos.lower() in canon else KINDS[1] if _norm_pos(pos) in rm else KINDS[2] if deps else KINDS[3]
        out[sid] = _status(sid, r, orgs, deps, kind, tasks.get(sid, ''))
    return out


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
    """리더급(보직장·R.Master) 대로 리더를 추가·갱신하고, 빠진 리더는 '지난 리더'로. 리더급 현황 칸은 임원·직접 추가한
    리더에게도 채운다. 경력·평가·리더십진단도 옮긴다. People in SAIT 를 못 읽으면 아무것도 바꾸지 않고 SyncError."""
    with db.SessionLocal() as s:
        people = list(s.scalars(select(Person)))
    manual = {p.source_id for p in people if p.role == 'leader' and p.source_id}
    execs = {p.source_id for p in people if p.role != 'leader' and p.source_id and p.hr_state != 'former'}
    with _people_db() as engine:
        leaders = fetch_leaders(engine)
        found = {L['source_id'] for L in leaders}
        sids = {L['source_id'] for L in leaders if not L['exec']} | manual
        extra = _researchers(engine, manual - found)
        extra_edu = _fetch_education(engine, list(extra))
        statuses = _status_for(engine, (manual | execs) - found)
        careers = _fetch_careers(engine, sids)
        evals = _evaluations(engine, sids)
        lead = _leadership(engine, sids)
    statuses.update({L['source_id']: L['status'] for L in leaders})
    now = datetime.now().replace(microsecond=0)
    added, updated, former, skipped_exec = [], [], [], []
    with db.SessionLocal() as s:
        by_sid = {p.source_id: p for p in s.scalars(select(Person)) if p.source_id}
        excluded = set(s.scalars(select(LeaderExclusion.source_id)))
        seen = set()
        for L in leaders:
            sid = L['source_id']
            p = by_sid.get(sid)
            if p is None and sid in excluded:  # 리더 명단에서 지운 사람 — 다시 추가하지 않는다
                continue
            if L['exec'] or (p is not None and p.role != 'leader'):  # 임원 — 임원 불러오기에서, 현황 칸만
                if p is None:
                    skipped_exec.append(L['name'])
                continue
            seen.add(sid)
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
            st = statuses.get(sid)
            if st is not None:
                p.leader_info = json.dumps(st, ensure_ascii=False)
            if p.role != 'leader' or sid in seen:
                continue
            if p.hr_state == 'current':  # 불러온 리더가 리더급에서 빠짐
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
    return {'total': len(leaders), 'execs': sum(L['exec'] for L in leaders), 'added': added, 'updated': updated, 'former': former,
            'skipped_exec': skipped_exec, 'career_added': career_added, 'eval_changed': eval_changed,
            'leadership_changed': lead_n}
