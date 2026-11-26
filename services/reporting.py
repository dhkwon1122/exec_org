"""보고가 목표인 업무 — 어디까지 보고했는지 단계로 챙긴다.

단계(아래로 갈수록 위로 올라가는 보고): 그룹장 → 팀장 → 원장 → 부문(파트장) → 부문(그룹장) → 부문(팀장) → 대표이사 → 실
- 업무마다 목표 단계(report_target)를 정하면 '보고 업무'가 된다. 비우면 보고 업무가 아니다.
- 단계마다 보고한 날을 남긴다(report_log {단계: 날짜}). 모든 단계를 다 거치지 않아도 된다 — 가장 높이 올라간
  단계가 '지금 단계', 그 위 목표까지 남은 첫 단계가 '다음 단계'.
- 보고를 남기면 업무 이력에 '보고' 기록도 함께 남는다(날짜·메모).
"""
import json
from datetime import date

from services import db, store
from services.db import Task

STAGES = ['그룹장', '팀장', '원장', '부문(파트장)', '부문(그룹장)', '부문(팀장)', '대표이사', '실']


def _rank(stage: str) -> int:
    return STAGES.index(stage) if stage in STAGES else -1


def progress(task: dict) -> dict | None:
    """보고 업무면 {'target', 'reached': {단계: 날짜}, 'current', 'current_date', 'next', 'done', 'stages': [
    {'stage', 'date', 'state': reached/skipped/next/todo/beyond}]} — 보고 업무가 아니면 None."""
    target = task.get('report_target') or ''
    if target not in STAGES:
        return None
    reached = {k: v for k, v in (task.get('report_log') or {}).items() if k in STAGES}
    top = max((_rank(k) for k in reached), default=-1)
    current = STAGES[top] if top >= 0 else ''
    t = _rank(target)
    nxt = next((s for s in STAGES[top + 1:t + 1]), '') if top < t else ''
    stages = []
    for i, s in enumerate(STAGES):
        if s in reached:
            state = 'reached'
        elif i < top:
            state = 'skipped'  # 위 단계로 이미 올라감
        elif s == nxt:
            state = 'next'
        elif i <= t:
            state = 'todo'
        else:
            state = 'beyond'
        stages.append({'stage': s, 'date': reached.get(s, ''), 'state': state})
    return {'target': target, 'reached': reached, 'current': current, 'current_date': reached.get(current, ''),
            'next': nxt, 'done': top >= t, 'stages': stages}


def label(task: dict) -> str:
    """목록에 붙일 짧은 글 — '팀장까지 보고 · 다음 원장 (목표 대표이사)'."""
    p = progress(task)
    if p is None:
        return ''
    if p['done']:
        return f"{p['target']} 보고 완료"
    head = f"{p['current']}까지 보고" if p['current'] else '보고 전'
    return f"{head} · 다음 {p['next']} (목표 {p['target']})"


def _save(task_id: int, **fields) -> dict:
    with db.SessionLocal() as s:
        t = s.get(Task, task_id)
        if t is None:
            raise ValueError('업무를 찾을 수 없습니다.')
        for k, v in fields.items():
            setattr(t, k, v)
        s.commit()
    return store.get_task(task_id)


def set_target(task_id: int, target: str, by: str = '') -> dict:
    """목표 단계 정하기 — '' 이면 보고 업무에서 뺀다(남긴 보고 날짜는 그대로 둔다)."""
    target = (target or '').strip()
    if target and target not in STAGES:
        raise ValueError('알 수 없는 보고 단계입니다.')
    t = store.get_task(task_id)
    if t is None:
        raise ValueError('업무를 찾을 수 없습니다.')
    if t['report_target'] == target:
        return t
    store.add_history(task_id, f"보고 목표: {t['report_target'] or '없음'} → {target or '없음'}", '상태변경', author=by)
    return _save(task_id, report_target=target)


def mark(task_id: int, stage: str, day: str = '', by: str = '', note: str = '') -> dict:
    """그 단계에 보고했다고 남긴다 (날짜 기본 오늘) — 업무 이력에 '보고' 기록도."""
    if stage not in STAGES:
        raise ValueError('보고 단계를 고르세요.')
    day = (day or date.today().isoformat())[:10]
    try:
        date.fromisoformat(day)
    except ValueError:
        raise ValueError('보고한 날을 확인하세요 (YYYY-MM-DD).') from None
    t = store.get_task(task_id)
    if t is None:
        raise ValueError('업무를 찾을 수 없습니다.')
    log = dict(t['report_log'])
    log[stage] = day
    fields = {'report_log': json.dumps(log, ensure_ascii=False)}
    if not t['report_target']:  # 목표 없이 보고부터 남기면 그 단계를 목표로
        fields['report_target'] = stage
    content = f'{stage} 보고' + (f'\n\n{note.strip()}' if (note or '').strip() else '')
    store.add_history(task_id, content, '보고', author=by, occurred_at=f'{day} 09:00', title=f'{stage} 보고')
    return _save(task_id, **fields)


def unmark(task_id: int, stage: str) -> dict:
    """잘못 남긴 단계 보고를 지운다 (이력의 '보고' 기록은 그대로 — 필요하면 이력에서 지운다)."""
    t = store.get_task(task_id)
    if t is None:
        raise ValueError('업무를 찾을 수 없습니다.')
    log = {k: v for k, v in t['report_log'].items() if k != stage}
    return _save(task_id, report_log=json.dumps(log, ensure_ascii=False))


def overview(include_done: bool = False) -> list[dict]:
    """보고 업무 — 끝나지 않은 업무 중 목표 단계가 있는 것. [{'task', 'progress'}] 다음 단계 순(낮은 단계 먼저),
    같은 단계 안에서는 마지막 보고가 오래된 것 먼저."""
    out = []
    for t in store.list_tasks(status=None if include_done else store.ACTIVE_STATUSES):
        p = progress(t)
        if p and (include_done or not p['done']):
            out.append({'task': t, 'progress': p})
    out.sort(key=lambda r: (r['progress']['done'], _rank(r['progress']['next']) if r['progress']['next'] else 99,
                            r['progress']['current_date'] or '0000'))
    return out
