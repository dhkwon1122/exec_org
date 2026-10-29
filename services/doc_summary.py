"""문서 요약·태그 — 메일 첨부와 올린 파일(참고자료 파일)의 글자를 뽑아(services/doc_text.py) AI 에게 문서 종류·요약·
태그·주요 날짜를 받는다. 같은 문서의 바로 앞 판이 있으면 — 메일로 받은 판이든 올린 판이든 (services/documents.py 의
판 순서) — 바뀐 줄을 함께 보내 '앞 판과 달라진 점'도 받는다.

뽑은 글자와 요약은 MailAttachment·Reference 의 같은 이름 칸에 남긴다. 내용이 똑같은 파일(sha256 같음)은 다시 읽거나
묻지 않고 복사한다. 아래 함수들은 둘 중 무엇이든 받는다 (_name 으로 파일 이름).
"""
import difflib
import json
import os
import re
from collections import Counter
from datetime import datetime

from sqlalchemy import or_, select

from services import db, doc_text, doc_versions, llm
from services.db import HistoryEntry, InboundMail, MailAttachment, Reference, Task

DOC_TYPES = ['보고서', '회의자료', '회의록', '계획서', '공문', '안내문', '명단', '일정표', '예산·집계표', '발표자료',
             '양식', '계약·협약', '기타']
MAX_DIFF_CHARS = 4000

SYSTEM = """너는 임원실 직원이 메일로 받은 업무 문서를 정리하는 비서다. 나중에 찾기 쉽게 문서 종류·요약·태그를 단다.
- 문서에 없는 사실을 지어내지 않는다. 숫자·이름·날짜는 문서에 있는 그대로 쓴다.
- 반드시 JSON 객체 하나만 출력한다. 다른 설명은 쓰지 않는다."""

USER = """파일 이름: {filename}
{where}
지금 쓰고 있는 태그: {known}

문서 내용{cut}:
<<<
{text}
>>>
{diff}
다음 형식의 JSON 으로 답하라.
{{
  "doc_type": "{types} 중 하나",
  "summary": "무엇에 관한 문서인지와 핵심 내용(결론·요청·수치)을 3~5문장으로",
  "tags": ["검색용 태그 3~7개"],
  "key_dates": [{{"date": "YYYY-MM-DD", "what": "그 날짜에 무엇이 있는지 (기한·행사·회의 등)"}}],
  "changes": "{changes_hint}"
}}
tags 는 짧은 명사(사업·과제·조직·사람 이름, 주제어)로 쓴다. '지금 쓰고 있는 태그'에 같은 뜻이 있으면 그 태그를 그대로 쓰고, 없을 때만 새로 만든다.
key_dates 에는 문서에 나온 기한·일정만 넣는다(없으면 빈 배열). 연도가 없으면 메일·올린 날짜의 연도로."""


def _name(a) -> str:
    """메일 첨부·올린 파일의 파일 이름."""
    return getattr(a, 'filename', None) or getattr(a, 'file_name', None) or getattr(a, 'title', '') or ''


def _twin(s, a, *cond):
    """내용이 똑같은 다른 파일 (메일 첨부·올린 파일 어느 쪽이든) — cond 는 그 파일이 갖춰야 할 칸 이름."""
    if not a.sha256:
        return None
    for model in (MailAttachment, Reference):
        q = select(model).where(model.sha256 == a.sha256, *[getattr(model, c).is_not(None) for c in cond])
        if isinstance(a, model):
            q = q.where(model.id != a.id)
        found = s.scalar(q.limit(1))
        if found is not None:
            return found
    return None


def _cached_text(s, a) -> None:
    """a 의 글자를 채운다 — 같은 내용의 파일을 읽은 적 있으면 복사, 아니면 파일에서 뽑는다."""
    if a.text_at:
        return
    twin = _twin(s, a, 'text_at')
    if twin is not None:
        a.text, a.text_note = twin.text, twin.text_note
    else:
        path = os.path.join(db.UPLOAD_DIR, a.file_path or '')
        try:
            with open(path, 'rb') as f:
                data = f.read()
        except OSError:
            a.text, a.text_note = '', '저장된 파일을 찾을 수 없습니다.'
        else:
            a.text, a.text_note = doc_text.extract(_name(a), data)
    a.text_at = datetime.now().replace(microsecond=0)


