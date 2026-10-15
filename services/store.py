"""업무·이력·참고자료·업무연결 CRUD.

화면(pages/)은 SQLAlchemy 객체 대신 이 모듈이 돌려주는 dict 만 다룬다.
상태 변경·업무 연결·참고자료 추가처럼 "기록할 가치가 있는 변화"는 이력에
자동으로 남겨, 업무의 흐름을 이력 타임라인 하나로 따라갈 수 있게 한다.
"""
import os
import re
import uuid
from datetime import datetime

from sqlalchemy import or_, select

from services import db
from services.db import HistoryEntry, Reference, Task, TaskLink

STATUSES = ['예정', '진행중', '보류', '완료']
ENTRY_TYPES = ['메모', '회의', '결정', '진행', '이슈', '요청']
AUTO_ENTRY_TYPES = ['상태변경', '연결', '자료']  # 시스템이 자동으로 남기는 유형
RELATIONS = ['관련', '선행', '후속', '파생']
# A→B 를 "선행"으로 연결하면 B 쪽에서 A 는 "후속" 관계로 보인다
_INVERSE = {'관련': '관련', '선행': '후속', '후속': '선행', '파생': '파생 원본'}


def _dt(v):
    return v.strftime('%Y-%m-%d %H:%M') if v else ''


def _tags(s: str) -> list[str]:
    return [t.strip() for t in (s or '').split(',') if t.strip()]


def _task_dict(t: Task) -> dict:
    return {
        'id': t.id, 'title': t.title, 'description': t.description or '', 'status': t.status,
        'owner': t.owner or '', 'tags': _tags(t.tags), 'parent_id': t.parent_id,
        'start_date': t.start_date or '', 'due_date': t.due_date or '',
        'created_at': _dt(t.created_at), 'updated_at': _dt(t.updated_at),
    }


def _history_dict(h: HistoryEntry) -> dict:
    return {
        'id': h.id, 'task_id': h.task_id, 'entry_type': h.entry_type, 'content': h.content,
        'author': h.author or '', 'occurred_at': _dt(h.occurred_at), 'created_at': _dt(h.created_at),
    }


def _ref_dict(r: Reference) -> dict:
    return {
        'id': r.id, 'task_id': r.task_id, 'kind': r.kind, 'title': r.title, 'url': r.url or '',
        'file_name': r.file_name or '', 'file_path': r.file_path or '', 'note': r.note or '',
        'created_at': _dt(r.created_at),
    }


def _log(s, task_id: int, entry_type: str, content: str, author: str = ''):
    s.add(HistoryEntry(task_id=task_id, entry_type=entry_type, content=content, author=author))


# ── 업무 ─────────────────────────────────────────────────────────────

def create_task(title: str, description: str = '', status: str = '진행중', owner: str = '',
                tags: list[str] | str = '', parent_id: int | None = None,
                start_date: str = '', due_date: str = '') -> dict:
    title = (title or '').strip()
    if not title:
        raise ValueError('업무 제목을 입력하세요.')
    if status not in STATUSES:
        raise ValueError(f'알 수 없는 상태: {status}')
    if isinstance(tags, list):
        tags = ', '.join(tags)
    with db.SessionLocal() as s:
        t = Task(title=title, description=description or '', status=status, owner=owner or '',
                 tags=', '.join(_tags(tags)), parent_id=parent_id or None,
                 start_date=start_date or '', due_date=due_date or '')
        s.add(t)
        s.flush()
        _log(s, t.id, '상태변경', f'업무 생성 (상태: {status})', owner)
        s.commit()
        return _task_dict(t)


def get_task(task_id: int) -> dict | None:
    with db.SessionLocal() as s:
        t = s.get(Task, task_id)
        return _task_dict(t) if t else None


def update_task(task_id: int, author: str = '', **fields) -> dict:
    allowed = {'title', 'description', 'status', 'owner', 'tags', 'parent_id', 'start_date', 'due_date'}
    with db.SessionLocal() as s:
        t = s.get(Task, task_id)
        if t is None:
            raise ValueError('업무를 찾을 수 없습니다.')
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k == 'title' and not (v or '').strip():
                raise ValueError('업무 제목을 입력하세요.')
            if k == 'status':
                if v not in STATUSES:
                    raise ValueError(f'알 수 없는 상태: {v}')
                if v != t.status:
                    _log(s, t.id, '상태변경', f'{t.status} → {v}', author)
            if k == 'tags':
                v = ', '.join(v) if isinstance(v, list) else ', '.join(_tags(v))
            if k == 'parent_id':
                v = v or None
                if v == t.id:
                    raise ValueError('자기 자신을 상위 업무로 지정할 수 없습니다.')
            setattr(t, k, v if v is not None or k == 'parent_id' else '')
        s.commit()
        return _task_dict(t)


def delete_task(task_id: int) -> None:
    with db.SessionLocal() as s:
        t = s.get(Task, task_id)
        if t is None:
            return
        files = [r.file_path for r in t.references if r.file_path]
        s.delete(t)
        s.commit()
    for f in files:
        _remove_upload(f)


