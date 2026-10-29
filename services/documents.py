"""문서별 판 이력 — 같은 문서(파일 이름에서 날짜·_r1·_v2·최종 같은 꼬리표를 뗀 열쇠, doc_versions.parse_name)가
메일 첨부와 직접 올린 파일(업무·기록 참고자료)로 여러 번 오가며 바뀌어 온 과정을 한 문서로 모은다.

문서: {'key', 'base', 'ext', 'versions': [판 — 오래된 판부터], 'latest', 'n_versions', 'updated', 'insight'}
판: {'kind': 'mail'|'ref', 'id', 'filename', 'href', 'date', 'version', 'dup', 'latest', 'source', 'insight', ...}
  mail — 'mail_id', 'mail_subject' / ref — 'ref_id', 'history_title', 'via_task' (하위 업무 등에서 온 자료)
판 순서와 번호는 doc_versions 와 같은 규칙(이름 속 날짜 → 판 번호 → 받은·올린 날짜), 내용이 같은 메일 첨부는 같은 판.
"""
import os

from services import db, doc_versions, store


def _ref_version(r: dict, hist_titles: dict, extra: dict) -> dict:
    """extra: 올린 파일 id → {'sha256', 'insight'} (DB 칸 — store 의 자료 사전에는 없는 것)."""
    name = r['file_name'] or r['title']
    p = doc_versions.parse_name(name)
    path = os.path.join(db.UPLOAD_DIR, r['file_path']) if r.get('file_path') else ''
    more = extra.get(r['id'], {})
    return {'kind': 'ref', 'id': f"r{r['id']}", 'ref_id': r['id'], 'filename': name, 'href': f"/files/{r['id']}",
            'size': os.path.getsize(path) if path and os.path.isfile(path) else 0, 'sha256': more.get('sha256', ''),
            'key': more.get('doc_key') or p['key'], 'base': p['base'], 'name_date': p['date'], 'name_version': p['version'],
            'date': (r.get('created_at') or '')[:10], 'received': r.get('created_at') or '',
            'source': '기록 첨부' if r.get('history_id') else '직접 등록',
            'history_title': hist_titles.get(r.get('history_id'), ''), 'via_task': r.get('via_task'),
            'insight': more.get('insight')}


def _ref_extra(ref_ids) -> dict:
    from sqlalchemy import select
    from services import doc_summary
    from services.db import Reference
    ids = list(ref_ids)
    if not ids:
        return {}
    with db.SessionLocal() as s:
        return {r.id: {'sha256': r.sha256 or '', 'doc_key': r.doc_key or '', 'insight': doc_summary.insight(r)}
                for r in s.scalars(select(Reference).where(Reference.id.in_(ids)))}


def _mail_version(v: dict) -> dict:
    return {**v, 'kind': 'mail', 'id': f"m{v['id']}", 'att_id': v['id'], 'href': f"/mail-files/{v['id']}",
            'key': v['doc_key'], 'date': v['mail_date'] or v['received'][:10], 'source': '메일 첨부'}


def _order(v: dict) -> tuple:
    d = v['name_date']
    if len(d) == 5 and v['date']:  # 연도 없는 이름 속 날짜는 받은·올린 해로
        d = f"{v['date'][:4]}-{d}"
    return (d or v['date'], v['name_version'], v['date'], v['received'], v['id'])


def _document(key: str, versions: list[dict]) -> dict:
    versions = sorted(versions, key=_order)
    seen, n = {}, 0
    for v in versions:
        if v['sha256'] and v['sha256'] in seen:
            v['version'], v['dup'] = seen[v['sha256']], True
        else:
            n += 1
            v['version'], v['dup'] = n, False
            if v['sha256']:
                seen[v['sha256']] = n
    for v in versions:
        v['latest'] = v['version'] == n
    latest = versions[-1]
    insight = next((v['insight'] for v in reversed(versions) if v['insight'] and v['insight']['analyzed']), None)
    return {'key': key, 'base': latest['base'], 'ext': key.rsplit('.', 1)[-1] if '.' in key else '',
            'versions': versions, 'latest': latest, 'n_versions': n,
            'updated': max(v['date'] for v in versions), 'insight': insight}


