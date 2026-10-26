"""캘린더 — '업무' 탭: 월 단위로 업무 마감·시작과 이력. '원장님 일정'·'피플팀장 일정' 탭: components/director_ui.py
(한 화면을 같이 쓰고, 탭에 따라 누구 일정인지만 바뀐다). '임원 부재' 탭: components/absence_ui.py."""
from datetime import date

import dash
import dash_bootstrap_components as dbc
from dash import ALL, Input, Output, State, callback, ctx, dcc, html, no_update

from components import absence_ui, director_ui, todo_ui, ui
from services import schedules, store

dash.register_page(__name__, path='/calendar', name='캘린더', title='업무 기록 · 캘린더')

WEEKDAYS = ['월', '화', '수', '목', '금', '토', '일']
_OWNER_TABS = {'director': 'director', 'people': 'people'}  # 탭 → schedules.OWNERS 열쇠
MAX_ITEMS = 3


def layout(tab=None, **_):
    tab = tab if tab in ('director', 'people', 'absence') else 'work'
    return html.Div([ui.page_head('캘린더'), dbc.Tabs([
        dbc.Tab(_work_layout(), label='업무 캘린더', tab_id='work', className='pt-3'),
        dbc.Tab(label='원장님 일정', tab_id='director'),
        dbc.Tab(label='피플팀장 일정', tab_id='people'),
        dbc.Tab(absence_ui.layout(), label='임원 부재', tab_id='absence', className='pt-3'),
    ], id='cal-tabs', active_tab=tab),
        # 일정 화면은 하나 — 두 탭이 같이 쓴다 (화면 요소 이름이 겹치지 않게 탭 밖에 두고 보이기만 바꾼다)
        html.Div(director_ui.layout(), id='cal-dr-wrap', className='pt-3',
                 style={} if tab in _OWNER_TABS else {'display': 'none'})])


@callback(Output('cal-dr-wrap', 'style'), Output('dr-owner', 'data'), Input('cal-tabs', 'active_tab'))
def switch_owner(tab):
    if tab in _OWNER_TABS:
        return {}, _OWNER_TABS[tab]
    return {'display': 'none'}, no_update


def _work_layout():
    today = date.today()
    return html.Div([
        dcc.Store(id='cal-month', data=[today.year, today.month]),
        dcc.Store(id='cal-day', data=today.isoformat()),
        dbc.Row([
            dbc.Col([
                html.Div([
                    dbc.Button(html.I(className='bi bi-chevron-left'), id='cal-prev', color='secondary',
                               outline=True, size='sm'),
                    html.H5(id='cal-title', className='mb-0 mx-3'),
                    dbc.Button(html.I(className='bi bi-chevron-right'), id='cal-next', color='secondary',
                               outline=True, size='sm'),
                    dbc.Button('오늘', id='cal-today', color='secondary', outline=True, size='sm', className='ms-2'),
                    html.Div([
                        html.Span([html.I(className='bi bi-check2-square me-1'), '업무 마감'], className='me-3'),
                        html.Span([html.I(className='bi bi-play-fill text-primary me-1'), '업무 시작'],
                                  className='me-3'),
                        dbc.Badge('n', color='secondary', pill=True, className='me-1'), '이력',
                    ], className='small text-muted ms-auto d-none d-md-block'),
                ], className='d-flex align-items-center mb-2'),
                html.Div(id='cal-grid'),
            ], lg=8),
            dbc.Col(html.Div(id='cal-detail'), lg=4),
        ]),
    ])


@callback(Output('cal-month', 'data'), Output('cal-day', 'data', allow_duplicate=True),
          Input('cal-prev', 'n_clicks'), Input('cal-next', 'n_clicks'), Input('cal-today', 'n_clicks'),
          State('cal-month', 'data'), prevent_initial_call=True)
def move_month(_p, _n, _t, ym):
    y, m = ym
    if ctx.triggered_id == 'cal-today':
        t = date.today()
        return [t.year, t.month], t.isoformat()
    m += -1 if ctx.triggered_id == 'cal-prev' else 1
    if m == 0:
        y, m = y - 1, 12
    elif m == 13:
        y, m = y + 1, 1
    return [y, m], no_update


@callback(Output('cal-day', 'data'), Input({'type': 'cal-cell', 'd': ALL}, 'n_clicks'), prevent_initial_call=True)
def pick_day(_):
    if not isinstance(ctx.triggered_id, dict) or not ctx.triggered[0]['value']:
        return no_update
    return ctx.triggered_id['d']