def read_text(att_id: int) -> dict:
    """{'text', 'note'} — 처음이면 파일에서 뽑아 저장한다."""
    with db.SessionLocal() as s:
        a = s.get(MailAttachment, att_id)
        if a is None:
            raise ValueError('첨부 파일을 찾을 수 없습니다.')
        _cached_text(s, a)
        s.commit()
        return {'text': a.text or '', 'note': a.text_note or ''}


def read_mail(mail_id: int) -> int:
    """메일의 첨부 글자를 모두 뽑아 둔다(AI 없이) — 받자마자 문서 내용으로도 검색되게. 읽은 파일 수."""
    with db.SessionLocal() as s:
        atts = s.scalars(select(MailAttachment).where(MailAttachment.mail_id == mail_id,
                                                      MailAttachment.text_at.is_(None))).all()
        for a in atts:
            _cached_text(s, a)
        s.commit()
        return sum(1 for a in atts if a.text)


def known_tags(limit: int = 40) -> list[tuple[str, int]]:
    """지금까지 단 태그와 그 문서 수 (많은 순) — 메일 첨부와 올린 파일."""
    with db.SessionLocal() as s:
        rows = s.scalars(select(MailAttachment.tags).where(MailAttachment.tags != '')).all() + \
            s.scalars(select(Reference.tags).where(Reference.tags != '', Reference.tags.is_not(None))).all()
    return Counter(t.strip() for r in rows for t in r.split(',') if t.strip()).most_common(limit)


def _previous(s, a):
    """같은 문서의 바로 앞 판 — 메일 첨부·올린 파일을 합친 판 순서에서 (내용이 같은 재전송이면 None)."""
    from services import documents
    ensure_reference_keys()
    me_id = f"m{a.id}" if isinstance(a, MailAttachment) else f"r{a.id}"
    versions = documents.timeline(a.doc_key)
    me = next((v for v in versions if v['id'] == me_id), None)
    if me is None or me['dup'] or me['version'] <= 1:
        return None
    prev = next((v for v in versions if v['version'] == me['version'] - 1 and not v['dup']), None)
    if prev is None:
        return None
    return s.get(MailAttachment, prev['att_id']) if prev['kind'] == 'mail' else s.get(Reference, prev['ref_id'])


def ensure_reference_keys() -> int:
    """문서 열쇠가 없는 예전 올린 파일에 열쇠·sha256 을 채운다 (한 번이면 된다). 채운 수."""
    import hashlib
    with db.SessionLocal() as s:
        rows = list(s.scalars(select(Reference).where(Reference.kind == 'file',
                                                      or_(Reference.doc_key == '', Reference.doc_key.is_(None)))))
        for r in rows:
            r.doc_key = doc_versions.parse_name(r.file_name or r.title or '')['key'][:300]
            try:
                with open(os.path.join(db.UPLOAD_DIR, r.file_path or ''), 'rb') as f:
                    r.sha256 = hashlib.sha256(f.read()).hexdigest()
            except OSError:
                pass
        if rows:
            s.commit()
        return len(rows)


def _where(s, a) -> str:
    """AI 에게 알려 줄 이 파일의 출처 한 줄."""
    if isinstance(a, MailAttachment):
        mail = s.get(InboundMail, a.mail_id)
        return (f"받은 메일: {(mail.subject if mail else '') or '-'} (보낸 사람 {(mail.sender if mail else '') or '-'}, "
                f"{(mail.mail_date if mail else '') or '-'})")
    task = s.get(Task, a.task_id) if a.task_id else None
    h = s.get(HistoryEntry, a.history_id) if a.history_id else None
    when = (a.created_at.strftime('%Y-%m-%d') if a.created_at else '-')
    return (f"직접 올린 파일 — 업무: {task.title if task else '-'}"
            + (f", 기록: {(h.title or (h.content or '')[:40])}" if h else '') + f" ({when})")


def _diff(old: str, new: str) -> str:
    """바뀐 줄만 (- 앞 판, + 이번 판)."""
    out = []
    for line in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm='', n=0):
        if line.startswith(('---', '+++', '@@')) or not line[1:].strip():
            continue
        out.append(f'{line[0]} {line[1:]}')
    text = '\n'.join(out)
    return text[:MAX_DIFF_CHARS] + ('\n…(이하 생략)' if len(text) > MAX_DIFF_CHARS else '')


