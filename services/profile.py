"""임원 인사 기록 — 주요 이력(발령이력을 정리한 핵심 경력), 평가 이력(연도·종류별), 추가 항목.

주요 이력 초안은 경력 이력(People in SAIT 인사발령·직급/소속 변천, person_career)에서 만든다.
LLM 이 설정돼 있으면 AI 가 중요한 것만 골라 다듬고, 아니면 규칙(승진·선임·보임 등 발령)으로 고른다.
초안은 화면에서 고친 뒤 저장해야 DB 에 남는다.
"""
import re

from sqlalchemy import select

from services import db, llm, store
from services.db import PersonAttribute, PersonEvaluation, PersonHighlight

DEFAULT_EVAL_KINDS = ['임원종합평가', 'MBO']
# 주요 이력 규칙 초안에 넣을 발령 — 이 말이 발령명에 있으면 핵심 경력으로 본다
KEY_ORDER_WORDS = ('승진', '승격', '선임', '보임', '임명', '위촉', '영입', '입사', '채용', '전보', '겸임', '겸직',
                   '퇴임', '퇴직', '해임', '전출', '파견', '복귀')


# ── 주요 이력 ─────────────────────────────────────────────────────────

def normalize_date(v) -> str:
    """'2023-03-01' / '2023.3' / '202303' / '2023년 3월' → 'YYYY.MM.DD' / 'YYYY.MM' / 'YYYY'. 못 읽으면 원래 글자."""
    s = str(v or '').strip()
    m = re.match(r'^(\d{4})\s*[.\-/년]?\s*(\d{1,2})?\s*[.\-/월]?\s*(\d{1,2})?\s*일?\.?$', s)
    if not m:
        return s[:10]
    y, mo, d = m.groups()
    if mo and not 1 <= int(mo) <= 12:
        return s[:10]
    return y + (f'.{int(mo):02d}' if mo else '') + (f'.{int(d):02d}' if mo and d else '')


def list_highlights(person_id: int) -> list[dict]:
    """시간순(오래된 것 먼저). 날짜 없는 항목은 입력 순서대로 뒤에."""
    with db.SessionLocal() as s:
        rows = list(s.scalars(select(PersonHighlight).where(PersonHighlight.person_id == person_id)
                              .order_by(PersonHighlight.sort, PersonHighlight.id)))
        return [{'date': h.date or '', 'text': h.text, 'source': h.source or ''} for h in rows]


def parse_highlight_lines(text: str) -> list[dict]:
    """'2023.03 상무 승진, AI센터장' 처럼 한 줄에 하나 → [{'date','text'}]. 날짜가 앞에 없으면 날짜 없이."""
    out = []
    for line in str(text or '').splitlines():
        line = line.strip().lstrip('-•·*').strip()
        if not line:
            continue
        m = re.match(r'^(\d{4}(?:\s*[.\-/년]\s*\d{1,2}(?:\s*[.\-/월]\s*\d{1,2}\s*일?)?\s*월?)?\.?)\s*[:)\-]?\s*(.+)$',
                     line)
        if m and normalize_date(m.group(1))[:4].isdigit():
            out.append({'date': normalize_date(m.group(1)), 'text': m.group(2).strip()})
        else:
            out.append({'date': '', 'text': line})
    return out


def save_highlights(person_id: int, items: list[dict], source: str = 'manual') -> int:
    """이 임원의 주요 이력을 통째로 바꾼다. 날짜순으로 정렬해 저장. 저장한 건수."""
    clean = []
    for it in items or []:
        text = str(it.get('text') or '').strip()
        if text:
            clean.append({'date': normalize_date(it.get('date')), 'text': text[:500],
                          'source': it.get('source') or source})
    dated = sorted([c for c in clean if c['date']], key=lambda c: c['date'])
    clean = dated + [c for c in clean if not c['date']]
    with db.SessionLocal() as s:
        for h in s.scalars(select(PersonHighlight).where(PersonHighlight.person_id == person_id)):
            s.delete(h)
        for i, c in enumerate(clean):
            s.add(PersonHighlight(person_id=person_id, date=c['date'], text=c['text'], sort=i, source=c['source']))
        s.commit()
    return len(clean)


