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
            'insight': more.get('insight'), 'excluded': more.get('excluded', False)}


def _ref_extra(ref_ids) -> dict:
    from sqlalchemy import select
    from services import doc_summary
    from services.db import Reference
    ids = list(ref_ids)
    if not ids:
        return {}
    from sqlalchemy import func
    from sqlalchemy.orm import defer
    with db.SessionLocal() as s:
        return {r.id: {'sha256': r.sha256 or '', 'doc_key': r.doc_key or '', 'insight': doc_summary.insight(r, n or 0),
                       'excluded': bool(r.excluded)}
                for r, n in s.execute(select(Reference, func.length(Reference.text)).options(defer(Reference.text))
                                      .where(Reference.id.in_(ids)))}


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
    latest = doc_versions.latest_version(versions)
    by_version = sorted(reversed(versions), key=lambda v: v['version'], reverse=True)
    insight = next((v['insight'] for v in by_version if v['insight'] and v['insight']['analyzed']), None)
    return {'key': key, 'base': latest['base'], 'ext': key.rsplit('.', 1)[-1] if '.' in key else '',
            'versions': versions, 'latest': latest, 'n_versions': n,
            'updated': max(v['date'] for v in versions), 'insight': insight}


def excluded_keys() -> set[str]:
    from sqlalchemy import select
    from services.db import DocExclusion
    with db.SessionLocal() as s:
        return set(s.scalars(select(DocExclusion.key)))


def _merge(mail_groups: list[dict], ref_files: list[dict]) -> list[dict]:
    """판 이력에서 뺀 판·문서(exclude)와 무시한 메일의 첨부는 빼고 묶는다."""
    by_key: dict[str, list[dict]] = {}
    for g in mail_groups:
        by_key.setdefault(g['doc_key'], []).extend(_mail_version(v) for v in g['versions'])
    hist_titles = store.history_titles(r['history_id'] for r in ref_files)
    extra = _ref_extra(r['id'] for r in ref_files)
    for r in ref_files:
        v = _ref_version(r, hist_titles, extra)
        by_key.setdefault(v['key'], []).append(v)
    skip = excluded_keys()
    docs = [_document(k, kept) for k, vs in by_key.items() if k not in skip
            for kept in [[v for v in vs if not v.get('excluded') and not v.get('mail_ignored')]] if kept]
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
    refs = store.get_references(ref_ids)
    docs = _merge([{'doc_key': key, 'versions': mail}] if mail else [], refs)
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
    refs = store.get_references(ref_ids)
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
    invalidate()
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
    invalidate()
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


# ── 미리 만들어 두기 (참고자료 화면) ──────────────────────────────────
# 전체 문서 목록은 모든 메일 첨부·올린 파일을 판으로 묶어야 해서 느리다. 쉬는 시간(doc_worker)에 문서마다 판 이력을
# document_cache 에 JSON 으로 만들어 두고, 화면은 그것을 읽는다. 화면을 열 때는 그 뒤로 새로 온·새로 요약한 파일의
# 문서만 다시 만든다(빠름). 지우기·합치기처럼 몇 문서가 바뀌었는지 모르는 변화는 invalidate() → 다음에 전부 다시.

FULL_EVERY = 30 * 60  # 쉬는 시간에 이 간격(초)마다 전부 다시 (업무 이름이 바뀐 것 등까지)
SEARCH_TEXT_PER_VERSION = 3000  # 판마다 본문 앞부분을 검색용으로


def _stamp(s) -> dict:
    from sqlalchemy import func, select
    from services.db import DocKeyAlias, MailAttachment, Reference

    def agg(model, *cond):
        n, mx, an = s.execute(select(func.count(), func.max(model.id), func.max(model.analyzed_at)).where(*cond)).one()
        return [n or 0, mx or 0, an.isoformat() if an else '']
    from services.db import InboundMail
    return {'mail': agg(MailAttachment), 'ref': agg(Reference, Reference.kind == 'file'),
            'alias': list(s.execute(select(func.count(), func.max(DocKeyAlias.id))).one()),
            # 메일을 무시하거나 되살리면 그 첨부가 빠지거나 다시 들어온다
            'ignored': list(s.execute(select(func.count(), func.coalesce(func.sum(InboundMail.id), 0))
                                      .where(InboundMail.status == 'ignored')).one())}