def _tags(raw) -> list[str]:
    items = raw if isinstance(raw, list) else re.split(r'[,\n]', str(raw or ''))
    seen, out = set(), []
    for t in items:
        t = re.sub(r'[,#]', ' ', str(t)).strip()[:30]
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out[:8]


def _dates(raw) -> list[dict]:
    out = []
    for d in raw if isinstance(raw, list) else []:
        if isinstance(d, dict) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', str(d.get('date') or '').strip()):
            out.append({'date': d['date'].strip(), 'what': str(d.get('what') or '').strip()[:100]})
    return sorted(out, key=lambda d: d['date'])[:10]


def _ask(s, a) -> None:
    prev = _previous(s, a)
    diff = ''
    if prev is not None:
        _cached_text(s, prev)
        if prev.text and prev.text.strip() == a.text.strip():
            diff = '(앞 판과 글 내용이 같음)'
        elif prev.text:
            diff = _diff(prev.text, a.text)
    budget = max(3000, llm.MAX_INPUT_CHARS - len(diff))
    body = a.text if len(a.text) <= budget else a.text[:budget]
    data = llm._extract_json(llm.chat(SYSTEM, USER.format(
        filename=_name(a), where=_where(s, a),
        known=', '.join(t for t, _ in known_tags(40)) or '(아직 없음)',
        cut=f' (앞 {budget:,}자 — 전체 {len(a.text):,}자)' if len(a.text) > budget else '', text=body,
        diff=(f"\n같은 문서의 앞 판({_name(prev)})과 달라진 줄 (- 앞 판, + 이번 판):\n<<<\n{diff}\n>>>\n"
              if diff else ''),
        types='·'.join(DOC_TYPES),
        changes_hint='앞 판과 달라진 점을 1~3문장으로 (앞 판 정보가 없으면 빈 문자열)' if diff else '빈 문자열')))
    doc_type = str(data.get('doc_type') or '').strip()
    a.doc_type = doc_type if doc_type in DOC_TYPES else ('기타' if doc_type else '')
    a.summary = str(data.get('summary') or '').strip()[:3000]
    a.tags = ', '.join(_tags(data.get('tags')))[:500]
    a.key_dates = json.dumps(_dates(data.get('key_dates')), ensure_ascii=False)
    a.changes = str(data.get('changes') or '').strip()[:1000] if diff else ''
    a.analyzed_at = datetime.now().replace(microsecond=0)


def analyze(att_id: int, force: bool = False) -> dict:
    """메일 첨부 하나를 요약한다 — insight() 형식으로 돌려준다. 글자를 못 뽑으면 ValueError(이유)."""
    return _analyze(MailAttachment, att_id, force)


def analyze_ref(ref_id: int, force: bool = False) -> dict:
    """올린 파일(참고자료 파일) 하나를 요약한다 — 같은 문서의 앞 판(메일 첨부든 올린 파일이든)과 비교."""
    ensure_reference_keys()
    return _analyze(Reference, ref_id, force)


def _analyze(model, item_id: int, force: bool) -> dict:
    with db.SessionLocal() as s:
        a = s.get(model, item_id)
        if a is None or (model is Reference and a.kind != 'file'):
            raise ValueError('파일을 찾을 수 없습니다.')
        if a.analyzed_at and not force:
            return insight(a)
        _cached_text(s, a)
        s.commit()
        if not a.text:
            raise ValueError(f'{_name(a)}: {a.text_note or "글자가 없습니다."}')
        twin = None if force else _twin(s, a, 'analyzed_at')
        if twin is not None:  # 같은 파일을 다시 보낸 것 — 앞 판과 비교한 결과는 그 파일 것이므로 빼고 복사
            a.doc_type, a.summary, a.tags, a.key_dates = twin.doc_type, twin.summary, twin.tags, twin.key_dates
            a.changes, a.analyzed_at = '', datetime.now().replace(microsecond=0)
        else:
            if not llm.is_configured():
                raise ValueError('문서 요약은 LLM 이 설정돼 있어야 합니다.')
            _ask(s, a)
        s.commit()
        return insight(a)


