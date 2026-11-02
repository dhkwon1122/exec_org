"""업무 요약 — 업무와 그 아래 하위 업무, 거기 걸친 이력, 참고자료(문서·링크·메모·컨플루언스)를 AI 가 한눈에 보게 정리한다.

{'overview': 지금 어디까지 왔는지 2~4문장, 'progress': [{'date', 'what'}] 주요 경과, 'next': [남은 일],
 'references': 참고자료 요약 1~3문장}

쉬는 시간(doc_worker)에 바뀐 업무부터 하나씩 미리 만든다 — 업무·하위 업무·이력·자료 중 무엇이든 요약한 뒤로
바뀌었으면(stale) 요약에 넣을 글을 다시 모아, 글이 정말 달라졌을 때만 AI 를 부른다(source_hash).
"""
import hashlib
import json
from datetime import datetime

from sqlalchemy import func, select

from services import db, llm, store
from services.db import HistoryEntry, HistoryTask, Reference, Task, TaskSummary

MAX_HISTORY = 60   # 요약에 넣을 이력 (최근 것부터)
MAX_CHILDREN = 40

SYSTEM = """너는 임원조직 담당자의 업무를 정리하는 비서다. 업무 하나와 그 아래 하위 업무, 그동안 남긴 기록(이력),
참고자료를 읽고, 이 업무를 처음 보는 사람도 한눈에 알 수 있게 요약한다.
- 원문에 없는 사실을 지어내지 않는다. 날짜는 원문에 있는 것만 쓴다.
- 반드시 JSON 객체 하나만 출력한다. 다른 설명은 쓰지 않는다."""

USER = """오늘: {today}
{body}

다음 형식의 JSON 으로:
{{
  "overview": "지금 어디까지 왔는지 — 목적, 현재 상태, 쟁점을 2~4문장으로",
  "progress": [{{"date": "YYYY-MM-DD", "what": "주요 경과 한 줄"}}],
  "next": ["남은 일·다음 단계 (미완료 하위 업무와 기록에서) — 짧게"],
  "references": "참고자료가 무엇을 담고 있는지 1~3문장 (자료가 없으면 빈 문자열)"
}}
progress 는 중요한 것만 오래된 순으로 최대 8개, next 는 최대 5개."""


# ── 무엇을 넣나 ──────────────────────────────────────────────────────

def compose(task_id: int) -> str:
    """요약에 넣을 글 — 업무, 하위 업무, 이력(최근 것부터), 참고자료."""
    from services import documents
    t = store.get_task(task_id)
    if t is None:
        return ''
    scope = store.task_scope(task_id)
    lines = [f"[업무] #{t['id']} {t['title']} · 상태 {t['status']}"
             + (f" · 시작 {t['start_date']}" if t['start_date'] else '') + (f" · 마감 {t['due_date']}" if t['due_date'] else ''),
             t['description'][:1500] if t['description'] else '']
    with db.SessionLocal() as s:
        kids = s.scalars(select(Task).where(Task.id.in_(scope[1:] or [0])).order_by(Task.id).limit(MAX_CHILDREN)).all()
        if kids:
            lines.append('\n[하위 업무]')
            lines += [f"- #{k.id} {k.title} · {k.status}" + (f" · 마감 {k.due_date}" if k.due_date else '') for k in kids]
        cond = HistoryEntry.task_id.in_(scope) | HistoryEntry.id.in_(
            select(HistoryTask.history_id).where(HistoryTask.task_id.in_(scope)))
        hist = s.scalars(select(HistoryEntry).where(cond, HistoryEntry.entry_type.notin_(store.AUTO_ENTRY_TYPES))
                         .order_by(HistoryEntry.occurred_at.desc(), HistoryEntry.id.desc()).limit(MAX_HISTORY)).all()
    if hist:
        lines.append('\n[이력 — 최근 것부터]')
        for h in hist:
            what = h.title or ''
            body = (h.summary or h.content or '').replace('\n', ' ')
            lines.append(f"- {h.occurred_at:%Y-%m-%d} [{h.entry_type}] {what} — {body[:300]}")
    docs = documents.for_task(task_id)
    refs = [r for r in store.references_for_task(task_id) if r['kind'] != 'file']
    if docs or refs:
        lines.append('\n[참고자료]')
        for d in docs[:15]:
            ins = d.get('insight') or {}
            lines.append(f"- 문서 {d['base']} (판 {d['n_versions']}개, 최근 {d['updated']})"
                         + (f": {ins.get('summary', '')[:200]}" if ins.get('summary') else ''))
        for r in refs[:15]:
            extra = r.get('content_summary') or r.get('note') or ''
            lines.append(f"- {'링크' if r['kind'] == 'link' else '메모'} {r['title']}" + (f": {extra[:200]}" if extra else ''))
    return '\n'.join(x for x in lines if x is not None)[:llm.MAX_INPUT_CHARS]


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


# ── 만들기 ───────────────────────────────────────────────────────────

def _clean(data: dict) -> dict:
    prog = []
    for p in data.get('progress') or []:
        if isinstance(p, dict) and str(p.get('what') or '').strip():
            prog.append({'date': str(p.get('date') or '')[:10], 'what': str(p['what']).strip()[:200]})
    return {'overview': str(data.get('overview') or '').strip()[:1500], 'progress': prog[:8],
            'next': [str(x).strip()[:200] for x in data.get('next') or [] if str(x).strip()][:5],
            'references': str(data.get('references') or '').strip()[:800]}