def _merge(mail_groups: list[dict], ref_files: list[dict]) -> list[dict]:
    by_key: dict[str, list[dict]] = {}
    for g in mail_groups:
        by_key.setdefault(g['doc_key'], []).extend(_mail_version(v) for v in g['versions'])
    hist_titles = store.history_titles(r['history_id'] for r in ref_files)
    extra = _ref_extra(r['id'] for r in ref_files)
    for r in ref_files:
        v = _ref_version(r, hist_titles, extra)
        by_key.setdefault(v['key'], []).append(v)
    docs = [_document(k, vs) for k, vs in by_key.items()]
    return sorted(docs, key=lambda d: (d['updated'], d['latest']['received']), reverse=True)


def timeline(key: str) -> list[dict]:
    """한 문서의 모든 판 (메일 첨부·올린 파일, 업무와 상관없이) — 오래된 판부터. 앞 판 찾기·비교에 쓴다."""
    from sqlalchemy import select
    from services.db import Reference
    if not key:
        return []
    with db.SessionLocal() as s:
        mail = doc_versions._rows(s, {key}).get(key, [])
        ref_ids = list(s.scalars(select(Reference.id).where(Reference.kind == 'file', Reference.doc_key == key)))
    refs = [store.get_reference(i) for i in ref_ids]
    docs = _merge([{'doc_key': key, 'versions': mail}] if mail else [], [r for r in refs if r])
    return docs[0]['versions'] if docs else []


def for_task(task_id: int, include_children: bool = True) -> list[dict]:
    """업무(와 하위 업무)의 문서 — 연결된 메일·업무를 만든 메일의 첨부와 이 업무들에 올린 파일, 문서마다 모든 판."""
    mail_groups = doc_versions.for_task(task_id, include_children)
    refs = [r for r in store.references_for_task(task_id, include_children) if r['kind'] == 'file']
    return _merge(mail_groups, refs)


def search(query: str = '', limit: int = 100) -> list[dict]:
    """전체 문서 — 메일 첨부(파일 이름·요약·태그·본문 검색)와 올린 파일(제목·파일 이름·업무명 검색)을 문서별로.
    걸린 문서는 검색어가 없는 판까지 모든 판을 보여 준다."""
    from sqlalchemy import select
    from services import doc_summary
    from services.db import Reference
    doc_summary.ensure_reference_keys()
    keys = [d['file']['doc_key'] for d in doc_versions.latest_documents(query, limit)]
    hit = [r['id'] for r in store.list_references(query=query or '', kind='file')]
    with db.SessionLocal() as s:
        keys += [k for k in s.scalars(select(Reference.doc_key).where(Reference.id.in_(hit or [0]))) if k]
        keys = list(dict.fromkeys(keys))
        groups = doc_versions._rows(s, set(keys)) if keys else {}
        ref_ids = list(s.scalars(select(Reference.id).where(Reference.kind == 'file',
                                                            Reference.doc_key.in_(keys or ['']))))
    refs = [r for r in (store.get_reference(i) for i in ref_ids) if r]
    mail_groups = [{'doc_key': k, 'versions': groups[k]} for k in keys if k in groups]
    return _merge(mail_groups, refs)[:limit]


# ── 사용자가 합치고 나누기 ─────────────────────────────────────────
# 이름이 조금 달라 따로 묶인 같은 문서는 합치고(merge), 잘못 묶인 판은 떼어 낸다(split). 합치면 그 이름 규칙의
# 열쇠를 DocKeyAlias 에 남겨, 앞으로 같은 이름 규칙으로 오는 메일 첨부·올린 파일도 합친 문서로 들어간다.

def _fill_keys() -> None:
    from services import doc_summary
    doc_summary.ensure_reference_keys()  # 문서 열쇠 칸이 생기기 전에 올린 파일


def _set_key(s, key: str, new: str, ids: list[str] | None = None) -> int:
    """문서 key 의 판들(ids 를 주면 그 판만 — 'm12'·'r5')을 new 문서로. 옮긴 수."""
    from sqlalchemy import update
    from services.db import MailAttachment, Reference
    n = 0
    for model, prefix in ((MailAttachment, 'm'), (Reference, 'r')):
        q = update(model).where(model.doc_key == key)
        if ids is not None:
            nums = [int(i[1:]) for i in ids if i[:1] == prefix and i[1:].isdigit()]
            if not nums:
                continue
            q = q.where(model.id.in_(nums))
        n += s.execute(q.values(doc_key=new)).rowcount or 0
    return n


