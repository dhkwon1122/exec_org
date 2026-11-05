"""받은 메일 1차 분류 — 분류 관리, AI 가 여러 통을 한 번에, LLM 이 없으면 처리 종류로, 사람이 고른 것은 그대로."""
import json

import pytest

from services import db, llm, mail_categories, mailbox
from tests.test_mailbox import SCHEDULE_HTML, FakePOP, _mail


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


def _cat(name):
    return next(c['id'] for c in mail_categories.list_categories() if c['name'] == name)


def _fetch(mails):
    mailbox.fetch_new(connect=lambda cfg: FakePOP(mails))
    return {m['subject']: m for m in mailbox.list_mails()}


def test_defaults_and_without_llm_kind_decides():
    assert [c['name'] for c in mail_categories.list_categories()] == [n for n, _ in mail_categories.DEFAULTS]
    by = _fetch([_mail('주간 일정', 'b@x', SCHEDULE_HTML, mid='<1@x>'), _mail('자료 요청', 'a@x', text='보내 주세요', mid='<2@x>')])
    assert by['주간 일정']['category_id'] == _cat('일정') and by['주간 일정']['category_source'] == 'kind'
    assert by['자료 요청']['category_id'] == _cat('업무 요청')
    assert mail_categories.pending_count() == 0
    assert [m['subject'] for m in mailbox.list_mails(category=_cat('일정'))] == ['주간 일정']
    assert mail_categories.counts() == {_cat('일정'): 1, _cat('업무 요청'): 1}


def test_ai_classifies_many_mails_at_once_and_manual_choice_stays(monkeypatch):
    asked = []

    def fake_chat(system, user, temperature=0.2):
        if '분류 목록' in user:
            asked.append(user)
            ids = [int(x.split(']')[0]) for x in user.split('[메일 ')[1:]]
            return json.dumps({'items': [{'id': i, 'category': '보고·자료' if n % 2 else '공지·안내'}
                                         for n, i in enumerate(ids)]}, ensure_ascii=False)
        return json.dumps({'kind': 'ignore', 'reason': '참고용', 'tasks': []})
    monkeypatch.setattr(llm, 'is_configured', lambda: True)
    monkeypatch.setattr(llm, 'chat', fake_chat)
    by = _fetch([_mail(f'메일{i}', 'a@x', text='본문', mid=f'<{i}@x>') for i in range(3)])
    assert len(asked) == 1 and '보고·자료: 보고서' in asked[0]  # 설명을 함께 — 한 번에
    assert {m['category_source'] for m in by.values()} == {'ai'}
    assert {mail_categories.list_categories()[0]['id']} - {m['category_id'] for m in by.values()}

    mail_categories.set_category(by['메일0']['id'], _cat('기타'))
    assert mail_categories.reset_auto() == 2  # 사람이 고른 것은 그대로
    assert mailbox.get_mail(by['메일0']['id'])['category_source'] == 'manual'
    assert mail_categories.pending_count() == 2
    assert mail_categories.categorize(mail_categories.pending()) == 2 and mail_categories.pending_count() == 0


def test_manage_categories_rename_reorder_delete():
    by = _fetch([_mail('공지', 'a@x', text='안내', mid='<1@x>')])
    m = by['공지']
    mail_categories.set_category(m['id'], _cat('공지·안내'))
    rows = mail_categories.list_categories()
    notice = next(r for r in rows if r['name'] == '공지·안내')
    rows = [{**notice, 'name': '사내 공지'}] + [r for r in rows if r['id'] != notice['id'] and r['name'] != '기타']
    rows.append({'id': None, 'name': '외부 협력', 'description': '대학·연구소·협력사 메일'})
    assert mail_categories.save_all(rows) == {'saved': 6, 'deleted': 1}
    cats = mail_categories.list_categories()
    assert cats[0]['name'] == '사내 공지' and cats[0]['id'] == notice['id'] and cats[-1]['name'] == '외부 협력'
    assert mailbox.get_mail(m['id'])['category_id'] == notice['id']  # 이름만 바뀜 — 같은 분류
    with pytest.raises(ValueError, match='두 번'):
        mail_categories.save_all([{'name': 'A'}, {'name': 'a'}])
    with pytest.raises(ValueError, match='이름'):
        mail_categories.save_all([{'name': '', 'description': '설명만'}])
    mail_categories.save_all([r for r in cats if r['id'] != notice['id']])  # 지우면 그 메일은 다시 분류
    assert mailbox.get_mail(m['id'])['category_id'] is None and mail_categories.pending_count() == 1


def test_idle_worker_categorizes_backlog(monkeypatch):
    from services import doc_worker
    from services.db import InboundMail
    _fetch([_mail('주간 일정', 'b@x', SCHEDULE_HTML, mid='<1@x>')])
    with db.SessionLocal() as s:  # 분류 기능 전에 받은 메일처럼
        s.query(InboundMail).update({'category_id': None, 'category_source': ''})
        s.commit()
    monkeypatch.setenv('DOC_SUMMARY_IDLE_MINUTES', '10')
    monkeypatch.setattr(doc_worker, '_pause_until', 0.0)
    assert doc_worker.step(10 ** 10) is True and mail_categories.pending_count() == 0


def test_rules_decide_category_for_new_and_existing_mails():
    by = _fetch([_mail('주간 보고', 'report@team.x', text='보고드립니다', mid='<1@x>'),
                 _mail('다른 메일', 'a@x', text='안내', mid='<2@x>')])
    old = by['주간 보고']
    assert old['category_id'] == _cat('업무 요청')  # 규칙 전 — 처리 종류로
    with pytest.raises(ValueError, match='분류를 골라야'):
        mailbox.save_rule({'field': 'sender', 'pattern': 'report@', 'action': 'classify'})
    mailbox.save_rule({'field': 'sender', 'pattern': 'report@', 'action': 'classify', 'category_id': _cat('보고·자료')})
    assert mailbox.match_rule('주간 보고', 'report@team.x', '') is None  # '분류만'은 처리를 정하지 않는다
    mail_categories.set_category(by['다른 메일']['id'], _cat('기타'))
    assert mail_categories.apply_rules() == 1  # 이미 받은 메일에도
    m = mailbox.get_mail(old['id'])
    assert m['category_id'] == _cat('보고·자료') and m['category_source'] == 'rule'
    assert mail_categories.reset_auto() == 0  # 규칙으로 정한 것은 다시 묻지 않는다

    by = _fetch([_mail('주간 보고', 'report@team.x', text='보고드립니다', mid='<1@x>'),
                 _mail('다른 메일', 'a@x', text='안내', mid='<2@x>'),
                 _mail('주간 보고 2', 'report@team.x', text='보고', mid='<3@x>')])
    new = by['주간 보고 2']
    assert new['category_id'] == _cat('보고·자료') and new['category_source'] == 'rule'
    assert new['kind'] == 'task'  # 처리는 평소대로
    # 처리 규칙에 분류를 붙여도 된다
    mailbox.save_rule({'field': 'subject', 'pattern': '공지', 'action': 'ignore', 'category_id': _cat('공지·안내')})
    by = _fetch([*(_mail(f's{i}', 'a@x', text='x', mid=f'<{i}@x>') for i in range(1, 4)),
                 _mail('[공지] 시스템 점검', 'it@x', text='점검', mid='<4@x>')])
    n = by['[공지] 시스템 점검']
    assert n['status'] == 'ignored' and n['category_id'] == _cat('공지·안내')
