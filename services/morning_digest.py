"""지정 수신자에게 전체 업무의 전날 진행 상황·오늘 할 일을 매일 발송한다."""

import html
import logging
import os
import threading
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError

from services import db, mailer, outbox, store
from services.db import (
    HistoryEntry,
    MorningDigestConfig,
    MorningDigestDelivery,
    OutboundMail,
    Task,
)

log = logging.getLogger(__name__)
_started = False
LIMIT = 100


def settings():
    with db.SessionLocal() as s:
        row = s.get(MorningDigestConfig, 1)
        return {
            'enabled': bool(row and row.enabled),
            'recipients': row.recipients if row else '',
            'send_time': (row.send_time or '07:00') if row else '07:00',
            'timezone': (row.timezone or 'Asia/Seoul') if row else 'Asia/Seoul',
            'weekdays_only': bool(row and row.weekdays_only),
            'app_url': row.app_url if row else os.environ.get('APP_PUBLIC_URL', ''),
            'updated_by': row.updated_by if row else '',
        }


def save(values, by=''):
    recipients, bad = outbox.parse_addrs(values.get('recipients') or '')
    if bad:
        raise ValueError('수신 주소를 확인하세요: ' + ', '.join(bad))
    at = str(values.get('send_time') or '07:00')
    try:
        parsed = time.fromisoformat(at)
        if len(at) != 5 or parsed.tzinfo:
            raise ValueError()
        zone = str(values.get('timezone') or 'Asia/Seoul')
        ZoneInfo(zone)
    except (ValueError, ZoneInfoNotFoundError):
        raise ValueError('발송 시간(HH:MM)과 시간대를 확인하세요.') from None
    app_url = validate_app_url(values.get('app_url'))
    enabled = bool(values.get('enabled'))
    if enabled and (not recipients or not app_url):
        raise ValueError('예약 발송을 켜려면 수신자와 앱 접속 주소를 입력하세요.')
    if enabled and not mailer.is_configured():
        raise ValueError(
            '서버에 메일 API 설정을 먼저 적용하세요 (.env.example의 메일 보내기).'
        )
    with db.transaction() as s:
        row = s.get(MorningDigestConfig, 1)
        if row is None:
            row = MorningDigestConfig(id=1)
            s.add(row)
        row.enabled, row.recipients, row.send_time, row.timezone = (
            enabled,
            ', '.join(recipients),
            at,
            zone,
        )
        row.weekdays_only = bool(values.get('weekdays_only'))
        row.app_url, row.updated_by = app_url, by
    return settings()


def validate_app_url(value):
    app_url = str(value or '').strip().rstrip('/')
    try:
        split = urlsplit(app_url)
        if app_url and (
            split.scheme not in ('http', 'https')
            or not split.netloc
            or split.username
            or split.password
            or split.query
            or split.fragment
        ):
            raise ValueError()
    except ValueError:
        raise ValueError(
            '앱 주소는 로그인 화면을 열 수 있는 http(s) 주소로 입력하세요.'
        ) from None
    return app_url


def _wall_window(day, zone):
    # 기존 DB는 TZ 환경변수의 로컬 시각을 naive datetime으로 보존한다.
    database_zone = (
        ZoneInfo(os.environ['TZ'])
        if os.environ.get('TZ')
        else datetime.now().astimezone().tzinfo
    )
    begin = datetime.combine(day, time.min, tzinfo=ZoneInfo(zone))
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=ZoneInfo(zone))
    return begin.astimezone(database_zone).replace(tzinfo=None), end.astimezone(
        database_zone
    ).replace(tzinfo=None)