def generate(task_id: int, force: bool = False) -> dict | None:
    """요약을 만든다 — 넣을 글이 지난번과 같으면(force 가 아니면) AI 를 부르지 않는다. get() 형식으로 돌려준다.
    LLM 오류는 그대로 올린다 (요약 기록에 오류를 남긴 뒤)."""
    text = compose(task_id)
    if not text:
        return None
    h = _hash(text)
    with db.SessionLocal() as s:
        row = s.scalar(select(TaskSummary).where(TaskSummary.task_id == task_id))
        if row is not None and row.source_hash == h and row.data and not force:
            row.generated_at = datetime.now().replace(microsecond=0)  # 글이 같다 — 시각만 새로 (다시 오래되지 않게)
            s.commit()
            return get(task_id)
    try:
        data = _clean(llm._extract_json(llm.chat(SYSTEM, USER.format(today=datetime.now().date(), body=text))))
    except llm.LLMError as e:
        with db.SessionLocal() as s:
            row = s.scalar(select(TaskSummary).where(TaskSummary.task_id == task_id)) or TaskSummary(task_id=task_id)
            row.error = str(e)[:500]
            s.add(row)
            s.commit()
        raise
    with db.SessionLocal() as s:
        row = s.scalar(select(TaskSummary).where(TaskSummary.task_id == task_id)) or TaskSummary(task_id=task_id)
        row.data, row.source_hash, row.error = json.dumps(data, ensure_ascii=False), h, ''
        row.generated_at = datetime.now().replace(microsecond=0)
        s.add(row)
        s.commit()
    return get(task_id)


def get(task_id: int) -> dict | None:
    """{'overview', 'progress', 'next', 'references', 'at', 'stale', 'error'} — 아직 없으면 None (오류만 있으면 error 만)."""
    with db.SessionLocal() as s:
        row = s.scalar(select(TaskSummary).where(TaskSummary.task_id == task_id))
        if row is None:
            return None
        try:
            data = json.loads(row.data) if row.data else {}
        except ValueError:
            data = {}
        at = row.generated_at
    stale = at is None or task_id in _stale_ids({task_id: at})
    return {**_clean(data), 'has': bool(data), 'at': at.strftime('%Y-%m-%d %H:%M') if at else '',
            'stale': stale, 'error': row.error or ''}


# ── 오래된 것 찾기 (쉬는 시간) ─────────────────────────────────────────

def last_activity() -> dict[int, datetime]:
    """업무별 마지막 변화 — 업무 자신·하위 업무의 수정, 이력, 참고자료(추가·내용 읽기·요약) 중 가장 늦은 것.
    하위 업무의 변화는 위로 올라간다."""
    own: dict[int, datetime] = {}

    def bump(tid, when):
        if tid and when and (tid not in own or when > own[tid]):
            own[tid] = when
    with db.SessionLocal() as s:
        parents = dict(s.execute(select(Task.id, Task.parent_id)).all())
        for tid, when in s.execute(select(Task.id, Task.updated_at)):
            bump(tid, when)
        changed = func.max(func.coalesce(HistoryEntry.updated_at, HistoryEntry.created_at))
        for tid, when in s.execute(select(HistoryEntry.task_id, changed).group_by(HistoryEntry.task_id)):
            bump(tid, when)
        for tid, when in s.execute(select(HistoryTask.task_id, changed)
                                   .join(HistoryEntry, HistoryEntry.id == HistoryTask.history_id)
                                   .group_by(HistoryTask.task_id)):
            bump(tid, when)
        for tid, c, r, a in s.execute(select(Reference.task_id, func.max(Reference.created_at),
                                             func.max(Reference.content_at), func.max(Reference.analyzed_at))
                                      .group_by(Reference.task_id)):
            for w in (c, r, a):
                bump(tid, w)
    out = dict(own)
    for tid, when in own.items():  # 위로 올린다 (고리가 있어도 멈춘다)
        up, seen = parents.get(tid), {tid}
        while up and up not in seen:
            seen.add(up)
            if up not in out or when > out[up]:
                out[up] = when
            up = parents.get(up)
    return out


def _stale_ids(made: dict[int, datetime]) -> set[int]:
    act = last_activity()
    return {tid for tid, at in made.items() if at is None or (act.get(tid) and act[tid] > at)}


def pending(limit: int = 1) -> list[int]:
    """요약이 없거나 오래된 업무 — 진행 중인 것, 최근 바뀐 것부터. 최상위·하위 업무 모두."""
    act = last_activity()
    with db.SessionLocal() as s:
        made = dict(s.execute(select(TaskSummary.task_id, TaskSummary.generated_at)).all())
        status = dict(s.execute(select(Task.id, Task.status)).all())
    todo = [tid for tid in status if tid not in made or made[tid] is None or (act.get(tid) and act[tid] > made[tid])]
    todo.sort(key=lambda t: (status[t] == '완료', -(act.get(t) or datetime.min).timestamp()))
    return todo[:limit]
