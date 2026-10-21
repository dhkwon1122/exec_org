import base64
import json
from email.message import EmailMessage

import pytest

from components import director_ui
from services import db, llm, mail_text, schedules


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'UPLOAD_DIR', str(tmp_path / 'uploads'))
    db.configure('sqlite:///' + str(tmp_path / 'test.db'))
    monkeypatch.setattr(llm, 'is_configured', lambda: False)
    yield


MAIL_HTML = """<p>원장님 주간 일정 공유드립니다.</p><table>
<tr><th>일자</th><th>시간</th><th>내용</th><th>장소</th><th>참석</th></tr>
<tr><td>10/27(월)</td><td>09:00~10:00</td><td>주간 임원회의</td><td>본관 5층<br>대회의실</td><td>전 임원</td></tr>
<tr><td></td><td>14:00</td><td>AI센터 보고</td><td>원장실</td><td>김영수 부사장</td></tr>
<tr><td>10/29(수)</td><td>종일</td><td>해외 출장</td><td>미국</td><td></td></tr></table><p>감사합니다.</p>"""
PASTED = '보낸 날짜: 2026년 10월 24일 금요일 오후 5:10\n제목: 원장님 주간 일정(10/27~31)\n' + MAIL_HTML


def test_pasted_mail_keeps_table_rows():
    m = mail_text.from_pasted(PASTED)
    assert (m['name'], m['mail_date']) == ('원장님 주간 일정(10/27~31)', '2026-10-24')
    assert '10/27(월) | 09:00~10:00 | 주간 임원회의 | 본관 5층 / 대회의실 | 전 임원' in m['text']


def test_eml_upload_and_bad_files():
    msg = EmailMessage()
    msg['Subject'] = '원장님 일정'
    msg['Date'] = 'Fri, 24 Oct 2026 17:10:00 +0900'
    msg.set_content('plain')
    msg.add_alternative(MAIL_HTML, subtype='html')
    up = 'data:message/rfc822;base64,' + base64.b64encode(msg.as_bytes()).decode()
    m = mail_text.from_upload(up, 'a.eml')
    assert m['name'] == '원장님 일정' and m['mail_date'] == '2026-10-24' and '| AI센터 보고 |' in m['text']
    with pytest.raises(ValueError, match='.msg .eml'):
        mail_text.from_upload(up, 'a.pdf')
    with pytest.raises(ValueError, match='Outlook'):
        mail_text.from_upload(up, 'broken.msg')


def test_rule_parse_table_rows():
    m = mail_text.from_pasted(PASTED)
    evs = schedules.rule_parse(m['text'], m['mail_date'])
    assert [(e['date'], e['start_time'], e['end_time'], e['title'], e['location'], e['attendees']) for e in evs] == [
        ('2026-10-27', '09:00', '10:00', '주간 임원회의', '본관 5층 / 대회의실', '전 임원'),
        ('2026-10-27', '14:00', '', 'AI센터 보고', '원장실', '김영수 부사장'),  # 날짜 칸이 빈 행 = 위와 같은 날
        ('2026-10-29', '', '', '해외 출장', '미국', '')]  # 종일
    # 12월 메일의 1월 일정은 다음 해
    assert schedules.rule_parse('1/5 10:00 신년 하례', '2026-12-28')[0]['date'] == '2027-01-05'


def test_clean_event_and_times():
    assert schedules.norm_time('오후 2시 30분') == '14:30' and schedules.norm_time('0900') == '09:00'
    e = schedules.clean_event({'date': '2026.10.27', 'start': '오후 3시', 'end': '14:00', 'title': ' 면담 ',
                               'category': '모름'})
    assert (e['date'], e['start_time'], e['end_time'], e['title'], e['category']) == (
        '2026-10-27', '15:00', '', '면담', '기타')  # 종료가 시작보다 빠르면 버림
    assert schedules.clean_event({'date': '', 'title': 'x'}) is None