def list_tasks(query: str = '', status: str | list[str] | None = None, tag: str = '',
               owner: str = '') -> list[dict]:
    """제목·설명·태그·이력 내용·참고자료 제목/메모까지 검색한다."""
    with db.SessionLocal() as s:
        stmt = select(Task)
        if status:
            stmt = stmt.where(Task.status.in_([status] if isinstance(status, str) else status))
        if owner:
            stmt = stmt.where(Task.owner == owner)
        if tag:
            stmt = stmt.where(Task.tags.like(f'%{tag}%'))
        if query:
            like = f'%{query.strip()}%'
            hist_ids = select(HistoryEntry.task_id).where(HistoryEntry.content.like(like))
            ref_ids = select(Reference.task_id).where(
                or_(Reference.title.like(like), Reference.note.like(like), Reference.url.like(like)))
            stmt = stmt.where(or_(Task.title.like(like), Task.description.like(like), Task.tags.like(like),
                                  Task.id.in_(hist_ids), Task.id.in_(ref_ids)))
        tasks = [_task_dict(t) for t in s.scalars(stmt.order_by(Task.updated_at.desc(), Task.id.desc()))]
        if tag:  # LIKE 는 부분일치라 정확한 태그만 남긴다
            tasks = [t for t in tasks if tag in t['tags']]
        return tasks


def all_tags() -> list[str]:
    with db.SessionLocal() as s:
        out = set()
        for (tags,) in s.execute(select(Task.tags)):
            out.update(_tags(tags))
        return sorted(out)


def all_owners() -> list[str]:
    with db.SessionLocal() as s:
        return sorted({o for (o,) in s.execute(select(Task.owner)) if o})


def child_tasks(task_id: int) -> list[dict]:
    with db.SessionLocal() as s:
        return [_task_dict(t) for t in s.scalars(select(Task).where(Task.parent_id == task_id).order_by(Task.id))]


# ── 이력 ─────────────────────────────────────────────────────────────

def add_history(task_id: int, content: str, entry_type: str = '메모', author: str = '',
                occurred_at: str | datetime | None = None) -> dict:
    content = (content or '').strip()
    if not content:
        raise ValueError('이력 내용을 입력하세요.')
    if entry_type not in ENTRY_TYPES + AUTO_ENTRY_TYPES:
        raise ValueError(f'알 수 없는 이력 유형: {entry_type}')
    if isinstance(occurred_at, str):
        occurred_at = datetime.fromisoformat(occurred_at) if occurred_at.strip() else None
    with db.SessionLocal() as s:
        t = s.get(Task, task_id)
        if t is None:
            raise ValueError('업무를 찾을 수 없습니다.')
        h = HistoryEntry(task_id=task_id, entry_type=entry_type, content=content, author=author or '')
        if occurred_at:
            h.occurred_at = occurred_at
        s.add(h)
        t.updated_at = datetime.now().replace(microsecond=0)
        s.commit()
        return _history_dict(h)


def list_history(task_id: int) -> list[dict]:
    with db.SessionLocal() as s:
        rows = s.scalars(select(HistoryEntry).where(HistoryEntry.task_id == task_id)
                         .order_by(HistoryEntry.occurred_at.desc(), HistoryEntry.id.desc()))
        return [_history_dict(h) for h in rows]


def delete_history(entry_id: int) -> None:
    with db.SessionLocal() as s:
        h = s.get(HistoryEntry, entry_id)
        if h:
            s.delete(h)
            s.commit()


def recent_history(limit: int = 20) -> list[dict]:
    """전체 업무의 최근 이력 (업무 제목 포함)."""
    with db.SessionLocal() as s:
        rows = s.execute(select(HistoryEntry, Task.title).join(Task, Task.id == HistoryEntry.task_id)
                         .order_by(HistoryEntry.occurred_at.desc(), HistoryEntry.id.desc()).limit(limit))
        return [{**_history_dict(h), 'task_title': title} for h, title in rows]


# ── 참고자료 ──────────────────────────────────────────────────────────

def _safe_name(name: str) -> str:
    base = os.path.basename(name or 'file')
    return re.sub(r'[^\w.\-가-힣]+', '_', base)[:120] or 'file'


def _remove_upload(rel_path: str) -> None:
    path = os.path.realpath(os.path.join(db.UPLOAD_DIR, rel_path))
    if path.startswith(os.path.realpath(db.UPLOAD_DIR) + os.sep) and os.path.exists(path):
        os.remove(path)


def upload_abspath(rel_path: str) -> str | None:
    """다운로드용 절대경로. UPLOAD_DIR 밖을 가리키면 None."""
    root = os.path.realpath(db.UPLOAD_DIR)
    path = os.path.realpath(os.path.join(root, rel_path))
    return path if path.startswith(root + os.sep) and os.path.isfile(path) else None


