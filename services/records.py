"""기록 수정 이력, 개인 북마크, 업무·기록 휴지통."""
import json
import logging
import uuid
from datetime import datetime

from sqlalchemy import func, select, update

from services import db
from services.db import (HistoryBookmark, HistoryEntry, HistoryPerson, HistoryRevision, HistoryTask,
                         Person, Reference, Task)

SNAPSHOT_FIELDS = ('entry_type', 'occurred_at', 'title', 'summary', 'keywords', 'content')


def _snapshot(s, h):
    data = {key: getattr(h, key) or '' for key in SNAPSHOT_FIELDS}
    data['occurred_at'] = h.occurred_at.isoformat() if h.occurred_at else ''
    data['person_ids'] = sorted(s.scalars(select(HistoryPerson.person_id).where(HistoryPerson.history_id == h.id)))
    others = list(s.scalars(select(HistoryTask.task_id).where(HistoryTask.history_id == h.id).order_by(HistoryTask.task_id)))
    data['task_ids'] = ([h.task_id] if h.task_id else []) + [tid for tid in others if tid != h.task_id]
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def save_revision(s, h, actor='', actor_id='', action='수정'):
    snapshot = _snapshot(s, h)
    last = s.scalar(select(HistoryRevision).where(HistoryRevision.history_id == h.id)
                    .order_by(HistoryRevision.id.desc()).limit(1))
    if last and last.snapshot == snapshot:
        return
    s.add(HistoryRevision(history_id=h.id, snapshot=snapshot, actor=actor or '시스템',
                          actor_id=actor_id, action=action))
    s.flush()


def ensure_revision(s, h):
    if not s.scalar(select(HistoryRevision.id).where(HistoryRevision.history_id == h.id).limit(1)):
        save_revision(s, h, action='기준 버전')


def bookmark_ids(user_id):
    if not user_id:
        return set()
    with db.SessionLocal() as s:
        return set(s.scalars(select(HistoryBookmark.history_id).join(HistoryEntry,
            HistoryEntry.id == HistoryBookmark.history_id).where(HistoryBookmark.user_id == user_id)))


def toggle_bookmark(entry_id, user_id):
    if not user_id:
        raise ValueError('로그인이 필요합니다.')
    with db.transaction() as s:
        # 같은 사용자의 동시 클릭과 영구 삭제를 직렬화한다.
        result = s.execute(update(HistoryEntry).where(HistoryEntry.id == entry_id, HistoryEntry.deleted_at.is_(None))
                           .values(id=HistoryEntry.id))
        if not result.rowcount:
            raise ValueError('기록을 찾을 수 없습니다.')
        row = s.get(HistoryBookmark, (entry_id, user_id))
        if row:
            s.delete(row)
            return False
        s.add(HistoryBookmark(history_id=entry_id, user_id=user_id))
        return True


def revisions(entry_id):
    with db.SessionLocal(info={'include_deleted': True}) as s:
        if s.get(HistoryEntry, entry_id) is None:
            return []
        rows = s.execute(select(HistoryRevision.id, HistoryRevision.actor, HistoryRevision.action,
                                HistoryRevision.created_at).where(HistoryRevision.history_id == entry_id)
                          .order_by(HistoryRevision.id.desc()))
        return [{'id': rid, 'actor': actor, 'action': action, 'at': at.strftime('%Y-%m-%d %H:%M:%S')}
                for rid, actor, action, at in rows]


def revision_pair(entry_id, revision_id):
    with db.SessionLocal(info={'include_deleted': True}) as s:
        current = s.get(HistoryRevision, revision_id)
        if not current or current.history_id != entry_id:
            raise ValueError('수정 이력을 찾을 수 없습니다.')
        previous = s.scalar(select(HistoryRevision).where(HistoryRevision.history_id == entry_id,
                            HistoryRevision.id < revision_id).order_by(HistoryRevision.id.desc()).limit(1))
        return (json.loads(previous.snapshot) if previous else {}, json.loads(current.snapshot))


