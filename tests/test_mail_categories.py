"""받은 메일 1차 분류 — 먼저 분류(규칙·AI)를 정하고 그 분류의 처리에 맞는 AI 만 부른다. 분류 관리·규칙·모아 보기."""
import json

import pytest

from services import db, llm, mail_categories, mailbox, schedules
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
    assert [(c['name'], c['action']) for c in mail_categories.list_categories()] == [
        ('원장님 일정', 'schedule'), ('피플팀장 일정', 'schedule_people'), ('임원 부재', 'absence'),
        ('업무 이력', 'task'), ('기타', '')]
    by = _fetch([_mail('주간 일정', 'b@x', SCHEDULE_HTML, mid='<1@x>'), _mail('자료 요청', 'a@x', text='보내 주세요', mid='<2@x>')])
    assert by['주간 일정']['category_id'] == _cat('원장님 일정') and by['주간 일정']['category_source'] == 'kind'
    assert by['자료 요청']['category_id'] == _cat('업무 이력')
    assert mail_categories.pending_count() == 0
    assert [m['subject'] for m in mailbox.list_mails(category=_cat('원장님 일정'))] == ['주간 일정']
    assert mail_categories.counts() == {_cat('원장님 일정'): 1, _cat('업무 이력'): 1}


def _llm(monkeypatch, category_for):
    """가짜 LLM — 1차 분류는 category_for(제목), 그 밖의 AI 호출은 무엇을 불렀는지 기록."""
    called = []

    def chat(system, user, temperature=0.2):
        if '분류 목록:' in user and '[메일 ' not in user:
            subject = user.split('제목: ')[1].split('\n')[0]
            called.append(('classify', subject))
            return json.dumps({'category': category_for(subject), 'reason': '내용'}, ensure_ascii=False)
        if '분류 목록:' in user:
            called.append(('batch', ''))
            return json.dumps({'items': []})
        if 'absences' in system:
            called.append(('absence', ''))
            return json.dumps({'absences': [{'person': '김영수', 'kind': '휴가', 'start_date': '2026-10-27'}]})
        if '"kind"' in system:
            called.append(('tasks', ''))
            return json.dumps({'kind': 'task', 'reason': '요청', 'tasks': [{'title': '자료 보내기'}]}, ensure_ascii=False)
        if 'parent' in system.lower() or '상위 업무' in system:
            called.append(('parent', ''))
            return json.dumps({'parent_id': None})
        called.append(('schedule', ''))
        return json.dumps({'events': [{'date': '2026-10-27', 'start_time': '09:00', 'title': '주간 임원회의'}]},
                          ensure_ascii=False)
    monkeypatch.setattr(llm, 'is_configured', lambda: True)
    monkeypatch.setattr(llm, 'chat', chat)
    return called


def test_classify_first_then_only_that_categorys_ai(monkeypatch):
    from services import store
    store.create_person('김영수', position='부사장')
    names = {'원장님 주간': '원장님 일정', '피플팀장 면담': '피플팀장 일정', '부사장 휴가': '임원 부재',
             '자료 요청': '업무 이력', '뉴스레터': '기타'}
    called = _llm(monkeypatch, lambda subj: names[subj])
    by = _fetch([_mail(s, 'a@x', text=f'{s} 본문 김영수', mid=f'<{i}@x>') for i, s in enumerate(names)])
    assert {s: (mailbox.get_mail(m['id'])['category_id'], m['category_source']) for s, m in by.items()} == {
        s: (_cat(n), 'ai') for s, n in names.items()}
    kinds = {s: m['kind'] for s, m in by.items()}
    assert kinds == {'원장님 주간': 'schedule', '피플팀장 면담': 'schedule', '부사장 휴가': 'absence',
                     '자료 요청': 'task', '뉴스레터': ''}
    owners = {s: schedules.get_import(by[s]['schedule_import_id'])['owner'] for s in ('원장님 주간', '피플팀장 면담')}
    assert owners == {'원장님 주간': 'director', '피플팀장 면담': 'people'}
    assert by['부사장 휴가']['suggestion']['absences'][0]['person'] == '김영수'
    assert by['자료 요청']['suggestion']['tasks'][0]['title'] == '자료 보내기'
    assert '분류: 원장님 일정' in by['원장님 주간']['reason'] and '→ 원장님 일정 정리' in by['원장님 주간']['reason']
    from collections import Counter  # 분류마다 그 처리만 (뉴스레터는 분류만, 상위 업무 추천은 업무 이력에 딸림)
    assert Counter(k for k, _ in called if k not in ('classify', 'parent')) == {'schedule': 2, 'absence': 1, 'tasks': 1}
    assert ('batch', '') not in called  # 받을 때 이미 분류 — 다시 묻지 않는다

    # 처리가 없는 분류로 옮기면 분류만 — 이미 만든 결과는 그대로
    sched = by['원장님 주간']
    mail_categories.set_category(sched['id'], _cat('기타'))
    assert mailbox.reprocess_category(sched['id']) is False
    assert mailbox.get_mail(sched['id'])['schedule_import_id'] == sched['schedule_import_id']
    # 사람이 분류를 바꾸면 그 분류의 처리를 다시
    m = by['뉴스레터']
    mail_categories.set_category(m['id'], _cat('업무 이력'))
    assert mailbox.reprocess_category(m['id']) is True
    m = mailbox.get_mail(m['id'])
    assert m['kind'] == 'task' and m['category_source'] == 'manual' and m['suggestion']['tasks']


