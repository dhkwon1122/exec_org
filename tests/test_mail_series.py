"""정기 메일 모음 — 조건으로 모으기, 새 메일은 AI 처리 없이 쌓기, 지난 메일 다시 모으기, 검색·엑셀."""
import io
from email.message import EmailMessage

import pytest

from services import db, llm, mail_series, mailbox


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'UPLOAD_DIR', str(tmp_path / 'uploads'))
    monkeypatch.setattr(db, 'DATA_DIR', str(tmp_path))
    monkeypatch.setattr(mailbox, '_STATUS_FILE', str(tmp_path / 'st.json'))
    db.configure('sqlite:///' + str(tmp_path / 'test.db'))
    monkeypatch.setattr(llm, 'is_configured', lambda: False)
    for k, v in {'MAIL_POP3_HOST': 'pop.example', 'MAIL_POP3_USER': 'me', 'MAIL_SINCE_DAYS': '0',
                 'MAIL_MAX_PER_RUN': '50'}.items():
        monkeypatch.setenv(k, v)


def _raw(subject, sender, body, day, mid):
    m = EmailMessage()
    m['Subject'], m['From'], m['Date'], m['Message-ID'] = subject, sender, f'{day} 09:00:00 +0900', mid
    m.set_content(body)
    return m.as_bytes()


class FakePOP:
    def __init__(self, mails):
        self.mails = mails

    def uidl(self):
        return b'+OK', [f'{i + 1} uid-{hash(m)}'.encode() for i, m in enumerate(self.mails)], 0

    def retr(self, n):
        return b'+OK', self.mails[n - 1].split(b'\n'), 0

    def dele(self, n):
        pass

    def quit(self):
        pass


def _fetch(*mails):
    mailbox.fetch_new(connect=lambda cfg: FakePOP(list(mails)))


def test_series_collects_past_and_new_mails_without_ai(monkeypatch):
    _fetch(_raw('[주간보고] 11월 1주차 AI센터', 'Report <report@x.com>', '1주차: 과제 A 착수', 'Mon, 2 Nov 2026', '<1@x>'),
           _raw('RE: [주간보고] 11월 1주차 AI센터', 'boss@x.com', '확인', 'Mon, 2 Nov 2026', '<2@x>'),
           _raw('예산 요청', 'report@x.com', '예산', 'Mon, 2 Nov 2026', '<3@x>'))
    first = mailbox.list_mails(['review'])
    seed = mail_series.suggest(next(m for m in first if m['subject'].startswith('[주간')))
    assert seed['subject_pattern'] == '[주간보고]' and seed['sender_pattern'] == 'report@x.com'
    assert mail_series.preview_count(seed) == 1  # 회신과 다른 제목은 빠진다
    s = mail_series.save(seed, by='담당자')
    assert s['count'] == 1 and s['name'] == '주간보고'

    monkeypatch.setattr(mailbox, 'process', lambda mid: pytest.fail('모음에 넣은 메일은 AI 처리하지 않음'))
    _fetch(_raw('[주간보고] 11월 2주차 AI센터', 'report@x.com', '2주차: 과제 A 50%', 'Mon, 9 Nov 2026', '<4@x>'))
    rows, total = mail_series.mails(s['id'])
    assert total == 2 and rows[0]['mail_date'] == '2026-11-09'  # 최근 것이 위
    assert rows[0]['status'] == 'done' and mailbox.get_mail(rows[0]['id'])['handled_by'] == mail_series.FILED_BY
    assert mail_series.mails(s['id'], '50%')[1] == 1
    assert mail_series.list_series()[0]['latest'] == '2026-11-09'


def test_keep_in_review_option_and_wildcard_and_refill():
    _fetch(_raw('월간 실적 보고 (10월)', 'kpi@x.com', '10월', 'Mon, 2 Nov 2026', '<1@x>'))
    s = mail_series.save({'name': '월간 실적', 'subject_pattern': '월간*보고', 'file_away': False})
    assert s['count'] == 1
    _fetch(_raw('월간 실적 보고 (11월)', 'kpi@x.com', '11월', 'Mon, 30 Nov 2026', '<2@x>'))
    rows, _ = mail_series.mails(s['id'])
    assert len(rows) == 2 and rows[0]['status'] == 'review'  # 확인 필요로 남겨 두는 모음
    s = mail_series.save({'name': '월간 실적', 'subject_pattern': '월간', 'sender_pattern': 'other@x.com'}, s['id'])
    assert s['count'] == 0  # 조건을 바꾸면 다시 모은다
    mail_series.delete(s['id'])
    assert mail_series.list_series() == [] and len(mailbox.list_mails()) == 2  # 메일은 남는다


def test_validation_export_and_trend_requires_llm():
    with pytest.raises(ValueError):
        mail_series.save({'name': '', 'subject_pattern': 'x'})
    with pytest.raises(ValueError):
        mail_series.save({'name': '빈 조건', 'subject_pattern': ' * '})
    _fetch(_raw('[주간보고] 1주차', 'r@x.com', '본문 1', 'Mon, 2 Nov 2026', '<1@x>'))
    s = mail_series.save({'name': '주간', 'subject_pattern': '[주간보고]'})
    from openpyxl import load_workbook
    ws = load_workbook(io.BytesIO(mail_series.export_xlsx(s['id']))).active
    assert [c.value for c in ws[1]] == ['날짜', '제목', '보낸 사람', '본문'] and ws['B2'].value == '[주간보고] 1주차'
    with pytest.raises(ValueError, match='LLM'):
        mail_series.trend(s['id'])