def test_import_flow_with_ai_update_and_apply_all(monkeypatch):
    calls = []

    def fake_chat(system, user, temperature=0.2):
        calls.append(user)
        if '수정' in user:
            return json.dumps({'events': [
                {'date': '2026-10-27', 'start_time': '10:00', 'end_time': '11:00', 'title': '주간 임원회의',
                 'location': '본관 5층 대회의실', 'attendees': '전 임원', 'category': '회의', 'details': '시간 변경'},
                {'date': '2026-10-30', 'start_time': '16:00', 'title': '외부 강연', 'category': '외부'}]})
        return '<think>…</think>' + json.dumps({'events': [
            {'date': '2026-10-27', 'start_time': '09:00', 'end_time': '10:00', 'title': '주간 임원회의',
             'location': '본관 5층 대회의실', 'attendees': '전 임원', 'category': '회의'},
            {'date': '2026-10-27', 'start_time': '14:00', 'title': 'AI센터 보고', 'location': '원장실',
             'attendees': '김영수 부사장', 'category': '보고'},
            {'date': 'bad', 'title': '버림'}]})
    monkeypatch.setattr(llm, 'is_configured', lambda: True)
    monkeypatch.setattr(llm, 'chat', fake_chat)

    m = mail_text.from_pasted(PASTED.replace('<tr><th>일자</th><th>시간</th><th>내용</th><th>장소</th><th>참석</th></tr>', ''))
    first = schedules.create_import(m['name'], m['mail_date'], m['text'])  # 제목 행 없는 표 → AI
    schedules.submit_import(first, background=False)
    imp = schedules.get_import(first)
    assert imp['status'] == 'done' and len(imp['events']) == 2
    assert '메일 보낸 날짜: 2026-10-24 (토)' in calls[0]
    assert [p['state'] for p in schedules.preview(imp['events'])] == ['new', 'new']

    second = schedules.create_import('원장님 일정 수정', '2026-10-26', '수정 메일 본문')
    schedules.submit_import(second, background=False)
    # 수정 메일이 먼저 도착해도 옛 메일부터 반영 → 최신 값(10시)이 남는다
    r = schedules.apply_all_pending()
    assert r == {'mails': 2, 'added': 3, 'updated': 1, 'same': 0}
    week = schedules.list_range('2026-10-26', '2026-11-01')
    assert [(e['date'], e['start_time'], e['title']) for e in week] == [
        ('2026-10-27', '10:00', '주간 임원회의'), ('2026-10-27', '14:00', 'AI센터 보고'), ('2026-10-30', '16:00', '외부 강연')]
    assert week[0]['details'] == '시간 변경' and week[0]['source'] == 'mail'
    assert all(i['applied'] for i in schedules.list_imports())
    assert schedules.apply_all_pending()['mails'] == 0  # 이미 반영한 메일은 다시 안 함
    assert [p['state'] for p in schedules.preview(schedules.get_import(first)['events'])] == ['changed', 'same']


def test_import_without_llm_uses_rules_and_reports():
    iid = schedules.create_import('x', '2026-10-24', '안녕하세요. 일정 없음')
    schedules.submit_import(iid, background=False)
    imp = schedules.get_import(iid)
    assert imp['status'] == 'failed' and '일정을 찾지 못했습니다' in imp['error'] and 'LLM 미설정' in imp['error']


def test_manual_save_search_and_lanes():
    a = schedules.save({'date': '2026-10-27', 'start_time': '09:00', 'end_time': '11:00', 'title': '회의 A',
                        'location': '본관'})
    schedules.save({'date': '2026-10-27', 'start_time': '10:00', 'title': '면담 B', 'attendees': '김영수'})
    schedules.save({'date': '2026-10-27', 'start_time': '13:00', 'title': '보고 C'})
    with pytest.raises(ValueError):
        schedules.save({'date': '2026-10-27', 'title': ''})
    schedules.save({**a, 'title': '회의 A (변경)'}, a['id'])
    assert [e['title'] for e in schedules.search('김영수')] == ['면담 B']
    lanes = director_ui._lanes(schedules.list_range('2026-10-27', '2026-10-27'))
    assert [(e['title'], lane, n) for e, lane, n in lanes] == [('회의 A (변경)', 0, 2), ('면담 B', 1, 2), ('보고 C', 0, 1)]
    schedules.delete(a['id'])
    assert len(schedules.list_range('2026-10-01', '2026-10-31')) == 2