def test_ai_classifies_backlog_at_once_and_manual_choice_stays(monkeypatch):
    asked = []
    by = _fetch([_mail(f'메일{i}', 'a@x', text='본문', mid=f'<{i}@x>') for i in range(3)])
    from services.db import InboundMail
    with db.SessionLocal() as s:  # 분류 기능 전에 받은 메일처럼
        s.query(InboundMail).update({'category_id': None, 'category_source': ''})
        s.commit()

    def fake_chat(system, user, temperature=0.2):
        asked.append(user)
        ids = [int(x.split(']')[0]) for x in user.split('[메일 ')[1:]]
        return json.dumps({'items': [{'id': i, 'category': '기타' if n % 2 else '업무 이력'}
                                     for n, i in enumerate(ids)]}, ensure_ascii=False)
    monkeypatch.setattr(llm, 'is_configured', lambda: True)
    monkeypatch.setattr(llm, 'chat', fake_chat)
    assert mail_categories.categorize(mail_categories.pending()) == 3
    assert len(asked) == 1 and '업무 이력: 해야 할 일' in asked[0]  # 설명을 함께 — 한 번에
    assert {mailbox.get_mail(m['id'])['category_source'] for m in by.values()} == {'ai'}
    mail_categories.set_category(by['메일0']['id'], _cat('기타'))
    assert mail_categories.reset_auto() == 2  # 사람이 고른 것은 그대로
    assert mailbox.get_mail(by['메일0']['id'])['category_source'] == 'manual'


def test_upgrade_old_default_categories():
    from services.db import InboundMail, MailCategory
    from services.mail_category_defaults import upgrade_defaults
    with db.SessionLocal() as s:  # 예전 기본 분류(처리 없음) + 사람이 더한 분류
        s.query(MailCategory).delete()
        old = ['일정', '업무 요청', '임원 동향', '보고·자료', '공지·안내', '기타', '외부 협력']
        for i, n in enumerate(old):
            s.add(MailCategory(name=n, description='예전', sort=i))
        s.flush()
        rep = s.query(MailCategory).filter_by(name='보고·자료').one()
        s.add(InboundMail(uidl='u1', subject='보고', status='review', category_id=rep.id, category_source='ai'))
        s.commit()
        assert upgrade_defaults(s, MailCategory, InboundMail) is True
        s.commit()
        assert upgrade_defaults(s, MailCategory, InboundMail) is False  # 한 번만
    assert [(c['name'], c['action']) for c in mail_categories.list_categories()] == [
        ('원장님 일정', 'schedule'), ('피플팀장 일정', 'schedule_people'), ('임원 부재', 'absence'),
        ('업무 이력', 'task'), ('외부 협력', ''), ('기타', '')]
    m = mailbox.list_mails()[0]
    assert m['category_id'] is None and mail_categories.pending_count() == 1  # 지운 분류의 메일은 다시 분류


def test_manage_categories_with_actions():
    rows = mail_categories.list_categories()
    rows.append({'id': None, 'name': '외부 협력', 'description': '대학·연구소', 'action': 'task'})
    rows[0]['name'] = '원장 일정'
    assert mail_categories.save_all(rows) == {'saved': 6, 'deleted': 0}
    cats = mail_categories.list_categories()
    assert cats[0]['name'] == '원장 일정' and cats[0]['action'] == 'schedule' and cats[-1]['action'] == 'task'
    with pytest.raises(ValueError, match='두 번'):
        mail_categories.save_all([{'name': 'A'}, {'name': 'a'}])
    mail_categories.save_all([{**r, 'action': 'bogus'} if r['name'] == '기타' else r for r in cats])
    assert next(c for c in mail_categories.list_categories() if c['name'] == '기타')['action'] == ''


def test_rules_decide_category_and_its_processing(monkeypatch):
    by = _fetch([_mail('주간 보고', 'report@team.x', text='보고드립니다', mid='<1@x>')])
    mailbox.save_rule({'field': 'sender', 'pattern': 'report@', 'action': 'classify', 'category_id': _cat('기타')})
    assert mail_categories.apply_rules() == 1
    assert mailbox.get_mail(by['주간 보고']['id'])['category_source'] == 'rule'
    called = _llm(monkeypatch, lambda subj: '업무 이력')
    mailbox.save_rule({'field': 'subject', 'pattern': '[비서실]', 'action': 'classify', 'category_id': _cat('원장님 일정')})
    by = _fetch([_mail('주간 보고', 'report@team.x', text='보고드립니다', mid='<1@x>'),
                 _mail('[비서실] 다음 주 일정', 'sec@x', text='일정표', mid='<2@x>')])
    m = by['[비서실] 다음 주 일정']
    assert m['category_id'] == _cat('원장님 일정') and m['category_source'] == 'rule' and m['kind'] == 'schedule'
    assert ('classify', '[비서실] 다음 주 일정') not in called  # 규칙이 정하면 AI 에게 분류를 묻지 않는다