def restore_revision(entry_id, revision_id, expected_version, actor='', actor_id=''):
    from services import store
    _, snapshot = revision_pair(entry_id, revision_id)
    with db.SessionLocal() as s:
        active_tasks = set(s.scalars(select(Task.id).where(Task.id.in_(snapshot['task_ids']))))
        active_people = set(s.scalars(select(Person.id).where(Person.id.in_(snapshot['person_ids']))))
    task_ids = [tid for tid in snapshot['task_ids'] if tid in active_tasks]
    if not task_ids and not active_people:
        raise ValueError('연결된 업무나 임원을 먼저 복원한 뒤 다시 시도하세요.')
    return store.update_history(entry_id, **{k: snapshot[k] for k in SNAPSHOT_FIELDS},
        task_ids=task_ids, person_ids=sorted(active_people), expected_version=expected_version,
        actor=actor, actor_id=actor_id, revision_action='버전 복원')


def trash_history(entry_id, actor=''):
    with db.transaction() as s:
        result = s.execute(update(HistoryEntry).where(HistoryEntry.id == entry_id, HistoryEntry.deleted_at.is_(None))
                           .values(ai_token=uuid.uuid4().hex, edit_version=func.coalesce(HistoryEntry.edit_version, 0) + 1))
        if not result.rowcount:
            return
        h = s.get(HistoryEntry, entry_id)
        ensure_revision(s, h)
        h.deleted_at, h.deleted_by = datetime.now().replace(microsecond=0), actor
        h.ai_status = '' if h.ai_status == 'pending' else h.ai_status


def trash_task(task_id, actor=''):
    with db.transaction() as s:
        locked = s.execute(update(Task).where(Task.id == task_id, Task.deleted_at.is_(None)).values(id=Task.id))
        if not locked.rowcount:
            return
        t = s.get(Task, task_id)
        now = datetime.now().replace(microsecond=0)
        for h in s.scalars(select(HistoryEntry).where(HistoryEntry.task_id == task_id)).all():
            s.execute(update(HistoryEntry).where(HistoryEntry.id == h.id).values(
                edit_version=func.coalesce(HistoryEntry.edit_version, 0) + 1, ai_token=uuid.uuid4().hex))
            ensure_revision(s, h)
            h.ai_status = '' if h.ai_status == 'pending' else h.ai_status
            other = s.scalar(select(HistoryTask).join(Task, Task.id == HistoryTask.task_id).where(
                HistoryTask.history_id == h.id, HistoryTask.task_id != task_id).order_by(HistoryTask.task_id))
            if other:
                # 여러 업무에 걸친 기록은 살아 있는 업무에서 계속 읽을 수 있다.
                h.task_id = other.task_id
                for ref in s.scalars(select(Reference).where(Reference.history_id == h.id)):
                    ref.task_id = other.task_id
                s.delete(other)
                s.flush()
                if not s.get(HistoryTask, (h.id, task_id)):
                    s.add(HistoryTask(history_id=h.id, task_id=task_id))
                save_revision(s, h, actor, action='업무 이동')
            else:
                h.deleted_at, h.deleted_by, h.deleted_with_task = now, actor, task_id
                h.ai_token = uuid.uuid4().hex
                h.ai_status = '' if h.ai_status == 'pending' else h.ai_status
        t.deleted_at, t.deleted_by = now, actor


