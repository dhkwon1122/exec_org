"""메일 첨부 문서의 판(버전) — 파일 이름에 붙는 날짜(20261021·261021·1021)와 _r1·_v2·rev3·(1)·최종·수정본 같은
꼬리표를 떼어 '같은 문서'로 묶고, 판 순서를 정한다.

판 순서: 파일 이름의 날짜(없으면 메일 날짜) → 이름의 판 번호(_v2 등) → 메일 날짜 → 받은 순서.
내용이 똑같은 파일(sha256 같음)은 새 판이 아니라 앞 판을 다시 보낸 것으로 본다.
"""
import hashlib
import os
import re

from sqlalchemy import select

from services import db
from services.db import InboundMail, MailAttachment

_VERSION = re.compile(r'(?i)(?:^|(?<=[\s_\-.(\[]))(?:rev|ver|v|r)\.?\s?(\d+(?:\.\d+)?)(?=$|[\s_\-.)\]])')
_COPY = re.compile(r'\s*\(\d{1,2}\)\s*$')  # 윈도우가 붙이는 '(1)'
_WORDS = re.compile(r'(?i)(진짜\s*최종|최최종|최종본?|final|수정본|수정안|수정|초안|draft|검토본|검토용|송부본|송부용|'
                    r'배포본|배포용|updated?|사본|copy)')
_DATES = [
    # 20261021 · 2026-10-21 · 2026.10.21
    re.compile(r'(?<!\d)((?:19|20)\d{2})[-._ ]?(0[1-9]|1[0-2])[-._ ]?(0[1-9]|[12]\d|3[01])(?!\d)'),
    # 261021 · 26.10.21
    re.compile(r'(?<!\d)(\d{2})[-._]?(0[1-9]|1[0-2])[-._]?(0[1-9]|[12]\d|3[01])(?!\d)'),
    # 1021 · 10.21 (연도 없음)
    re.compile(r'(?<![\d.])()(0[1-9]|1[0-2])[-._]?(0[1-9]|[12]\d|3[01])(?![\d.])'),
]
_SEPS = re.compile(r'[\s_\-.()\[\]{}~+,]+')


def parse_name(filename: str) -> dict:
    """파일 이름 → {'key': 같은 문서 열쇠, 'base': 꼬리표 뗀 이름, 'ext', 'date': 'YYYY-MM-DD'|'MM-DD'|'',
    'version': 판 번호(없으면 0)}."""
    name = os.path.basename(filename or '').strip()
    stem, ext = os.path.splitext(name)
    ext = ext.lower().lstrip('.')
    if len(ext) > 6 or not ext.isalnum():  # '보고서.v2' 처럼 점 뒤가 확장자가 아님
        stem, ext = name, ''
    work = _COPY.sub('', stem)
    version = 0.0
    m = _VERSION.search(work)
    if m:
        version = float(m.group(1))
        work = work[:m.start()] + ' ' + work[m.end():]
    found = ''
    for i, pat in enumerate(_DATES):
        for d in pat.finditer(work):
            if not found:
                y = d.group(1)
                y = y if len(y) == 4 else (f'20{y}' if y else '')
                found = f'{y}-{d.group(2)}-{d.group(3)}' if y else f'{d.group(2)}-{d.group(3)}'
        work = pat.sub(' ', work)
    work = _WORDS.sub(' ', work)
    base = _SEPS.sub(' ', work).strip()
    norm = _SEPS.sub('', work).lower() or _SEPS.sub('', stem).lower() or name.lower()
    return {'key': f'{norm}.{ext}' if ext else norm, 'base': base or stem, 'ext': ext, 'date': found,
            'version': version}


def _order(item: dict) -> tuple:
    d = item['name_date']
    when = item['mail_date'] or ''
    if len(d) == 5 and when:  # 연도 없는 날짜는 메일 연도로
        d = f'{when[:4]}-{d}'
    return (d or when, item['name_version'], when, item['received'], item['id'])


