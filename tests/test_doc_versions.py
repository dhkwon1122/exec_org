from email.message import EmailMessage

import pytest

from services import db, doc_versions, llm, mail_text, mailbox, store
from services.doc_versions import parse_name
from tests.test_mailbox import FakePOP


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'UPLOAD_DIR', str(tmp_path / 'uploads'))
    monkeypatch.setattr(db, 'DATA_DIR', str(tmp_path))
    monkeypatch.setattr(mailbox, '_STATUS_FILE', str(tmp_path / 'st.json'))
    db.configure('sqlite:///' + str(tmp_path / 'test.db'))
    monkeypatch.setattr(llm, 'is_configured', lambda: False)
    for k, v in {'MAIL_POP3_HOST': 'pop.example', 'MAIL_POP3_USER': 'me', 'MAIL_SINCE_DAYS': '0'}.items():
        monkeypatch.setenv(k, v)
    yield


def _mail(subject, date, files, mid, inline_image=False) -> bytes:
    m = EmailMessage()
    m['Subject'], m['From'], m['Date'], m['Message-ID'] = subject, '김팀장 <kim@x.com>', date, mid
    m.set_content('자료 송부드립니다.')
    if inline_image:
        m.add_alternative('<p>서명<img src="cid:logo"></p>', subtype='html')
        m.get_payload()[1].add_related(b'PNGDATA', maintype='image', subtype='png', cid='<logo>')
    for name, data in files:
        m.add_attachment(data, maintype='application', subtype='octet-stream', filename=name)
    return m.as_bytes()


@pytest.mark.parametrize('name, key, date, version', [
    ('2026 임원 평가 결과_20261021_r1.pptx', '2026임원평가결과.pptx', '2026-10-21', 1),
    ('2026임원평가결과(최종).pptx', '2026임원평가결과.pptx', '', 0),
    ('임원평가결과_v1.2.xlsx', '임원평가결과.xlsx', '', 1.2),
    ('보고서_1022 (1).docx', '보고서.docx', '10-22', 0),
    ('조직도_수정본_261020.pdf', '조직도.pdf', '2026-10-20', 0),
    ('회의록_10.21_v2.hwp', '회의록.hwp', '10-21', 2),
    ('2026 사업계획.pptx', '2026사업계획.pptx', '', 0),  # 연도만 있는 건 날짜가 아님
    ('Rev3_report.pdf', 'report.pdf', '', 3),
])
def test_parse_name(name, key, date, version):
    p = parse_name(name)
    assert (p['key'], p['date'], p['version']) == (key, date, version)


def test_attachments_skip_inline_images():
    raw = _mail('자료', 'Fri, 23 Oct 2026 17:10:00 +0900', [('보고서_v1.pptx', b'A')], '<a@x>', inline_image=True)
    assert mail_text.attachments(raw) == [('보고서_v1.pptx', b'A')]
    assert mail_text.attachments(raw, max_bytes=0) == []


def test_versions_follow_mail_dates_and_ignore_resends():
    mails = [
        _mail('평가 자료', 'Mon, 19 Oct 2026 09:00:00 +0900', [('임원평가_r1.pptx', b'one'), ('참고.pdf', b'p')], '<1@x>'),
        _mail('RE: 평가 자료', 'Wed, 21 Oct 2026 09:00:00 +0900', [('임원평가_1021_r2.pptx', b'two')], '<2@x>'),
        _mail('FW: 평가 자료', 'Thu, 22 Oct 2026 09:00:00 +0900', [('임원평가_r2 (1).pptx', b'two')], '<3@x>'),
        _mail('평가 자료 최종', 'Fri, 23 Oct 2026 09:00:00 +0900', [('임원평가(최종).pptx', b'three')], '<4@x>'),
    ]
    mailbox.fetch_new(connect=lambda cfg: FakePOP(mails))
    ids = {m['subject']: m['id'] for m in mailbox.list_mails()}
    groups = doc_versions.for_mails([ids['RE: 평가 자료']])
    assert len(groups) == 1
    g = groups[0]
    assert [(v['filename'], v['version'], v['dup']) for v in g['versions']] == [
        ('임원평가_r1.pptx', 1, False), ('임원평가_1021_r2.pptx', 2, False),
        ('임원평가_r2 (1).pptx', 2, True),  # 같은 내용을 다시 보낸 것 — 새 판 아님
        ('임원평가(최종).pptx', 3, False)]
    f = g['files'][0]
    assert (f['version'], f['latest'], f['n_versions']) == (2, False, 3) and g['latest']['filename'] == '임원평가(최종).pptx'
    # 업무에 연결된 메일들의 첨부 문서
    t = store.create_task('2026 임원 평가')
    mailbox.link_task(ids['평가 자료'], t['id'], '나')
    docs = doc_versions.for_task(t['id'])
    assert sorted(d['base'] for d in docs) == ['임원평가', '참고']
    assert next(d for d in docs if d['base'] == '임원평가')['latest']['version'] == 3
    # 내려받기용 경로
    att = doc_versions.get(f['id'])
    assert store.upload_abspath(att['file_path']) and open(store.upload_abspath(att['file_path']), 'rb').read() == b'two'