def restore(kind, item_id):
    model = {'history': HistoryEntry, 'task': Task}.get(kind)
    if model is None:
        raise ValueError('복원할 항목을 확인하세요.')
    with db.transaction() as s:
        s.info['include_deleted'] = True
        row = s.get(model, item_id, with_for_update=True)
        if not row or not row.deleted_at:
            raise ValueError('휴지통에서 항목을 찾을 수 없습니다.')
        if kind == 'history' and row.task_id:
            task = s.get(Task, row.task_id)
            if not task or task.deleted_at:
                raise ValueError('이 기록의 업무를 먼저 복원하세요.')
        row.deleted_at, row.deleted_by = None, ''
        if kind == 'history':
            row.deleted_with_task = None
        else:
            ancestor, seen = row.parent_id, {row.id}
            while ancestor:
                if ancestor in seen:
                    raise ValueError('상위 업무 연결이 순환합니다. 상위 업무의 연결을 먼저 변경하세요.')
                seen.add(ancestor)
                parent = s.get(Task, ancestor)
                ancestor = parent.parent_id if parent else None
            for h in s.scalars(select(HistoryEntry).where(HistoryEntry.deleted_with_task == item_id)):
                h.deleted_at, h.deleted_by, h.deleted_with_task = None, '', None


def list_trash(query='', kind='', limit=100):
    with db.SessionLocal(info={'include_deleted': True}) as s:
        out = []
        if kind in ('', 'task'):
            stmt = select(Task).where(Task.deleted_at.is_not(None))
            if query:
                stmt = stmt.where(func.lower(Task.title).contains(query.lower(), autoescape=True))
            for t in s.scalars(stmt.order_by(Task.deleted_at.desc(), Task.id.desc()).limit(limit)):
                count = s.scalar(select(func.count()).select_from(HistoryEntry)
                    .where(HistoryEntry.deleted_with_task == t.id))
                out.append({'kind': 'task', 'id': t.id, 'title': t.title, 'by': t.deleted_by or '',
                            'at': t.deleted_at.isoformat(sep=' ', timespec='seconds'), 'count': count})
        if kind in ('', 'history'):
            stmt = select(HistoryEntry).where(HistoryEntry.deleted_at.is_not(None),
                                             HistoryEntry.deleted_with_task.is_(None))
            if query:
                stmt = stmt.where(func.lower(HistoryEntry.title + HistoryEntry.content).contains(query.lower(), autoescape=True))
            for h in s.scalars(stmt.order_by(HistoryEntry.deleted_at.desc(), HistoryEntry.id.desc()).limit(limit)):
                out.append({'kind': 'history', 'id': h.id, 'title': h.title or h.content[:100], 'by': h.deleted_by or '',
                            'at': h.deleted_at.isoformat(sep=' ', timespec='seconds'), 'count': 0})
        return sorted(out, key=lambda item: (item['at'], item['id']), reverse=True)[:limit]


def purge(kind, item_id):
    from services import store
    model = {'history': HistoryEntry, 'task': Task}.get(kind)
    if model is None:
        raise ValueError('삭제할 항목을 확인하세요.')
    paths = []
    with db.transaction() as s:
        s.info['include_deleted'] = True
        row = s.get(model, item_id, with_for_update=True)
        if not row or not row.deleted_at:
            raise ValueError('휴지통에 있는 항목만 영구 삭제할 수 있습니다.')
        def remove_history(h):
            refs = list(s.scalars(select(Reference).where(Reference.history_id == h.id)))
            # ALTER 로 추가된 예전 출처 컬럼에는 외래키가 없을 수도 있다.
            s.execute(update(Task).where(Task.source_history_id == h.id).values(source_history_id=None))
            for ref in refs:
                if ref.file_path:
                    paths.append(ref.file_path)
                s.delete(ref)
            s.delete(h)

        if kind == 'history':
            remove_history(row)
        else:
            for h in s.scalars(select(HistoryEntry).where(HistoryEntry.task_id == item_id)).all():
                remove_history(h)
            s.flush()
            for ref in s.scalars(select(Reference).where(Reference.task_id == item_id)).all():
                if ref.file_path:
                    paths.append(ref.file_path)
                s.delete(ref)
            s.execute(update(Task).where(Task.parent_id == item_id).values(parent_id=None))
            s.delete(row)
    for path in set(paths):
        try:
            store._remove_upload(path)
        except OSError:
            logging.getLogger(__name__).exception('영구 삭제한 첨부 파일 정리 실패')