def _cell(d: date, month: int, info: dict | None, selected: str):
    iso = d.isoformat()
    items = []
    if info:
        for t in info['tasks_due']:
            items.append(html.Div([html.I(className='bi bi-flag-fill text-danger me-1'), t['title']],
                                  className='text-truncate'))
        for t in info['tasks_start']:
            items.append(html.Div([html.I(className='bi bi-play-fill text-primary me-1'), t['title']],
                                  className='text-truncate'))
        for t in info['todos']:
            items.append(html.Div([html.I(className=f"bi {'bi-check2-square' if t['done'] else 'bi-square'} me-1"),
                                   t['title']], className='text-truncate',
                                  style={'textDecoration': 'line-through', 'opacity': .6} if t['done'] else None))
    more = len(items) - MAX_ITEMS
    is_today = iso == date.today().isoformat()
    head = html.Div([
        html.Span(d.day, className='fw-bold text-primary' if is_today else
                  ('text-muted' if d.month != month else '')),
        dbc.Badge(info['history'], color='secondary', pill=True, className='ms-auto')
        if info and info['history'] else None,
    ], className='d-flex align-items-center')
    border = 'border-primary border-2' if iso == selected else ''
    return html.Td(html.Div([
        head, *items[:MAX_ITEMS], html.Div(f'+{more}', className='text-muted') if more > 0 else None,
    ], id={'type': 'cal-cell', 'd': iso}, n_clicks=0, className=f'p-1 h-100 border rounded {border}',
        style={'cursor': 'pointer', 'minHeight': '92px', 'fontSize': '0.75rem',
               'background': 'var(--bs-tertiary-bg)' if d.month != month else None}),
        className='p-1', style={'width': '14.28%', 'verticalAlign': 'top'})


@callback(Output('cal-grid', 'children'), Output('cal-title', 'children'), Output('cal-detail', 'children'),
          Input('cal-month', 'data'), Input('cal-day', 'data'), Input('todo-changed', 'data'))
def render(ym, selected, _):
    y, m = ym
    days = store.calendar_month(y, m)
    weeks = schedules.month_weeks(y, m)
    # 앞뒤 달 날짜 칸에도 내용을 보여주기 위해 이웃 달도 읽는다
    for d in (weeks[0][0], weeks[-1][-1]):
        if d.month != m:
            days.update({k: v for k, v in store.calendar_month(d.year, d.month).items()})
    grid = html.Table([
        html.Thead(html.Tr([html.Th(w, className='text-center small text-muted fw-normal' +
                                    (' text-danger' if w == '일' else ' text-primary' if w == '토' else ''))
                            for w in schedules.WEEK_HEAD])),
        html.Tbody([html.Tr([_cell(d, m, days.get(d.isoformat()), selected) for d in week]) for week in weeks]),
    ], className='w-100', style={'tableLayout': 'fixed'})
    return grid, f'{y}년 {m}월', _detail(selected)


def _detail(iso: str):
    d = date.fromisoformat(iso)
    info = store.calendar_month(d.year, d.month).get(iso, {})
    hist = store.search_history('', date_from=iso, date_to=iso)
    todos = info.get('todos', [])
    tasks = [(t, '마감') for t in info.get('tasks_due', [])] + [(t, '시작') for t in info.get('tasks_start', [])]
    return html.Div([
        html.H5(f"{d.month}월 {d.day}일 ({WEEKDAYS[d.weekday()]})", className='mb-3'),
        html.H6('마감 업무', className='text-muted'),
        todo_ui.todo_list(todos, empty='이 날 마감인 업무가 없습니다.'),
        html.H6('시작 업무', className='text-muted mt-3'),
        dbc.ListGroup([dbc.ListGroupItem([dbc.Badge(kind, color='danger' if kind == '마감' else 'primary',
                                                    className='me-2'), ui.task_link(t)], className='py-2')
                       for t, kind in tasks]) if tasks else html.Div('없음', className='small text-muted'),
        html.H6(f'이력 ({len(hist)})', className='text-muted mt-3'),
        dbc.ListGroup([ui.history_card(h, show_task=True, actions=False) for h in hist]) if hist else
        html.Div('이 날 기록된 이력이 없습니다.', className='small text-muted'),
    ])
