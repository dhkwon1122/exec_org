"""리더급 — 목록·직접 추가·리더십진단·엑셀 내보내기. 불러오기는 services/leader_sync.py.
리더는 people 표의 role='leader' 라 기록·경력·평가·주요 이력은 임원과 같은 함수(store·profile)를 쓴다."""
import io
import json
from datetime import date

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from sqlalchemy import select

from services import db, profile, store
from services.db import Person, PersonLeadership
from services.leader_sync import LEADERSHIP_DIMS

OTHERS = '타인평균'  # 리더십진단 요약에 쓰는 평가자 그룹 (동료+상사+부서원 평균)
# 리더급 현황 칸 — 불러온 값(Person.leader_info, services/leader_sync.py)에 화면에서 고친 값(leader_manual)을 덮는다
STATUS_COLUMNS = ['사원번호', 'Platform', '그룹', '부서명', '과제/파트명', '성명', 'CL', 'Knox ID', '구분', '직책구분',
                  '보직장', '플랫폼장/팀장', '그룹장/PL', 'PM/파트장', '과제구분', '과제명', '보직 수행시작일']
KIND_ORDER = {k: i for i, k in enumerate(['임원/Master', 'R.Master', '보직장', '일반'])}
# 사번으로 People in SAIT 에서 읽는 칸 — 표·엑셀 업로드로 고치지 않는다 (고친 값이 있어도 무시)
FROM_DB = ('Platform', '그룹', '부서명')


def _loads(raw) -> dict:
    try:
        v = json.loads(raw or '{}')
    except ValueError:
        return {}
    return v if isinstance(v, dict) else {}


def _status_of(p: Person) -> tuple[dict, set]:
    """→ (현황 칸 값, 화면에서 고친 칸 이름)."""
    info, manual = _loads(p.leader_info), _loads(p.leader_manual)
    base = {'사원번호': p.source_id or '', '성명': p.name, 'CL': p.position or '', '부서명': p.org or '',
            '직책구분': '임원/Master' if p.role != 'leader' else '일반'}
    def pick(c):
        if c in manual and c not in FROM_DB:
            return manual[c]
        return info.get(c) or base.get(c, '')
    row = {c: str(pick(c) or '') for c in STATUS_COLUMNS}
    return row, {c for c in manual if c in STATUS_COLUMNS and c not in FROM_DB}


def list_status(query: str = '', include_former: bool = False) -> list[dict]:
    """리더급 현황 — 리더 + 지금 임원. 행마다 STATUS_COLUMNS 칸과 id·role·former·edited(고친 칸).
    직책구분(임원/Master → R.Master → 보직장 → 일반) → Platform → 그룹 → 과제/파트 → 성명 순."""
    words = [w.lower() for w in store.search_terms(query or '')]
    with db.SessionLocal() as s:
        people = list(s.scalars(select(Person).where(Person.active.is_(True) | (Person.role == 'leader'))))
    out = []
    for p in people:
        leader = p.role == 'leader'
        former = (p.hr_state or '') == 'former'
        if not leader and (former or not p.active):  # 임원은 지금 재직 중인 사람만
            continue
        if leader and former and not include_former:
            continue
        row, edited = _status_of(p)
        if words and not all(any(w in v.lower() for v in row.values()) or w in (p.note or '').lower() for w in words):
            continue
        out.append({**row, 'id': p.id, 'role': p.role or '', 'former': former, 'edited': sorted(edited)})
    return sorted(out, key=lambda r: (r['former'], KIND_ORDER.get(r['직책구분'], 9), r['Platform'], r['그룹'],
                                      r['과제/파트명'], r['성명']))


def save_status(person_id: int, column: str, value: str) -> None:
    """현황 칸 하나를 고친다 — 불러온 값과 같아지면 고친 표시를 지운다. 불러오기를 다시 해도 고친 값이 앞선다."""
    if column not in STATUS_COLUMNS:
        raise ValueError(f'없는 칸입니다: {column}')
    if column in FROM_DB:
        raise ValueError(f'{column} 은(는) 사번으로 People in SAIT 에서 읽는 칸이라 고칠 수 없습니다.')
    value = str(value if value is not None else '').strip()
    with db.SessionLocal() as s:
        p = s.get(Person, person_id)
        if p is None:
            raise ValueError('사람을 찾을 수 없습니다.')
        manual = _loads(p.leader_manual)
        synced = str(_loads(p.leader_info).get(column, '') or '')
        if value == synced:
            manual.pop(column, None)
        else:
            manual[column] = value
        p.leader_manual = json.dumps(manual, ensure_ascii=False) if manual else ''
        s.commit()


