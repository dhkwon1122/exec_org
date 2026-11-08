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
    return store.create_person(name, position=position, org=org, role='leader', assignment=assignment,
                               source_id=source_id)


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
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = Alignment(vertical='top', wrap_text=True)
    ws.freeze_panes = 'B2'
    return ws


def export_xlsx(person_ids: list[int], records: int = 20) -> bytes:
    """고른 리더(또는 임원)의 자료를 엑셀로 — 기본 정보 / 경력·발령 / 평가 / 리더십진단 / 최근 기록(사람마다 records 건)."""
    people = [p for p in (store.get_person(int(i)) for i in person_ids) if p]
    if not people:
        raise ValueError('내보낼 사람을 고르세요.')
    wb = Workbook()
    wb.remove(wb.active)
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
                    '시트: 기본 정보 · 경력·발령 · 평가 · 리더십진단 · 최근 기록',
            'table_html': '', 'attachments': [{'value': key, 'label': export_filename(len(people))}],
            'attach_value': [key], 'related_kind': 'leaders', 'related_id': ''}
