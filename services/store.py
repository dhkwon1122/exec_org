"""업무·이력·참고자료·업무연결 CRUD.

화면(pages/)은 SQLAlchemy 객체 대신 이 모듈이 돌려주는 dict 만 다룬다.
상태 변경·업무 연결·참고자료 추가처럼 "기록할 가치가 있는 변화"는 이력에
자동으로 남겨, 업무의 흐름을 이력 타임라인 하나로 따라갈 수 있게 한다.
"""
import calendar
import json
import math
import os
import re
import uuid
from datetime import date, datetime, timedelta

from sqlalchemy import false, func, or_, select, update
from sqlalchemy.orm import aliased

from services import db
from services.db import HistoryEntry, HistoryPerson, HistoryReference, HistoryTask, InboundMail, TaskAssignee, Person, PersonCareer, Reference, Task, TaskLink

STATUSES = ['예정', '진행중', '보류', '완료']
ENTRY_TYPES = ['메모', '피드백', '동향', '회의', '결정', '진행', '이슈', '요청']
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
        'assignee_id': t.assignee_id or '', 'assignees': _assignee_list(t), 'done_at': _dt(t.done_at),
        'source_history_id': t.source_history_id, 'source_mail_id': t.source_mail_id,
        'created_at': _dt(t.created_at), 'updated_at': _dt(t.updated_at),
    }


def _assignee_list(t: Task) -> list[dict]:
    return [{'name': a.name, 'user_id': a.user_id or ''} for a in t.assignee_rows]


def parse_assignees(value) -> list[dict]:
    """담당자 입력 → [{'name', 'user_id'}]. 쉼표·/ 로 이은 이름 글, 이름 목록, dict 목록 모두 받는다.
    같은 이름은 한 번만."""
    if value is None:
        return []
    if isinstance(value, str):
        value = re.split(r'[,/;]', value)
    out, seen = [], set()
    for v in value:
        d = {'name': str(v.get('name') or ''), 'user_id': str(v.get('user_id') or '')} if isinstance(v, dict) \
            else {'name': str(v or ''), 'user_id': ''}
        d['name'] = d['name'].strip()[:100]
        if d['name'] and d['name'] not in seen:
            seen.add(d['name'])
            out.append(d)
    return out


def _set_assignees(s, t: Task, people: list[dict]) -> None:
    """담당자 바꾸기 — task_assignees 를 새로 쓰고 owner(이름 이은 글)·assignee_id(첫 user_id)도 맞춘다."""
    for a in list(t.assignee_rows):
        t.assignee_rows.remove(a)
    s.flush()
    for i, p in enumerate(people):
        t.assignee_rows.append(TaskAssignee(name=p['name'], user_id=p['user_id'], pos=i))
    t.owner = ', '.join(p['name'] for p in people)[:100]
    t.assignee_id = next((p['user_id'] for p in people if p['user_id']), '')


def _mine(user_id: str, name: str = ''):
    """내가 담당자인 업무 조건 — user_id 가 같거나, user_id 없이 이름만 같은 담당자."""
    cond = TaskAssignee.user_id == user_id
    if name:
        cond = or_(cond, (TaskAssignee.user_id == '') & (TaskAssignee.name == name))
    return Task.id.in_(select(TaskAssignee.task_id).where(cond))


def _history_dict(h: HistoryEntry) -> dict:
    return {
        'id': h.id, 'task_id': h.task_id, 'entry_type': h.entry_type, 'content': h.content,
        'author': h.author or '', 'occurred_at': _dt(h.occurred_at), 'created_at': _dt(h.created_at),
        'title': h.title or '', 'summary': h.summary or '', 'keywords': _tags(h.keywords),
        'ai_status': h.ai_status or '', 'ai_error': h.ai_error or '', 'ai_todos': _load_json_list(h.ai_todos),
        'ai_token': h.ai_token or '',
        'ai_reference_date': h.ai_reference_date or '',
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
        'id': r.id, 'task_id': r.task_id, 'history_id': r.history_id, 'kind': r.kind, 'title': r.title, 'url': r.url or '',
        'file_name': r.file_name or '', 'file_path': r.file_path or '', 'note': r.note or '',
        'created_at': _dt(r.created_at), 'content': r.content or '', 'content_at': _dt(r.content_at),
        'content_error': r.content_error or '', 'content_summary': r.content_summary or '',
        'content_tries': r.content_tries or 0,
    }


def _log(s, task_id: int, entry_type: str, content: str, author: str = ''):
    s.add(HistoryEntry(task_id=task_id, entry_type=entry_type, content=content, author=author))


# ── 업무 ─────────────────────────────────────────────────────────────

def create_task(title: str, description: str = '', status: str = '진행중', owner: str = '',
                tags: list[str] | str = '', parent_id: int | None = None,
                start_date: str = '', due_date: str = '', assignees: list | None = None) -> dict:
    """assignees: 담당자 [{'name', 'user_id'}] — 없으면 owner(쉼표로 여러 명) 이름으로."""
    title = (title or '').strip()
    if not title:
        raise ValueError('업무 제목을 입력하세요.')
    if status not in STATUSES:
        raise ValueError(f'알 수 없는 상태: {status}')
    if isinstance(tags, list):
        tags = ', '.join(tags)
    with db.SessionLocal() as s:
        people = parse_assignees(assignees if assignees is not None else owner)
        t = Task(title=title, description=description or '', status=status, owner='',
                 tags=', '.join(_tags(tags)), parent_id=parent_id or None,
                 start_date=start_date or '', due_date=due_date or '')
        s.add(t)
        s.flush()
        _set_assignees(s, t, people)
        _log(s, t.id, '상태변경', f'업무 생성 (상태: {status})', t.owner)
        s.commit()
        return _task_dict(t)


def get_task(task_id: int) -> dict | None:
    with db.SessionLocal() as s:
        t = s.get(Task, task_id)
        return _task_dict(t) if t else None


def update_task(task_id: int, author: str = '', **fields) -> dict:
    """assignees([{'name', 'user_id'}]) 나 owner(쉼표로 여러 이름)로 담당자를 바꾼다."""
    allowed = {'title', 'description', 'status', 'tags', 'parent_id', 'start_date', 'due_date'}
    with db.SessionLocal() as s:
        t = s.get(Task, task_id)
        if t is None:
            raise ValueError('업무를 찾을 수 없습니다.')
        if 'assignees' in fields or 'owner' in fields:
            _set_assignees(s, t, parse_assignees(fields['assignees'] if 'assignees' in fields else fields['owner']))
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
                    t.done_at = datetime.now().replace(microsecond=0) if v == '완료' else None
            if k == 'tags':
                v = ', '.join(v) if isinstance(v, list) else ', '.join(_tags(v))
            if k == 'parent_id':
                v = v or None
                if v == t.id:
                    raise ValueError('자기 자신을 상위 업무로 지정할 수 없습니다.')
                up, seen = v, set()
                while up and up not in seen:  # 하위 업무를 상위로 두면 고리가 생긴다
                    seen.add(up)
                    if up == t.id:
                        raise ValueError('이 업무의 하위 업무를 상위 업무로 지정할 수 없습니다.')
                    p = s.get(Task, up)
                    up = p.parent_id if p else None
            setattr(t, k, v if v is not None or k == 'parent_id' else '')
        s.commit()
        return _task_dict(t)