def _number(items: list[dict]) -> list[dict]:
    """같은 문서의 첨부들에 판 번호를 매긴다 — 내용이 같은 파일은 같은 판."""
    items = sorted(items, key=_order)
    seen, n = {}, 0
    for it in items:
        if it['sha256'] and it['sha256'] in seen:
            it['version'], it['dup'] = seen[it['sha256']], True
        else:
            n += 1
            it['version'], it['dup'] = n, False
            if it['sha256']:
                seen[it['sha256']] = n
    for it in items:
        it['n_versions'] = n
        it['latest'] = it['version'] == n
    return items


def _rows(s, keys: set[str]) -> dict[str, list[dict]]:
    from services.doc_summary import insight  # doc_summary 가 이 모듈을 쓰므로 여기서
    rows = s.execute(select(MailAttachment, InboundMail.subject, InboundMail.mail_date, InboundMail.received_at)
                     .join(InboundMail, InboundMail.id == MailAttachment.mail_id)
                     .where(MailAttachment.doc_key.in_(keys))) if keys else []
    out: dict[str, list[dict]] = {}
    for a, subject, mail_date, received in rows:
        p = parse_name(a.filename)
        out.setdefault(a.doc_key, []).append({
            'id': a.id, 'mail_id': a.mail_id, 'filename': a.filename, 'size': a.size or 0, 'sha256': a.sha256 or '',
            'doc_key': a.doc_key, 'base': p['base'], 'name_date': p['date'], 'name_version': p['version'],
            'mail_subject': subject or '(제목 없음)', 'mail_date': mail_date or '',
            'received': received.strftime('%Y-%m-%d %H:%M') if received else '', 'insight': insight(a)})
    return {k: _number(v) for k, v in out.items()}


def save(mail_id: int, files: list[tuple[str, bytes]]) -> int:
    """메일 첨부를 UPLOAD_DIR/mail/<메일 id>/ 에 저장하고 기록한다. 저장한 개수."""
    if not files:
        return 0
    folder = os.path.join(db.UPLOAD_DIR, 'mail', str(mail_id))
    os.makedirs(folder, exist_ok=True)
    n = 0
    with db.SessionLocal() as s:
        for i, (name, data) in enumerate(files):
            safe = re.sub(r'[^\w.\-가-힣()]+', '_', os.path.basename(name or 'file'))[:120] or 'file'
            rel = os.path.join('mail', str(mail_id), f'{i + 1}_{safe}')
            with open(os.path.join(db.UPLOAD_DIR, rel), 'wb') as f:
                f.write(data)
            s.add(MailAttachment(mail_id=mail_id, filename=name[:300], file_path=rel, size=len(data),
                                 sha256=hashlib.sha256(data).hexdigest(), doc_key=parse_name(name)['key'][:300]))
            n += 1
        s.commit()
    return n


def get(att_id: int) -> dict | None:
    with db.SessionLocal() as s:
        a = s.get(MailAttachment, att_id)
        return {'id': a.id, 'filename': a.filename, 'file_path': a.file_path, 'mail_id': a.mail_id} if a else None


def for_mails(mail_ids: list[int]) -> list[dict]:
    """메일들의 첨부 — 문서별로 [{'doc_key', 'base', 'files': 이 메일들의 첨부(판 정보 포함), 'versions': 그 문서의
    모든 판(다른 메일 것까지), 'latest': 최신 판}] (최근 받은 문서 먼저)."""
    if not mail_ids:
        return []
    with db.SessionLocal() as s:
        mine = list(s.scalars(select(MailAttachment).where(MailAttachment.mail_id.in_(mail_ids))
                              .order_by(MailAttachment.id)))
        groups = _rows(s, {a.doc_key for a in mine})
    ids = {a.id for a in mine}
    out = []
    for key, versions in groups.items():
        files = [v for v in versions if v['id'] in ids]
        out.append({'doc_key': key, 'base': versions[-1]['base'], 'files': files, 'versions': versions,
                    'latest': versions[-1]})
    return sorted(out, key=lambda g: max(f['id'] for f in g['files']), reverse=True)


def for_task(task_id: int) -> list[dict]:
    """업무와 연결된 메일들의 첨부 문서 (판별)."""
    with db.SessionLocal() as s:
        mids = [m_id for m_id, ids in s.execute(select(InboundMail.id, InboundMail.task_ids)
                                                .where(InboundMail.task_ids.like(f'%{task_id}%')))
                if str(task_id) in (ids or '').split(',')]
    return for_mails(mids)
