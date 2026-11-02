"""사내 용어집 — 같은 것을 여러 이름으로 부르는 말(종기원 = SAIT = 종합기술원, 과제 약칭, 회의 이름, 사내 약어)을
대표 용어 하나로 묶는다. 사람은 임원 별칭(people.aliases)이 맡고, 여기는 사람이 아닌 것만.

쓰이는 곳
  - 검색: 같은 말 하나로 찾으면 나머지 말이 들어간 기록도 (expand — store.search_history, 첨부·자료 검색)
  - AI: 원문에 나온 용어만 골라 뜻을 알려 준다 (prompt_note — llm.chat 이 모든 AI 호출에 붙인다)
  - 태그: AI 가 같은 말로 단 키워드·태그를 대표 용어로 (canonical_tags)
"""
from datetime import datetime

from sqlalchemy import select

from services import db
from services.db import GlossaryTerm

CATEGORIES = ['조직', '과제·사업', '회의', '약어', '기타']
MAX_NOTE_TERMS = 30  # AI 에게 한 번에 알려 줄 용어 수


def _split(v) -> list[str]:
    items = v if isinstance(v, list) else str(v or '').replace('\n', ',').split(',')
    out, seen = [], set()
    for x in items:
        x = str(x).strip().lstrip('#').strip()[:100]
        if x and x.lower() not in seen:
            seen.add(x.lower())
            out.append(x)
    return out


def _dict(g: GlossaryTerm) -> dict:
    return {'id': g.id, 'term': g.term, 'synonyms': _split(g.synonyms), 'category': g.category or '',
            'description': g.description or ''}


def list_terms(query: str = '') -> list[dict]:
    q = (query or '').strip().lower()
    with db.SessionLocal() as s:
        rows = [_dict(g) for g in s.scalars(select(GlossaryTerm).order_by(GlossaryTerm.term))]
    if q:
        rows = [r for r in rows if q in ' '.join([r['term'], *r['synonyms'], r['description'], r['category']]).lower()]
    return rows


def _check(rows: list[dict]) -> list[dict]:
    """표 → 저장할 항목. 대표 용어가 없거나, 같은 말이 두 용어에 걸치면 ValueError."""
    out, owner = [], {}
    for i, r in enumerate(rows, 1):
        term = str(r.get('term') or '').strip()[:100]
        syns = [x for x in _split(r.get('synonyms')) if x.lower() != term.lower()]
        desc = str(r.get('description') or '').strip()[:2000]
        cat = str(r.get('category') or '').strip()
        if not term:
            if syns or desc:
                raise ValueError(f'{i}번째 줄: 대표 용어를 적으세요.')
            continue
        for w in [term, *syns]:
            k = w.lower()
            if k in owner and owner[k] != term:
                raise ValueError(f"'{w}' 가 '{owner[k]}' 와 '{term}' 두 용어에 있습니다 — 한 곳에만 두세요.")
            owner[k] = term
        out.append({'term': term, 'synonyms': syns, 'description': desc,
                    'category': cat if cat in CATEGORIES else ('기타' if cat else '')})
    return out


def save_all(rows: list[dict]) -> dict:
    """화면 표 전체를 저장 — 표에 없는 용어는 지운다. {'saved', 'deleted'}."""
    items = _check(rows)
    with db.SessionLocal() as s:
        have = {g.term: g for g in s.scalars(select(GlossaryTerm))}
        keep = {it['term'] for it in items}
        deleted = [g for t, g in have.items() if t not in keep]
        for g in deleted:
            s.delete(g)
        s.flush()
        for it in items:
            g = have.get(it['term']) or GlossaryTerm(term=it['term'])
            g.synonyms, g.description, g.category = ', '.join(it['synonyms']), it['description'], it['category']
            g.updated_at = datetime.now().replace(microsecond=0)
            s.add(g)
        s.commit()
    return {'saved': len(items), 'deleted': len(deleted)}


def merge_rows(rows: list[dict]) -> dict:
    """엑셀에서 읽은 줄을 더한다 — 같은 대표 용어는 같은 말을 합치고 설명·분류는 새 값이 있으면 바꾼다."""
    current = {r['term']: r for r in list_terms()}
    added = updated = 0
    for r in _check(rows):
        old = current.get(r['term'])
        if old is None:
            current[r['term']] = r
            added += 1
        else:
            old['synonyms'] = _split(old['synonyms'] + r['synonyms'])
            old['description'] = r['description'] or old['description']
            old['category'] = r['category'] or old['category']
            updated += 1
    save_all(list(current.values()))
    return {'added': added, 'updated': updated}


HEADERS = {'term': ('대표 용어', '용어', '정식 명칭', '이름'), 'synonyms': ('같은 말', '동의어', '약어', '별칭', '다른 이름'),
           'description': ('설명', '뜻', '메모'), 'category': ('분류', '종류', '구분')}


def rows_from_table(headers: list[str], rows: list[list[str]]) -> list[dict]:
    """엑셀·CSV 표(첫 행 제목) → 용어 줄. 제목이 '대표 용어'·'같은 말'·'설명'·'분류' 와 비슷하면 그 칸."""
    col = {}
    for i, h in enumerate(headers):
        h = (h or '').replace(' ', '')
        for field, names in HEADERS.items():
            if field not in col and any(n.replace(' ', '') in h for n in names):
                col[field] = i
                break
    if 'term' not in col:
        col['term'] = 0
    return [{f: (r[i] if i < len(r) else '') for f, i in col.items()} for r in rows]


# ── 쓰는 곳 ──────────────────────────────────────────────────────────

def _groups() -> list[tuple[dict, list[str]]]:
    return [(r, [r['term'], *r['synonyms']]) for r in list_terms()]


def expand(word: str) -> list[str]:
    """검색어 하나 → 같은 말 묶음 (용어집에 없으면 그대로 [word])."""
    w = (word or '').strip().lower()
    for _, words in _groups():
        if w in (x.lower() for x in words):
            return [word] + [x for x in words if x.lower() != w]
    return [word]


def canonical_tags(tags: list[str]) -> list[str]:
    """태그·키워드 중 같은 말을 대표 용어로 바꾸고 겹친 것은 하나로."""
    to = {x.lower(): r['term'] for r, words in _groups() for x in words}
    out, seen = [], set()
    for t in tags:
        t = to.get(str(t).lower(), t)
        if t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def for_text(text: str, limit: int = MAX_NOTE_TERMS) -> list[dict]:
    """원문에 대표 용어나 같은 말이 나오는 용어."""
    low = (text or '').lower()
    if not low:
        return []
    return [r for r, words in _groups() if any(len(x) >= 2 and x.lower() in low for x in words)][:limit]


def prompt_note(text: str) -> str:
    """AI 에게 붙일 안내 — 원문에 나온 사내 용어의 같은 말과 뜻. 없으면 ''."""
    try:
        found = for_text(text)
    except Exception:  # noqa: BLE001 — 용어집 때문에 AI 호출이 실패하면 안 된다
        return ''
    if not found:
        return ''
    lines = [f"- {r['term']}" + (f" (= {', '.join(r['synonyms'])})" if r['synonyms'] else '')
             + (f": {r['description']}" if r['description'] else '') for r in found]
    return ('\n\n[사내 용어집 — 원문에 나온 용어]\n' + '\n'.join(lines)
            + '\n같은 말은 같은 대상이다. 태그·키워드를 달 때는 대표 용어(맨 앞)를 쓴다.')
