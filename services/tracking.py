"""자료 추적 업무 — 끝내는 일이 아니라 주기적으로 오는 자료(주간보고·월간 실적 등)를 받아 쌓고 업데이트를 지켜보는 업무.

업무 상태가 '추적'(store.TRACKING)이면:
- 정기 메일 모음(services/mail_series.py)을 이 업무에 걸어 두면, 맞는 새 메일이 오면 자동으로 이 업무에 연결되고
  (원문이 '요청' 기록으로 남음) 메일은 처리 완료가 된다.
- status(task_id) 가 업데이트 횟수·마지막 업데이트·평소 간격을 계산하고, 평소보다 오래 안 오면 '늦음'으로 알린다.
"""
from datetime import date
from statistics import median

from sqlalchemy import select

from services import db, store
from services.db import HistoryEntry, MailSeries

RECENT = 8  # 평소 간격을 볼 최근 업데이트 수


def updates(task_id: int) -> list[str]:
    """업데이트가 있었던 날짜들 (YYYY-MM-DD, 최근 것이 앞, 같은 날은 한 번) — 사람이 쓰거나 메일로 들어온 기록."""
    with db.SessionLocal() as s:
        rows = s.scalars(select(HistoryEntry.occurred_at).where(
            store._history_of_task(task_id), HistoryEntry.entry_type.notin_(store.AUTO_ENTRY_TYPES))
            .order_by(HistoryEntry.occurred_at.desc()))
        days = [d.strftime('%Y-%m-%d') for d in rows if d]
    return list(dict.fromkeys(days))


def status(task_id: int, today: date | None = None) -> dict:
    """{'count', 'last', 'days_since', 'interval'(평소 간격 일수 또는 None), 'late'(평소보다 늦음), 'expected'(다음 예상일)}"""
    today = today or date.today()
    days = updates(task_id)
    out = {'count': len(days), 'last': days[0] if days else '', 'days_since': None, 'interval': None,
           'late': False, 'expected': ''}
    if not days:
        return out
    last = date.fromisoformat(days[0])
    out['days_since'] = (today - last).days
    recent = [date.fromisoformat(d) for d in days[:RECENT]]
    gaps = [(a - b).days for a, b in zip(recent, recent[1:]) if (a - b).days > 0]
    if len(gaps) >= 2:
        interval = round(median(gaps))
        out['interval'] = interval
        out['expected'] = date.fromordinal(last.toordinal() + interval).isoformat()
        out['late'] = out['days_since'] > interval + max(2, interval // 2)  # 평소 간격보다 꽤 지남
    return out


def rules(task_id: int) -> list[dict]:
    """이 업무에 메일을 모아 주는 정기 메일 모음."""
    with db.SessionLocal() as s:
        ids = list(s.scalars(select(MailSeries.id).where(MailSeries.task_id == task_id).order_by(MailSeries.id)))
    from services import mail_series
    return [r for r in (mail_series.get(i) for i in ids) if r]


def tracking_tasks() -> list[dict]:
    """'추적' 업무 목록 — 정기 메일 모음 화면의 '연결할 업무' 고르기에 먼저 보인다."""
    return store.list_tasks(status=[store.TRACKING])


def latest_entry(task_id: int) -> dict | None:
    """가장 최근 업데이트 기록 (제목·날짜·id)."""
    with db.SessionLocal() as s:
        h = s.scalars(select(HistoryEntry).where(
            store._history_of_task(task_id), HistoryEntry.entry_type.notin_(store.AUTO_ENTRY_TYPES))
            .order_by(HistoryEntry.occurred_at.desc(), HistoryEntry.id.desc()).limit(1)).first()
        if h is None:
            return None
        return {'id': h.id, 'title': h.title or (h.content or '')[:80],
                'date': h.occurred_at.strftime('%Y-%m-%d') if h.occurred_at else ''}


def overview(query: str = '', today: date | None = None) -> list[dict]:
    """추적 자료 화면 — 추적 업무마다 상태·최근 업데이트·모으는 조건. 늦은 것 먼저, 그다음 최근 업데이트 순."""
    rows = []
    for t in store.list_tasks(query=query or '', status=[store.TRACKING]):
        rows.append({'task': t, 'status': status(t['id'], today), 'latest': latest_entry(t['id']),
                     'rules': rules(t['id'])})
    rows.sort(key=lambda r: r['status']['last'], reverse=True)  # 최근 업데이트 순
    rows.sort(key=lambda r: (not r['status']['late'], r['status']['last'] == ''))  # 늦은 것 먼저, 업데이트 없는 것 끝
    return rows


def create(title: str, by: str = '') -> dict:
    """메일 없이 추적 업무만 만든다 (자료를 직접 기록으로 남기거나, 나중에 메일 조건을 건다)."""
    if not (title or '').strip():
        raise ValueError('추적할 자료 이름을 적으세요.')
    return store.create_task(title.strip(), status=store.TRACKING,
                             description='주기적으로 받는 자료를 모아 업데이트를 추적합니다.')
