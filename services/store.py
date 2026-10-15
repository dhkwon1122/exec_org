"""업무·이력·참고자료·업무연결 CRUD.

화면(pages/)은 SQLAlchemy 객체 대신 이 모듈이 돌려주는 dict 만 다룬다.
상태 변경·업무 연결·참고자료 추가처럼 "기록할 가치가 있는 변화"는 이력에
자동으로 남겨, 업무의 흐름을 이력 타임라인 하나로 따라갈 수 있게 한다.
"""
import calendar
import json
import os
import re
import uuid
from datetime import date, datetime, timedelta

from sqlalchemy import or_, select

from services import db
from services.db import HistoryEntry, Reference, Task, TaskLink, Todo

STATUSES = ['예정', '진행중', '보류', '완료']
ENTRY_TYPES = ['메모', '피드백', '회의', '결정', '진행', '이슈', '요청']
AUTO_ENTRY_TYPES = ['상태변경', '연결', '자료', '할일']  # 시스템이 자동으로 남기는 유형
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
        'title': h.title or '', 'summary': h.summary or '', 'keywords': _tags(h.keywords),
        'ai_status': h.ai_status or '', 'ai_error': h.ai_error or '', 'ai_todos': _load_json_list(h.ai_todos),
    }


def _load_json_list(v: str) -> list:
    try:
        out = json.loads(v) if v else []
    except ValueError:
        return []
    return out if isinstance(out, list) else []


def search_terms(query: str) -> list[str]:
    """공백으로 나눈 검색어. "따옴표 묶음"은 한 덩어리로 취급한다."""
    return [a or b for a, b in re.findall(r'"([^"]+)"|(\S+)', query or '') if (a or b).strip()]


def _join_keywords(v) -> str:
    items = v if isinstance(v, list) else re.split(r'[,\n]', v or '')
    seen, out = set(), []
    for k in items:
        k = str(k).strip().lstrip('#').strip()
        if k and k.lower() not in seen:
            seen.add(k.lower())
            out.append(k)
    return ', '.join(out)


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
        for term in search_terms(query):  # 검색어가 여러 개면 모두 포함(AND)
            like = f'%{term}%'
            hist_ids = select(HistoryEntry.task_id).where(or_(
                HistoryEntry.content.like(like), HistoryEntry.title.like(like),
                HistoryEntry.summary.like(like), HistoryEntry.keywords.like(like)))
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
                occurred_at: str | datetime | None = None, title: str = '', summary: str = '',
                keywords: list[str] | str = '') -> dict:
    """원문(content)을 그대로 저장한다. AI 요약·키워드는 services.enrich.submit() 으로 따로 요청."""
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
        h = HistoryEntry(task_id=task_id, entry_type=entry_type, content=content, author=author or '',
                         title=(title or '').strip(), summary=(summary or '').strip(),
                         keywords=_join_keywords(keywords))
        if occurred_at:
            h.occurred_at = occurred_at
        s.add(h)
        t.updated_at = datetime.now().replace(microsecond=0)
        s.commit()
        return _history_dict(h)


def get_history(entry_id: int) -> dict | None:
    with db.SessionLocal() as s:
        h = s.get(HistoryEntry, entry_id)
        return _history_dict(h) if h else None


def update_history(entry_id: int, **fields) -> dict:
    """사용자가 이력(원문·유형·일시·제목·요약·키워드)을 고칠 때."""
    with db.SessionLocal() as s:
        h = s.get(HistoryEntry, entry_id)
        if h is None:
            raise ValueError('이력을 찾을 수 없습니다.')
        if 'content' in fields:
            if not (fields['content'] or '').strip():
                raise ValueError('이력 내용을 입력하세요.')
            h.content = fields['content'].strip()
        if 'entry_type' in fields:
            if fields['entry_type'] not in ENTRY_TYPES + AUTO_ENTRY_TYPES:
                raise ValueError(f"알 수 없는 이력 유형: {fields['entry_type']}")
            h.entry_type = fields['entry_type']
        if fields.get('occurred_at'):
            v = fields['occurred_at']
            h.occurred_at = datetime.fromisoformat(v) if isinstance(v, str) else v
        for k in ('title', 'summary'):
            if k in fields:
                setattr(h, k, (fields[k] or '').strip())
        if 'keywords' in fields:
            h.keywords = _join_keywords(fields['keywords'])
        s.commit()
        return _history_dict(h)


def set_history_ai(entry_id: int, status: str, title: str | None = None, summary: str | None = None,
                   keywords: list[str] | None = None, error: str = '', todos: list[dict] | None = None) -> None:
    """AI 정리 상태/결과 저장. None 인 필드는 기존 값을 유지한다(재생성 중에도 이전 결과가 보이도록)."""
    with db.SessionLocal() as s:
        h = s.get(HistoryEntry, entry_id)
        if h is None:
            return
        h.ai_status, h.ai_error = status, error or ''
        if title is not None:
            h.title = title
        if summary is not None:
            h.summary = summary
        if keywords is not None:
            h.keywords = _join_keywords(keywords)
        if todos is not None:
            h.ai_todos = json.dumps(todos, ensure_ascii=False)
        s.commit()


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


