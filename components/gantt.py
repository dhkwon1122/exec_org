"""업무 간트 차트 — 업무 트리(store.task_tree)를 왼쪽에 들여 쓰고, 오른쪽에 시작일~마감일 막대를 그린다.

막대: 시작 = 시작일(없으면 등록일), 끝 = 마감일(없으면 완료일, 진행 중이면 오늘까지 흐리게 — 마감 미정).
막대 위 점: 그 업무에 이력(회의·메모 등)이 있었던 날 — 마우스를 올리면 그날의 기록. 업무 이름을 누르면 AI 요약 창.
색은 상태(예정·진행중·보류·완료), 마감이 지났는데 끝나지 않았으면 빨간 테두리. 세로 실선은 오늘, 점선은 달의 첫날.
"""
from datetime import date, timedelta

from dash import html

_STATUS_CLASS = {'예정': 'plan', '진행중': 'doing', '보류': 'hold', '완료': 'done'}
MIN_DAYS = 28
MAX_PAST_DAYS = 365  # 오래된 업무가 많아도 한 해 전부터만 (그 앞은 잘린 막대로)


def _d(v: str) -> date | None:
    try:
        return date.fromisoformat((v or '')[:10])
    except ValueError:
        return None


def span(t: dict, today: date) -> tuple[date, date, bool]:
    """업무 → (시작, 끝, 끝이 정해졌는지)."""
    start = _d(t['start_date']) or _d(t['created_at']) or today
    end = _d(t['due_date']) or (_d(t['done_at']) if t['status'] == '완료' else None)
    fixed = end is not None
    if end is None:
        end = max(today, start)
    if end < start:
        start, end = end, start
    return start, end, fixed


# 보기 — (이름, 오늘 앞 날수, 전체 날수, 눈금). 'all' 은 업무 기간에 맞춘다. offset 은 한 화면 단위로 앞뒤로 옮긴다.
SCALES = {'week': ('주', 7, 42, 'week'), 'month': ('월', 30, 180, 'month'), 'quarter': ('분기', 90, 450, 'month'),
          'all': ('전체', 0, 0, 'month')}


def window(spans: list[tuple[date, date, bool]], today: date, scale: str = 'all', offset: int = 0) -> tuple[date, date]:
    """보여 줄 기간. 주·월·분기는 오늘 기준 고정 폭(offset 만큼 한 화면씩 앞뒤로), 전체는 업무 기간에 맞춘다."""
    if scale in SCALES and scale != 'all':
        _, before, days, _ = SCALES[scale]
        lo = today - timedelta(days=before) + timedelta(days=days * (offset or 0))
        if scale == 'week':
            lo -= timedelta(days=lo.weekday())  # 월요일부터
        return lo, lo + timedelta(days=days)
    lo = min(s for s, _, _ in spans) - timedelta(days=3)
    hi = max(max(e for _, e, _ in spans), today) + timedelta(days=3)
    lo = max(lo, today - timedelta(days=MAX_PAST_DAYS))
    if (hi - lo).days < MIN_DAYS:
        hi = lo + timedelta(days=MIN_DAYS)
    return lo, hi


def _ticks(lo: date, hi: date, kind: str) -> list[tuple[date, str]]:
    if kind == 'week':  # 월요일마다
        d = lo + timedelta(days=(7 - lo.weekday()) % 7)
        out = []
        while d <= hi:
            out.append((d, f'{d.month}/{d.day}'))
            d += timedelta(days=7)
        return out
    out, m = [], date(lo.year, lo.month, 1)
    while m <= hi:
        if m >= lo:
            out.append((m, f'{m:%y.%m}'))
        m = date(m.year + (m.month == 12), m.month % 12 + 1, 1)
    return out


def _dots(days: list[dict], lo: date, hi: date, pct) -> list:
    """이력이 있었던 날마다 점 (같은 날 여러 건이면 크게)."""
    out = []
    for m in days:
        d = _d(m['date'])
        if d is None or not lo <= d <= hi:
            continue
        tip = f"{m['date']} 기록 {m['n']}건\n" + '\n'.join(m['items'][:6]) + ('\n…' if m['n'] > 6 else '')
        out.append(html.Div(className='gantt-dot' + (' many' if m['n'] > 1 else ''), title=tip,
                            style={'left': f'{pct(d + timedelta(hours=12)):.2f}%'}))
    return out