def _career_line(c: dict) -> str:
    if c['kind'] == '발령':
        return ' · '.join(x for x in (c['title'], c['org'], c['position'], c['assignment']) if x)
    return ' · '.join(x for x in (c['position'], c['org']) if x)


def rule_draft(career: list[dict]) -> list[dict]:
    """규칙 초안 — 핵심 발령(승진·선임·보임 등)만, 발령이 없으면 직급·소속이 바뀐 시점."""
    asc = sorted(career, key=lambda c: c['date'])
    orders = [c for c in asc if c['kind'] == '발령']
    picked = [c for c in orders if any(w in (c['title'] or '') for w in KEY_ORDER_WORDS)] or orders
    if not picked:
        picked = [c for c in asc if c['kind'] == '직급·소속']
    out, seen = [], set()
    for c in picked:
        item = {'date': normalize_date(c['date'][:7]), 'text': _career_line(c), 'source': 'rule'}
        if item['text'] and (item['date'], item['text']) not in seen:
            seen.add((item['date'], item['text']))
            out.append(item)
    return out


DRAFT_SYSTEM = """너는 임원 인사 기록을 정리하는 인사 담당자다. 인사발령 이력과 직급·소속 변천을 읽고, 임원 프로필에 올릴
'주요 이력'을 시간순으로 간결하게 정리한다.
규칙:
- 승진·선임·보임(직책 부여)·조직 이동·겸직·퇴임처럼 경력상 의미 있는 것만 남긴다. 단순 조직명 변경, 같은 내용 반복, 행정 발령은 합친다.
- 한 줄은 '무엇이 되었는지' 위주로 짧게 (예: "상무 승진, AI센터장 보임"). 같은 달의 여러 발령은 한 줄로 합친다.
- 날짜는 YYYY.MM 형식.
- 입력에 없는 사실은 만들지 않는다.
반드시 JSON 만 출력: {"highlights": [{"date": "YYYY.MM", "text": "..."}]}"""


def ai_draft(name: str, career: list[dict]) -> list[dict]:
    lines = '\n'.join(f"- {c['date']} [{c['kind']}] {_career_line(c)}" for c in sorted(career, key=lambda c: c['date']))
    raw = llm.chat(DRAFT_SYSTEM, f'임원: {name}\n\n경력 이력:\n{lines[:llm.MAX_INPUT_CHARS]}')
    data = llm._extract_json(raw)
    out = []
    for h in data.get('highlights') or []:
        if isinstance(h, dict) and str(h.get('text') or '').strip():
            out.append({'date': normalize_date(h.get('date')), 'text': str(h['text']).strip()[:500], 'source': 'ai'})
    if not out:
        raise llm.LLMError('AI 가 주요 이력을 만들지 못했습니다.')
    return out


def draft_highlights(person_id: int, use_ai: bool = True) -> tuple[list[dict], str]:
    """경력 이력으로 주요 이력 초안을 만든다 → (초안, 안내 문구). 저장은 하지 않는다."""
    p = store.get_person(person_id)
    career = store.list_career(person_id)
    if not career:
        return [], '경력 이력(인사발령)이 없습니다 — 임원 목록에서 "People in SAIT에서 불러오기"를 먼저 반영하세요.'
    if use_ai and llm.is_configured():
        try:
            return ai_draft(p['name'], career), f'AI 가 경력 이력 {len(career)}건을 정리한 초안입니다. 고친 뒤 저장하세요.'
        except llm.LLMError as e:
            return rule_draft(career), f'AI 정리 실패({e}) — 규칙으로 고른 초안입니다. 고친 뒤 저장하세요.'
    return rule_draft(career), f'경력 이력 {len(career)}건 중 승진·선임·보임 등 핵심 발령을 고른 초안입니다. 고친 뒤 저장하세요.'


# ── 평가 이력 ─────────────────────────────────────────────────────────

def eval_kinds() -> list[str]:
    """평가 종류 — 기본(임원종합평가, MBO) + DB 에 있는 다른 종류."""
    with db.SessionLocal() as s:
        extra = sorted({k for k in s.scalars(select(PersonEvaluation.kind).distinct()) if k} - set(DEFAULT_EVAL_KINDS))
    return DEFAULT_EVAL_KINDS + extra