def delete_task(task_id: int) -> None:
    with db.SessionLocal() as s:
        t = s.get(Task, task_id)
        if t is None:
            return
        # 다른 업무에도 걸친 이력은 지우지 않고 그 업무로 옮긴다 (거기 붙은 자료도 같이)
        for h in s.scalars(select(HistoryEntry).where(HistoryEntry.task_id == task_id)):
            link = s.scalars(select(HistoryTask).where(HistoryTask.history_id == h.id, HistoryTask.task_id != task_id)
                             .order_by(HistoryTask.task_id)).first()
            if link is None:
                continue
            h.task_id = link.task_id
            for r in s.scalars(select(Reference).where(Reference.history_id == h.id)):
                r.task_id = link.task_id
            s.delete(link)
        s.flush()
        s.expire(t, ['references'])
        files = [r.file_path for r in t.references if r.file_path]
        s.delete(t)
        s.commit()
    for f in files:
        _remove_upload(f)


def list_tasks(query: str = '', status: str | list[str] | None = None, tag: str = '',
               owner: str = '', top_level: bool = False) -> list[dict]:
    """제목·설명·태그·이력 내용·참고자료 제목/메모까지 검색한다. top_level: 상위 업무가 없는 업무만."""
    with db.SessionLocal() as s:
        stmt = select(Task)
        if top_level:
            stmt = stmt.where(Task.parent_id.is_(None))
        if status:
            stmt = stmt.where(Task.status.in_([status] if isinstance(status, str) else status))
        if owner:
            stmt = stmt.where(Task.id.in_(select(TaskAssignee.task_id).where(TaskAssignee.name == owner)))
        if tag:
            stmt = stmt.where(Task.tags.like(f'%{tag}%'))
        for term in search_terms(query):  # 검색어가 여러 개면 모두 포함(AND)
            like = f'%{term}%'
            hist_match = or_(HistoryEntry.content.like(like), HistoryEntry.title.like(like),
                             HistoryEntry.summary.like(like), HistoryEntry.keywords.like(like))
            hist_ids = select(HistoryEntry.task_id).where(hist_match).union(
                select(HistoryTask.task_id).join(HistoryEntry, HistoryEntry.id == HistoryTask.history_id)
                .where(hist_match))
            ref_ids = select(Reference.task_id).where(
                or_(Reference.title.like(like), Reference.note.like(like), Reference.url.like(like)))
            stmt = stmt.where(or_(Task.title.like(like), Task.description.like(like), Task.tags.like(like),
                                  Task.id.in_(hist_ids), Task.id.in_(ref_ids)))
        tasks = [_task_dict(t) for t in s.scalars(stmt.order_by(Task.updated_at.desc(), Task.id.desc()))]
        if tag:  # LIKE 는 부분일치라 정확한 태그만 남긴다
            tasks = [t for t in tasks if tag in t['tags']]
        return tasks


def task_tree(statuses: list[str] | None = None, depth: int = 1) -> list[dict]:
    """최상위 업무부터 depth 단계 아래 하위 업무까지 펼친 목록 — [{'task', 'level', 'n_children', 'n_hidden'}].
    statuses 가 있으면 그 상태의 업무만(상위가 걸러지면 그 아래도 안 보임). 형제는 최근 수정 순, 하위는 만든 순.
    n_hidden = depth 를 넘어 펼치지 않은 바로 아래 하위 업무 수. 고리(잘못된 상위 지정)가 있어도 멈춘다."""
    tasks = list_tasks()
    keep = set(statuses or [])
    children: dict[int | None, list[dict]] = {}
    ids = {t['id'] for t in tasks}
    for t in tasks:
        children.setdefault(t['parent_id'] if t['parent_id'] in ids else None, []).append(t)
    for pid, kids in children.items():
        if pid is not None:
            kids.sort(key=lambda t: t['id'])
    out, seen = [], set()

    def walk(t, level):
        if t['id'] in seen or (keep and t['status'] not in keep):
            return
        seen.add(t['id'])
        kids = children.get(t['id'], [])
        shown = level < depth
        out.append({'task': t, 'level': level, 'n_children': len(kids),
                    'n_done': sum(k['status'] == '완료' for k in kids), 'n_hidden': 0 if shown else len(kids)})
        if shown:
            for k in kids:
                walk(k, level + 1)

    for t in children.get(None, []):
        walk(t, 0)
    return out


def child_progress(task_ids: list[int]) -> dict[int, tuple[int, int]]:
    """업무별 바로 아래 하위 업무 (완료 수, 전체 수)."""
    if not task_ids:
        return {}
    out: dict[int, list[int]] = {}
    with db.SessionLocal() as s:
        for pid, status in s.execute(select(Task.parent_id, Task.status).where(Task.parent_id.in_(task_ids))):
            c = out.setdefault(pid, [0, 0])
            c[0] += status == '완료'
            c[1] += 1
    return {k: (v[0], v[1]) for k, v in out.items()}


def all_tags() -> list[str]:
    with db.SessionLocal() as s:
        out = set()
        for (tags,) in s.execute(select(Task.tags)):
            out.update(_tags(tags))
        return sorted(out)


def all_owners() -> list[str]:
    with db.SessionLocal() as s:
        return sorted({o for (o,) in s.execute(select(TaskAssignee.name).distinct()) if o})


def task_scope(task_id: int, include_children: bool = True) -> list[int]:
    """업무와 (선택) 그 아래 모든 하위 업무의 id — 업무 자신이 먼저."""
    out, todo = [task_id], [task_id]
    if not include_children:
        return out
    with db.SessionLocal() as s:
        while todo:
            kids = list(s.scalars(select(Task.id).where(Task.parent_id.in_(todo))))
            todo = [k for k in kids if k not in out]  # 부모 고리가 꼬여 있어도 멈춘다
            out += todo
    return out


def child_tasks(task_id: int) -> list[dict]:
    with db.SessionLocal() as s:
        return [_task_dict(t) for t in s.scalars(select(Task).where(Task.parent_id == task_id).order_by(Task.id))]


# ── 이력 ─────────────────────────────────────────────────────────────

def add_history(task_id: int | None, content: str, entry_type: str = '메모', author: str = '',
                occurred_at: str | datetime | None = None, title: str = '', summary: str = '',
                keywords: list[str] | str = '', person_ids: list[int] | None = None,
                also_task_ids: list[int] | None = None, ref_ids: list[int] | None = None, *, session=None) -> dict:
    """원문(content)을 그대로 저장한다. AI 요약·키워드는 services.enrich.submit() 으로 따로 요청.
    업무(task_id)나 임원(person_ids) 중 하나는 있어야 한다 — 임원 동향은 업무 없이 임원에만 연결된다.
    also_task_ids: 이 이력이 함께 걸친 다른 업무들 (task_id 가 없으면 그 첫 업무가 이력의 업무가 된다).
    ref_ids: 이 이력이 다룬 참고자료 (참고자료 탭의 '이 자료에 대한 기록')."""
    person_ids = sorted({int(p) for p in (person_ids or [])})
    also = list(dict.fromkeys(int(x) for x in (also_task_ids or [])))
    if task_id is None and also:
        task_id = also[0]
    also = [x for x in also if x != task_id]
    if task_id is None and not person_ids:
        raise ValueError('업무나 임원 중 하나는 지정하세요.')
    content = (content or '').strip()
    if not content:
        raise ValueError('이력 내용을 입력하세요.')
    if entry_type not in ENTRY_TYPES + AUTO_ENTRY_TYPES:
        raise ValueError(f'알 수 없는 이력 유형: {entry_type}')
    if isinstance(occurred_at, str):
        occurred_at = datetime.fromisoformat(occurred_at) if occurred_at.strip() else None
    with db.transaction(session) as s:
        t = s.get(Task, task_id) if task_id is not None else None
        if task_id is not None and t is None:
            raise ValueError('업무를 찾을 수 없습니다.')
        if person_ids and len(s.scalars(select(Person.id).where(Person.id.in_(person_ids))).all()) != len(person_ids):
            raise ValueError('임원을 찾을 수 없습니다.')
        others = _check_tasks(s, also)
        ref_ids = list(dict.fromkeys(int(r) for r in ref_ids or []))
        if ref_ids and len(s.scalars(select(Reference.id).where(Reference.id.in_(ref_ids))).all()) != len(ref_ids):
            raise ValueError('참고자료를 찾을 수 없습니다.')
        h = HistoryEntry(task_id=task_id, entry_type=entry_type, content=content, author=author or '',
                         title=(title or '').strip(), summary=(summary or '').strip(),
                         keywords=_join_keywords(keywords))
        if occurred_at:
            h.occurred_at = occurred_at
        s.add(h)
        s.flush()
        for pid in person_ids:
            s.add(HistoryPerson(history_id=h.id, person_id=pid))
        for o in others:
            s.add(HistoryTask(history_id=h.id, task_id=o.id))
        for rid in ref_ids:
            s.add(HistoryReference(history_id=h.id, reference_id=rid))
        for x in [t, *others]:
            if x is not None:
                x.updated_at = datetime.now().replace(microsecond=0)
        s.flush()
        return _decorate(s, [_history_dict(h)])[0]


