"""임원 정보 엑셀 일괄 업로드.

1) 파일(xlsx/csv)의 첫 행을 제목으로 읽는다.
2) 제목마다 DB 의 어느 칸에 넣을지 정한다(매칭).
   - 규칙으로 확실한 것: 사번·성명·직위 같은 정해진 이름, '25mbo'·'2024 임원종합평가' 같은 평가 제목(연도+종류)
   - 나머지는 사내 LLM 이 제목·예시 값을 보고 판단 (LLM 이 없으면 '새 항목'으로)
   - 기존 칸이 없으면 미리 만든다: 처음 보는 평가 종류는 새 종류로, 그 밖의 제목은 '추가 항목'으로
   화면에서 열마다 매칭을 바꿀 수 있다.
3) 사번(없으면 성명)으로 임원을 찾아 값을 넣는다. 빈 칸은 건드리지 않는다.
"""
import base64
import csv
import io
import re
from datetime import date, datetime

from sqlalchemy import select

from services import db, llm, profile
from services.db import Person, PersonAttribute, PersonEvaluation, PersonHighlight

# 사람 정보 칸 — target 'field:<이름>'
PERSON_FIELDS = {
    'position': '직위(CL)', 'org': '소속', 'employment_status': '재직상태', 'birth_year': '출생연도',
    'hire_date': '입사일', 'promotion_date': '승격일', 'education': '최종학력', 'aliases': '별칭', 'note': '메모',
}
KEYS = {'source_id': '사번', 'name': '성명'}

# 제목 → 칸 (공백·밑줄 없애고 소문자로 비교)
SYNONYMS = {
    'key:source_id': ['사번', '사원번호', '직원번호', '임직원번호', 'researcherid', 'employeeid', 'empno', 'id'],
    'key:name': ['성명', '이름', '임원명', '성함', 'name'],
    'field:position': ['직위', '직급', 'cl', '직급명', '직위명', 'position'],
    'field:org': ['소속', '부서', '부서명', '소속부서', '현소속부서명', '조직', 'department', 'org'],
    'field:employment_status': ['재직상태', '재직상태명', '재직여부', '상태'],
    'field:birth_year': ['출생연도', '생년', '출생년도', '생년도', 'birthyear'],
    'field:hire_date': ['입사일', '입사일자', '그룹입사일', '근속기준일', '근속기준일_그룹입사일', 'hiredate'],
    'field:promotion_date': ['승격일', '승격일자', '승격산정기준일자', '승진일', '임원선임일', '선임일'],
    'field:education': ['최종학력', '학력'],
    'field:aliases': ['별칭', '호칭'],
    'field:note': ['메모', '비고', '참고'],
    'ignore': ['no', '번호', '순번', '연번', '순서', '#'],
    'highlights': ['주요이력', '주요경력', '주요 이력', '경력사항', '주요경력사항', '이력'],
}
EVAL_ALIASES = {'mbo': 'MBO', '임원종합평가': '임원종합평가', '종합평가': '임원종합평가', '종합': '임원종합평가'}


def _norm(h: str) -> str:
    return re.sub(r'[\s_\-()\[\]./]', '', str(h or '')).lower()


def cell_str(v) -> str:
    if v is None:
        return ''
    if isinstance(v, datetime):
        return v.strftime('%Y-%m-%d')
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = str(v).strip()
    return '' if s.lower() in ('nan', 'none') else s


# ── 파일 읽기 ──────────────────────────────────────────────────────────

class ImportError_(ValueError):
    pass


def parse_upload(contents: str, filename: str) -> dict:
    """dcc.Upload 내용 → {'headers': [...], 'rows': [[...], ...]} (첫 행이 제목, 빈 행 제외)."""
    try:
        data = base64.b64decode(contents.split(',', 1)[1])
    except (IndexError, ValueError) as e:
        raise ImportError_('파일을 읽을 수 없습니다.') from e
    name = (filename or '').lower()
    if name.endswith('.csv'):
        for enc in ('utf-8-sig', 'cp949'):
            try:
                table = list(csv.reader(io.StringIO(data.decode(enc))))
                break
            except UnicodeDecodeError:
                continue
        else:
            raise ImportError_('CSV 인코딩을 알 수 없습니다 (UTF-8 또는 CP949).')
    elif name.endswith(('.xlsx', '.xlsm')):
        import openpyxl
        try:
            wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        except Exception as e:  # noqa: BLE001
            raise ImportError_(f'엑셀 파일을 열 수 없습니다: {type(e).__name__}') from e
        table = [list(r) for r in wb.worksheets[0].iter_rows(values_only=True)]
        wb.close()
    else:
        raise ImportError_('xlsx 또는 csv 파일만 올릴 수 있습니다.')
    table = [[cell_str(v) for v in r] for r in table if any(cell_str(v) for v in r)]
    if len(table) < 2:
        raise ImportError_('제목 행과 데이터가 있어야 합니다 (첫 행 = 제목).')
    headers = table[0]
    while headers and not headers[-1]:
        headers.pop()
    width = len(headers)
    rows = [(r + [''] * width)[:width] for r in table[1:]]
    return {'headers': [h or f'(제목 없음 {i + 1})' for i, h in enumerate(headers)], 'rows': rows}


