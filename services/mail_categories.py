"""받은 메일 1차 분류 — 메일을 '일정·업무 요청·임원 동향·보고·공지…' 같은 분류로 나눠 같은 분류끼리 모아 본다.

분류(이름·설명)는 받은 메일 화면 '분류 관리'에서 고친다. 설명은 AI 가 고를 때 참고한다.
메일을 받으면 AI 가 여러 통을 한 번에 분류하고(categorize), 쉬는 시간에도 아직 분류하지 않은 메일을 채운다.
LLM 이 없으면 처리 종류(일정·업무·부재·무시)를 같은 이름의 분류로 옮긴다. 사람이 고른 분류(manual)는 건드리지 않는다.
"""
from sqlalchemy import func, or_, select, update

from services import db, llm
from services.db import InboundMail, MailCategory

DEFAULTS = [
    ('일정', '원장님·피플팀장 일정, 회의·행사 일시 안내·변경'),
    ('업무 요청', '자료 제출·검토·회신·보고 요청 등 해야 할 일이 있는 메일'),
    ('임원 동향', '임원의 휴가·출장·부재, 인사·근황 소식'),
    ('보고·자료', '보고서·회의자료·문서를 보내 주는 메일 (참고용)'),
    ('공지·안내', '전사 공지, 제도·시스템 안내, 행사 홍보, 뉴스레터'),
    ('기타', '어느 분류에도 맞지 않는 메일'),
]
# LLM 이 없을 때 — 처리 종류 → 같은 뜻의 분류 이름
KIND_TO_NAME = {'schedule': '일정', 'both': '업무 요청', 'task': '업무 요청', 'absence': '임원 동향', 'ignore': '공지·안내'}
BATCH = 12  # AI 에게 한 번에 맡길 메일 수
SOURCES = {'ai': 'AI 분류', 'kind': '처리 종류로', 'manual': '직접 고름', 'rule': '규칙'}

SYSTEM = """너는 임원조직 담당자의 받은 메일을 분류하는 비서다. 메일마다 아래 분류 중 가장 맞는 하나를 고른다.
분류 이름은 목록에 있는 그대로 쓴다. 맞는 것이 없으면 '기타'가 있으면 '기타', 없으면 빈 문자열.
반드시 JSON 만 출력: {"items": [{"id": 메일번호, "category": "분류 이름"}]}"""


def list_categories() -> list[dict]:
    with db.SessionLocal() as s:
        return [{'id': c.id, 'name': c.name, 'description': c.description or '', 'sort': c.sort or 0}
                for c in s.scalars(select(MailCategory).order_by(MailCategory.sort, MailCategory.id))]


def save_all(rows: list[dict]) -> dict:
    """분류 표 전체 저장 — 표 순서대로, 이름이 바뀌면 같은 분류(id)로, 표에 없는 분류는 지운다(그 메일은 미분류)."""
    items, seen = [], set()
    for i, r in enumerate(rows or [], 1):
        name = str(r.get('name') or '').strip()[:50]
        desc = str(r.get('description') or '').strip()[:500]
        if not name:
            if desc:
                raise ValueError(f'{i}번째 줄: 분류 이름을 적으세요.')
            continue
        if name.lower() in seen:
            raise ValueError(f"'{name}' 분류가 두 번 있습니다.")
        seen.add(name.lower())
        items.append({'id': r.get('id'), 'name': name, 'description': desc})
    with db.SessionLocal() as s:
        have = {c.id: c for c in s.scalars(select(MailCategory))}
        keep = {it['id'] for it in items if it['id'] in have}
        gone = [c for cid, c in have.items() if cid not in keep]
        for c in gone:
            s.execute(update(InboundMail).where(InboundMail.category_id == c.id)
                      .values(category_id=None, category_source=''))  # 다시 분류된다
            s.delete(c)
        s.flush()
        for c in have.values():  # 이름 바꾸기가 겹치지 않게 잠시 다른 이름으로
            if c.id in keep:
                c.name = f'__{c.id}__'
        s.flush()
        for i, it in enumerate(items):
            c = have.get(it['id']) if it['id'] in keep else MailCategory()
            c.name, c.description, c.sort = it['name'], it['description'], i
            s.add(c)
        s.commit()
    return {'saved': len(items), 'deleted': len(gone)}


def set_category(mail_id: int, category_id: int | None, source: str = 'manual') -> None:
    with db.SessionLocal() as s:
        m = s.get(InboundMail, mail_id)
        if m is None:
            raise ValueError('메일을 찾을 수 없습니다.')
        m.category_id = category_id or None
        m.category_source = source if category_id else ('manual' if source == 'manual' else '')
        s.commit()