def add_reference(task_id: int, kind: str, title: str = '', url: str = '', note: str = '',
                  file_name: str = '', file_bytes: bytes | None = None, author: str = '') -> dict:
    if kind not in ('link', 'file', 'note'):
        raise ValueError(f'알 수 없는 자료 유형: {kind}')
    title = (title or '').strip()
    url = (url or '').strip()
    if kind == 'link':
        if not url:
            raise ValueError('링크 URL 을 입력하세요.')
        if not re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*://', url):
            url = 'https://' + url
        title = title or url
    elif kind == 'file':
        if file_bytes is None:
            raise ValueError('파일을 선택하세요.')
        title = title or file_name
    else:
        if not (note or '').strip():
            raise ValueError('메모 내용을 입력하세요.')
        title = title or (note.strip().splitlines()[0][:60])

    with db.SessionLocal() as s:
        t = s.get(Task, task_id)
        if t is None:
            raise ValueError('업무를 찾을 수 없습니다.')
        r = Reference(task_id=task_id, kind=kind, title=title, url=url if kind == 'link' else '',
                      note=note or '')
        if kind == 'file':
            rel = os.path.join(str(task_id), f'{uuid.uuid4().hex[:8]}_{_safe_name(file_name)}')
            dest = os.path.join(db.UPLOAD_DIR, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, 'wb') as f:
                f.write(file_bytes)
            r.file_name, r.file_path = file_name, rel
        s.add(r)
        label = {'link': '링크', 'file': '파일', 'note': '메모'}[kind]
        _log(s, task_id, '자료', f'참고자료 추가 ({label}): {title}', author)
        t.updated_at = datetime.now().replace(microsecond=0)
        s.commit()
        return _ref_dict(r)


def list_references(task_id: int | None = None, query: str = '', kind: str = '') -> list[dict]:
    with db.SessionLocal() as s:
        stmt = select(Reference, Task.title).join(Task, Task.id == Reference.task_id)
        if task_id is not None:
            stmt = stmt.where(Reference.task_id == task_id)
        if kind:
            stmt = stmt.where(Reference.kind == kind)
        if query:
            like = f'%{query.strip()}%'
            stmt = stmt.where(or_(Reference.title.like(like), Reference.note.like(like),
                                  Reference.url.like(like), Reference.file_name.like(like),
                                  Task.title.like(like)))
        rows = s.execute(stmt.order_by(Reference.created_at.desc(), Reference.id.desc()))
        return [{**_ref_dict(r), 'task_title': title} for r, title in rows]


def get_reference(ref_id: int) -> dict | None:
    with db.SessionLocal() as s:
        r = s.get(Reference, ref_id)
        return _ref_dict(r) if r else None


def delete_reference(ref_id: int) -> None:
    with db.SessionLocal() as s:
        r = s.get(Reference, ref_id)
        if r is None:
            return
        path = r.file_path
        s.delete(r)
        s.commit()
    if path:
        _remove_upload(path)


# ── 업무 연결 ─────────────────────────────────────────────────────────

def link_tasks(src_id: int, dst_id: int, relation: str = '관련', note: str = '', author: str = '') -> None:
    if src_id == dst_id:
        raise ValueError('같은 업무끼리는 연결할 수 없습니다.')
    if relation not in RELATIONS:
        raise ValueError(f'알 수 없는 관계: {relation}')
    with db.SessionLocal() as s:
        src, dst = s.get(Task, src_id), s.get(Task, dst_id)
        if src is None or dst is None:
            raise ValueError('업무를 찾을 수 없습니다.')
        exists = s.scalar(select(TaskLink).where(or_(
            (TaskLink.src_id == src_id) & (TaskLink.dst_id == dst_id),
            (TaskLink.src_id == dst_id) & (TaskLink.dst_id == src_id))))
        if exists:
            raise ValueError('이미 연결된 업무입니다.')
        s.add(TaskLink(src_id=src_id, dst_id=dst_id, relation=relation, note=note or ''))
        _log(s, src_id, '연결', f'[{relation}] #{dst.id} {dst.title}' + (f' — {note}' if note else ''), author)
        _log(s, dst_id, '연결', f'[{_INVERSE[relation]}] #{src.id} {src.title}' + (f' — {note}' if note else ''),
             author)
        s.commit()


def unlink_tasks(link_id: int) -> None:
    with db.SessionLocal() as s:
        link = s.get(TaskLink, link_id)
        if link:
            s.delete(link)
            s.commit()


def linked_tasks(task_id: int) -> list[dict]:
    """이 업무와 연결된 업무들. relation 은 이 업무 기준으로 본 관계."""
    with db.SessionLocal() as s:
        links = s.scalars(select(TaskLink).where(or_(TaskLink.src_id == task_id, TaskLink.dst_id == task_id))
                          .order_by(TaskLink.created_at))
        out = []
        for link in links:
            other_id = link.dst_id if link.src_id == task_id else link.src_id
            other = s.get(Task, other_id)
            rel = link.relation if link.src_id == task_id else _INVERSE[link.relation]
            out.append({'link_id': link.id, 'relation': rel, 'note': link.note or '', **_task_dict(other)})
        return out
