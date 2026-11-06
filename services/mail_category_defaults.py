"""받은 메일 기본 분류와 처리 — db.py(처음 만들 때·예전 분류 바꾸기)와 mail_categories.py 가 같이 쓴다.
다른 services 모듈을 불러오지 않는다 (db.py 가 시작할 때 불러도 순환하지 않게)."""
from sqlalchemy import select, update

DEFAULTS = [  # (이름, 설명 — AI 가 고를 때 참고, 처리)
    ('원장님 일정', '원장님(기관장) 일정을 알리거나 바꾸는 메일 — 비서실 주간 일정, 회의·행사·보고 일시 안내', 'schedule'),
    ('피플팀장 일정', '피플팀장 본인의 일정을 알리거나 바꾸는 메일', 'schedule_people'),
    ('임원 부재', '임원(상무·부사장·마스터 등)의 휴가·출장·교육·병가 등 자리 비움 안내, 부재 현황표', 'absence'),
    ('업무 이력', '해야 할 일이 있거나 업무 기록으로 남길 메일 — 요청·지시·자료 제출·검토·회신, 진행 상황·결과 공유', 'task'),
    ('기타', '공지·광고·뉴스레터·자동 발송 등 위에 맞지 않는 메일', ''),
]
# 분류의 처리 — 이 분류로 정해지면 그 일만 AI 에게 맡긴다 (mailbox.process)
CATEGORY_ACTIONS = {'schedule': '원장님 일정 정리', 'schedule_people': '피플팀장 일정 정리', 'absence': '임원 부재 추출',
                    'task': '할 일(업무) 추출', '': '처리 없음 (분류만)'}
# 예전 기본 분류 → 새 분류 (이름, 처리). None 이면 지운다 (그 메일은 다시 분류).
OLD_DEFAULTS = {'일정': ('원장님 일정', 'schedule'), '업무 요청': ('업무 이력', 'task'), '임원 동향': ('임원 부재', 'absence'),
                 '보고·자료': None, '공지·안내': None, '기타': ('기타', '')}


def upgrade_defaults(s, MailCategory, InboundMail) -> bool:
    """예전 기본 분류를 처리와 묶인 새 기본 분류로 (db.make_engine 이 부른다). 바꿨으면 True."""
    cats = list(s.scalars(select(MailCategory)))
    if not cats or any(c.action for c in cats) or not any(c.name in OLD_DEFAULTS for c in cats):
        return False
    names = {c.name for c in cats}
    for c in cats:
        if c.name not in OLD_DEFAULTS:
            continue
        new = OLD_DEFAULTS[c.name]
        if new is None:
            s.execute(update(InboundMail).where(InboundMail.category_id == c.id)
                      .values(category_id=None, category_source=''))
            s.delete(c)
            continue
        name, action = new
        if name != c.name and name in names:  # 같은 이름이 이미 있으면 이름은 그대로
            name = c.name
        c.name, c.action = name, action
        c.description = next(d for n, d, _ in DEFAULTS if n == new[0])
        names.add(name)
    s.flush()
    if '피플팀장 일정' not in {c.name for c in s.scalars(select(MailCategory))}:
        n, d, a = DEFAULTS[1]
        s.add(MailCategory(name=n, description=d, action=a))
        s.flush()
    order = [n for n, _, _ in DEFAULTS]
    rest = sorted(s.scalars(select(MailCategory)), key=lambda c: (
        order.index(c.name) if c.name in order and c.name != '기타' else (99 if c.name == '기타' else 50), c.sort or 0))
    for i, c in enumerate(rest):
        c.sort = i
    return True