def reset_auto() -> int:
    """AI·종류로 정한 분류를 비워 다시 분류하게 한다 (분류를 고친 뒤). 사람이 고른 것은 그대로. 비운 수."""
    with db.SessionLocal() as s:
        n = s.execute(update(InboundMail).where(InboundMail.category_source.in_(['ai', 'kind']))
                      .values(category_id=None, category_source='')).rowcount
        s.commit()
    return n or 0


def _uncategorized():
    return (or_(InboundMail.category_source.is_(None), InboundMail.category_source == ''),
            InboundMail.status != 'skipped', InboundMail.status != 'pending')


def pending(limit: int = BATCH) -> list[int]:
    """아직 분류하지 않은 메일 — 최근 것부터."""
    with db.SessionLocal() as s:
        return list(s.scalars(select(InboundMail.id).where(*_uncategorized())
                              .order_by(InboundMail.id.desc()).limit(limit)))


def pending_count() -> int:
    with db.SessionLocal() as s:
        return s.scalar(select(func.count()).select_from(InboundMail).where(*_uncategorized())) or 0


def _by_name(cats: list[dict]) -> dict[str, int]:
    return {c['name'].strip().lower(): c['id'] for c in cats}


def categorize(mail_ids: list[int]) -> int:
    """메일들을 분류한다 — LLM 이 있으면 한 번에 묻고, 없으면(또는 AI 가 못 고르면) 처리 종류로. 분류한 수.
    LLM 오류는 그대로 올린다."""
    cats = list_categories()
    if not mail_ids or not cats:
        return 0
    names = _by_name(cats)
    from services import mailbox
    rules = mailbox.list_rules()
    with db.SessionLocal() as s:
        mails = [m for m in s.scalars(select(InboundMail).where(InboundMail.id.in_(mail_ids)))
                 if m.category_source in (None, '')]
        done = 0
        for m in list(mails):  # 분류를 정한 규칙이 맞으면 그대로 (AI 에게 묻지 않는다)
            cid = mailbox.rule_category(m.subject, m.sender, m.body, rules)
            if cid:
                m.category_id, m.category_source = cid, 'rule'
                mails.remove(m)
                done += 1
        picked: dict[int, int] = {}
        if llm.is_configured() and mails:
            listing = '\n'.join(f"- {c['name']}: {c['description']}" for c in cats)
            body = '\n\n'.join(f"[메일 {m.id}] 보낸 사람: {m.sender or ''}\n제목: {m.subject or ''}\n"
                               f"처리 종류: {m.kind or '-'}\n본문 앞부분: {(m.body or '')[:400]}" for m in mails)
            data = llm._extract_json(llm.chat(SYSTEM, f'분류 목록:\n{listing}\n\n메일:\n{body}'))
            for it in data.get('items') or []:
                try:
                    mid = int(it.get('id'))
                except (TypeError, ValueError):
                    continue
                cid = names.get(str(it.get('category') or '').strip().lower())
                if cid:
                    picked[mid] = cid
        for m in mails:
            if m.id in picked:
                m.category_id, m.category_source = picked[m.id], 'ai'
            else:  # AI 가 못 골랐거나 LLM 이 없으면 처리 종류로 (그것도 없으면 '기타'·미분류로 두고 다시 묻지 않음)
                cid = names.get(KIND_TO_NAME.get(m.kind or '', '').lower()) or names.get('기타')
                m.category_id, m.category_source = cid, 'kind'
            done += 1
        s.commit()
    return done


def apply_rules() -> int:
    """분류를 정한 규칙을 이미 받은 메일에 — 사람이 고른 분류는 그대로. 바꾼 수."""
    from services import mailbox
    rules = mailbox.list_rules()
    if not any(r['category_id'] for r in rules if r['active']):
        return 0
    n = 0
    with db.SessionLocal() as s:
        for m in s.scalars(select(InboundMail).where(InboundMail.status != 'skipped',
                                                    or_(InboundMail.category_source.is_(None),
                                                        InboundMail.category_source != 'manual'))):
            cid = mailbox.rule_category(m.subject, m.sender, m.body, rules)
            if cid and (m.category_id != cid or m.category_source != 'rule'):
                m.category_id, m.category_source = cid, 'rule'
                n += 1
        s.commit()
    return n


def counts(statuses: list[str] | None = None) -> dict:
    """분류별 메일 수 — {분류 id 또는 None(미분류): 수}. statuses 가 있으면 그 처리 상태만."""
    with db.SessionLocal() as s:
        q = select(InboundMail.category_id, func.count()).where(InboundMail.status != 'skipped')
        if statuses:
            q = q.where(InboundMail.status.in_(statuses))
        return dict(s.execute(q.group_by(InboundMail.category_id)).all())