def search_history(query: str = '', entry_types: list[str] | None = None, author: str = '',
                   date_from: str = '', date_to: str = '', task_id: int | None = None,
                   include_auto: bool = False, limit: int = 200) -> list[dict]:
    """이력 검색. 검색어가 여러 개면 모두 포함(AND)해야 하며, 각 검색어는
    원문·AI 제목·요약·키워드·작성자·업무명 중 어디에든 있으면 된다.
    include_auto=False 면 상태변경/연결/자료 같은 자동 이력은 뺀다."""
    with db.SessionLocal() as s:
        stmt = select(HistoryEntry, Task.title).join(Task, Task.id == HistoryEntry.task_id)
        for term in search_terms(query):
            like = f'%{term}%'
            stmt = stmt.where(or_(HistoryEntry.content.like(like), HistoryEntry.title.like(like),
                                  HistoryEntry.summary.like(like), HistoryEntry.keywords.like(like),
                                  HistoryEntry.author.like(like), Task.title.like(like)))
        if entry_types:
            stmt = stmt.where(HistoryEntry.entry_type.in_(entry_types))
        elif not include_auto:
            stmt = stmt.where(HistoryEntry.entry_type.notin_(AUTO_ENTRY_TYPES))
        if author:
            stmt = stmt.where(HistoryEntry.author == author)
        if task_id is not None:
            stmt = stmt.where(HistoryEntry.task_id == task_id)
        if date_from:
            stmt = stmt.where(HistoryEntry.occurred_at >= datetime.fromisoformat(date_from))
        if date_to:  # 그날 하루 전체 포함
            stmt = stmt.where(HistoryEntry.occurred_at < datetime.fromisoformat(date_to) + timedelta(days=1))
        rows = s.execute(stmt.order_by(HistoryEntry.occurred_at.desc(), HistoryEntry.id.desc()).limit(limit))
        return [{**_history_dict(h), 'task_title': title} for h, title in rows]


def keyword_stats(limit: int = 40, task_id: int | None = None) -> list[tuple[str, int]]:
    """이력 키워드 빈도 (많이 나온 순)."""
    counts: dict[str, int] = {}
    display: dict[str, str] = {}
    with db.SessionLocal() as s:
        stmt = select(HistoryEntry.keywords).where(HistoryEntry.keywords != '')
        if task_id is not None:
            stmt = stmt.where(HistoryEntry.task_id == task_id)
        for (kws,) in s.execute(stmt):
            for k in _tags(kws):
                counts[k.lower()] = counts.get(k.lower(), 0) + 1
                display.setdefault(k.lower(), k)
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    return [(display[k], n) for k, n in ranked]


def history_authors() -> list[str]:
    with db.SessionLocal() as s:
        return sorted({a for (a,) in s.execute(select(HistoryEntry.author).distinct()) if a})


def pending_ai_ids(task_id: int | None = None) -> list[int]:
    with db.SessionLocal() as s:
        stmt = select(HistoryEntry.id).where(HistoryEntry.ai_status == 'pending')
        if task_id is not None:
            stmt = stmt.where(HistoryEntry.task_id == task_id)
        return list(s.scalars(stmt))


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


# ── 할 일 ─────────────────────────────────────────────────────────────

_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def _check_date(v: str) -> str:
    v = (v or '').strip()
    if v and not _DATE_RE.match(v):
        raise ValueError(f'날짜 형식이 올바르지 않습니다: {v}')
    return v


def _todo_dict(t: Todo, task_title: str | None = None) -> dict:
    today = date.today().isoformat()
    return {
        'id': t.id, 'task_id': t.task_id, 'task_title': task_title or '', 'title': t.title,
        'due_date': t.due_date or '', 'done': bool(t.done), 'done_at': _dt(t.done_at),
        'assignee_id': t.assignee_id or '', 'assignee_name': t.assignee_name or '',
        'created_by': t.created_by or '', 'source_history_id': t.source_history_id,
        'created_at': _dt(t.created_at),
        'overdue': bool(not t.done and t.due_date and t.due_date < today),
    }


def create_todo(title: str, task_id: int | None = None, due_date: str = '', assignee_id: str = '',
                assignee_name: str = '', created_by: str = '', source_history_id: int | None = None) -> dict:
    title = (title or '').strip()
    if not title:
        raise ValueError('할 일 내용을 입력하세요.')
    due_date = _check_date(due_date)
    with db.SessionLocal() as s:
        task = s.get(Task, task_id) if task_id else None
        if task_id and task is None:
            raise ValueError('업무를 찾을 수 없습니다.')
        t = Todo(title=title, task_id=task_id or None, due_date=due_date, assignee_id=assignee_id or '',
                 assignee_name=assignee_name or '', created_by=created_by or '', source_history_id=source_history_id)
        s.add(t)
        s.commit()
        return _todo_dict(t, task.title if task else '')