def _texts(s, keys: set[str]) -> dict[str, list[str]]:
    """문서별 검색할 글 — 판마다 본문 앞부분, 올린 파일의 제목·메모·본문."""
    from sqlalchemy import func, select
    from services.db import MailAttachment, Reference
    out: dict[str, list[str]] = {}
    n = SEARCH_TEXT_PER_VERSION
    for k, t in s.execute(select(MailAttachment.doc_key, func.substr(MailAttachment.text, 1, n))
                          .where(MailAttachment.doc_key.in_(keys))):
        out.setdefault(k, []).append(t or '')
    for k, title, note, t in s.execute(select(Reference.doc_key, Reference.title, Reference.note,
                                              func.substr(Reference.text, 1, n))
                                       .where(Reference.kind == 'file', Reference.doc_key.in_(keys))):
        out.setdefault(k, []).extend([title or '', note or '', t or ''])
    return out


def _search_text(d: dict, texts: list[str]) -> str:
    parts = [d['base'], d['key']]
    for v in d['versions']:
        ins = v.get('insight') or {}
        parts += [v['filename'], v.get('mail_subject', ''), v.get('history_title', ''),
                  (v.get('via_task') or {}).get('title', ''), ins.get('doc_type', ''), ins.get('summary', ''),
                  ' '.join(ins.get('tags', []))]
    return '\n'.join(p for p in parts + texts if p).lower()


def _build(keys: set[str]) -> list[dict]:
    """문서 열쇠들의 문서 (모든 판) — search() 와 같은 모양."""
    from sqlalchemy import select
    from services.db import Reference
    if not keys:
        return []
    with db.SessionLocal() as s:
        groups = doc_versions._rows(s, keys)
        ref_ids = list(s.scalars(select(Reference.id).where(Reference.kind == 'file', Reference.doc_key.in_(keys))))
    refs = store.get_references(ref_ids)
    return _merge([{'doc_key': k, 'versions': v} for k, v in groups.items()], refs)


def _all_keys(s) -> set[str]:
    from sqlalchemy import select
    from services.db import MailAttachment, Reference
    return {k for k in s.scalars(select(MailAttachment.doc_key).distinct()) if k} | \
        {k for k in s.scalars(select(Reference.doc_key).where(Reference.kind == 'file').distinct()) if k}


def _write(s, docs: list[dict], keys: set[str]) -> None:
    import json
    from datetime import datetime
    from sqlalchemy import delete
    from services.db import DocumentCache
    texts = _texts(s, {d['key'] for d in docs})
    now = datetime.now().replace(microsecond=0)
    s.execute(delete(DocumentCache).where(DocumentCache.key.in_(keys)))
    for d in docs:
        s.add(DocumentCache(key=d['key'], data=json.dumps(d, ensure_ascii=False, default=str),
                            search_text=_search_text(d, texts.get(d['key'], [])), updated=d['updated'],
                            latest_at=d['latest'].get('received', ''),
                            has_mail=any(v['kind'] == 'mail' for v in d['versions']), refreshed_at=now))


def invalidate() -> None:
    """다음 refresh_cache 때 전부 다시 만들게 한다 (지우기·합치기 등 어느 문서가 바뀌었는지 모를 때)."""
    from sqlalchemy import delete
    from services.db import DocumentCache
    with db.SessionLocal() as s:
        s.execute(delete(DocumentCache).where(DocumentCache.key == ''))
        s.commit()