def link_history_task(entry_id: int, task_id: int) -> bool:
    """이미 있는 기록을 업무에 연결 — 업무 없는 기록(동향)이면 그 업무가 기록의 업무가 되고, 아니면 함께 걸친 업무로.
    이미 걸려 있으면 False."""
    with db.transaction() as s:
        h = s.get(HistoryEntry, entry_id)
        if h is None:
            raise ValueError('이력을 찾을 수 없습니다.')
        if s.get(Task, task_id) is None:
            raise ValueError('업무를 찾을 수 없습니다.')
        if h.task_id == task_id or s.get(HistoryTask, (entry_id, task_id)) is not None:
            return False
        if h.task_id is None:
            h.task_id = task_id
        else:
            s.add(HistoryTask(history_id=entry_id, task_id=task_id))
        return True


def reference_history_counts(task_id: int, ref_ids: list[int] | None = None) -> dict[int, int]:
    """참고자료별 기록 수 — 자료가 붙은 이력 + 그 자료를 다룬 기록. ref_ids 를 주면 그 자료들(하위 업무 것 포함)."""
    with db.SessionLocal() as s:
        cond = Reference.id.in_(ref_ids or [0]) if ref_ids is not None else Reference.task_id == task_id
        refs = list(s.scalars(select(Reference).where(cond)))
        ids = [r.id for r in refs]
        out = {r.id: 1 if r.history_id else 0 for r in refs}
        if ids:
            for rid, n in s.execute(select(HistoryReference.reference_id, func.count())
                                    .where(HistoryReference.reference_id.in_(ids))
                                    .group_by(HistoryReference.reference_id)):
                out[rid] += n
        return out


def history_titles(ids) -> dict[int, str]:
    """이력 id → '날짜 제목' (참고자료가 어느 기록에 붙었는지 보여 줄 때)."""
    ids = [i for i in set(ids) if i]
    if not ids:
        return {}
    with db.SessionLocal() as s:
        return {h.id: f"{_dt(h.occurred_at)[:10]} {h.title or (h.content or '')[:30]}"
                for h in s.scalars(select(HistoryEntry).where(HistoryEntry.id.in_(ids)))}


def shared_history_count(a: int, b: int) -> int:
    """두 업무에 함께 걸친 기록 수 (연결 업무 탭)."""
    with db.SessionLocal() as s:
        in_b = select(HistoryEntry.id).where(_history_of_task(b))
        return s.scalar(select(func.count()).select_from(HistoryEntry)
                        .where(_history_of_task(a), HistoryEntry.id.in_(in_b))) or 0


def _check_tasks(s, ids: list[int]) -> list[Task]:
    tasks = list(s.scalars(select(Task).where(Task.id.in_(ids)))) if ids else []
    if len(tasks) != len(set(ids)):
        raise ValueError('업무를 찾을 수 없습니다.')
    return tasks


def _history_of_task(task_id: int):
    """그 업무의 이력 조건 — 업무에 속한 이력 + 다른 업무 이력인데 이 업무에도 걸친 것."""
    return or_(HistoryEntry.task_id == task_id,
               HistoryEntry.id.in_(select(HistoryTask.history_id).where(HistoryTask.task_id == task_id)))


def get_history(entry_id: int | None) -> dict | None:
    if entry_id is None:
        return None
    with db.SessionLocal() as s:
        h = s.get(HistoryEntry, entry_id)
        return _decorate(s, [_history_dict(h)])[0] if h else None


def update_history(entry_id: int, *, session=None, **fields) -> dict:
    """사용자가 이력(원문·유형·일시·제목·요약·키워드·임원·걸친 업무 task_ids)을 고칠 때."""
    with db.transaction(session) as s:
        h = s.get(HistoryEntry, entry_id)
        if h is None:
            raise ValueError('이력을 찾을 수 없습니다.')
        old_content, old_when = h.content, h.occurred_at
        # 이 편집 전에 시작한 AI 작업은 원문·직접 고친 요약을 덮어쓸 수 없다.
        h.ai_token = uuid.uuid4().hex
        if h.ai_status == 'pending':
            h.ai_status, h.ai_error = '', ''
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
            v = datetime.fromisoformat(v) if isinstance(v, str) else v
            # 수정 창의 일시 칸은 분 단위라 초가 빠진다 — 같은 분이면 원래 값(초 포함)을 그대로 둬서
            # 같은 분에 기록된 이력들의 순서가 수정만으로 뒤바뀌지 않게 한다
            if not (h.occurred_at and h.occurred_at.replace(second=0, microsecond=0) == v.replace(second=0, microsecond=0)):
                h.occurred_at = v
        for k in ('title', 'summary'):
            if k in fields:
                setattr(h, k, (fields[k] or '').strip())
        if 'keywords' in fields:
            h.keywords = _join_keywords(fields['keywords'])
        if 'task_ids' in fields:  # 이 이력이 걸친 업무 전체 — 지금 업무가 빠지면 남은 첫 업무가 이력의 업무가 된다
            ids = list(dict.fromkeys(int(v) for v in (fields['task_ids'] or [])))
            _check_tasks(s, ids)
            primary = h.task_id if h.task_id in ids else (ids[0] if ids else None)
            has_people = bool(fields['person_ids']) if 'person_ids' in fields else bool(s.scalar(
                select(func.count()).select_from(HistoryPerson).where(HistoryPerson.history_id == entry_id)))
            if primary is None and not has_people:
                raise ValueError('업무나 임원 중 하나는 지정하세요.')
            if primary != h.task_id:
                h.task_id = primary
                for r in s.scalars(select(Reference).where(Reference.history_id == entry_id)):
                    r.task_id = primary
            for link in s.scalars(select(HistoryTask).where(HistoryTask.history_id == entry_id)):
                s.delete(link)
            s.flush()
            for tid in ids:
                if tid != primary:
                    s.add(HistoryTask(history_id=entry_id, task_id=tid))
        if 'person_ids' in fields:
            ids = sorted({int(p) for p in (fields['person_ids'] or [])})
            if h.task_id is None and not ids:
                raise ValueError('업무 없는 기록(동향)은 임원을 한 명 이상 지정해야 합니다.')
            for link in s.scalars(select(HistoryPerson).where(HistoryPerson.history_id == entry_id)):
                s.delete(link)
            s.flush()
            for pid in ids:
                s.add(HistoryPerson(history_id=entry_id, person_id=pid))
        if h.content != old_content or h.occurred_at != old_when:
            h.ai_todos, h.ai_reference_date = '', ''
        s.flush()
        return _decorate(s, [_history_dict(h)])[0]


