"""쉬는 시간 첨부 문서 요약 — 사용자 입력이 한동안 없으면 백그라운드에서 요약하지 않은 첨부를 하나씩 요약한다.

- '사용자 입력' = 로그인한 사람의 화면 요청(app.py 가 touch() 를 부른다). 사이드바 1분 갱신(app-tick)은 치지 않는다.
  여러 gunicorn 워커가 같이 보도록 DATA_DIR/.last_activity 파일의 수정 시각으로 남긴다.
- DOC_SUMMARY_IDLE_MINUTES (기본 10) 동안 입력이 없을 때만 돈다. 0 이면 끈다. 한 번에 한 파일씩 요약하고,
  매번 다시 입력이 없는지 확인하므로 사람이 돌아오면 지금 요약 중인 파일만 마치고 멈춘다.
- 예전 첨부의 글자 뽑기(AI 없이)도 같이 채운다 — 요약 전에도 내용으로 검색되게.
- LLM 오류가 나면 10분 쉬고, 같은 파일이 3번 실패하면 그 파일은 건너뛴다(손으로 'AI 요약'은 가능).
"""
import fcntl
import json
import logging
import os
import threading
import time
from datetime import datetime

from sqlalchemy import and_, func, or_, select

from services import confluence, db, doc_summary, doc_text, llm
from services.db import InboundMail, MailAttachment

log = logging.getLogger(__name__)

MAX_FAILS = 3
PAUSE_ON_ERROR = 600
_TOUCH_EVERY = 20  # 초 — 요청마다 파일을 건드리지 않게

_last_touch = 0.0
_fails: dict[int, int] = {}
_pause_until = 0.0
_started = False
lock_holder: list = []


def idle_minutes_setting() -> int:
    try:
        return max(0, int(os.environ.get('DOC_SUMMARY_IDLE_MINUTES', '10')))
    except ValueError:
        return 10


def _activity_file() -> str:
    return os.path.join(db.DATA_DIR, '.last_activity')


def _status_file() -> str:
    return os.path.join(db.DATA_DIR, '.doc_worker_status.json')


def touch(now: float | None = None) -> None:
    """사용자 입력이 있었다고 남긴다."""
    global _last_touch
    now = time.time() if now is None else now
    if now - _last_touch < _TOUCH_EVERY:
        return
    _last_touch = now
    try:
        os.makedirs(db.DATA_DIR, exist_ok=True)
        with open(_activity_file(), 'a'):
            pass
        os.utime(_activity_file(), (now, now))
    except OSError:
        pass


def idle_seconds(now: float | None = None) -> float:
    now = time.time() if now is None else now
    try:
        return now - os.path.getmtime(_activity_file())
    except OSError:
        return float('inf')  # 아무도 들어온 적 없음


def _unread_cond():
    """요약할 거리: 아직 요약 안 했고, 글자를 아직 안 뽑았거나 뽑은 글자가 있는 첨부."""
    return and_(MailAttachment.analyzed_at.is_(None),
                or_(MailAttachment.text_at.is_(None), func.coalesce(MailAttachment.text, '') != ''))


def pending(limit: int = 200) -> list[int]:
    """요약을 기다리는 첨부 id — 최근 받은 메일 먼저. 실패가 쌓인 것은 뺀다."""
    with db.SessionLocal() as s:
        rows = s.execute(select(MailAttachment.id, MailAttachment.filename)
                         .join(InboundMail, InboundMail.id == MailAttachment.mail_id)
                         .where(_unread_cond())
                         .order_by(InboundMail.received_at.desc(), MailAttachment.id.desc()).limit(limit * 3)).all()
    return [i for i, name in rows if doc_text.supported(name) and _fails.get(i, 0) < MAX_FAILS][:limit]


def _fill_texts(batch: int = 20) -> int:
    """글자를 아직 안 뽑은 첨부를 batch 개 채운다 (AI 없음)."""
    with db.SessionLocal() as s:
        atts = s.scalars(select(MailAttachment).where(MailAttachment.text_at.is_(None))
                         .order_by(MailAttachment.id.desc()).limit(batch)).all()
        for a in atts:
            doc_summary._cached_text(s, a)
        s.commit()
        return len(atts)


def _save_status(**st) -> None:
    try:
        with open(_status_file(), 'w', encoding='utf-8') as f:
            json.dump(st, f, ensure_ascii=False)
    except OSError:
        pass


def last_status() -> dict:
    try:
        with open(_status_file(), encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def step(now: float | None = None) -> bool:
    """한 번 돈다 — 무언가 했으면 True (곧바로 다음 것을 본다)."""
    global _pause_until
    now = time.time() if now is None else now
    minutes = idle_minutes_setting()
    if not minutes or idle_seconds(now) < minutes * 60 or now < _pause_until:
        return False
    with confluence.background():
        read_refs = confluence.read_new_references(limit=3)
    if _fill_texts() or read_refs:  # 예전 첨부 글자 · 아직 안 읽은 컨플루언스 링크
        return True
    if not llm.is_configured():
        return False
    ids = pending(limit=1)
    if not ids:
        return False
    att_id = ids[0]
    at = datetime.now().strftime('%Y-%m-%d %H:%M')
    try:
        r = doc_summary.analyze(att_id)
    except llm.LLMError as e:
        _fails[att_id] = _fails.get(att_id, 0) + 1
        _pause_until = now + PAUSE_ON_ERROR
        _save_status(at=at, ok=False, error=str(e)[:200])
        log.warning('첨부 자동 요약 실패 %s: %s', att_id, e)
        return False
    except ValueError as e:  # 글자를 못 뽑음 — 다시 시도해도 같다
        _fails[att_id] = MAX_FAILS
        _save_status(at=at, ok=False, error=str(e)[:200])
        return True
    _save_status(at=at, ok=True, filename=r['filename'])
    return True


def status() -> dict:
    """화면 표시용 — {'minutes': 설정, 'pending': 대기 수, 'last': 마지막 결과}."""
    return {'minutes': idle_minutes_setting(), 'pending': len(pending()) if llm.is_configured() else 0,
            'last': last_status()}


def start() -> bool:
    """백그라운드 요약을 시작한다(여러 워커 중 파일 잠금을 잡은 하나만). 시작했으면 True."""
    global _started
    if _started or not idle_minutes_setting():
        return False
    try:
        os.makedirs(db.DATA_DIR, exist_ok=True)
        lock = open(os.path.join(db.DATA_DIR, '.doc_worker.lock'), 'w')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    _started = True

    def loop():
        time.sleep(30)  # 앱이 다 뜬 뒤
        while True:
            try:
                did = step()
            except Exception:  # noqa: BLE001 — 백그라운드가 죽지 않게
                log.exception('첨부 자동 요약 오류')
                did = False
            time.sleep(2 if did else 60)

    threading.Thread(target=loop, name='doc-summary-worker', daemon=True).start()
    lock_holder.append(lock)
    return True