def analyze_mail(mail_id: int, force: bool = False) -> dict:
    """메일의 첨부를 모두 요약 — {'done': 요약한 수, 'skipped': [(파일 이름, 이유)]}. LLM 오류는 그대로 올린다."""
    with db.SessionLocal() as s:
        atts = [(a.id, a.filename) for a in s.scalars(select(MailAttachment).where(MailAttachment.mail_id == mail_id)
                                                      .order_by(MailAttachment.id))]
    done, skipped = 0, []
    for att_id, name in atts:
        if not doc_text.supported(name):
            skipped.append((name, doc_text.extract(name, b'')[1]))
            continue
        try:
            analyze(att_id, force=force)
            done += 1
        except ValueError as e:
            skipped.append((name, str(e).split(': ', 1)[-1]))
    return {'done': done, 'skipped': skipped}


def pending_count(mail_id: int) -> int:
    """아직 요약하지 않은, 읽을 수 있는 형식의 첨부 수."""
    with db.SessionLocal() as s:
        names = s.scalars(select(MailAttachment.filename).where(MailAttachment.mail_id == mail_id,
                                                                MailAttachment.analyzed_at.is_(None))).all()
    return sum(1 for n in names if doc_text.supported(n))


def pending_refs(limit: int = 1) -> list[int]:
    """아직 요약하지 않은, 읽을 수 있는 형식의 올린 파일 — 최근 것부터 (쉬는 시간 자동 요약)."""
    ensure_reference_keys()
    with db.SessionLocal() as s:
        rows = s.execute(select(Reference.id, Reference.file_name).where(
            Reference.kind == 'file', Reference.analyzed_at.is_(None),
            or_(Reference.text_at.is_(None), Reference.text != '')).order_by(Reference.id.desc()).limit(limit * 5)).all()
    return [i for i, name in rows if doc_text.supported(name or '')][:limit]


def insight(a) -> dict:
    try:
        dates = json.loads(a.key_dates or '[]')
    except ValueError:
        dates = []
    return {'id': a.id, 'filename': _name(a), 'analyzed': a.analyzed_at is not None,
            'analyzed_at': a.analyzed_at.strftime('%Y-%m-%d %H:%M') if a.analyzed_at else '',
            'doc_type': a.doc_type or '', 'summary': a.summary or '',
            'tags': [t.strip() for t in (a.tags or '').split(',') if t.strip()], 'key_dates': dates,
            'changes': a.changes or '', 'note': a.text_note or '', 'readable': doc_text.supported(_name(a)),
            'text_len': len(a.text or '')}


def get_insight(att_id: int) -> dict | None:
    with db.SessionLocal() as s:
        a = s.get(MailAttachment, att_id)
        return insight(a) if a else None


def get_ref_insight(ref_id: int) -> dict | None:
    with db.SessionLocal() as s:
        r = s.get(Reference, ref_id)
        return insight(r) if r is not None and r.kind == 'file' else None


def search(query: str, limit: int = 30) -> list[dict]:
    """파일 이름·요약·태그·본문에 검색어가 모두 들어 있는 첨부 — 같은 문서는 가장 최근 것 하나만."""
    terms = [a or b for a, b in re.findall(r'"([^"]+)"|(\S+)', query or '') if (a or b).strip()]
    if not terms:
        return []
    cond = [or_(*(col.ilike(f'%{t}%') for col in (MailAttachment.filename, MailAttachment.summary,
                                                  MailAttachment.tags, MailAttachment.text))) for t in terms]
    with db.SessionLocal() as s:
        rows = s.execute(select(MailAttachment, InboundMail.subject, InboundMail.mail_date)
                         .join(InboundMail, InboundMail.id == MailAttachment.mail_id)
                         .where(*cond).order_by(InboundMail.mail_date.desc(), MailAttachment.id.desc())
                         .limit(limit * 5)).all()
        out, seen = [], set()
        for a, subject, mail_date in rows:
            if a.doc_key in seen:
                continue
            seen.add(a.doc_key)
            out.append({**insight(a), 'mail_id': a.mail_id, 'mail_subject': subject or '(제목 없음)',
                        'mail_date': mail_date or '', 'snippet': _snippet(a.text or '', terms)})
            if len(out) >= limit:
                break
    return out


def _snippet(text: str, terms: list[str], width: int = 60) -> str:
    low = text.lower()
    for t in terms:
        i = low.find(t.lower())
        if i >= 0:
            start = max(0, i - width)
            return ('…' if start else '') + text[start:i + len(t) + width].replace('\n', ' ') + '…'
    return ''