def unlink_history_task(entry_id: int, task_id: int) -> None:
    """이 업무에서 이력 빼기 — 다른 업무에도 걸친 이력에서 한 업무만 뗀다. 이력의 업무를 빼면
    함께 걸친 다음 업무가 이력의 업무가 된다(자료도 같이). 걸친 업무가 하나뿐이면 뺄 수 없다(삭제를 쓴다)."""
    with db.SessionLocal() as s:
        h = s.get(HistoryEntry, entry_id)
        if h is None:
            raise ValueError('이력을 찾을 수 없습니다.')
        links = list(s.scalars(select(HistoryTask).where(HistoryTask.history_id == entry_id)
                               .order_by(HistoryTask.task_id)))
        if h.task_id == task_id:
            if not links:
                raise ValueError('이 업무에만 걸린 이력입니다 — 빼려면 삭제하세요.')
            h.task_id = links[0].task_id
            for r in s.scalars(select(Reference).where(Reference.history_id == entry_id)):
                r.task_id = h.task_id
            s.delete(links[0])
        else:
            for link in links:
                if link.task_id == task_id:
                    s.delete(link)
        s.commit()


def begin_history_ai(entry_id: int, status: str = 'pending') -> str | None:
    """새 요청 번호를 저장해 이전 AI 요청의 응답을 무효화한다."""
    token = uuid.uuid4().hex
    with db.transaction() as s:
        result = s.execute(update(HistoryEntry).where(HistoryEntry.id == entry_id).values(
            ai_token=token, ai_status=status, ai_error=''))
        return token if result.rowcount else None


def set_history_ai(entry_id: int, status: str, title: str | None = None, summary: str | None = None,
                   keywords: list[str] | None = None, error: str = '', todos: list[dict] | None = None,
                   *, token: str | None = None, reference_date: str | None = None) -> None:
    """AI 정리 상태/결과 저장. None 인 필드는 기존 값을 유지한다(재생성 중에도 이전 결과가 보이도록)."""
    with db.transaction() as s:
        values = {'ai_status': status, 'ai_error': error or ''}
        if title is not None:
            values['title'] = title
        if summary is not None:
            values['summary'] = summary
        if keywords is not None:
            values['keywords'] = _join_keywords(keywords)
        if todos is not None:
            values['ai_todos'] = json.dumps(todos, ensure_ascii=False)
        if reference_date is not None:
            values['ai_reference_date'] = reference_date
        stmt = update(HistoryEntry).where(HistoryEntry.id == entry_id)
        if token is not None:
            stmt = stmt.where(HistoryEntry.ai_token == token)
        s.execute(stmt.values(**values))


def mark_ai_todo(entry_id: int, idx: int, task_id: int) -> None:
    """AI 가 찾은 할 일 idx 번째를 업무로 만들었다고 표시 — 제목을 고쳐 등록해도 '추가됨'으로 보이게."""
    with db.transaction() as s:
        h = s.get(HistoryEntry, entry_id)
        todos = _load_json_list(h.ai_todos) if h else []
        if 0 <= idx < len(todos) and isinstance(todos[idx], dict):
            todos[idx]['task_id'] = task_id
            h.ai_todos = json.dumps(todos, ensure_ascii=False)


def _decorate(s, entries: list[dict]) -> list[dict]:
    """이력 dict 들에 'refs'(붙은 관련 자료)·'people'(연결된 임원)·'task_title' 을 채운다."""
    ids = [e['id'] for e in entries]
    refs: dict[int, list[dict]] = {i: [] for i in ids}
    people: dict[int, list[dict]] = {i: [] for i in ids}
    titles: dict[int, str] = {}
    also: dict[int, list[dict]] = {i: [] for i in ids}
    if ids:
        for hid, tid, title in s.execute(select(HistoryTask.history_id, Task.id, Task.title)
                                         .join(Task, Task.id == HistoryTask.task_id)
                                         .where(HistoryTask.history_id.in_(ids)).order_by(Task.id)):
            also[hid].append({'id': tid, 'title': title})
        for r in s.scalars(select(Reference).where(Reference.history_id.in_(ids)).order_by(Reference.id)):
            refs[r.history_id].append(_ref_dict(r))
        for hid, r in s.execute(select(HistoryReference.history_id, Reference)  # 이 기록이 다룬 자료
                                .join(Reference, Reference.id == HistoryReference.reference_id)
                                .where(HistoryReference.history_id.in_(ids)).order_by(Reference.id)):
            if all(x['id'] != r.id for x in refs[hid]):
                refs[hid].append(_ref_dict(r))
        for hid, p in s.execute(select(HistoryPerson.history_id, Person).join(Person, Person.id == HistoryPerson.person_id)
                                .where(HistoryPerson.history_id.in_(ids)).order_by(Person.name)):
            people[hid].append({'id': p.id, 'name': p.name, 'position': p.position or ''})
        need_titles = {e['task_id'] for e in entries if e['task_id'] and 'task_title' not in e}
        if need_titles:
            titles = dict(s.execute(select(Task.id, Task.title).where(Task.id.in_(need_titles))).all())
    for e in entries:
        e['refs'] = refs.get(e['id'], [])
        e['people'] = people.get(e['id'], [])
        if 'task_title' not in e:
            e['task_title'] = titles.get(e['task_id'], '') if e['task_id'] else ''
        e['task_title'] = e['task_title'] or ''
        e['also_tasks'] = also.get(e['id'], [])  # 함께 걸친 다른 업무 [{'id', 'title'}]
    return entries


def list_history(task_id: int) -> list[dict]:
    with db.SessionLocal() as s:
        rows = s.scalars(select(HistoryEntry).where(_history_of_task(task_id))
                         .order_by(HistoryEntry.occurred_at.desc(), HistoryEntry.id.desc()))
        return _decorate(s, [_history_dict(h) for h in rows])


def delete_history(entry_id: int) -> None:
    """이력을 지워도 거기 붙은 자료는 업무 참고자료로 남긴다."""
    with db.SessionLocal() as s:
        h = s.get(HistoryEntry, entry_id)
        if h:
            # 예전 DB 에 ALTER 로 추가된 컬럼엔 외래키(ON DELETE SET NULL)가 없을 수 있어 직접 끊는다
            for r in s.scalars(select(Reference).where(Reference.history_id == entry_id)):
                r.history_id = None
            s.delete(h)
            s.commit()