# ── 매칭 ──────────────────────────────────────────────────────────────

def eval_target(kind: str, year: int) -> str:
    return f'eval:{kind}:{year}'


def parse_target(t: str) -> tuple:
    """'eval:MBO:2025' → ('eval', 'MBO', 2025), 'attr:취미' → ('attr', '취미'), 'field:org' → ('field', 'org')."""
    parts = (t or 'ignore').split(':', 2)
    if parts[0] == 'eval' and len(parts) == 3 and parts[2].isdigit():
        return 'eval', parts[1], int(parts[2])
    if parts[0] in ('key', 'field', 'attr') and len(parts) >= 2:
        return parts[0], ':'.join(parts[1:])
    if parts[0] == 'highlights':
        return ('highlights',)
    return ('ignore',)


def target_label(t: str) -> str:
    p = parse_target(t)
    if p[0] == 'key':
        return f'{KEYS.get(p[1], p[1])} (임원 찾기)'
    if p[0] == 'field':
        return PERSON_FIELDS.get(p[1], p[1])
    if p[0] == 'eval':
        return f'평가 · {p[2]} {p[1]}'
    if p[0] == 'attr':
        return f'추가 항목 · {p[1]}'
    if p[0] == 'highlights':
        return '주요 이력 (한 줄에 하나, 기존 것을 바꿈)'
    return '무시'


def _eval_from_header(h: str) -> tuple[str, int] | None:
    """'25mbo' / 'MBO 2024' / '2024년 임원종합평가' / "'23 종합" → (종류, 연도). 연도 + 평가 단서가 있어야 한다."""
    raw = str(h or '').strip()
    m = re.search(r"(?<!\d)'?((?:19|20)\d{2}|\d{2})(?!\d)\s*년?", raw)
    if not m:
        return None
    year = profile.normalize_year(m.group(1))
    rest = (raw[:m.start()] + ' ' + raw[m.end():]).strip(" _-·.()[]'년도")
    key = _norm(rest)
    kind = None
    for alias, k in EVAL_ALIASES.items():
        if alias in key:
            kind = k
            break
    if kind is None and ('평가' in rest or '등급' in rest):
        kind = re.sub(r'\s*(등급|결과)$', '', rest).strip()
        if _norm(kind) in ('평가', '평가등급', '등급', ''):  # 어떤 평가인지 모름 → AI·사람이 정한다
            kind = None
    return (kind, year) if kind and year else None


def rule_match(header: str, attrs: list[str]) -> str | None:
    n = _norm(header)
    for target, words in SYNONYMS.items():
        if n in {_norm(w) for w in words}:
            return target
    ev = _eval_from_header(header)
    if ev:
        return eval_target(*ev)
    if header.strip() in attrs:
        return f'attr:{header.strip()}'
    return None


MAP_SYSTEM = """너는 인사 데이터 담당자다. 엑셀 열 제목과 예시 값을 보고, 임원 DB 의 어느 칸에 넣을지 판단한다.
DB 칸:
- key:source_id (사번) / key:name (성명) — 임원을 찾는 열
- field:position(직위·CL), field:org(소속), field:employment_status(재직상태), field:birth_year(출생연도),
  field:hire_date(입사일), field:promotion_date(승격·선임일), field:education(최종학력), field:aliases(별칭), field:note(메모)
- highlights — 주요 이력·경력 요약(여러 줄 글)
- eval:<종류>:<연도> — 연도별 평가 등급. 종류는 기존 종류 중 고르고({kinds}), 맞는 게 없으면 새 종류 이름을 쓴다.
  2자리 연도(25)는 2025 로.
- attr:<이름> — 위에 맞는 칸이 없을 때 새 추가 항목. 기존 추가 항목: {attrs}
- ignore — 순번·합계처럼 저장할 필요 없는 열
반드시 JSON 만 출력: {{"columns": [{{"index": 0, "target": "...", "reason": "짧은 근거"}}]}}"""


