"""미등록 AI 후속조치, 담당자 미지정·장기 정체 업무."""
import hashlib
import json
from datetime import date, datetime, timedelta

from sqlalchemy import func, select, update

from services import db, store
from services.db import FollowupConversion, FollowupDecision, HistoryEntry, HistoryTask, Task


def suggestion_key(todo):
    raw = json.dumps({k: str(todo.get(k) or '').strip() for k in ('title', 'due', 'assignee')},
                     ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def _candidate(s, hid, key):
    h = s.get(HistoryEntry, hid)
    if not h:
        raise ValueError('기록이 없거나 휴지통에 있습니다.')
    for index, todo in enumerate(store._load_json_list(h.ai_todos)):
        if isinstance(todo, dict) and suggestion_key(todo) == key:
            return h, index, todo
    raise ValueError('AI 제안이 변경됐습니다. 목록을 새로 확인하세요.')


def suggestions(user_id, view='pending', today=None):
    today = today or date.today()
    with db.SessionLocal() as s:
        decisions = {(d.history_id, d.suggestion_key): d for d in s.scalars(
            select(FollowupDecision).where(FollowupDecision.user_id == user_id))}
        converted = {(r.history_id, r.suggestion_key) for r in s.scalars(
            select(FollowupConversion).where(FollowupConversion.task_id.is_not(None)))}
        known = {}
        for hid, title in s.execute(select(Task.source_history_id, Task.title).where(Task.source_history_id.is_not(None))):
            known.setdefault(hid, set()).add(title)
        rows = s.execute(select(HistoryEntry.id, HistoryEntry.title, HistoryEntry.task_id, HistoryEntry.ai_todos,
                                HistoryEntry.occurred_at).where(HistoryEntry.ai_todos.notin_(['', '[]']))
                         .order_by(HistoryEntry.occurred_at.desc(), HistoryEntry.id.desc()).execution_options(yield_per=100))
        out = []
        for hid, title, task_id, raw, occurred in rows:
            seen = set()
            for todo in store._load_json_list(raw):
                if not isinstance(todo, dict) or not str(todo.get('title') or '').strip():
                    continue
                key = suggestion_key(todo)
                if key in seen:
                    continue
                seen.add(key)
                if todo.get('task_id') or (hid, key) in converted or todo['title'] in known.get(hid, set()):
                    continue
                decision = decisions.get((hid, key))
                status = decision.status if decision else 'pending'
                until = decision.until if decision else ''
                if status == 'deferred' and until <= today.isoformat():
                    status = 'pending'
                if status != view:
                    continue
                out.append({'kind': 'suggestion', 'id': hid, 'key': key, 'title': todo['title'],
                    'due': todo.get('due', ''), 'assignee': todo.get('assignee', ''), 'task_id': task_id,
                    'source_title': title or f'기록 #{hid}', 'at': store._dt(occurred), 'until': until, 'status': status})
        return out


def attention_tasks(stale_days=14, today=None):
    today = today or date.today()
    cutoff = datetime.combine(today - timedelta(days=stale_days), datetime.min.time())
    with db.SessionLocal() as s:
        last = dict(s.execute(select(HistoryEntry.task_id, func.max(func.coalesce(HistoryEntry.updated_at,
            HistoryEntry.created_at))).where(HistoryEntry.entry_type.notin_(store.AUTO_ENTRY_TYPES))
            .group_by(HistoryEntry.task_id)).all())
        for tid, at in s.execute(select(HistoryTask.task_id, func.max(func.coalesce(HistoryEntry.updated_at,
            HistoryEntry.created_at))).join(HistoryEntry, HistoryEntry.id == HistoryTask.history_id)
            .where(HistoryEntry.entry_type.notin_(store.AUTO_ENTRY_TYPES)).group_by(HistoryTask.task_id)):
            if at and (not last.get(tid) or at > last[tid]):
                last[tid] = at
        out = []
        for t in s.scalars(select(Task).where(Task.status != '완료').order_by(Task.id.desc())):
            activity = max(x for x in (t.created_at, t.updated_at, last.get(t.id)) if x)
            reasons = []
            if not t.owner and not t.assignee_rows:
                reasons.append('담당자 미지정')
            if activity < cutoff:
                reasons.append(f'{stale_days}일 이상 변경 없음')
            if t.due_date and t.due_date < today.isoformat():
                reasons.append('마감 지남')
            if reasons:
                out.append({'id': t.id, 'title': t.title, 'reasons': reasons, 'due': t.due_date or '',
                            'at': store._dt(activity), 'assignee': t.owner or '미지정'})
        return out


def decide(user_id, hid, key, status, until=''):
    if not user_id or status not in ('pending', 'deferred', 'excluded'):
        raise ValueError('처리 상태를 확인하세요.')
    if status == 'deferred':
        try:
            day = date.fromisoformat(until)
        except (ValueError, TypeError):
            raise ValueError('보류 기한을 입력하세요.') from None
        if day <= date.today():
            raise ValueError('보류 기한은 내일 이후로 선택하세요.')
    with db.transaction() as s:
        locked = s.execute(update(HistoryEntry).where(HistoryEntry.id == hid,
            HistoryEntry.deleted_at.is_(None)).values(id=HistoryEntry.id))
        if not locked.rowcount:
            raise ValueError('기록이 없거나 휴지통에 있습니다.')
        _candidate(s, hid, key)
        row = s.get(FollowupDecision, (user_id, hid, key))
        if status == 'pending':
            if row:
                s.delete(row)
        elif row:
            row.status, row.until = status, until if status == 'deferred' else ''
        else:
            s.add(FollowupDecision(user_id=user_id, history_id=hid, suggestion_key=key,
                status=status, until=until if status == 'deferred' else ''))


def convert(hid, key, title, due='', assignee='', parent_id=None, by=''):
    """기록 잠금으로 모든 사용자·화면의 동시 등록을 직렬화한다."""
    with db.transaction() as s:
        locked = s.execute(update(HistoryEntry).where(HistoryEntry.id == hid,
            HistoryEntry.deleted_at.is_(None)).values(id=HistoryEntry.id))
        if not locked.rowcount:
            raise ValueError('기록이 없거나 휴지통에 있습니다.')
        h, index, todo = _candidate(s, hid, key)
        prior = s.get(FollowupConversion, (hid, key))
        tid = prior.task_id if prior else todo.get('task_id')
        if not tid:
            tid = s.scalar(select(Task.id).where(Task.source_history_id == hid, Task.title == todo['title']))
        if tid:
            existing = s.get(Task, tid)
            if not existing:
                raise ValueError('이미 등록된 업무가 휴지통에 있습니다. 휴지통에서 복원하세요.')
            return {'id': tid, 'created': False}
        task = store.create_todo(title, task_id=parent_id, due_date=due, assignee_name=assignee,
                                  created_by=by, source_history_id=hid, session=s)
        if prior:
            prior.task_id, prior.created_by = task['id'], by
        else:
            s.add(FollowupConversion(history_id=hid, suggestion_key=key, task_id=task['id'], created_by=by))
        todos = store._load_json_list(h.ai_todos)
        todos[index]['task_id'] = task['id']
        h.ai_todos = json.dumps(todos, ensure_ascii=False)
        return {'id': task['id'], 'created': True}