def search_history(query: str = '', entry_types: list[str] | None = None, author: str = '',
                   date_from: str = '', date_to: str = '', task_id: int | None = None,
                   person_id: int | None = None,
                   include_auto: bool = False, limit: int = 200) -> list[dict]:
    """이력 검색. 검색어가 여러 개면 모두 포함(AND)해야 하며, 각 검색어는
    원문·AI 제목·요약·키워드·작성자·업무명 중 어디에든 있으면 된다.
    include_auto=False 면 상태변경/연결/자료 같은 자동 이력은 뺀다."""
    with db.SessionLocal() as s:
        stmt = select(HistoryEntry, Task.title).outerjoin(Task, Task.id == HistoryEntry.task_id)
        other_task = aliased(Task)  # 함께 걸친 업무의 제목 (바깥 Task 와 섞이지 않게 별칭)
        for term in search_terms(query):
            like = f'%{term}%'
            ref_hist = select(Reference.history_id).where(Reference.history_id.is_not(None), or_(
                Reference.title.like(like), Reference.url.like(like), Reference.file_name.like(like),
                Reference.note.like(like)))
            person_hist = select(HistoryPerson.history_id).join(Person, Person.id == HistoryPerson.person_id).where(
                or_(Person.name.like(like), Person.aliases.like(like), Person.position.like(like),
                    Person.org.like(like)))
            stmt = stmt.where(or_(HistoryEntry.content.like(like), HistoryEntry.title.like(like),
                                  HistoryEntry.summary.like(like), HistoryEntry.keywords.like(like),
                                  HistoryEntry.author.like(like), Task.title.like(like),
                                  HistoryEntry.id.in_(select(HistoryTask.history_id)
                                                      .join(other_task, other_task.id == HistoryTask.task_id)
                                                      .where(other_task.title.like(like))),
                                  HistoryEntry.id.in_(ref_hist), HistoryEntry.id.in_(person_hist)))
        if entry_types:
            stmt = stmt.where(HistoryEntry.entry_type.in_(entry_types))
        elif not include_auto:
            stmt = stmt.where(HistoryEntry.entry_type.notin_(AUTO_ENTRY_TYPES))
        if author:
            stmt = stmt.where(HistoryEntry.author == author)
        if task_id is not None:
            stmt = stmt.where(_history_of_task(task_id))
        if person_id is not None:
            stmt = stmt.where(HistoryEntry.id.in_(
                select(HistoryPerson.history_id).where(HistoryPerson.person_id == person_id)))
        if date_from:
            stmt = stmt.where(HistoryEntry.occurred_at >= datetime.fromisoformat(date_from))
        if date_to:  # 그날 하루 전체 포함
            stmt = stmt.where(HistoryEntry.occurred_at < datetime.fromisoformat(date_to) + timedelta(days=1))
        rows = s.execute(stmt.order_by(HistoryEntry.occurred_at.desc(), HistoryEntry.id.desc()).limit(limit))
        return _decorate(s, [{**_history_dict(h), 'task_title': title} for h, title in rows])


def keyword_stats(limit: int = 40, task_id: int | None = None, person_id: int | None = None) -> list[tuple[str, int]]:
    """이력 키워드 빈도 (많이 나온 순)."""
    counts: dict[str, int] = {}
    display: dict[str, str] = {}
    with db.SessionLocal() as s:
        stmt = select(HistoryEntry.keywords).where(HistoryEntry.keywords != '')
        if task_id is not None:
            stmt = stmt.where(_history_of_task(task_id))
        if person_id is not None:
            stmt = stmt.where(HistoryEntry.id.in_(
                select(HistoryPerson.history_id).where(HistoryPerson.person_id == person_id)))
        for (kws,) in s.execute(stmt):
            for k in _tags(kws):
                counts[k.lower()] = counts.get(k.lower(), 0) + 1
                display.setdefault(k.lower(), k)
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    return [(display[k], n) for k, n in ranked]


def history_authors() -> list[str]:
    with db.SessionLocal() as s:
        return sorted({a for (a,) in s.execute(select(HistoryEntry.author).distinct()) if a})


def pending_ai_ids(task_id: int | None = None, person_id: int | None = None) -> list[int]:
    with db.SessionLocal() as s:
        stmt = select(HistoryEntry.id).where(HistoryEntry.ai_status == 'pending')
        if task_id is not None:
            stmt = stmt.where(_history_of_task(task_id))
        if person_id is not None:
            stmt = stmt.where(HistoryEntry.id.in_(
                select(HistoryPerson.history_id).where(HistoryPerson.person_id == person_id)))
        return list(s.scalars(stmt))


def recent_history(limit: int = 20) -> list[dict]:
    """전체 업무의 최근 이력 (업무 제목 포함)."""
    with db.SessionLocal() as s:
        rows = s.execute(select(HistoryEntry, Task.title).outerjoin(Task, Task.id == HistoryEntry.task_id)
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


def add_reference(task_id: int | None, kind: str, title: str = '', url: str = '', note: str = '',
                  file_name: str = '', file_bytes: bytes | None = None, author: str = '',
                  history_id: int | None = None, *, session=None) -> dict:
    """history_id 를 주면 그 이력의 관련 자료로 붙는다 (이력 자체가 기록이므로 '자료' 자동 이력은 남기지 않음)."""
    if kind not in ('link', 'file', 'note'):
        raise ValueError(f'알 수 없는 자료 유형: {kind}')
    title = (title or '').strip()
    url = (url or '').strip()
    if kind == 'link':
        if not url:
            raise ValueError('링크 URL 을 입력하세요.')
        if not re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*://', url) and not is_unc_path(url):
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

    if task_id is None and history_id is None:
        raise ValueError('업무를 지정하세요.')
    with db.transaction(session) as s:
        t = s.get(Task, task_id) if task_id is not None else None
        if task_id is not None and t is None:
            raise ValueError('업무를 찾을 수 없습니다.')
        if history_id is not None:
            h = s.get(HistoryEntry, history_id)
            if h is None or h.task_id != task_id:
                raise ValueError('이력을 찾을 수 없습니다.')
        r = Reference(task_id=task_id, kind=kind, title=title, url=url if kind == 'link' else '',
                      note=note or '', history_id=history_id)
        if kind == 'file':
            folder = str(task_id) if task_id is not None else f'h{history_id}'  # 업무 없는 동향은 이력별 폴더
            rel = os.path.join(folder, f'{uuid.uuid4().hex[:8]}_{_safe_name(file_name)}')
            dest = os.path.join(db.UPLOAD_DIR, rel)
            s.info.setdefault('rollback_callbacks', []).append(lambda: _remove_upload(rel))
            try:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, 'wb') as f:
                    f.write(file_bytes)
            except OSError as e:
                raise ValueError('첨부 파일을 저장하지 못했습니다. 저장을 취소했으니 다시 시도하세요.') from e
            r.file_name, r.file_path = file_name, rel
        s.add(r)
        if history_id is None:
            label = {'link': '링크', 'file': '파일', 'note': '메모'}[kind]
            _log(s, task_id, '자료', f'참고자료 추가 ({label}): {title}', author)
        if t is not None:
            t.updated_at = datetime.now().replace(microsecond=0)
        s.flush()
        return _ref_dict(r)


def list_references(task_id: int | None = None, query: str = '', kind: str = '') -> list[dict]:
    with db.SessionLocal() as s:
        stmt = select(Reference, Task.title).outerjoin(Task, Task.id == Reference.task_id)
        if task_id is not None:
            stmt = stmt.where(Reference.task_id == task_id)
        if kind:
            stmt = stmt.where(Reference.kind == kind)
        if query:
            like = f'%{query.strip()}%'
            stmt = stmt.where(or_(Reference.title.like(like), Reference.note.like(like),
                                  Reference.url.like(like), Reference.file_name.like(like),
                                  Reference.content.like(like), Reference.content_summary.like(like),
                                  Task.title.like(like)))
        rows = s.execute(stmt.order_by(Reference.created_at.desc(), Reference.id.desc()))
        return [{**_ref_dict(r), 'task_title': title or ''} for r, title in rows]