def preview(day=None, config=None):
    config = config or settings()
    day = (
        day
        or datetime.now(timezone.utc).astimezone(ZoneInfo(config['timezone'])).date()
    )
    if isinstance(day, str):
        day = date.fromisoformat(day)
    yesterday = day - timedelta(days=1)
    begin, end = _wall_window(yesterday, config['timezone'])
    base = validate_app_url(config['app_url'])
    with db.SessionLocal() as s:
        hist = list(
            s.scalars(
                select(HistoryEntry)
                .where(
                    or_(
                        HistoryEntry.entry_type.notin_(store.AUTO_ENTRY_TYPES),
                        HistoryEntry.entry_type == '상태변경',
                    ),
                    or_(
                        (HistoryEntry.occurred_at >= begin)
                        & (HistoryEntry.occurred_at < end),
                        (HistoryEntry.created_at >= begin)
                        & (HistoryEntry.created_at < end),
                        (HistoryEntry.updated_at >= begin)
                        & (HistoryEntry.updated_at < end),
                    ),
                )
                .order_by(HistoryEntry.occurred_at.desc(), HistoryEntry.id.desc())
                .limit(LIMIT + 1)
            )
        )
        done = list(
            s.scalars(
                select(Task)
                .where(Task.done_at >= begin, Task.done_at < end, Task.status == '완료')
                .order_by(Task.done_at.desc())
                .limit(LIMIT + 1)
            )
        )
        active = select(Task).where(Task.status != '완료')
        today_due = list(
            s.scalars(
                active.where(
                    or_(
                        Task.due_date == day.isoformat(),
                        Task.start_date == day.isoformat(),
                    )
                )
                .order_by(Task.due_date, Task.id)
                .limit(LIMIT + 1)
            )
        )
        today_ids = {t.id for t in today_due}
        overdue = list(
            s.scalars(
                active.where(
                    Task.due_date != '',
                    Task.due_date < day.isoformat(),
                    Task.id.notin_(today_ids or [0]),
                )
                .order_by(Task.due_date, Task.id)
                .limit(LIMIT + 1)
            )
        )
        soon = list(
            s.scalars(
                active.where(
                    Task.due_date > day.isoformat(),
                    Task.due_date <= (day + timedelta(days=7)).isoformat(),
                    Task.id.notin_(today_ids or [0]),
                )
                .order_by(Task.due_date, Task.id)
                .limit(LIMIT + 1)
            )
        )
    groups = []
    history_rows = []
    for h in hist[:LIMIT]:
        event_day = h.occurred_at.strftime('%Y-%m-%d') if h.occurred_at else ''
        history_rows.append(
            {
                'title': f'[{h.entry_type}] {h.title or h.content[:120]}',
                'detail': (h.summary or h.content)[:600]
                + (
                    f' · 기록 일자 {event_day}'
                    if event_day != yesterday.isoformat()
                    else ''
                ),
                'href': f'{base}/history/{h.id}',
            }
        )
    groups.append(('전날 진행·입력·수정한 기록', history_rows, len(hist) > LIMIT))
    for title, rows in [
        ('전날 완료한 업무', done),
        ('오늘 마감·시작하는 업무', today_due),
        ('마감이 지난 미완료 업무', overdue),
        ('다가오는 마감 (7일)', soon),
    ]:
        groups.append(
            (
                title,
                [
                    {
                        'title': t.title,
                        'detail': f'{t.status} · 담당 {t.owner or "미지정"} · 시작 {t.start_date or "-"} · 마감 {t.due_date or "-"}',
                        'href': f'{base}/task/{t.id}',
                    }
                    for t in rows[:LIMIT]
                ],
                len(rows) > LIMIT,
            )
        )
    subject = f'[업무 아침 요약] {day.isoformat()} · 전날 진행 / 오늘 할 일'
    text = [
        subject,
        f'전체 업무 기준 · {config["timezone"]} · 전날 {yesterday.isoformat()}',
        '업무 상태·미완료 여부는 생성 시점 기준입니다. 기한은 지정된 업무 날짜를 따릅니다.',
        '',
    ]
    body = [
        f'<h2>{html.escape(subject)}</h2>',
        '<p>전체 업무의 전날 진행 상황과 오늘 확인할 일입니다.</p>',
    ]
    for title, items, limited in groups:
        text += [title] + [
            f'- {r["title"]} — {r["detail"]}\n  {r["href"]}' for r in items
        ]
        body += [f'<h3>{html.escape(title)}</h3>', '<ul>']
        for r in items:
            body.append(
                f'<li><a href="{html.escape(r["href"], quote=True)}">{html.escape(r["title"])}</a>'
                f'<br>{html.escape(r["detail"])}</li>'
            )
        if not items:
            text.append('- 해당 항목 없음')
            body.append('<li>해당 항목 없음</li>')
        body.append('</ul>')
        if limited:
            text.append(f'※ 표시 한도 {LIMIT}건. 앱에서 전체 목록을 확인하세요.')
            body.append(f'<p>표시 한도 {LIMIT}건. 앱에서 전체 목록을 확인하세요.</p>')
        text.append('')
    return {
        'subject': subject,
        'text': '\n'.join(text),
        'table_html': ''.join(body),
        'day': day.isoformat(),
    }