def test_trend_sends_recent_mails_oldest_first(monkeypatch):
    _fetch(_raw('[주간보고] 1주차', 'r@x.com', '첫 주', 'Mon, 2 Nov 2026', '<1@x>'),
           _raw('[주간보고] 2주차', 'r@x.com', '둘째 주', 'Mon, 9 Nov 2026', '<2@x>'))
    s = mail_series.save({'name': '주간', 'subject_pattern': '[주간보고]'})
    monkeypatch.setattr(llm, 'is_configured', lambda: True)
    seen = {}
    monkeypatch.setattr(llm, 'chat', lambda system, user: seen.setdefault('text', user) and '## 계속되는 주제')
    assert mail_series.trend(s['id']).startswith('## 계속되는')
    assert seen['text'].index('첫 주') < seen['text'].index('둘째 주')


def test_exclude_wrong_mails_stays_out_on_refill_and_can_be_undone():
    _fetch(_raw('[주간보고] 1주차', 'r@x.com', '본문 1', 'Mon, 2 Nov 2026', '<1@x>'),
           _raw('[주간보고] 안내 — 양식 변경', 'r@x.com', '양식 안내', 'Mon, 2 Nov 2026', '<2@x>'))
    s = mail_series.save({'name': '주간', 'subject_pattern': '[주간보고]', 'file_away': True})
    rows, _ = mail_series.mails(s['id'])
    wrong = next(r for r in rows if '양식' in r['subject'])
    # 모음이 처리 완료로 둔 메일이라고 가정 (새로 받은 메일과 같은 상태)
    with db.transaction() as sess:
        m = sess.get(db.InboundMail, wrong['id'])
        m.status, m.handled_by = 'done', mail_series.FILED_BY
    assert mail_series.exclude(s['id'], [wrong['id'], 99999]) == 1  # 모음에 없는 id 는 무시
    after = mailbox.get_mail(wrong['id'])
    assert after['series_id'] is None and after['status'] == 'review' and '뺌' in after['reason']
    assert mail_series.get(s['id'])['count'] == 1 and mail_series.get(s['id'])['excluded'] == [wrong['id']]
    s = mail_series.save({'name': '주간', 'subject_pattern': '[주간보고]', 'file_away': True}, s['id'])
    assert s['count'] == 1  # 조건을 다시 저장해도 뺀 메일은 다시 모으지 않는다
    assert mail_series.include_again(s['id']) == 2 and mail_series.get(s['id'])['excluded'] == []
    with pytest.raises(ValueError):
        mail_series.exclude(12345, [1])


def test_replies_and_forwards_collected_by_subject_and_left_for_review(monkeypatch):
    _fetch(_raw('[주간보고] 1주차', 'r@x.com', '본문 1', 'Mon, 2 Nov 2026', '<1@x>'),
           _raw('RE: [주간보고] 1주차', 'boss@x.com', '과제 A 일정 확인 바랍니다', 'Tue, 3 Nov 2026', '<2@x>'),
           _raw('FW: [주간보고] 1주차', 'r@x.com', '참고로 전달', 'Tue, 3 Nov 2026', '<3@x>'))
    base = {'name': '주간', 'subject_pattern': '[주간보고]', 'sender_pattern': 'r@x.com', 'file_away': True}
    assert mail_series.preview_count(base) == 1  # 기본은 원래 메일만
    assert mail_series.preview_count({**base, 'include_replies': True}) == 3  # 회신은 보낸 사람이 달라도
    s = mail_series.save({**base, 'include_replies': True})
    rows, total = mail_series.mails(s['id'])
    assert total == 3 and {r['reply'] for r in rows} == {'', '회신', '전달'}
    assert s['include_replies']
    # 새로 온 회신은 모음에 들어가되 확인 필요로 남는다 (원래 메일은 처리 완료)
    monkeypatch.setattr(mailbox, 'process', lambda mid: None)
    _fetch(_raw('[주간보고] 2주차', 'r@x.com', '본문 2', 'Mon, 9 Nov 2026', '<4@x>'),
           _raw('Re: [주간보고] 2주차', 'boss@x.com', '좋습니다', 'Tue, 10 Nov 2026', '<5@x>'))
    new = {r['subject']: r for r in mail_series.mails(s['id'])[0]}
    assert new['[주간보고] 2주차']['status'] == 'done' and new['Re: [주간보고] 2주차']['status'] == 'review'
    # 회신 메일로 모음을 만들면 회신 모으기가 켜진 채로 제안
    seed = mail_series.suggest({'subject': 'RE: [월간보고] 10월', 'sender': 'a@x.com'})
    assert seed['include_replies'] and seed['sender_pattern'] == '' and seed['subject_pattern'] == '[월간보고]'