def references_for_task(task_id: int, include_children: bool = True) -> list[dict]:
    """업무 참고자료 탭에 모을 자료 — 이 업무(와 하위 업무)에 직접 등록한 자료, 그 업무들의 기록에 붙은 자료,
    여러 업무에 걸친 기록(history_tasks)에 붙은 자료. 'source': 직접 등록 / 기록 첨부,
    'via_task': 다른 업무(하위 업무·기록의 주 업무)에서 온 자료면 {id, title}, 'via': self / child / shared."""
    scope = task_scope(task_id, include_children)
    with db.SessionLocal() as s:
        shared = set(s.scalars(select(HistoryTask.history_id).where(HistoryTask.task_id.in_(scope))))
        stmt = select(Reference, Task.title).outerjoin(Task, Task.id == Reference.task_id).where(
            or_(Reference.task_id.in_(scope), Reference.history_id.in_(shared) if shared else false()))
        rows = s.execute(stmt.order_by(Reference.created_at.desc(), Reference.id.desc())).all()
        out = []
        for r, title in rows:
            d = {**_ref_dict(r), 'task_title': title or '', 'source': '기록 첨부' if r.history_id else '직접 등록'}
            if r.task_id == task_id:
                d['via'], d['via_task'] = 'self', None
            elif r.task_id in scope:
                d['via'], d['via_task'] = 'child', {'id': r.task_id, 'title': title or ''}
            else:  # 다른 업무가 주 업무인, 이 업무에도 걸친 기록의 자료
                d['via'] = 'shared'
                d['via_task'] = {'id': r.task_id, 'title': title or ''} if r.task_id else None
            out.append(d)
        return out


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


# ── 이력의 관련 자료 링크 ─────────────────────────────────────────────

_URL_IN_TEXT = re.compile(r'https?://[^\s<>"\'\]\)]+')
_LINK_TOKEN = re.compile(r'(https?://\S+|www\.\S+|\\\\\S+|[\w.-]+\.(?:com|net|kr|io|org|co\.kr)(?:/\S*)?)')


def is_unc_path(v: str) -> bool:
    """사내 공유 폴더 경로(\\\\서버\\폴더\\파일). 브라우저 링크로는 안 열리므로 복사용으로 보여준다."""
    return (v or '').startswith('\\\\')


def urls_in_text(text: str) -> list[str]:
    """원문에 들어 있는 http(s) 링크 (문장 끝 마침표·쉼표는 뗀다, 중복 제거)."""
    out = []
    for u in _URL_IN_TEXT.findall(text or ''):
        u = u.rstrip('.,;:!?…')
        if u not in out:
            out.append(u)
    return out


def parse_links(text: str) -> list[dict]:
    """'관련 자료' 입력칸 → [{'title', 'url'}]. 한 줄에 하나, 링크 앞뒤 글자는 제목으로 쓴다.
    예) '회의록 https://conf.../123'  'https://a.b/c'  '\\\\fs01\\팀\\보고서.pptx 최종본'"""
    out = []
    for line in (text or '').splitlines():
        line = line.strip()
        m = _LINK_TOKEN.search(line)
        if not m:
            continue
        url = m.group(1).rstrip('.,;')
        title = (line[:m.start()] + ' ' + line[m.end():]).strip(' -:|·\t')
        out.append({'title': title or url, 'url': url})
    return out


def attach_links(task_id: int | None, history_id: int, links: list[dict], author: str = '', *, session=None) -> int:
    """이력에 링크들을 붙인다. 이미 붙은 같은 URL 은 건너뛴다. 붙인 개수를 돌려준다."""
    with db.transaction(session) as s:
        existing = set(s.scalars(select(Reference.url).where(Reference.history_id == history_id)))
        n = 0
        for link in links:
            url = link['url']
            if url in existing or ('https://' + url) in existing:
                continue
            ref = add_reference(task_id, 'link', title=link.get('title') or url, url=url, author=author,
                                history_id=history_id, session=s)
            existing.add(ref['url'])
            n += 1
        return n


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


# ── 하위 업무(체크리스트) ──────────────────────────────────────────────
# 예전 '할 일'은 업무로 합쳐졌다. 아래 함수들은 업무를 '마감·담당·완료가 있는 체크 항목'으로 다루는
# 얇은 층 — 업무 상세의 하위 업무 목록, '내 업무' 화면, 캘린더, AI 제안 추가가 쓴다.
# (task_id = 상위 업무, done = 상태 '완료')

def _todo_dict(t: Task, parent_title: str | None = None) -> dict:
    today = date.today().isoformat()
    done = t.status == '완료'
    return {
        'id': t.id, 'task_id': t.parent_id, 'task_title': parent_title or '', 'title': t.title,
        'status': t.status, 'due_date': t.due_date or '', 'done': done, 'done_at': _dt(t.done_at),
        'assignee_id': t.assignee_id or '', 'assignee_name': t.owner or '', 'assignees': _assignee_list(t),
        'created_by': t.created_by or '', 'source_history_id': t.source_history_id,
        'source_mail_id': t.source_mail_id, 'created_at': _dt(t.created_at),
        'overdue': bool(not done and t.due_date and t.due_date < today),
    }


def create_todo(title: str, task_id: int | None = None, due_date: str = '', assignee_id: str = '',
                assignee_name: str = '', created_by: str = '', source_history_id: int | None = None,
                source_mail_id: int | None = None, assignees: list | None = None, *, session=None,
                source_mail_key: str = '') -> dict:
    """체크 항목처럼 업무를 하나 만든다 — task_id 가 있으면 그 업무의 하위 업무.
    담당자: assignees([{'name', 'user_id'}]) 또는 assignee_name(쉼표로 여러 명) + assignee_id(한 명일 때).
    source_history_id: AI 가 이 할 일을 찾은 기록 — 그 기록을 새 업무에도 걸어 새 업무 이력에서 보이게 한다."""
    title = (title or '').strip()
    if not title:
        raise ValueError('업무 내용을 입력하세요.')
    due_date = _check_date(due_date)
    with db.transaction(session) as s:
        parent = s.get(Task, task_id) if task_id else None
        if task_id and parent is None:
            raise ValueError('상위 업무를 찾을 수 없습니다.')
        t = Task(title=title, parent_id=task_id or None, due_date=due_date, status='진행중',
                 owner='', assignee_id='', created_by=created_by or '',
                 source_history_id=source_history_id, source_mail_id=source_mail_id, source_mail_key=source_mail_key,
                 description='', tags='',
                 start_date='')
        s.add(t)
        s.flush()
        _set_assignees(s, t, assignees_of(assignees, assignee_name, assignee_id))
        if source_history_id and s.get(HistoryEntry, source_history_id) is not None:
            s.add(HistoryTask(history_id=source_history_id, task_id=t.id))  # 할 일을 찾은 기록이 새 업무 이력에도
        if parent:
            _log(s, parent.id, '할일', f'하위 업무 추가: {title}', created_by)
        s.flush()
        return _todo_dict(t, parent.title if parent else '')


def assignees_of(assignees: list | None, assignee_name: str = '', assignee_id: str = '') -> list[dict]:
    """예전 방식(이름 + user_id 하나)과 새 방식(담당자 목록)을 담당자 목록으로."""
    if assignees is not None:
        return parse_assignees(assignees)
    people = parse_assignees(assignee_name or '')
    if assignee_id and not people:  # 이름 없이 user_id 만 — id 를 이름 자리에
        people = [{'name': assignee_id[:100], 'user_id': assignee_id}]
    elif assignee_id and len(people) == 1:
        people[0]['user_id'] = assignee_id
    return people