def ai_match(headers: list[str], samples: list[list[str]], idx: list[int], attrs: list[str]) -> dict[int, tuple[str, str]]:
    lines = []
    for i in idx:
        ex = [s[i] for s in samples if i < len(s) and s[i]][:3]
        lines.append(f'{i}: "{headers[i]}" 예시: {ex}')
    raw = llm.chat(MAP_SYSTEM.format(kinds=', '.join(profile.eval_kinds()), attrs=', '.join(attrs) or '없음'),
                   '열 목록:\n' + '\n'.join(lines))
    out = {}
    for c in llm._extract_json(raw).get('columns') or []:
        try:
            i = int(c.get('index'))
        except (TypeError, ValueError):
            continue
        t = str(c.get('target') or '').strip()
        p = parse_target(t)
        if i in idx and (p[0] != 'ignore' or t == 'ignore'):
            if p[0] == 'field' and p[1] not in PERSON_FIELDS or p[0] == 'key' and p[1] not in KEYS:
                continue
            out[i] = (t, str(c.get('reason') or '')[:100])
    return out


def suggest_mapping(headers: list[str], rows: list[list[str]], use_ai: bool = True) -> list[dict]:
    """열마다 {'index','header','target','source'(rule/ai/new),'reason','samples'}."""
    attrs = profile.attribute_names()
    out, todo = [], []
    for i, h in enumerate(headers):
        t = rule_match(h, attrs)
        out.append({'index': i, 'header': h, 'target': t or f'attr:{h.strip()}', 'source': 'rule' if t else 'new',
                    'reason': '제목 규칙' if t else '맞는 칸이 없어 새 추가 항목으로',
                    'samples': [r[i] for r in rows if r[i]][:3]})
        if not t:
            todo.append(i)
    if todo and use_ai and llm.is_configured():
        try:
            for i, (t, reason) in ai_match(headers, rows[:5], todo, attrs).items():
                out[i].update(target=t, source='ai', reason=reason or 'AI 판단')
        except llm.LLMError as e:
            for i in todo:
                out[i]['reason'] = f'AI 판단 실패({e}) — 새 추가 항목으로'
    return out


def target_options(mapping: list[dict]) -> list[dict]:
    """매칭 드롭다운 — 고정 칸 + 평가(기존 종류 × 파일에 나온 연도) + 추가 항목 + 무시."""
    opts = [{'label': target_label(f'key:{k}'), 'value': f'key:{k}'} for k in KEYS]
    opts += [{'label': v, 'value': f'field:{k}'} for k, v in PERSON_FIELDS.items()]
    opts.append({'label': target_label('highlights'), 'value': 'highlights'})
    years = sorted({parse_target(m['target'])[2] for m in mapping if parse_target(m['target'])[0] == 'eval'}
                   | {date.today().year - 1}, reverse=True)
    kinds = profile.eval_kinds() + sorted({parse_target(m['target'])[1] for m in mapping
                                          if parse_target(m['target'])[0] == 'eval'} - set(profile.eval_kinds()))
    opts += [{'label': target_label(eval_target(k, y)), 'value': eval_target(k, y)} for y in years for k in kinds]
    names = sorted(set(profile.attribute_names()) | {parse_target(m['target'])[1] for m in mapping
                                                     if parse_target(m['target'])[0] == 'attr'})
    opts += [{'label': target_label(f'attr:{n}'), 'value': f'attr:{n}'} for n in names]
    opts.append({'label': '무시', 'value': 'ignore'})
    seen, uniq = set(), []
    for o in opts:
        if o['value'] not in seen:
            seen.add(o['value'])
            uniq.append(o)
    return uniq


# ── 반영 ──────────────────────────────────────────────────────────────

def _field_value(field: str, v: str) -> str:
    if field == 'birth_year':
        m = re.search(r'(19|20)\d{2}', v)
        return m.group(0) if m else v[:4]
    if field in ('hire_date', 'promotion_date'):
        d = profile.normalize_date(v[:10])
        return d.replace('.', '-') if re.match(r'^\d{4}\.\d{2}\.\d{2}$', d) else v[:10]
    return v


def exclude_columns(targets: list[str], use: list | None) -> list[str]:
    """체크를 푼 열(use 가 거짓)은 '무시'로 — 그 열의 값은 읽지도 바꾸지도 않는다."""
    use = list(use or [])
    return [t if (i >= len(use) or use[i]) else 'ignore' for i, t in enumerate(targets)]


