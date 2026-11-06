"""AI 요약 형식 — 기록 유형('회의'·'피드백'…)과 문서 종류('회의록'·'보고서'…)마다 AI 가 채울 칸을 화면에서 정한다.

형식이 있으면 AI 정리(llm.analyze_record)와 문서 요약(doc_summary)이 그 칸을 함께 채우고(sections),
기록 카드와 문서 요약에 칸별로 보인다. 형식이 없으면 지금처럼 요약 글만. 칸: [{'name', 'hint'(무엇을 쓰는지)}].
"""
import json

from sqlalchemy import select

from services import db
from services.db import SummaryFormat

TARGETS = {'history': '기록 (유형별)', 'document': '문서 (종류별)'}
MAX_SECTIONS = 8

from services.summary_format_defaults import DEFAULTS  # noqa: E402,F401


def kinds(target: str) -> list[str]:
    """그 대상에서 고를 수 있는 종류 — 기록 유형 / 문서 종류."""
    if target == 'history':
        from services import store
        return list(store.ENTRY_TYPES)
    from services import doc_summary
    return list(doc_summary.DOC_TYPES)


def _sections(raw: str) -> list[dict]:
    try:
        data = json.loads(raw or '[]')
    except ValueError:
        return []
    return [{'name': str(x.get('name') or '').strip(), 'hint': str(x.get('hint') or '').strip()}
            for x in data if isinstance(x, dict) and str(x.get('name') or '').strip()]


def list_formats(target: str | None = None) -> list[dict]:
    with db.SessionLocal() as s:
        q = select(SummaryFormat).order_by(SummaryFormat.target, SummaryFormat.id)
        if target:
            q = q.where(SummaryFormat.target == target)
        return [{'id': f.id, 'target': f.target, 'kind': f.kind, 'sections': _sections(f.sections)} for f in s.scalars(q)]


def get(target: str, kind: str) -> list[dict]:
    """그 종류의 칸 — 형식이 없으면 []."""
    if not kind:
        return []
    with db.SessionLocal() as s:
        f = s.scalar(select(SummaryFormat).where(SummaryFormat.target == target, SummaryFormat.kind == kind))
        return _sections(f.sections) if f else []


def parse_text(text: str) -> list[dict]:
    """화면의 글 → 칸. 한 줄에 한 칸: '칸 이름: 무엇을 쓰는지' (설명은 없어도 된다)."""
    out, seen = [], set()
    for line in (text or '').splitlines():
        line = line.strip().lstrip('-•·*').strip()
        if not line:
            continue
        name, _, hint = line.partition(':')
        name = name.strip()[:40]
        if name and name not in seen:
            seen.add(name)
            out.append({'name': name, 'hint': hint.strip()[:200]})
    if len(out) > MAX_SECTIONS:
        raise ValueError(f'칸은 {MAX_SECTIONS}개까지입니다.')
    return out


def to_text(sections: list[dict]) -> str:
    return '\n'.join(f"{x['name']}: {x['hint']}" if x['hint'] else x['name'] for x in sections)


def save(target: str, kind: str, text: str) -> list[dict]:
    if target not in TARGETS or not kind:
        raise ValueError('대상과 종류를 고르세요.')
    sections = parse_text(text)
    if not sections:
        raise ValueError('칸을 한 줄에 하나씩 적으세요 — 예: 결정 사항: 정해진 것')
    with db.SessionLocal() as s:
        f = s.scalar(select(SummaryFormat).where(SummaryFormat.target == target, SummaryFormat.kind == kind)) \
            or SummaryFormat(target=target, kind=kind)
        f.sections = json.dumps(sections, ensure_ascii=False)
        s.add(f)
        s.commit()
    return sections


def delete(target: str, kind: str) -> None:
    with db.SessionLocal() as s:
        f = s.scalar(select(SummaryFormat).where(SummaryFormat.target == target, SummaryFormat.kind == kind))
        if f is not None:
            s.delete(f)
            s.commit()


# ── AI 에게 / AI 에게서 ───────────────────────────────────────────────

def _lines(sections: list[dict]) -> str:
    return '\n'.join(f"  - {x['name']}" + (f": {x['hint']}" if x['hint'] else '') for x in sections)


def history_prompt(entry_type: str) -> str:
    """기록 AI 정리에 붙일 안내 — 그 유형의 형식이 없으면 ''."""
    sections = get('history', entry_type)
    if not sections:
        return ''
    keys = ', '.join(f'"{x["name"]}": ""' for x in sections)
    return (f"\n\n이 기록 유형('{entry_type}')은 정해진 형식이 있다. 위 JSON 에 \"sections\" 를 더해 아래 칸을 채운다 — "
            f"원문에 없으면 빈 문자열, 한 칸에 여러 항목이면 줄바꿈으로:\n{_lines(sections)}\n"
            f"\"sections\": {{{keys}}}")


def document_prompt() -> str:
    """문서 요약에 붙일 안내 — 문서 종류별 형식 전부 (종류는 AI 가 그 자리에서 고르므로). 형식이 없으면 ''."""
    formats = list_formats('document')
    if not formats:
        return ''
    blocks = '\n'.join(f"[{f['kind']}]\n{_lines(f['sections'])}" for f in formats)
    return ("\n\n고른 doc_type 에 아래 형식이 있으면 JSON 에 \"sections\" 객체를 더해 그 칸을 채운다 (칸 이름 그대로, "
            "문서에 없으면 빈 문자열, 여러 항목이면 줄바꿈). 형식이 없는 종류면 \"sections\": {}.\n" + blocks)


def clean(raw, sections: list[dict]) -> list[dict]:
    """AI 가 준 sections → 형식의 칸 순서대로 [{name, text}] (빈 칸은 뺀다). 형식이 없으면 []."""
    if not sections:
        return []
    if isinstance(raw, list):  # [{"name":..,"text":..}] 로 줄 때도
        raw = {str(x.get('name') or ''): x.get('text') for x in raw if isinstance(x, dict)}
    if not isinstance(raw, dict):
        return []
    out = []
    for x in sections:
        v = raw.get(x['name'])
        if isinstance(v, list):
            v = '\n'.join(str(i).strip() for i in v if str(i).strip())
        v = str(v or '').strip()
        if v:
            out.append({'name': x['name'], 'text': v[:1500]})
    return out


def loads(raw: str) -> list[dict]:
    """DB 칸(JSON) → [{name, text}]."""
    try:
        data = json.loads(raw or '[]')
    except ValueError:
        return []
    return [x for x in data if isinstance(x, dict) and x.get('name') and x.get('text')]