def list_leaders(query: str = '', include_former: bool = False) -> list[dict]:
    """리더 목록 + 최근 평가·리더십진단 요약. 지난 리더(직책자 목록에서 빠짐)·추적 중지는 include_former 일 때만."""
    rows = store.list_people(query, include_inactive=include_former, role='leader', order='recent')
    if not include_former:
        rows = [r for r in rows if r['hr_state'] != 'former']
    ids = [r['id'] for r in rows]
    evals, lead = _latest_evals(ids), _latest_leadership(ids)
    for r in rows:
        r['eval'] = evals.get(r['id'], '')
        r['leadership'] = lead.get(r['id'], '')
        r['former'] = r['hr_state'] == 'former'
    return sorted(rows, key=lambda r: (r['former'], r['org_path'] or r['org'], r['name']))


def _latest_evals(ids: list[int]) -> dict[int, str]:
    """사람별 가장 최근 연도의 평가 — '2026 연봉등급 가 · 2025 하반기업적 EM' 처럼 종류마다 최근 것."""
    if not ids:
        return {}
    from services.db import PersonEvaluation
    best: dict[int, dict[str, tuple]] = {}
    with db.SessionLocal() as s:
        for e in s.scalars(select(PersonEvaluation).where(PersonEvaluation.person_id.in_(ids))):
            cur = best.setdefault(e.person_id, {}).get(e.kind)
            if (e.grade or '').strip() and (cur is None or e.year > cur[0]):
                best[e.person_id][e.kind] = (e.year, e.grade)
    return {pid: ' · '.join(f'{y} {k} {g}' for k, (y, g) in sorted(kinds.items(), key=lambda x: -x[1][0]))
            for pid, kinds in best.items()}


def _avg(scores: dict) -> float | None:
    vals = [v for k, v in scores.items() if k != '저해행동' and isinstance(v, (int, float))]
    return round(sum(vals) / len(vals), 2) if vals else None


def _latest_leadership(ids: list[int]) -> dict[int, str]:
    """사람별 가장 최근 리더십진단의 타인평균(저해행동 뺀 역량 평균) — '2025 3.85'."""
    out: dict[int, tuple] = {}
    for pid, year, group, scores in _leadership_rows(ids):
        if group == OTHERS and (pid not in out or year > out[pid][0]):
            avg = _avg(scores)
            if avg is not None:
                out[pid] = (year, avg)
    return {pid: f'{y} {a:.2f}' for pid, (y, a) in out.items()}


def _leadership_rows(ids: list[int]):
    if not ids:
        return []
    with db.SessionLocal() as s:
        rows = s.execute(select(PersonLeadership.person_id, PersonLeadership.year, PersonLeadership.evaluator_group,
                                PersonLeadership.scores).where(PersonLeadership.person_id.in_(ids))).all()
    out = []
    for pid, year, group, raw in rows:
        try:
            scores = json.loads(raw or '{}')
        except ValueError:
            scores = {}
        out.append((pid, year, group or '', scores if isinstance(scores, dict) else {}))
    return out


GROUP_ORDER = ['본인', '상사', '동료', '부서원', OTHERS]


def leadership(person_id: int) -> list[dict]:
    """이 사람의 리더십진단 — 최근 연도 먼저, 연도 안에서는 평가자 그룹 순. [{'year','group','scores','avg'}]"""
    rows = [{'year': y, 'group': g, 'scores': sc, 'avg': _avg(sc)} for _, y, g, sc in _leadership_rows([person_id])]
    order = {g: i for i, g in enumerate(GROUP_ORDER)}
    return sorted(rows, key=lambda r: (-r['year'], order.get(r['group'], len(order)), r['group']))