def update_todo(todo_id: int, **fields) -> dict:
    if 'task_id' in fields:
        fields['parent_id'] = fields.pop('task_id')
    if {'assignee_name', 'assignee_id'} & fields.keys():
        fields['assignees'] = assignees_of(None, fields.pop('assignee_name', ''), fields.pop('assignee_id', ''))
    if fields:
        update_task(todo_id, **fields)
    with db.SessionLocal() as s:
        t = s.get(Task, todo_id)
        parent = s.get(Task, t.parent_id) if t.parent_id else None
        return _todo_dict(t, parent.title if parent else '')


def set_todo_done(todo_id: int, done: bool, by: str = '') -> bool:
    """체크/체크 해제 = 상태 완료/진행중. 상위 업무가 있으면 그 이력에도 남긴다. 바뀌었으면 True."""
    with db.SessionLocal() as s:
        t = s.get(Task, todo_id)
        if t is None or (t.status == '완료') == bool(done):
            return False
        new = '완료' if done else '진행중'
        _log(s, t.id, '상태변경', f'{t.status} → {new}', by)
        t.status = new
        t.done_at = datetime.now().replace(microsecond=0) if done else None
        if t.parent_id:
            _log(s, t.parent_id, '할일', f"{'완료' if done else '완료 취소'}: {t.title}", by)
        s.commit()
        return True


def can_delete_quick(task_id: int) -> bool:
    """체크 목록에서 바로 지워도 되는 업무인지 — 하위 업무·직접 쓴 기록·참고자료가 없을 때만."""
    with db.SessionLocal() as s:
        if s.scalar(select(func.count()).select_from(Task).where(Task.parent_id == task_id)):
            return False
        if s.scalar(select(func.count()).select_from(HistoryEntry).where(
                HistoryEntry.task_id == task_id, HistoryEntry.entry_type.notin_(AUTO_ENTRY_TYPES))):
            return False
        return not s.scalar(select(func.count()).select_from(Reference).where(Reference.task_id == task_id))


def delete_todo(todo_id: int) -> None:
    """체크 목록에서 지우기 — 기록·자료·하위 업무가 있는 업무는 업무 화면에서 지워야 한다."""
    if not can_delete_quick(todo_id):
        raise ValueError('기록·참고자료·하위 업무가 있는 업무는 업무 화면에서 삭제하세요.')
    delete_task(todo_id)


def list_todos(task_id: int | None = None, assignee_id: str | None = None, done: bool | None = None,
               date_from: str = '', date_to: str = '', done_since: str = '', assignee_name: str = '') -> list[dict]:
    """업무를 체크 목록으로. task_id: 그 업무의 하위 업무만. assignee_id: 내 업무 — 담당자 중에 user_id 가
    같거나, user_id 없이 이름(assignee_name)만 같은 사람이 있는 업무.
    정렬: 미완료 먼저, 마감일 빠른 순(날짜 없는 것은 뒤), 완료는 최근 완료 순."""
    parent = aliased(Task)
    with db.SessionLocal() as s:
        stmt = select(Task, parent.title).outerjoin(parent, parent.id == Task.parent_id)
        if task_id is not None:
            stmt = stmt.where(Task.parent_id == task_id)
        if assignee_id is not None:
            stmt = stmt.where(_mine(assignee_id, assignee_name))
        if done is not None:
            stmt = stmt.where((Task.status == '완료') if done else (Task.status != '완료'))
        if date_from:
            stmt = stmt.where(Task.due_date >= date_from)
        if date_to:
            stmt = stmt.where(Task.due_date <= date_to, Task.due_date != '')
        if done_since:
            stmt = stmt.where(or_(Task.status != '완료', Task.done_at >= datetime.fromisoformat(done_since)))
        rows = [_todo_dict(t, title) for t, title in s.execute(stmt)]
    open_ = sorted([r for r in rows if not r['done']], key=lambda r: (r['due_date'] == '', r['due_date'], r['id']))
    closed = sorted([r for r in rows if r['done']], key=lambda r: r['done_at'], reverse=True)
    return open_ + closed


def todo_titles_for_history(entry_id: int) -> set[str]:
    """이 이력의 AI 제안으로 이미 만든 업무 제목들."""
    return set(todos_for_history(entry_id))


def todos_for_history(entry_id: int) -> dict[str, int]:
    """이 이력의 AI 제안으로 만든 업무 — {제목: 업무 id} (같은 제목이 여럿이면 먼저 만든 것)."""
    with db.SessionLocal() as s:
        rows = s.execute(select(Task.title, Task.id).where(Task.source_history_id == entry_id)
                         .order_by(Task.id.desc()))
        return dict(rows.all())


def task_sources(task_id: int) -> dict:
    """업무의 출처 — {'history': AI 가 할 일을 찾은 메모(이력) | None, 'mails': 이 업무와 연결된 받은 메일들,
    'mail': 그 첫 메일 | None}. 메일마다 'created' = 그 메일로 만든 업무인지(아니면 나중에 연결한 것).
    메일로 만든 업무는 source_mail_id 로, 나중에 연결한 메일은 받은 메일의 '연결된 업무' 목록으로 찾는다."""
    with db.SessionLocal() as s:
        t = s.get(Task, task_id)
        if t is None:
            return {'history': None, 'mail': None, 'mails': []}
        hid, mid = t.source_history_id, t.source_mail_id
        linked = [m for m in s.scalars(select(InboundMail).where(InboundMail.task_ids.like(f'%{task_id}%'))
                                       .order_by(InboundMail.id))
                  if str(task_id) in (m.task_ids or '').split(',')]
        src = s.get(InboundMail, mid) if mid else None
        ordered = ([src] if src else []) + [m for m in linked if src is None or m.id != src.id]
        mails = [{'id': m.id, 'subject': m.subject or '(제목 없음)', 'sender': m.sender or '',
                  'mail_date': m.mail_date or '', 'body': m.body or '', 'created': src is not None and m.id == src.id}
                 for m in ordered]
    return {'history': get_history(hid), 'mails': mails, 'mail': mails[0] if mails else None}


def clear_source_mail(task_id: int, mail_id: int, *, session=None) -> None:
    """메일과 연결을 끊을 때 — 그 메일로 만든 업무였으면 출처 표시도 지운다."""
    with db.transaction(session) as s:
        t = s.get(Task, task_id)
        if t is not None and t.source_mail_id == mail_id:
            t.source_mail_id = None
            t.source_mail_key = ''
            s.flush()


# ── 캘린더 ────────────────────────────────────────────────────────────

def month_range(year: int, month: int) -> tuple[str, str]:
    last = calendar.monthrange(year, month)[1]
    return f'{year:04d}-{month:02d}-01', f'{year:04d}-{month:02d}-{last:02d}'


def calendar_month(year: int, month: int) -> dict[str, dict]:
    """{'YYYY-MM-DD': {'todos': [마감인 업무], 'tasks_due': [] (예전 호환), 'tasks_start': [...], 'history': n}}"""
    first, last = month_range(year, month)
    days: dict[str, dict] = {}

    def day(d):
        return days.setdefault(d, {'todos': [], 'tasks_due': [], 'tasks_start': [], 'history': 0})

    for t in list_todos(date_from=first, date_to=last):  # 마감인 업무 (체크 목록)
        day(t['due_date'])['todos'].append(t)
    with db.SessionLocal() as s:
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


# ── 임원 ─────────────────────────────────────────────────────────────

def _parse_day(v: str) -> date | None:
    try:
        return date.fromisoformat((v or '')[:10])
    except ValueError:
        return None


