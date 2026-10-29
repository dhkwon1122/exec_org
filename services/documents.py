"""문서별 판 이력 — 같은 문서(파일 이름에서 날짜·_r1·_v2·최종 같은 꼬리표를 뗀 열쇠, doc_versions.parse_name)가
메일 첨부와 직접 올린 파일(업무·기록 참고자료)로 여러 번 오가며 바뀌어 온 과정을 한 문서로 모은다.

문서: {'key', 'base', 'ext', 'versions': [판 — 오래된 판부터], 'latest', 'n_versions', 'updated', 'insight'}
판: {'kind': 'mail'|'ref', 'id', 'filename', 'href', 'date', 'version', 'dup', 'latest', 'source', 'insight', ...}
  mail — 'mail_id', 'mail_subject' / ref — 'ref_id', 'history_title', 'via_task' (하위 업무 등에서 온 자료)
판 순서와 번호는 doc_versions 와 같은 규칙(이름 속 날짜 → 판 번호 → 받은·올린 날짜), 내용이 같은 메일 첨부는 같은 판.
"""
import os

from services import db, doc_versions, store


def _ref_version(r: dict, hist_titles: dict) -> dict:
    name = r['file_name'] or r['title']
    p = doc_versions.parse_name(name)
    path = os.path.join(db.UPLOAD_DIR, r['file_path']) if r.get('file_path') else ''
    return {'kind': 'ref', 'id': f"r{r['id']}", 'ref_id': r['id'], 'filename': name, 'href': f"/files/{r['id']}",
            'size': os.path.getsize(path) if path and os.path.isfile(path) else 0, 'sha256': '',
            'key': p['key'], 'base': p['base'], 'name_date': p['date'], 'name_version': p['version'],
            'date': (r.get('created_at') or '')[:10], 'received': r.get('created_at') or '',
            'source': '기록 첨부' if r.get('history_id') else '직접 등록',
            'history_title': hist_titles.get(r.get('history_id'), ''), 'via_task': r.get('via_task'),
            'insight': None}


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
    for r in ref_files:
        v = _ref_version(r, hist_titles)
        by_key.setdefault(v['key'], []).append(v)
    docs = [_document(k, vs) for k, vs in by_key.items()]
    return sorted(docs, key=lambda d: (d['updated'], d['latest']['received']), reverse=True)


def for_task(task_id: int, include_children: bool = True) -> list[dict]:
    """업무(와 하위 업무)의 문서 — 연결된 메일·업무를 만든 메일의 첨부와 이 업무들에 올린 파일, 문서마다 모든 판."""
    mail_groups = doc_versions.for_task(task_id, include_children)
    refs = [r for r in store.references_for_task(task_id, include_children) if r['kind'] == 'file']
    return _merge(mail_groups, refs)


def search(query: str = '', limit: int = 100) -> list[dict]:
    """전체 문서 — 메일 첨부(파일 이름·요약·태그·본문 검색)와 올린 파일(제목·파일 이름·업무명 검색)을 문서별로."""
    keys = [d['file']['doc_key'] for d in doc_versions.latest_documents(query, limit)]
    with db.SessionLocal() as s:  # 문서의 모든 판 (검색에 걸린 판만이 아니라)
        groups = doc_versions._rows(s, set(keys)) if keys else {}
    mail_groups = [{'doc_key': k, 'versions': groups[k]} for k in keys if k in groups]
    refs = store.list_references(query=query or '', kind='file')
    # 검색에 걸린 올린 파일과 같은 문서의 메일 첨부도 함께 (이름으로 같은 문서)
    extra = {doc_versions.parse_name(r['file_name'] or r['title'])['key'] for r in refs} - set(keys)
    if extra:
        with db.SessionLocal() as s:
            more = doc_versions._rows(s, extra)
        mail_groups += [{'doc_key': k, 'versions': v} for k, v in more.items()]
    return _merge(mail_groups, refs)[:limit]
