"""업무 간트 차트 — 업무 트리(store.task_tree)를 왼쪽에 들여 쓰고, 오른쪽에 시작일~마감일 막대를 그린다.

막대: 시작 = 시작일(없으면 등록일), 끝 = 마감일(없으면 완료일, 진행 중이면 오늘까지 흐리게 — 마감 미정).
색은 상태(예정·진행중·보류·완료), 마감이 지났는데 끝나지 않았으면 빨간 테두리. 세로 실선은 오늘, 점선은 달의 첫날.
"""
from datetime import date, timedelta

from dash import dcc, html

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


def chart(rows: list[dict], today: date | None = None):
    """rows: store.task_tree 의 줄 [{'task', 'level', ...}]."""
    today = today or date.today()
    if not rows:
        return None
    spans = {r['task']['id']: span(r['task'], today) for r in rows}
    lo = min(s for s, _, _ in spans.values()) - timedelta(days=3)
    hi = max(max(e for _, e, _ in spans.values()), today) + timedelta(days=3)
    lo = max(lo, today - timedelta(days=MAX_PAST_DAYS))
    if (hi - lo).days < MIN_DAYS:
        hi = lo + timedelta(days=MIN_DAYS)
    total = (hi - lo).days

    def pct(d: date) -> float:
        return max(0.0, min(100.0, (d - lo).days / total * 100))

    months, m = [], date(lo.year, lo.month, 1)
    while m <= hi:
        if m >= lo:
            months.append(m)
        m = date(m.year + (m.month == 12), m.month % 12 + 1, 1)
    grid = [html.Div(className='gantt-month', style={'left': f'{pct(x):.2f}%'}) for x in months] + \
        [html.Div(className='gantt-today', style={'left': f'{pct(today):.2f}%'}, title=f'오늘 {today}')]
    head = html.Div([
        html.Div('업무', className='gantt-label small text-muted'),
        html.Div([*[html.Span(f"{x:%y.%m}", className='gantt-month-label', style={'left': f'{pct(x):.2f}%'})
                    for x in months if abs(pct(x) - pct(today)) > 4],  # '오늘'과 겹치는 달 글자는 뺀다
                  html.Span('오늘', className='gantt-today-label', style={'left': f'{pct(today):.2f}%'})],
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
        left, right = pct(start), pct(end + timedelta(days=1))
        tip = (f"#{t['id']} {t['title']} · {t['status']} · {start} ~ " + (f'{end}' if fixed else '마감 미정')
               + (f" · 하위 {r['n_done']}/{r['n_children']}" if r.get('n_children') else ''))
        lines.append(html.Div([
            html.Div([
                html.Span(className='gantt-indent', style={'width': f"{r['level'] * 1.1}rem"}),
                html.I(className='bi bi-arrow-return-right text-muted me-1') if r['level'] else None,
                dcc.Link([html.Span(f"#{t['id']}", className='task-no'), t['title']], href=f"/task/{t['id']}",
                         className='task-title text-truncate' + (' done' if t['status'] == '완료' else ''),
                         title=t['title']),
                html.Span(f"{r['n_done']}/{r['n_children']}", className='small text-muted ms-1')
                if r.get('n_children') else None,
            ], className='gantt-label'),
            html.Div([*grid, html.Div(className=cls, title=tip,
                                      style={'left': f'{left:.2f}%', 'width': f'{max(right - left, 0.6):.2f}%'})],
                     className='gantt-track'),
        ], className='gantt-row'))
    legend = html.Div([html.Span([html.Span(className=f'gantt-key {c}'), s], className='me-3')
                       for s, c in _STATUS_CLASS.items()]
                      + [html.Span([html.Span(className='gantt-key doing open'), '마감 미정 (오늘까지)'], className='me-3'),
                         html.Span([html.Span(className='gantt-key doing late'), '마감 지남'])],
                      className='small text-muted mt-2')
    return html.Div([html.Div(lines, className='gantt'), legend])
