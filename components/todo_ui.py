"""업무 체크 목록 공용 조각 (예전 '할 일'은 업무로 합쳐졌다 — 하위 업무·내 업무·캘린더가 이 목록을 쓴다).

체크(=완료)/삭제는 어느 화면에서 하든 이 모듈의 콜백 하나가 처리하고, 앱 전역 Store
'todo-changed' 값을 올린다. 목록을 보여주는 화면은 그 Store 를 Input 으로 받아 다시 그린다.
"""
from datetime import date, timedelta

import dash_bootstrap_components as dbc
from dash import ALL, Input, Output, State, callback, ctx, dcc, html, no_update

from components import ui
from services import auth, store


def due_badge(due: str, done: bool = False):
    if not due:
        return None
    today = date.today()
    d = date.fromisoformat(due)
    if done:
        color, label = 'light', due
    elif d < today:
        color, label = 'danger', f'{due} · {(today - d).days}일 지남'
    elif d == today:
        color, label = 'warning', '오늘'
    elif d == today + timedelta(days=1):
        color, label = 'info', '내일'
    else:
        color, label = 'light', f"{due} ({'월화수목금토일'[d.weekday()]})"
    return dbc.Badge(label, color=color, text_color='dark' if color in ('light', 'warning') else None,
                     className='me-2 border' if color == 'light' else 'me-2')


def todo_row(t: dict, show_task: bool = True):
    title_style = {'textDecoration': 'line-through', 'opacity': 0.6} if t['done'] else None
    return dbc.ListGroupItem(html.Div([
        dbc.Checkbox(id={'type': 'todo-done', 'id': t['id']}, value=t['done'], className='me-2 mb-0'),
        html.Div([
            dcc.Link(t['title'], href=f"/task/{t['id']}", style=title_style, className='me-2 text-reset'),
            *ui.source_icons(t),
            html.Div([
                due_badge(t['due_date'], t['done']),
                html.Span([html.I(className='bi bi-person me-1'), t['assignee_name']], className='me-2')
                if t['assignee_name'] else None,
                html.Span(['상위 ', dcc.Link(f"#{t['task_id']} {t['task_title']}", href=f"/task/{t['task_id']}")],
                          className='me-2 text-muted') if show_task and t['task_id'] else None,
                html.Span(f"완료 {t['done_at']}", className='text-muted') if t['done'] else None,
            ], className='small'),
        ], className='flex-grow-1'),
        dbc.Button(html.I(className='bi bi-x'), id={'type': 'todo-del', 'id': t['id']}, color='link', size='sm',
                   className='text-muted p-0', title='삭제') if store.can_delete_quick(t['id']) else None,
    ], className='d-flex align-items-start'), className='py-2')


def todo_list(todos: list[dict], show_task: bool = True, empty: str = '업무가 없습니다.'):
    if not todos:
        return html.Div(empty, className='small text-muted')
    return dbc.ListGroup([todo_row(t, show_task) for t in todos])


def member_options() -> list[dict]:
    return [{'label': m['name'], 'value': m['user_id']} for m in auth.list_members()]


def add_form(prefix: str, with_task: bool = True):
    """업무 빠른 추가 폼. with_task=False 면 상위 업무 선택 칸이 없다(업무 상세 화면 — 그 업무의 하위 업무로)."""
    return html.Div([
        dbc.Input(id=f'{prefix}-title', placeholder='하위 업무 (Enter로 추가)' if not with_task else
                  '새 업무 (Enter로 추가)', className='mb-2', n_submit=0),
        dbc.Row([
            dbc.Col(dbc.Input(id=f'{prefix}-due', type='date'), xs=6, md=3),
            dbc.Col(dcc.Dropdown(id=f'{prefix}-assignee', placeholder='담당자'), xs=6, md=3),
            dbc.Col(dcc.Dropdown(id=f'{prefix}-task', placeholder='상위 업무 (선택)'), xs=9, md=4)
            if with_task else None,
            dbc.Col(dbc.Button('추가', id=f'{prefix}-add', color='primary', className='w-100'), xs=3, md=2),
        ], className='g-2'),
        dbc.Alert(id=f'{prefix}-error', color='danger', is_open=False, className='mt-2 mb-0 py-2'),
    ])


def register_add_callback(prefix: str, task_id_store: str | None = None):
    """add_form(prefix) 의 추가 버튼 콜백을 등록한다. task_id_store 가 있으면 그 Store 의 업무에 붙인다."""
    states = [State(f'{prefix}-title', 'value'), State(f'{prefix}-due', 'value'),
              State(f'{prefix}-assignee', 'value'),
              State(task_id_store, 'data') if task_id_store else State(f'{prefix}-task', 'value'),
              State('todo-changed', 'data')]

    @callback(Output('todo-changed', 'data', allow_duplicate=True), Output(f'{prefix}-title', 'value'),
              Output(f'{prefix}-error', 'children'), Output(f'{prefix}-error', 'is_open'),
              Input(f'{prefix}-add', 'n_clicks'), Input(f'{prefix}-title', 'n_submit'), *states,
              prevent_initial_call=True)
    def _add(_c, _s, title, due, assignee_id, task_id, n):
        if not (ctx.triggered and ctx.triggered[0]['value']):
            return no_update, no_update, no_update, no_update
        names = {o['value']: o['label'] for o in member_options()}
        try:
            store.create_todo(title, task_id=int(task_id) if task_id else None, due_date=due or '',
                              assignee_id=assignee_id or '', assignee_name=names.get(assignee_id, ''),
                              created_by=auth.current_name())
        except ValueError as e:
            return no_update, no_update, str(e), True
        return (n or 0) + 1, '', '', False


@callback(Output('todo-changed', 'data', allow_duplicate=True),
          Input({'type': 'todo-done', 'id': ALL}, 'value'), Input({'type': 'todo-del', 'id': ALL}, 'n_clicks'),
          State('todo-changed', 'data'), prevent_initial_call=True)
def _todo_action(_v, _d, n):
    trig = ctx.triggered_id
    if not isinstance(trig, dict):
        return no_update
    value = ctx.triggered[0]['value']
    if trig['type'] == 'todo-del':
        if not value:  # 목록이 다시 그려질 때의 빈 트리거
            return no_update
        try:
            store.delete_todo(trig['id'])
        except ValueError:  # 그 사이 기록이 붙었으면 지우지 않는다 (버튼도 다시 그리면 사라짐)
            pass
        return (n or 0) + 1
    # 체크박스: 목록이 다시 그려질 때도 호출되지만 DB 와 같은 값이면 아무것도 바뀌지 않는다
    if store.set_todo_done(trig['id'], bool(value), by=auth.current_name()):
        return (n or 0) + 1
    return no_update