def run(table: dict, targets: list[str], create_missing: bool = False, apply: bool = False,
        by: str = '') -> dict:
    """apply=False 면 미리보기(DB 변경 없음), True 면 반영. 결과 요약을 돌려준다."""
    headers, rows = table['headers'], table['rows']
    parsed = [parse_target(t) for t in targets]
    key_sid = next((i for i, p in enumerate(parsed) if p == ('key', 'source_id')), None)
    key_name = next((i for i, p in enumerate(parsed) if p == ('key', 'name')), None)
    if key_sid is None and key_name is None:
        raise ImportError_('임원을 찾을 열(사번 또는 성명)이 없습니다 — 한 열을 "사번" 또는 "성명"으로 맞추세요.')
    result = {'rows': len(rows), 'matched': 0, 'created': [], 'unmatched': [], 'ambiguous': [],
              'changes': {target_label(t): 0 for t, p in zip(targets, parsed) if p[0] not in ('ignore', 'key')}}
    with db.SessionLocal() as s:
        people = list(s.scalars(select(Person)))
        by_sid = {p.source_id: p for p in people if p.source_id}
        by_name: dict[str, list[Person]] = {}
        for p in people:
            by_name.setdefault(p.name, []).append(p)
        for r in rows:
            sid = r[key_sid].strip() if key_sid is not None else ''
            name = r[key_name].strip() if key_name is not None else ''
            if sid and sid.isdigit() and sid not in by_sid and sid.zfill(8) in by_sid:
                sid = sid.zfill(8)  # 엑셀이 앞자리 0 을 지운 사번
            p = by_sid.get(sid) if sid else None
            if p is None and not sid and name:
                cands = by_name.get(name, [])
                if len(cands) > 1:
                    result['ambiguous'].append(name)
                    continue
                p = cands[0] if cands else None
            if p is None:
                if create_missing and name:
                    p = Person(name=name, source_id=sid, position='', org='', aliases='', note='')
                    if apply:
                        s.add(p)
                        s.flush()
                    if sid:
                        by_sid[sid] = p
                    by_name.setdefault(name, []).append(p)
                    result['created'].append(name)
                else:
                    result['unmatched'].append(sid or name or '(빈 행)')
                    continue
            result['matched'] += 1
            for i, (t, pt) in enumerate(zip(targets, parsed)):
                v = r[i].strip() if i < len(r) else ''
                if not v or pt[0] in ('ignore', 'key'):
                    continue
                label = target_label(t)
                changed = False
                if pt[0] == 'field':
                    nv = _field_value(pt[1], v)
                    changed = (getattr(p, pt[1]) or '') != nv
                    if changed and apply:
                        setattr(p, pt[1], nv)
                elif pt[0] == 'eval':
                    cur = s.scalar(select(PersonEvaluation.grade).where(
                        PersonEvaluation.person_id == p.id, PersonEvaluation.year == pt[2],
                        PersonEvaluation.kind == pt[1])) if p.id else None
                    changed = cur != v[:50]
                    if changed and apply:
                        profile.upsert_evaluation(s, p.id, pt[2], pt[1], v[:50], source='excel')
                elif pt[0] == 'attr':
                    cur = s.scalar(select(PersonAttribute.value).where(
                        PersonAttribute.person_id == p.id, PersonAttribute.name == pt[1])) if p.id else None
                    changed = cur != v
                    if changed and apply:
                        profile.upsert_attribute(s, p.id, pt[1], v)
                elif pt[0] == 'highlights':
                    cur = [(h.date or '', h.text) for h in s.scalars(
                        select(PersonHighlight).where(PersonHighlight.person_id == p.id)
                        .order_by(PersonHighlight.sort, PersonHighlight.id))] if p.id else None
                    changed = cur != [(h['date'], h['text'][:500]) for h in _sorted_lines(v)]
                    if changed and apply:
                        s.flush()
                        _replace_highlights(s, p.id, v)
                if changed:
                    result['changes'][label] += 1
        if apply:
            s.commit()
    return result


def _sorted_lines(text: str) -> list[dict]:
    items = profile.parse_highlight_lines(text)
    return sorted([c for c in items if c['date']], key=lambda c: c['date']) + [c for c in items if not c['date']]


def _replace_highlights(s, person_id: int, text: str):
    for h in s.scalars(select(PersonHighlight).where(PersonHighlight.person_id == person_id)):
        s.delete(h)
    for n, c in enumerate(_sorted_lines(text)):
        s.add(PersonHighlight(person_id=person_id, date=c['date'], text=c['text'][:500], sort=n, source='excel'))