def add_leader(name: str, assignment: str = '', org: str = '', source_id: str = '', position: str = '') -> dict:
    """직접 추가 — 사번을 넣으면 다음 불러오기 때 인사 정보 빈 칸·경력·평가·리더십진단이 채워진다."""
    p = store.create_person(name, position=position, org=org, role='leader', assignment=assignment,
                            source_id=source_id)
    if p['source_id']:  # 지웠던 사번을 다시 넣으면 제외를 푼다
        from services.db import LeaderExclusion
        with db.SessionLocal() as s:
            x = s.get(LeaderExclusion, p['source_id'])
            if x is not None:
                s.delete(x)
                s.commit()
    if p['source_id']:  # 사번이 있으면 Platform·그룹·부서명 등 현황 칸을 바로 (8자리로 맞춘 사번)
        from services import leader_sync
        leader_sync.refresh_status([p['source_id']])
    return p


def delete_leaders(person_ids: list[int], by: str = '') -> dict:
    """리더 명단에서 지운다 → {'deleted': [이름], 'blocked': [(이름, 이유)], 'execs': [이름]}.
    임원은 지우지 않는다(임원 화면에서). People in SAIT 사번이 있는 리더는 다음 불러오기에서 다시 생기지 않게
    사번을 제외 목록에 남긴다. 그 사람에게만 걸린 동향이 있으면 지우지 않는다(store.delete_person)."""
    from services.db import LeaderExclusion
    out = {'deleted': [], 'blocked': [], 'execs': []}
    for pid in person_ids or []:
        p = store.get_person(int(pid))
        if p is None:
            continue
        if p['role'] != 'leader':
            out['execs'].append(p['name'])
            continue
        try:
            store.delete_person(p['id'])
        except ValueError as e:
            out['blocked'].append((p['name'], str(e).replace('이 임원에게만', '이 사람에게만')))
            continue
        if p['source_id']:
            with db.SessionLocal() as s:
                if s.get(LeaderExclusion, p['source_id']) is None:
                    s.add(LeaderExclusion(source_id=p['source_id'], name=p['name'], excluded_by=by or ''))
                    s.commit()
        out['deleted'].append(p['name'])
    return out


def excluded_sids() -> set[str]:
    from services.db import LeaderExclusion
    with db.SessionLocal() as s:
        return set(s.scalars(select(LeaderExclusion.source_id)))


def is_leader(person_id: int) -> bool:
    with db.SessionLocal() as s:
        return s.scalar(select(Person.role).where(Person.id == person_id)) == 'leader'


# ── 엑셀 내보내기 ──────────────────────────────────────────────────────

_HEAD_FILL = PatternFill('solid', fgColor='E8EEF7')