def _korean_eml(charset: str, with_date: bool = True) -> str:
    html = '<table><tr><td>10/27(월)</td><td>09:00~10:00</td><td>똠양꿍 오찬</td><td>본관</td></tr></table>'
    b64 = lambda t: base64.b64encode(t.encode('cp949')).decode()  # noqa: E731
    head = (f'Subject: =?{charset}?B?{b64("원장님 주간 일정")}?=\n'
            + ('Date: Fri, 23 Oct 2026 17:10:00 +0900\n' if with_date else '')
            + 'MIME-Version: 1.0\nContent-Type: multipart/alternative; boundary="b1"\n\n')
    body = (f'--b1\nContent-Type: text/plain; charset="{charset}"\nContent-Transfer-Encoding: base64\n\n'
            f'{b64("보낸 날짜: 2026년 10월 22일 목요일")}\n'
            f'--b1\nContent-Type: text/html; charset="{charset}"\nContent-Transfer-Encoding: base64\n\n'
            f'{b64(html)}\n--b1--\n')
    return 'data:message/rfc822;base64,' + base64.b64encode((head + body).encode()).decode()


@pytest.mark.parametrize('charset', ['ks_c_5601-1987', 'euc-kr'])
def test_korean_corporate_eml(charset):
    m = mail_text.from_upload(_korean_eml(charset), '원장님일정.eml')
    assert m['name'] == '원장님 주간 일정' and m['mail_date'] == '2026-10-23'
    assert '10/27(월) | 09:00~10:00 | 똠양꿍 오찬 | 본관' in m['text']  # euc-kr 이라고 적힌 cp949 확장 한글도
    ev = schedules.rule_parse(m['text'], m['mail_date'])
    assert [(e['date'], e['title']) for e in ev] == [('2026-10-27', '똠양꿍 오찬')]


MERGED_HTML = """<table><tr><th>일자</th><th>시간</th><th>내용</th><th>장소</th></tr>
<tr><td rowspan="2">10/21(화)</td><td>00;00 ~</td><td>일정 1</td><td rowspan="3">원장실</td></tr>
<tr><td>01;00 ~</td><td>일정 2</td></tr>
<tr><td>10/22(수)</td><td>09:00~10:00</td><td>일정 3<br>준비: 자료</td></tr>
<tr><td colspan="2">비고</td><td>없음</td><td>-</td></tr></table>"""


def test_merged_cells_are_expanded():
    text = mail_text.html_to_text(MERGED_HTML)
    assert text.splitlines() == [
        '일자 | 시간 | 내용 | 장소',
        '10/21(화) | 00;00 ~ | 일정 1 | 원장실',
        '10/21(화) | 01;00 ~ | 일정 2 | 원장실',  # 세로 병합 → 아래 행에도 날짜·장소
        '10/22(수) | 09:00~10:00 | 일정 3 / 준비: 자료 | 원장실',
        '비고 | | 없음 | -']
    ev = schedules.rule_parse(text, '2026-10-20')
    assert [(e['date'], e['start_time'], e['title']) for e in ev] == [
        ('2026-10-21', '00:00', '일정 1'), ('2026-10-21', '01:00', '일정 2'), ('2026-10-22', '09:00', '일정 3 / 준비: 자료')]


def test_pasted_table_without_date_cell_gets_previous_date(monkeypatch):
    pasted = ('보낸 날짜: 2026년 10월 20일 월요일\n제목: 원장님 일정\n원장님 일정입니다\t\t\n'
              '10/21(화)\t00;00 ~\t일정 1\n\t01;00 ~\t일정 2\n01;30 ~\t일정 3\n감사합니다.\n15:00 회의')
    m = mail_text.from_pasted(pasted)
    filled = schedules.fill_dates(m['text'], m['mail_date']).splitlines()
    assert filled[3:6] == ['10/21(화) | 00;00 ~ | 일정 1', '2026-10-21 | 01;00 ~ | 일정 2', '2026-10-21 | 01;30 ~ | 일정 3']
    assert filled[-1] == '15:00 회의'  # 표 밖 문장 뒤는 이어 쓰지 않음
    seen = {}

    def fake_chat(system, user, temperature=0.2):
        seen['user'] = user
        return json.dumps({'events': [{'date': '2026-10-21', 'start_time': '01:00', 'title': '일정 2'}]})
    monkeypatch.setattr(llm, 'is_configured', lambda: True)
    monkeypatch.setattr(llm, 'chat', fake_chat)
    iid = schedules.create_import(m['name'], m['mail_date'], m['text'])
    schedules.submit_import(iid, background=False)
    assert '2026-10-21 | 01;00 ~ | 일정 2' in seen['user']  # AI 에는 날짜를 채운 표를 준다
    assert '병합' in schedules.PARSE_SYSTEM
    assert schedules.get_import(iid)['raw_text'] == m['text']  # 원문은 그대로 보관


