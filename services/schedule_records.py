"""원장님·피플팀장 일정에서 남기는 보고 기록 — 주요 보고 일정의 보고 내용·받은 피드백을 '보고' 이력으로 남긴다.

- 기록은 일반 이력(history)이라 업무 화면·이력 검색·AI 요약에 그대로 쓰인다. schedule_id 로 그 일정에 묶인다.
- 관련 업무를 고르면 그 업무의 이력이 된다. 일정의 참석자·제목·내용에 나온 임원에도 연결한다
  (업무를 고르지 않았으면 임원이 있어야 남길 수 있다).
- 원장님 일정이고 관련 업무가 보고 업무(services/reporting.py)면 '원장' 단계를 그 일정 날짜로 함께 표시할 수 있다.
"""
from sqlalchemy import func, select, update

from services import db, enrich, reporting, schedules, store
from services.db import HistoryEntry

STAGE_FOR_OWNER = {'director': '원장'}  # 이 사람 일정의 보고 = 보고 단계


def add(schedule_id: int, content: str = '', feedback: str = '', task_id: int | None = None, by: str = '',
        mark_stage: bool = False) -> dict:
    """보고 기록 남기기 → 이력 dict. 보고 내용·피드백 둘 다 비면 ValueError."""
    e = schedules.get(schedule_id)
    if e is None:
        raise ValueError('일정을 찾을 수 없습니다.')
    content, feedback = (content or '').strip(), (feedback or '').strip()
    if not content and not feedback:
        raise ValueError('보고 내용이나 피드백을 적으세요.')
    task = store.get_task(int(task_id)) if task_id else None
    if task_id and task is None:
        raise ValueError('관련 업무를 찾을 수 없습니다.')
    who = schedules.OWNERS[e['owner']]
    head = f"[{who} 일정] {e['date']} {e['start_time'] or ''} {e['title']}".replace('  ', ' ')
    body = '\n\n'.join(x for x in [head, f'보고 내용:\n{content}' if content else '',
                                   f'피드백·지시:\n{feedback}' if feedback else ''] if x)
    # 기록은 업무나 임원에 걸린다 — 업무가 없으면 일정의 참석자·제목·내용에 나온 임원에
    people = store.detect_people(f"{e['title']}\n{e['attendees']}\n{content}\n{feedback}")
    if not task and not people:
        raise ValueError('관련 업무를 고르세요 (보고 기록은 업무나 임원에 연결됩니다).')
    h = store.add_history(task['id'] if task else None, body, '보고', author=by, person_ids=people,
                          occurred_at=f"{e['date']} {e['start_time'] or '09:00'}", title=f"{who} 보고: {e['title']}"[:200])
    with db.transaction() as s:
        s.execute(update(HistoryEntry).where(HistoryEntry.id == h['id']).values(schedule_id=schedule_id))
    stage = STAGE_FOR_OWNER.get(e['owner'])
    if mark_stage and task and stage and reporting.progress(task) is not None:
        reporting.mark(task['id'], stage, e['date'], by, record=False)
    enrich.submit(h['id'])  # AI 요약·키워드 (설정돼 있으면)
    return store.get_history(h['id'])


def for_schedule(schedule_id: int) -> list[dict]:
    """그 일정의 보고 기록 (오래된 것부터)."""
    with db.SessionLocal() as s:
        ids = list(s.scalars(select(HistoryEntry.id).where(HistoryEntry.schedule_id == schedule_id,
                                                           HistoryEntry.deleted_at.is_(None))
                             .order_by(HistoryEntry.id)))
    return [h for h in (store.get_history(i) for i in ids) if h]


def counts(schedule_ids: list[int]) -> dict[int, int]:
    """일정별 보고 기록 수 (달력 표시용)."""
    if not schedule_ids:
        return {}
    with db.SessionLocal() as s:
        return dict(s.execute(select(HistoryEntry.schedule_id, func.count()).where(
            HistoryEntry.schedule_id.in_(schedule_ids), HistoryEntry.deleted_at.is_(None))
            .group_by(HistoryEntry.schedule_id)).all())