def deliveries(limit=14):
    with db.SessionLocal() as s:
        return [
            {
                'day': r.day,
                'status': r.status,
                'recipients': r.recipients,
                'outbound_id': r.outbound_id,
                'error': r.error or '',
            }
            for r in s.scalars(
                select(MorningDigestDelivery)
                .order_by(MorningDigestDelivery.day.desc())
                .limit(limit)
            )
        ]


def dispatch_due(now=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError('예약 시각은 시간대가 있는 datetime이어야 합니다.')
    config = settings()
    local = now.astimezone(ZoneInfo(config['timezone']))
    if (
        not config['enabled']
        or local.strftime('%H:%M') < config['send_time']
        or (config['weekdays_only'] and local.weekday() >= 5)
        or not mailer.is_configured()
    ):
        return False
    day, utc = (
        local.date().isoformat(),
        now.astimezone(timezone.utc).replace(tzinfo=None),
    )
    try:
        with db.transaction() as s:
            s.add(
                MorningDigestDelivery(
                    day=day,
                    status='preparing',
                    recipients=config['recipients'],
                    timezone=config['timezone'],
                    started_at=utc,
                )
            )
    except IntegrityError:
        return False  # 날짜 PK로 여러 프로세스·재시작의 중복 발송을 막는다.
    try:
        message = preview(local.date(), config)
        latest = settings()
        if latest != config or not latest['enabled']:
            raise ValueError('요약 생성 중 발송 설정이 변경되어 발송하지 않았습니다.')
        with db.transaction() as s:
            s.execute(
                update(MorningDigestDelivery)
                .where(MorningDigestDelivery.day == day)
                .values(status='sending')
            )
        sent = outbox.send(
            config['recipients'],
            message['subject'],
            table_html=message['table_html'],
            related_kind='morning_digest',
            related_id=day,
            by='아침 요약 예약 발송',
        )
        status, error, outbound_id = 'sent', '', sent['id']
    except (
        Exception
    ) as exc:  # 원격 API timeout은 이미 전달됐을 수도 있어 자동 재시도하지 않는다.
        log.exception('아침 요약 발송 실패 %s', day)
        status, error = 'failed', str(exc)[:1000]
        with db.SessionLocal() as s:
            existing = s.scalar(
                select(OutboundMail)
                .where(
                    OutboundMail.related_kind == 'morning_digest',
                    OutboundMail.related_id == day,
                )
                .order_by(OutboundMail.id.desc())
                .limit(1)
            )
            outbound_id = existing.id if existing else None
    with db.transaction() as s:
        s.execute(
            update(MorningDigestDelivery)
            .where(MorningDigestDelivery.day == day)
            .values(
                status=status,
                error=error,
                outbound_id=outbound_id,
                finished_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
        )
    return True


def start():
    global _started
    if (
        _started
        or not mailer.is_configured()
        or os.environ.get('MORNING_DIGEST_WORKER', '1') == '0'
    ):
        return False
    _started = True

    def loop():
        while True:
            try:
                dispatch_due()
            except Exception:
                log.exception('아침 요약 예약 확인 실패')
            threading.Event().wait(30)

    threading.Thread(target=loop, name='morning-digest', daemon=True).start()
    return True