def promotion_ref_date(today: date) -> date:
    """연차 기준일 — Researcher-board(researcher_profile_export)와 같은 규칙: 2027-03-01부터 오늘을 지나지 않은 첫 3/1."""
    ref = date(2027, 3, 1)
    while ref < today:
        ref = ref.replace(year=ref.year + 1)
    return ref


def hr_figures(birth_year: str, hire_date: str, promotion_date: str, today: date | None = None) -> dict:
    """나이(올해 - 출생연도), 연차(승격일부터 연차 기준일까지 햇수, 최소 1 — People in SAIT 와 같은 계산),
    근속(입사일부터 오늘까지 햇수, 소수 첫째 자리). 계산할 수 없으면 None."""
    today = today or date.today()
    age = today.year - int(birth_year) if (birth_year or '').isdigit() else None
    promo, hire = _parse_day(promotion_date), _parse_day(hire_date)
    years = max(1, math.floor((promotion_ref_date(today) - promo).days / 365 * 10) / 10) if promo else None
    tenure = round((today - hire).days / 365, 1) if hire else None
    return {'age': age, 'grade_years': int(years) if years is not None else None, 'tenure': tenure}


def _person_dict(p: Person) -> dict:
    hr = {'employment_status': p.employment_status or '', 'birth_year': p.birth_year or '',
          'hire_date': p.hire_date or '', 'promotion_date': p.promotion_date or '', 'education': p.education or '',
          'hr_state': p.hr_state or ''}
    return {
        'id': p.id, 'name': p.name, 'position': p.position or '', 'org': p.org or '',
        'aliases': _tags(p.aliases), 'note': p.note or '', 'active': bool(p.active),
        'source_id': p.source_id or '', 'synced_at': _dt(p.synced_at),
        'created_at': _dt(p.created_at), 'updated_at': _dt(p.updated_at),
        **hr, **hr_figures(hr['birth_year'], hr['hire_date'], hr['promotion_date']),
        # 재직 중 = People in SAIT 현재 임원, 또는 사번 없이 직접 등록해 추적 중인 임원
        'employed': hr['hr_state'] == 'current' or (hr['hr_state'] == '' and bool(p.active)),
    }


def create_person(name: str, position: str = '', org: str = '', aliases: list[str] | str = '',
                  note: str = '') -> dict:
    name = (name or '').strip()
    if not name:
        raise ValueError('이름을 입력하세요.')
    with db.SessionLocal() as s:
        p = Person(name=name, position=(position or '').strip(), org=(org or '').strip(),
                   aliases=_join_keywords(aliases), note=note or '')
        s.add(p)
        s.commit()
        return _person_dict(p)


def update_person(person_id: int, **fields) -> dict:
    with db.SessionLocal() as s:
        p = s.get(Person, person_id)
        if p is None:
            raise ValueError('임원을 찾을 수 없습니다.')
        if 'name' in fields:
            if not (fields['name'] or '').strip():
                raise ValueError('이름을 입력하세요.')
            p.name = fields['name'].strip()
        for k in ('position', 'org', 'note'):
            if k in fields:
                setattr(p, k, (fields[k] or '').strip())
        if 'aliases' in fields:
            p.aliases = _join_keywords(fields['aliases'])
        if 'active' in fields:
            p.active = bool(fields['active'])
        s.commit()
        return _person_dict(p)


def get_person(person_id: int) -> dict | None:
    with db.SessionLocal() as s:
        p = s.get(Person, person_id)
        return _person_dict(p) if p else None


def delete_person(person_id: int) -> None:
    """이 임원에게만 연결된(업무도 없는) 동향이 있으면 지우지 않는다 — 기록이 어디에도 안 보이게 되므로.
    더 추적하지 않을 임원은 '추적 중지'(active=False)를 쓴다."""
    with db.SessionLocal() as s:
        p = s.get(Person, person_id)
        if p is None:
            return
        mine = select(HistoryPerson.history_id).where(HistoryPerson.person_id == person_id)
        others = select(HistoryPerson.history_id).where(HistoryPerson.person_id != person_id)
        orphan = s.scalar(select(func.count()).select_from(HistoryEntry).where(
            HistoryEntry.id.in_(mine), HistoryEntry.task_id.is_(None), HistoryEntry.id.notin_(others)))
        if orphan:
            raise ValueError(f'이 임원에게만 연결된 동향 {orphan}건이 있어 삭제할 수 없습니다. '
                             '"추적 중지"를 쓰거나 동향을 먼저 정리하세요.')
        for link in s.scalars(select(HistoryPerson).where(HistoryPerson.person_id == person_id)):
            s.delete(link)
        s.delete(p)
        s.commit()


def list_career(person_id: int) -> list[dict]:
    """임원 경력 이력 (People in SAIT 직급·소속 변천·인사발령), 최근 것이 위로."""
    with db.SessionLocal() as s:
        rows = s.scalars(select(PersonCareer).where(PersonCareer.person_id == person_id)
                         .order_by(PersonCareer.date.desc(), PersonCareer.id.desc()))
        return [{'kind': c.kind, 'date': c.date, 'position': c.position or '', 'org': c.org or '',
                 'title': c.title or '', 'assignment': c.assignment or ''} for c in rows]


def list_people(query: str = '', include_inactive: bool = False, employed_only: bool = False,
                order: str = 'recent') -> list[dict]:
    """임원 목록 + 연결된 기록 수·마지막 기록 일시.
    employed_only: 재직 중인 임원만(퇴직·전출 제외 — 추적 중지 여부와 무관).
    order: 'recent' 최근 기록된 사람이 위로 / 'rank' 직위 순(EXEC_ORG_POSITIONS) → 이름."""
    with db.SessionLocal() as s:
        stats = (select(HistoryPerson.person_id, func.count().label('n'),
                        func.max(HistoryEntry.occurred_at).label('last'))
                 .join(HistoryEntry, HistoryEntry.id == HistoryPerson.history_id)
                 .group_by(HistoryPerson.person_id).subquery())
        stmt = select(Person, stats.c.n, stats.c.last).outerjoin(stats, stats.c.person_id == Person.id)
        if not include_inactive:
            stmt = stmt.where(Person.active.is_(True))
        for term in search_terms(query):
            like = f'%{term}%'
            stmt = stmt.where(or_(Person.name.like(like), Person.position.like(like), Person.org.like(like),
                                  Person.aliases.like(like), Person.note.like(like), Person.source_id.like(like),
                                  Person.education.like(like)))
        rows = [{**_person_dict(p), 'count': n or 0, 'last_at': _dt(last) if last else ''}
                for p, n, last in s.execute(stmt)]
    if employed_only:
        rows = [r for r in rows if r['employed']]
    if order == 'rank':
        from services.people_sync import exec_positions
        rank = {x.lower(): i for i, x in enumerate(exec_positions())}
        return sorted(rows, key=lambda r: (not r['employed'], rank.get(r['position'].lower(), len(rank)), r['name']))
    # 최근 기록 순, 기록 없는 사람은 뒤에서 이름순
    recent = sorted([r for r in rows if r['last_at']], key=lambda r: r['last_at'], reverse=True)
    return recent + sorted([r for r in rows if not r['last_at']], key=lambda r: r['name'])


def detect_people(text: str) -> list[int]:
    """원문에 이름이나 별칭이 나오는 (추적 중인) 임원 id. 두 글자 미만 별칭은 오탐이 많아 쓰지 않는다."""
    text = text or ''
    found = []
    with db.SessionLocal() as s:
        for p in s.scalars(select(Person).where(Person.active.is_(True))):
            names = [p.name] + _tags(p.aliases)
            if any(len(n) >= 2 and n in text for n in names):
                found.append(p.id)
    return found