HEADER_TABLE = """<table><tr><th>일자</th><th>요일</th><th>시간</th><th>구분</th><th>일정</th><th>장소</th><th>참석자</th>
<th>비고</th><th>담당</th></tr>
<tr><td rowspan=2>21일</td><td rowspan=2>화</td><td>00;00 ~</td><td>회의</td><td>일정 1</td><td>원장실</td>
<td>김영수 부사장</td><td></td><td>비서실</td></tr>
<tr><td>01;00 ~ 02;00</td><td>보고</td><td>일정 2</td><td>본관</td><td></td><td>자료 준비</td><td></td></tr>
<tr><td>22일</td><td>수</td><td>종일</td><td></td><td>해외 출장</td><td>미국</td><td></td><td></td><td></td></tr>
<tr><td>3일</td><td>월</td><td>10:00</td><td></td><td>다음 달 일정</td><td></td><td></td><td></td><td></td></tr></table>
<p>감사합니다</p>"""


def test_header_table_reads_columns_by_name(monkeypatch):
    called = []
    monkeypatch.setattr(llm, 'is_configured', lambda: True)
    monkeypatch.setattr(llm, 'chat', lambda *a, **k: called.append(1) or '{}')
    iid = schedules.create_import('일정', '2026-10-20', mail_text.html_to_text(HEADER_TABLE))
    schedules.submit_import(iid, background=False)
    imp = schedules.get_import(iid)
    assert called == [] and '제목 행' in imp['error']  # 제목 행이 있으면 AI 없이 열 이름대로
    assert [(e['date'], e['start_time'], e['end_time'], e['category'], e['title'], e['location'], e['attendees'],
             e['details']) for e in imp['events']] == [
        ('2026-10-21', '00:00', '', '회의', '일정 1', '원장실', '김영수 부사장', '비서실'),
        ('2026-10-21', '01:00', '02:00', '보고', '일정 2', '본관', '', '자료 준비'),
        ('2026-10-22', '', '', '', '해외 출장', '미국', '', ''),  # 종일
        ('2026-11-03', '10:00', '', '', '다음 달 일정', '', '', '')]  # '3일' — 날이 줄면 다음 달


def test_header_table_pasted_with_missing_merged_cell():
    text = mail_text.from_pasted('일시\t내용\t장소\n10/21(화) 09:00~10:00\t회의\t본관\n'
                                 '일자\t시간\t행사명\t장소\t참석\n10월 22일\t14:00\t강연\t코엑스\t원장님\n'
                                 '15:00~16:00\t면담\t원장실\t')['text']
    ev = schedules.table_parse(text, '2026-10-20')
    assert [(e['date'], e['start_time'], e['end_time'], e['title'], e['location'], e['attendees']) for e in ev] == [
        ('2026-10-21', '09:00', '10:00', '회의', '본관', ''),  # 일시 한 칸
        ('2026-10-22', '14:00', '', '강연', '코엑스', '원장님'),
        ('2026-10-22', '15:00', '16:00', '면담', '원장실', '')]  # 날짜 칸이 빠진 행은 앞을 비워 맞춤


def test_bulk_delete_and_undo_import():
    iid = schedules.create_import('일정', '2026-10-20', mail_text.html_to_text(HEADER_TABLE))
    schedules.submit_import(iid, background=False)
    schedules.apply_events(schedules.get_import(iid)['events'], iid)
    manual = schedules.save({'date': '2026-10-21', 'start_time': '18:00', 'title': '직접 넣은 만찬'})
    assert [i['n_events'] for i in schedules.list_imports()] == [4]
    # 메일로 들어간 일정만 지우고 메일은 '확인 전'으로
    assert schedules.delete_by_import(iid) == 4
    imp = schedules.list_imports()[0]
    assert imp['n_events'] == 0 and not imp['applied'] and imp['status'] == 'done'
    assert [e['title'] for e in schedules.list_range('2026-10-01', '2026-11-30')] == ['직접 넣은 만찬']
    schedules.apply_all_pending()  # 다시 반영 가능
    ids = [e['id'] for e in schedules.list_range('2026-10-21', '2026-10-21')]
    assert schedules.delete_many(ids) == 3 and schedules.delete_many([]) == 0
    assert [e['title'] for e in schedules.list_range('2026-10-01', '2026-11-30')] == ['해외 출장', '다음 달 일정']
    assert manual['id'] in ids