def chart(rows: list[dict], today: date | None = None, scale: str = 'all', offset: int = 0,
          marks: dict[int, list[dict]] | None = None):
    """rows: store.task_tree 의 줄 [{'task', 'level', ...}]. scale: week / month / quarter / all.
    marks: 업무별 이력이 있었던 날 (store.history_marks)."""
    today = today or date.today()
    if not rows:
        return None
    spans = {r['task']['id']: span(r['task'], today) for r in rows}
    lo, hi = window(list(spans.values()), today, scale, offset)
    total = (hi - lo).days

    def pct(d: date) -> float:
        return max(0.0, min(100.0, (d - lo).days / total * 100))

    ticks = _ticks(lo, hi, SCALES.get(scale, SCALES['all'])[3])
    in_view = lo <= today <= hi
    grid = [html.Div(className='gantt-month', style={'left': f'{pct(x):.2f}%'}) for x, _ in ticks] + \
        ([html.Div(className='gantt-today', style={'left': f'{pct(today):.2f}%'}, title=f'오늘 {today}')]
         if in_view else [])
    head = html.Div([
        html.Div(f'업무 · {lo:%y.%m.%d} ~ {hi:%y.%m.%d}', className='gantt-label small text-muted'),
        html.Div([*[html.Span(t, className='gantt-month-label', style={'left': f'{pct(x):.2f}%'})
                    for x, t in ticks if not in_view or abs(pct(x) - pct(today)) > 4],  # '오늘'과 겹치는 눈금 글자는 뺀다
                  html.Span('오늘', className='gantt-today-label', style={'left': f'{pct(today):.2f}%'})
                  if in_view else None],
                 className='gantt-track'),
    ], className='gantt-row gantt-head')

    lines = [head]
    for r in rows:
        t = r['task']
        start, end, fixed = spans[t['id']]
        cls = 'gantt-bar ' + _STATUS_CLASS.get(t['status'], 'plan')
        if not fixed:
            cls += ' open'  # 마감 미정 — 오늘까지 흐리게
        if fixed and t['status'] != '완료' and end < today:
            cls += ' late'
        if start < lo:
            cls += ' cut'
        if end + timedelta(days=1) > hi:
            cls += ' cut-end'
        left, right = pct(start), pct(end + timedelta(days=1))
        if right <= 0 or left >= 100:  # 보이는 기간 밖 — 줄만 남기고 막대는 끝에 화살표로
            cls += ' outside'
            left, right = (0, 0.8) if right <= 0 else (99.2, 100)
        tip = (f"#{t['id']} {t['title']} · {t['status']} · {start} ~ " + (f'{end}' if fixed else '마감 미정')
               + (f" · 하위 {r['n_done']}/{r['n_children']}" if r.get('n_children') else ''))
        lines.append(html.Div([
            html.Div([
                html.Span(className='gantt-indent', style={'width': f"{r['level'] * 1.1}rem"}),
                html.I(className='bi bi-arrow-return-right text-muted me-1') if r['level'] else None,
                html.A([html.Span(f"#{t['id']}", className='task-no'), t['title']], id={'type': 'gantt-task', 'id': t['id']},
                       n_clicks=0, className='task-title text-truncate' + (' done' if t['status'] == '완료' else ''),
                       title=f"{t['title']} — 눌러서 AI 요약 보기", role='button'),
                html.Span(f"{r['n_done']}/{r['n_children']}", className='small text-muted ms-1')
                if r.get('n_children') else None,
            ], className='gantt-label'),
            html.Div([*grid, html.Div(className=cls, title=tip,
                                      style={'left': f'{left:.2f}%', 'width': f'{max(right - left, 0.6):.2f}%'}),
                      *_dots((marks or {}).get(t['id'], []), lo, hi, pct)],
                     className='gantt-track'),
        ], className='gantt-row'))
    legend = html.Div([html.Span([html.Span(className=f'gantt-key {c}'), s], className='me-3')
                       for s, c in _STATUS_CLASS.items()]
                      + [html.Span([html.Span(className='gantt-key doing open'), '마감 미정 (오늘까지)'], className='me-3'),
                         html.Span([html.Span(className='gantt-key doing late'), '마감 지남'], className='me-3'),
                         html.Span([html.Span(className='gantt-dot-key'), '이력이 있었던 날 (점에 마우스를 올리면 그날 기록) · 업무 이름을 누르면 AI 요약'])],
                      className='small text-muted mt-2')
    return html.Div([html.Div(lines, className='gantt'), legend])
