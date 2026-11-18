"""저장 공간 점검 — 받은 메일·첨부가 얼마나 차지하는지, 같은 첨부가 여러 벌 저장돼 있지 않은지.

    docker compose exec app python scripts/storage_report.py          # 보고만
    docker compose exec app python scripts/storage_report.py --dedup  # 같은 첨부를 한 벌로 (파일만, 기록은 그대로)
"""
import os
from collections import Counter, defaultdict

from sqlalchemy import func, select

from services import db
from services.db import HistoryEntry, InboundMail, MailAttachment


def _file_size(rel: str) -> int:
    path = os.path.join(db.UPLOAD_DIR, rel or '')
    return os.path.getsize(path) if rel and os.path.isfile(path) else 0


def report() -> dict:
    """{'mails', 'mail_text', 'by_status', 'mail_records', 'mail_record_text', 'attachments', 'attachment_files',
        'attachment_bytes', 'duplicate_files', 'duplicate_bytes'} — 글자 수·바이트."""
    with db.SessionLocal() as s:
        mails, text = s.execute(select(func.count(), func.coalesce(func.sum(func.length(InboundMail.body)), 0))).one()
        by_status = dict(s.execute(select(InboundMail.status, func.count()).group_by(InboundMail.status)).all())
        rec, rec_text = s.execute(select(func.count(), func.coalesce(func.sum(func.length(HistoryEntry.content)), 0))
                                  .where(HistoryEntry.content.like('[받은 메일]%'))).one()
        atts = list(s.execute(select(MailAttachment.sha256, MailAttachment.size, MailAttachment.file_path)))
    paths = {p for _, _, p in atts if p}
    total = sum(_file_size(p) for p in paths)
    groups = defaultdict(set)
    for digest, size, path in atts:
        if digest and path:
            groups[(digest, size)].add(path)
    dup_files = sum(len(p) - 1 for p in groups.values() if len(p) > 1)
    dup_bytes = sum(size * (len(p) - 1) for (_d, size), p in groups.items() if len(p) > 1)
    return {'mails': mails, 'mail_text': int(text), 'by_status': by_status, 'mail_records': rec,
            'mail_record_text': int(rec_text), 'attachments': len(atts), 'attachment_files': len(paths),
            'attachment_bytes': total, 'duplicate_files': dup_files, 'duplicate_bytes': dup_bytes}


def dedup_attachments(apply: bool = False) -> dict:
    """같은 내용(sha256·크기)의 메일 첨부 파일을 한 벌로 — 기록은 남은 한 벌을 가리키고 나머지 파일은 지운다.
    apply=False 면 무엇을 할지만 센다. → {'files', 'bytes'}"""
    removed = freed = 0
    with db.transaction() as s:
        rows = list(s.scalars(select(MailAttachment).where(MailAttachment.sha256 != '').order_by(MailAttachment.id)))
        uses = Counter(a.file_path for a in rows)  # 한 파일을 가리키는 기록 수
        keep: dict = {}
        for a in rows:
            key = (a.sha256, a.size)
            if key not in keep:
                if _file_size(a.file_path):
                    keep[key] = a.file_path
                continue
            if a.file_path == keep[key]:
                continue
            old = a.file_path
            uses[old] -= 1
            uses[keep[key]] += 1
            if apply:
                a.file_path = keep[key]
            if not uses[old] and _file_size(old):
                freed += _file_size(old)
                removed += 1
                if apply:
                    s.flush()
                    os.remove(os.path.join(db.UPLOAD_DIR, old))
    return {'files': removed, 'bytes': freed}