def update_todo(todo_id: int, **fields) -> dict:
    with db.SessionLocal() as s:
        t = s.get(Todo, todo_id)
        if t is None:
            raise ValueError('할 일을 찾을 수 없습니다.')
        if 'title' in fields:
            if not (fields['title'] or '').strip():
                raise ValueError('할 일 내용을 입력하세요.')
            t.title = fields['title'].strip()
        if 'due_date' in fields:
            t.due_date = _check_date(fields['due_date'])
        if 'task_id' in fields:
            t.task_id = fields['task_id'] or None
        if 'assignee_id' in fields:
            t.assignee_id = fields['assignee_id'] or ''
            t.assignee_name = fields.get('assignee_name') or ''
        s.commit()
        task = s.get(Task, t.task_id) if t.task_id else None
        return _todo_dict(t, task.title if task else '')


def set_todo_done(todo_id: int, done: bool, by: str = '') -> bool:
    """완료/완료 취소. 업무에 속한 할 일이면 그 업무 이력에도 남긴다. 바뀌었으면 True."""
    with db.SessionLocal() as s:
        t = s.get(Todo, todo_id)
        if t is None or bool(t.done) == bool(done):
            return False
        t.done = bool(done)
        t.done_at = datetime.now().replace(microsecond=0) if done else None
        if t.task_id:
            _log(s, t.task_id, '할일', f"{'완료' if done else '완료 취소'}: {t.title}", by)
        s.commit()
        return True


def delete_todo(todo_id: int) -> None:
    with db.SessionLocal() as s:
        t = s.get(Todo, todo_id)
        if t:
            s.delete(t)
            s.commit()


def list_todos(task_id: int | None = None, assignee_id: str | None = None, done: bool | None = None,
               date_from: str = '', date_to: str = '', done_since: str = '') -> list[dict]:
    """정렬: 미완료 먼저, 마감일 빠른 순(날짜 없는 것은 뒤), 완료는 최근 완료 순."""
    with db.SessionLocal() as s:
        stmt = select(Todo, Task.title).outerjoin(Task, Task.id == Todo.task_id)
        if task_id is not None:
            stmt = stmt.where(Todo.task_id == task_id)
        if assignee_id is not None:
            stmt = stmt.where(Todo.assignee_id == assignee_id)
        if done is not None:
            stmt = stmt.where(Todo.done == done)
        if date_from:
            stmt = stmt.where(Todo.due_date >= date_from)
        if date_to:
            stmt = stmt.where(Todo.due_date <= date_to, Todo.due_date != '')
        if done_since:
            stmt = stmt.where(or_(Todo.done.is_(False), Todo.done_at >= datetime.fromisoformat(done_since)))
        rows = [_todo_dict(t, title) for t, title in s.execute(stmt)]
    open_ = sorted([r for r in rows if not r['done']], key=lambda r: (r['due_date'] == '', r['due_date'], r['id']))
    closed = sorted([r for r in rows if r['done']], key=lambda r: r['done_at'], reverse=True)
    return open_ + closed


def todo_titles_for_history(entry_id: int) -> set[str]:
    with db.SessionLocal() as s:
        return set(s.scalars(select(Todo.title).where(Todo.source_history_id == entry_id)))


# ── 캘린더 ────────────────────────────────────────────────────────────

def month_range(year: int, month: int) -> tuple[str, str]:
    last = calendar.monthrange(year, month)[1]
    return f'{year:04d}-{month:02d}-01', f'{year:04d}-{month:02d}-{last:02d}'


def calendar_month(year: int, month: int) -> dict[str, dict]:
    """{'YYYY-MM-DD': {'todos': [...], 'tasks_due': [...], 'tasks_start': [...], 'history': n}}"""
    first, last = month_range(year, month)
    days: dict[str, dict] = {}

    def day(d):
        return days.setdefault(d, {'todos': [], 'tasks_due': [], 'tasks_start': [], 'history': 0})

    for t in list_todos(date_from=first, date_to=last):
        day(t['due_date'])['todos'].append(t)
    with db.SessionLocal() as s:
        for t in s.scalars(select(Task).where(Task.due_date >= first, Task.due_date <= last)):
            day(t.due_date)['tasks_due'].append(_task_dict(t))
        for t in s.scalars(select(Task).where(Task.start_date >= first, Task.start_date <= last)):
            day(t.start_date)['tasks_start'].append(_task_dict(t))
        start = datetime.fromisoformat(first)
        end = datetime.fromisoformat(last) + timedelta(days=1)
        for (occ,) in s.execute(select(HistoryEntry.occurred_at).where(
                HistoryEntry.occurred_at >= start, HistoryEntry.occurred_at < end,
                HistoryEntry.entry_type.notin_(AUTO_ENTRY_TYPES))):
            day(occ.date().isoformat())['history'] += 1
    return days


def tasks_due_between(date_from: str, date_to: str) -> list[dict]:
    with db.SessionLocal() as s:
        rows = s.scalars(select(Task).where(Task.due_date >= date_from, Task.due_date <= date_to,
                                            Task.status != '완료').order_by(Task.due_date))
        return [_task_dict(t) for t in rows]