def merge(src: str, dst: str) -> int:
    """문서 src 의 모든 판을 문서 dst 로 합친다 (dst 의 이름·판 순서 규칙으로 다시 매겨진다). 옮긴 판 수."""
    from sqlalchemy import delete, update
    from services.db import DocKeyAlias
    if not src or not dst or src == dst:
        raise ValueError('합칠 문서를 고르세요.')
    _fill_keys()
    with db.SessionLocal() as s:
        dst = doc_versions.resolve(s, dst)
        if dst == src:
            raise ValueError('같은 문서입니다.')
        n = _set_key(s, src, dst)
        s.execute(delete(DocKeyAlias).where(DocKeyAlias.key.in_([src, dst])))  # dst → src 같은 고리도 끊는다
        s.execute(update(DocKeyAlias).where(DocKeyAlias.target == src).values(target=dst))
        s.add(DocKeyAlias(key=src, target=dst))
        s.commit()
    return n


def split(key: str, version_ids: list[str], name: str) -> str:
    """문서 key 에서 고른 판들을 떼어 name 이라는 새 문서로 (같은 이름의 문서가 이미 있으면 그 문서로).
    떼어 낸 문서 열쇠를 돌려준다. 그 이름 규칙이 다른 문서로 합쳐져 있었으면 합친 것을 푼다."""
    from sqlalchemy import delete
    from services.db import DocKeyAlias
    name = (name or '').strip()
    if not version_ids:
        raise ValueError('떼어 낼 판을 고르세요.')
    if not name:
        raise ValueError('새 문서 이름을 적으세요.')
    ext = key.rsplit('.', 1)[-1] if '.' in key else ''
    p = doc_versions.parse_name(name if not ext or name.lower().endswith(f'.{ext}') else f'{name}.{ext}')
    new = p['key'][:300]
    _fill_keys()
    if new == key:
        raise ValueError('지금 문서와 같은 이름입니다. 다른 이름을 적으세요.')
    with db.SessionLocal() as s:
        s.execute(delete(DocKeyAlias).where(DocKeyAlias.key == new))
        if not _set_key(s, key, new, version_ids):
            raise ValueError('옮길 판을 찾지 못했습니다.')
        s.commit()
    return new


def split_suggestion(key: str, version: dict) -> str:
    """떼어 낼 때 기본 이름 — 그 판의 파일 이름이 원래 다른 문서였으면 그 이름, 아니면 '이름 2'."""
    p = doc_versions.parse_name(version['filename'])
    return p['base'] if p['key'] != key else f"{p['base']} 2"


def catalog(exclude: str = '', limit: int = 500) -> list[dict]:
    """합칠 대상 고르기용 — 모든 문서 [{'key', 'label'}] (최근 것부터)."""
    from sqlalchemy import func, select
    from services.db import InboundMail, MailAttachment, Reference
    _fill_keys()
    with db.SessionLocal() as s:
        rows = {}
        for key, name, n, when in s.execute(
                select(MailAttachment.doc_key, func.max(MailAttachment.filename), func.count(),
                       func.max(InboundMail.received_at))
                .join(InboundMail, InboundMail.id == MailAttachment.mail_id).group_by(MailAttachment.doc_key)):
            rows[key] = [name, n, when]
        for key, name, n, when in s.execute(
                select(Reference.doc_key, func.max(Reference.file_name), func.count(), func.max(Reference.created_at))
                .where(Reference.kind == 'file', Reference.doc_key != '').group_by(Reference.doc_key)):
            if key in rows:
                rows[key][1] += n
                rows[key][2] = max(filter(None, [rows[key][2], when]), default=None)
            else:
                rows[key] = [name, n, when]
    out = []
    for key, (name, n, when) in rows.items():
        if not key or key == exclude:
            continue
        p = doc_versions.parse_name(name or key)
        out.append({'key': key, 'label': f"{p['base']}{'.' + p['ext'] if p['ext'] else ''} — 파일 {n}개"
                                         + (f" · {when:%Y-%m-%d}" if when else ''), 'when': when})
    out.sort(key=lambda d: (d['when'] is not None, d['when'] or 0), reverse=True)
    return [{'key': d['key'], 'label': d['label']} for d in out[:limit]]