def refresh_cache(full: bool = False, idle: bool = False) -> int:
    """미리 만든 문서 목록을 지금 상태로 — 다시 만든 문서 수. idle 이면 FULL_EVERY 가 지났을 때 전부 다시."""
    import json
    from datetime import datetime
    from sqlalchemy import delete, or_, select
    from services import doc_summary
    from services.db import DocumentCache, MailAttachment, Reference
    if doc_summary.ensure_reference_keys():  # 열쇠를 새로 채운 예전 파일 — 어느 문서인지 바뀐다
        invalidate()
    with db.SessionLocal() as s:
        meta = s.get(DocumentCache, '')
        old = json.loads(meta.data) if meta and meta.data else None
        new = _stamp(s)
        if old == new and not full and not (idle and meta and
                                            (datetime.now() - meta.refreshed_at).total_seconds() > FULL_EVERY):
            return 0
        shrank = old is not None and (new['mail'][0] < old['mail'][0] or new['ref'][0] < old['ref'][0])
        if full or old is None or shrank or old['alias'] != new['alias'] or old.get('ignored') != new['ignored'] \
                or idle:
            keys = _all_keys(s)
            s.execute(delete(DocumentCache))
        else:  # 그 뒤로 새로 온·새로 요약한 파일의 문서만
            m, r = old['mail'], old['ref']
            keys = set(s.scalars(select(MailAttachment.doc_key).where(or_(
                MailAttachment.id > m[1], MailAttachment.analyzed_at >= datetime.fromisoformat(m[2])
                if m[2] else MailAttachment.analyzed_at.is_not(None)))))
            keys |= set(s.scalars(select(Reference.doc_key).where(Reference.kind == 'file', or_(
                Reference.id > r[1], Reference.analyzed_at >= datetime.fromisoformat(r[2])
                if r[2] else Reference.analyzed_at.is_not(None)))))
            keys.discard('')
        docs = _build(keys)
        _write(s, docs, keys)
        s.merge(DocumentCache(key='', data=json.dumps(new), refreshed_at=datetime.now().replace(microsecond=0)))
        s.commit()
    return len(docs) or (1 if keys else 0)


def cached(query: str = '', mail_only: bool = False, limit: int = 30) -> tuple[list[dict], int]:
    """참고자료 화면의 문서 — 미리 만든 목록에서 (최근 바뀐 문서부터). (문서들, 걸린 문서 수).
    검색어는 낱말마다(용어집의 같은 말 포함) 파일 이름·요약·태그·메일 제목·본문 앞부분 중 어디에든 있어야 한다."""
    import json
    from sqlalchemy import func, or_, select
    from services import glossary
    from services.db import DocumentCache
    refresh_cache()
    cond = [DocumentCache.key != '']
    if mail_only:
        cond.append(DocumentCache.has_mail.is_(True))
    for t in store.search_terms(query or ''):
        cond.append(or_(*(DocumentCache.search_text.like(f'%{w.lower()}%') for w in glossary.expand(t))))
    with db.SessionLocal() as s:
        total = s.scalar(select(func.count()).select_from(DocumentCache).where(*cond)) or 0
        rows = s.scalars(select(DocumentCache.data).where(*cond)
                         .order_by(DocumentCache.updated.desc(), DocumentCache.latest_at.desc()).limit(limit)).all()
    return [json.loads(r) for r in rows], total



def similar(query: str, kind: str = '', skip_keys=(), skip_refs=(), limit: int = 10) -> tuple[list[dict], list[dict]]:
    """참고자료 화면의 의미 검색 (services/embed.py) — 검색어와 뜻이 가까운 (문서들, 링크·메모들), 높은 순.
    문서는 메일 첨부·올린 파일의 벡터를 같은 문서끼리 묶어 가장 높은 유사도로, 미리 만든 목록에서 꺼낸다.
    kind 는 화면의 유형 고르기('' / file / mail / link / note), skip_* 는 키워드로 이미 보여 준 것.
    임베딩 서버가 없으면 ([], []), 실패하면 embed.EmbedError."""
    import json
    from sqlalchemy import select
    from services import embed
    from services.db import DocumentCache, MailAttachment, Reference, Task
    want_docs, want_refs = kind in ('', 'file', 'mail'), kind in ('', 'link', 'note')
    kinds = (('mail',) if kind == 'mail' else ('mail', 'ref')) if want_docs else ('ref',)
    if not (want_docs or want_refs):
        return [], []
    hits = embed.search(query, kinds=kinds, limit=30)
    if not hits:
        return [], []
    mail_ids = [h['id'] for h in hits if h['kind'] == 'mail']
    ref_ids = [h['id'] for h in hits if h['kind'] == 'ref']
    with db.SessionLocal() as s:
        mail_key = dict(s.execute(select(MailAttachment.id, MailAttachment.doc_key)
                                  .where(MailAttachment.id.in_(mail_ids or [0]))).all())
        refs = {r.id: (r, title) for r, title in s.execute(
            select(Reference, Task.title).outerjoin(Task, Task.id == Reference.task_id)
            .where(Reference.id.in_(ref_ids or [0])))}
        doc_score: dict[str, float] = {}
        link_refs = []
        for h in hits:  # 높은 순
            if h['kind'] == 'mail':
                key = mail_key.get(h['id'])
            else:
                r, title = refs.get(h['id'], (None, ''))
                if r is None:
                    continue
                if r.kind != 'file':
                    if want_refs and (kind in ('', r.kind)) and r.id not in skip_refs and len(link_refs) < limit:
                        link_refs.append({**store._ref_dict(r), 'task_title': title or '', 'score': h['score']})
                    continue
                key = r.doc_key
            if want_docs and key and key not in skip_keys and key not in doc_score:
                doc_score[key] = h['score']
        keys = list(doc_score)[:limit * 2]
        cond = [DocumentCache.key.in_(keys or [''])]
        if kind == 'mail':
            cond.append(DocumentCache.has_mail.is_(True))
        rows = dict(s.execute(select(DocumentCache.key, DocumentCache.data).where(*cond)).all())
    # 미리 만든 목록에 없는 문서(판 이력에서 뺀 것 등)는 보여 주지 않는다
    docs = [{**json.loads(rows[k]), 'score': doc_score[k]} for k in keys if k in rows][:limit]
    return docs, link_refs