def list_evaluations(person_id: int) -> list[dict]:
    with db.SessionLocal() as s:
        rows = s.scalars(select(PersonEvaluation).where(PersonEvaluation.person_id == person_id)
                         .order_by(PersonEvaluation.year.desc(), PersonEvaluation.kind))
        return [{'year': e.year, 'kind': e.kind, 'grade': e.grade or '', 'note': e.note or '',
                 'source': e.source or ''} for e in rows]


def normalize_year(v) -> int | None:
    """'2025' / '25' / "'25" / '25년' / 2025.0 → 2025."""
    m = re.search(r'\d+', str(v or '').replace('.0', ''))
    if not m:
        return None
    n = int(m.group(0))
    if n < 100:
        n += 2000 if n < 70 else 1900
    return n if 1950 <= n <= 2100 else None


def upsert_evaluation(s, person_id: int, year: int, kind: str, grade: str, note: str | None = None,
                      source: str = 'manual') -> bool:
    """세션 안에서 (임원, 연도, 종류) 평가를 넣거나 고친다. 바뀌었으면 True."""
    e = s.scalar(select(PersonEvaluation).where(PersonEvaluation.person_id == person_id,
                                                PersonEvaluation.year == year, PersonEvaluation.kind == kind))
    if e is None:
        s.add(PersonEvaluation(person_id=person_id, year=year, kind=kind, grade=grade, note=note or '', source=source))
        return True
    if e.grade == grade and (note is None or e.note == note):
        return False
    e.grade, e.source = grade, source
    if note is not None:
        e.note = note
    return True


def save_evaluations(person_id: int, items: list[dict]) -> int:
    """이 임원의 평가 이력을 통째로 바꾼다 (화면 편집). 연도·종류가 없거나 등급·메모가 다 빈 줄은 버린다."""
    clean = {}
    for it in items or []:
        year, kind = normalize_year(it.get('year')), str(it.get('kind') or '').strip()
        grade, note = str(it.get('grade') or '').strip(), str(it.get('note') or '').strip()
        if year and kind and (grade or note):
            clean[(year, kind)] = (grade[:50], note)
    with db.SessionLocal() as s:
        for e in s.scalars(select(PersonEvaluation).where(PersonEvaluation.person_id == person_id)):
            if (e.year, e.kind) not in clean:
                s.delete(e)
        s.flush()
        for (year, kind), (grade, note) in clean.items():
            upsert_evaluation(s, person_id, year, kind, grade, note)
        s.commit()
    return len(clean)


def evaluation_table(person_id: int) -> tuple[list[str], list[dict]]:
    """화면 표 — (종류 열 목록, 연도별 행 [{'year', 종류: 등급}]) 최근 연도가 위."""
    evals = list_evaluations(person_id)
    kinds = [k for k in DEFAULT_EVAL_KINDS] + sorted({e['kind'] for e in evals} - set(DEFAULT_EVAL_KINDS))
    by_year: dict[int, dict] = {}
    for e in evals:
        by_year.setdefault(e['year'], {'year': e['year']})[e['kind']] = e['grade'] or e['note']
    return kinds, [by_year[y] for y in sorted(by_year, reverse=True)]


# ── 추가 항목 ─────────────────────────────────────────────────────────

def attribute_names() -> list[str]:
    with db.SessionLocal() as s:
        return sorted({n for n in s.scalars(select(PersonAttribute.name).distinct()) if n})


def list_attributes(person_id: int) -> list[dict]:
    with db.SessionLocal() as s:
        rows = s.scalars(select(PersonAttribute).where(PersonAttribute.person_id == person_id)
                         .order_by(PersonAttribute.name))
        return [{'name': a.name, 'value': a.value or ''} for a in rows]


def upsert_attribute(s, person_id: int, name: str, value: str) -> bool:
    a = s.scalar(select(PersonAttribute).where(PersonAttribute.person_id == person_id, PersonAttribute.name == name))
    if a is None:
        s.add(PersonAttribute(person_id=person_id, name=name, value=value))
        return True
    if a.value == value:
        return False
    a.value = value
    return True
