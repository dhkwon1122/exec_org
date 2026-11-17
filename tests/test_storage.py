"""저장 공간 — 같은 첨부는 한 벌만, 보고서, 기존 중복 줄이기."""
import os

import pytest

from services import db, doc_versions, storage
from services.db import InboundMail, MailAttachment


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'UPLOAD_DIR', str(tmp_path / 'uploads'))
    monkeypatch.setattr(db, 'DATA_DIR', str(tmp_path))
    db.configure('sqlite:///' + str(tmp_path / 'test.db'))


def _mail(subject):
    with db.SessionLocal() as s:
        m = InboundMail(uidl=subject, subject=subject, body='본문 ' * 10, status='review')
        s.add(m)
        s.commit()
        return m.id


def test_same_attachment_is_stored_once():
    a, b = _mail('보고'), _mail('FW: 보고')
    assert doc_versions.save(a, [('보고서.pdf', b'PDF' * 1000)]) == 1
    assert doc_versions.save(b, [('보고서(최종).pdf', b'PDF' * 1000), ('다른.pdf', b'other')]) == 2
    with db.SessionLocal() as s:
        rows = list(s.query(MailAttachment).order_by(MailAttachment.id))
    assert rows[0].file_path == rows[1].file_path != rows[2].file_path  # 같은 내용은 같은 파일
    assert rows[1].filename == '보고서(최종).pdf'  # 기록(이름·메일)은 따로
    r = storage.report()
    assert r['attachments'] == 3 and r['attachment_files'] == 2 and r['duplicate_files'] == 0


def test_dedup_existing_duplicates_keeps_records_and_frees_files():
    ids = [_mail(f'메일{i}') for i in range(3)]
    paths = []
    for i, mid in enumerate(ids):  # 예전 방식처럼 메일마다 따로 저장된 같은 파일
        rel = os.path.join('mail', str(mid), '1_a.pdf')
        os.makedirs(os.path.join(db.UPLOAD_DIR, 'mail', str(mid)), exist_ok=True)
        with open(os.path.join(db.UPLOAD_DIR, rel), 'wb') as f:
            f.write(b'X' * 2048)
        paths.append(rel)
        with db.SessionLocal() as s:
            s.add(MailAttachment(mail_id=mid, filename='a.pdf', file_path=rel, size=2048, sha256='h1'))
            s.commit()
    r = storage.report()
    assert r['duplicate_files'] == 2 and r['duplicate_bytes'] == 4096
    assert storage.dedup_attachments(apply=False) == {'files': 2, 'bytes': 4096}
    assert all(os.path.isfile(os.path.join(db.UPLOAD_DIR, p)) for p in paths)  # 보고만 하면 그대로
    assert storage.dedup_attachments(apply=True) == {'files': 2, 'bytes': 4096}
    with db.SessionLocal() as s:
        left = {a.file_path for a in s.query(MailAttachment)}
    assert left == {paths[0]} and os.path.isfile(os.path.join(db.UPLOAD_DIR, paths[0]))
    assert not os.path.exists(os.path.join(db.UPLOAD_DIR, paths[1]))
    assert storage.dedup_attachments(apply=True) == {'files': 0, 'bytes': 0}