# ── 판 이력에서 빼기 ────────────────────────────────────────────────
# 서명 그림(image001.png)이나 업무와 상관없는 첨부처럼 이력을 볼 필요가 없는 문서는 통째로 빼고(앞으로 같은 이름
# 규칙으로 오는 파일도), 잘못 끼어든 판은 하나만 뺀다. 메일·올린 파일 자체는 지우지 않고, 되돌릴 수 있다.

def _version_model(vid: str):
    from services.db import MailAttachment, Reference
    if not vid or vid[0] not in 'mr' or not vid[1:].isdigit():
        raise ValueError('판을 찾을 수 없습니다.')
    return (MailAttachment if vid[0] == 'm' else Reference), int(vid[1:])


def exclude_version(vid: str, excluded: bool = True) -> None:
    """판 하나('m12' 메일 첨부 / 'r5' 올린 파일)를 이력에서 빼거나 되돌린다."""
    model, num = _version_model(vid)
    with db.SessionLocal() as s:
        x = s.get(model, num)
        if x is None:
            raise ValueError('판을 찾을 수 없습니다.')
        x.excluded = excluded
        s.commit()
    invalidate()


def exclude(key: str, by: str = '', label: str = '') -> None:
    """문서를 통째로 이력에서 뺀다 — 앞으로 같은 이름 규칙으로 오는 파일도."""
    from sqlalchemy import select
    from services.db import DocExclusion
    if not key:
        raise ValueError('문서를 찾을 수 없습니다.')
    with db.SessionLocal() as s:
        if s.scalar(select(DocExclusion).where(DocExclusion.key == key)) is None:
            s.add(DocExclusion(key=key, label=(label or key)[:300], created_by=by[:100]))
            s.commit()
    invalidate()


def include(key: str) -> None:
    """뺀 문서를 되돌린다."""
    from sqlalchemy import delete
    from services.db import DocExclusion
    with db.SessionLocal() as s:
        s.execute(delete(DocExclusion).where(DocExclusion.key == key))
        s.commit()
    invalidate()


def excluded() -> dict:
    """뺀 것 — {'docs': [{'key', 'label', 'by', 'at'}], 'versions': [{'id', 'filename', 'source'}]} (되돌리기 화면)."""
    from sqlalchemy import select
    from services.db import DocExclusion, InboundMail, MailAttachment, Reference
    with db.SessionLocal() as s:
        docs = [{'key': x.key, 'label': x.label or x.key, 'by': x.created_by or '',
                 'at': x.created_at.strftime('%Y-%m-%d') if x.created_at else ''}
                for x in s.scalars(select(DocExclusion).order_by(DocExclusion.id.desc()))]
        versions = [{'id': f'm{a.id}', 'filename': a.filename, 'source': f"메일: {subject or '(제목 없음)'}"}
                    for a, subject in s.execute(select(MailAttachment, InboundMail.subject)
                                                .join(InboundMail, InboundMail.id == MailAttachment.mail_id)
                                                .where(MailAttachment.excluded.is_(True)))]
        versions += [{'id': f'r{r.id}', 'filename': r.file_name or r.title, 'source': '올린 파일'}
                     for r in s.scalars(select(Reference).where(Reference.excluded.is_(True)))]
    return {'docs': docs, 'versions': versions}