def _sheet(wb: Workbook, title: str, header: list[str], rows: list[list], widths: list[int] | None = None):
    ws = wb.create_sheet(title)
    ws.append(header)
    for c in ws[1]:
        c.font, c.fill = Font(bold=True), _HEAD_FILL
        c.alignment = Alignment(horizontal='center', vertical='center')
    for r in rows:
        ws.append(['' if v is None else v for v in r])
    for i, w in enumerate(widths or [], start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    sid_cols = {i for i, h in enumerate(header) if h in ('사원번호', '사번')}
    for row in ws.iter_rows(min_row=2):
        for i, c in enumerate(row):
            c.alignment = Alignment(vertical='top', wrap_text=True)
            if i in sid_cols:
                c.number_format = '@'  # 텍스트 — 엑셀에서 열어 고쳐도 앞자리 0 이 지워지지 않게
    ws.freeze_panes = 'B2'
    return ws


def export_xlsx(person_ids: list[int], records: int = 20) -> bytes:
    """고른 리더(또는 임원)의 자료를 엑셀로 — 리더급 현황 / 기본 정보 / 경력·발령 / 평가 / 리더십진단 /
    최근 기록(사람마다 records 건)."""
    people = [p for p in (store.get_person(int(i)) for i in person_ids) if p]
    if not people:
        raise ValueError('내보낼 사람을 고르세요.')
    wb = Workbook()
    wb.remove(wb.active)
    with db.SessionLocal() as s:
        status = {p.id: _status_of(p)[0] for p in s.scalars(select(Person).where(Person.id.in_([p['id'] for p in people])))}
    _sheet(wb, '리더급 현황', STATUS_COLUMNS, [[status[p['id']][c] for c in STATUS_COLUMNS] for p in people],
           [10, 14, 16, 16, 18, 9, 7, 12, 8, 11, 7, 11, 9, 10, 9, 24, 13])
    _sheet(wb, '기본 정보', ['이름', '사번', '직책', '조직', '상위 조직', '직급', '재직상태', '나이', '입사일', '근속(년)',
                         '승격일', '연차', '최종학력', '최근 평가', '리더십진단(타인평균)', '메모'],
           [[p['name'], p['source_id'], p['assignment'], p['org'], p['org_path'], p['position'],
             p['employment_status'], p['age'], p['hire_date'], p['tenure'], p['promotion_date'], p['grade_years'],
             p['education'], _latest_evals([p['id']]).get(p['id'], ''),
             _latest_leadership([p['id']]).get(p['id'], ''), p['note']] for p in people],
           [10, 11, 14, 22, 34, 10, 9, 6, 11, 8, 11, 6, 28, 30, 14, 30])
    _sheet(wb, '경력·발령', ['이름', '구분', '날짜', '직급', '조직', '발령', '직책'],
           [[p['name'], c['kind'], c['date'], c['position'], c['org'], c['title'], c['assignment']]
            for p in people for c in store.list_career(p['id'])], [10, 9, 11, 10, 30, 24, 14])
    evals = [(p, e) for p in people for e in profile.list_evaluations(p['id'])]
    _sheet(wb, '평가', ['이름', '연도', '종류', '등급', '메모'],
           [[p['name'], e['year'], e['kind'], e['grade'], e['note']] for p, e in evals], [10, 7, 14, 8, 40])
    _sheet(wb, '리더십진단', ['이름', '연도', '평가자', *LEADERSHIP_DIMS, '평균(저해행동 제외)'],
           [[p['name'], x['year'], x['group'], *[x['scores'].get(d) for d in LEADERSHIP_DIMS], x['avg']]
            for p in people for x in leadership(p['id'])], [10, 7, 9] + [9] * len(LEADERSHIP_DIMS) + [12])
    hist_rows = []
    for p in people:
        for h in store.search_history('', person_id=p['id'], limit=records):
            hist_rows.append([p['name'], h['occurred_at'][:10], h['entry_type'], h['title'] or '',
                              h['summary'] or h['content'][:500], h.get('task_title') or ''])
    _sheet(wb, '최근 기록', ['이름', '날짜', '유형', '제목', '요약·내용', '업무'], hist_rows, [10, 11, 8, 30, 60, 24])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def status_template() -> bytes:
    """리더급 현황 엑셀 — 지금 표(지난 리더 빼고) 그대로. 고쳐서 리더 엑셀 업로드(/leaders/import)로 다시 올리는 양식."""
    wb = Workbook()
    wb.remove(wb.active)
    _sheet(wb, '리더급 현황', STATUS_COLUMNS, [[r[c] for c in STATUS_COLUMNS] for r in list_status()],
           [10, 14, 16, 16, 18, 9, 7, 12, 8, 11, 7, 11, 9, 10, 9, 24, 13])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def export_filename(n: int) -> str:
    return f"리더_자료_{date.today().strftime('%Y%m%d')}_{n}명.xlsx"


def attachment_key(person_ids: list[int]) -> str:
    """메일 보내기 창(components/mail_ui.py)의 첨부 값 — 보낼 때 services/outbox.py 가 엑셀을 새로 만든다."""
    return 'leaders:' + '.'.join(str(int(i)) for i in person_ids)


def draft_mail(person_ids: list[int]) -> dict:
    """메일 초안 — 본문은 짧은 안내, 자료는 엑셀 첨부(미리 체크)."""
    people = [p for p in (store.get_person(int(i)) for i in person_ids) if p]
    if not people:
        raise ValueError('내보낼 리더를 고르세요.')
    names = ', '.join(p['name'] for p in people[:10]) + (f' 외 {len(people) - 10}명' if len(people) > 10 else '')
    key = attachment_key([p['id'] for p in people])
    return {'subject': f'리더 자료 ({len(people)}명)', 'to': '',
            'text': f'리더 {len(people)}명의 자료를 엑셀로 첨부합니다.\n\n대상: {names}\n'
                    '시트: 리더급 현황 · 기본 정보 · 경력·발령 · 평가 · 리더십진단 · 최근 기록',
            'table_html': '', 'attachments': [{'value': key, 'label': export_filename(len(people))}],
            'attach_value': [key], 'related_kind': 'leaders', 'related_id': ''}
