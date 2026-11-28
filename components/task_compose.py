"""공통 업무 추가 창 — 어느 화면에서든 (사이드바 '업무 추가', 휴대폰의 떠 있는 단추).
업무 화면에서 열면 그 업무를 상위 업무로 미리 고른다. 저장하면 알림에 '업무 열기' 링크."""
import re

import dash_bootstrap_components as dbc
from dash import ALL, Input, Output, State, callback, ctx, dcc, html, no_update

from components import todo_ui
from services import auth, reporting, store


def open_button(context: str = 'global', label: str = '업무 추가', **kwargs):
    style = {'color': 'primary', 'outline': True, **kwargs}
    return dbc.Button([html.I(className='bi bi-check2-square me-1'), label],
                      id={'type': 'task-compose', 'context': context}, **style)


def layout():
    return html.Div([
        dbc.Modal([
            dbc.ModalHeader(dbc.ModalTitle('업무 추가')),
            dbc.ModalBody([
                dbc.Label('제목'), dbc.Input(id='tc-title', className='mb-2'),
                dbc.Label('설명'), dbc.Textarea(id='tc-desc', rows=3, className='mb-2'),
                dbc.Row([
                    dbc.Col([dbc.Label('상태'), dbc.Select(id='tc-status', value='진행중',
                                                          options=[{'label': s, 'value': s} for s in store.STATUSES])],
                            md=4),
                    dbc.Col([dbc.Label('담당자'), todo_ui.assignee_picker('tc')], md=8),
                ], className='mb-2'),
                dbc.Row([
                    dbc.Col([dbc.Label('시작일'), dbc.Input(id='tc-start', type='date')]),
                    dbc.Col([dbc.Label('마감일'), dbc.Input(id='tc-due', type='date')]),
                ], className='mb-2'),
                dbc.Label('상위 업무 (선택)'), dcc.Dropdown(id='tc-parent', placeholder='없음', className='mb-2'),
                dbc.Row([
                    dbc.Col([dbc.Label('태그 (쉼표로 구분)'), dbc.Input(id='tc-tags')], md=6),
                    dbc.Col([dbc.Label('보고 목표 (보고가 목표인 업무면)'),
                             dbc.Select(id='tc-report', value='', options=[{'label': '없음', 'value': ''}]
                                        + [{'label': s, 'value': s} for s in reporting.STAGES])], md=6),
                ], className='mb-2'),
                dbc.Alert(id='tc-error', color='danger', is_open=False, className='mt-2 mb-0'),
            ]),
            dbc.ModalFooter([
                dbc.Button('취소', id='tc-cancel', color='secondary', outline=True),
                dbc.Button('등록', id='tc-save', color='primary'),
            ]),
        ], id='tc-modal', is_open=False, size='lg', scrollable=True),
        dbc.Toast(id='tc-toast', header='업무 추가', icon='success', is_open=False, dismissable=True, duration=6000,
                  style={'position': 'fixed', 'bottom': 76, 'right': 16, 'zIndex': 1080}),
    ])


def _task_from_path(path: str | None) -> int | None:
    m = re.match(r'^/task/(\d+)', path or '')
    return int(m.group(1)) if m else None


@callback(Output('tc-modal', 'is_open'), Output('tc-parent', 'options'), Output('tc-parent', 'value'),
          Output('tc-assignees', 'options'), Output('tc-assignees', 'value'),
          Output('tc-title', 'value'), Output('tc-desc', 'value'), Output('tc-start', 'value'),
          Output('tc-due', 'value'), Output('tc-tags', 'value'), Output('tc-report', 'value'),
          Output('tc-error', 'children'), Output('tc-error', 'is_open'),
          Output('tc-toast', 'children'), Output('tc-toast', 'is_open'),
          Output('todo-changed', 'data', allow_duplicate=True),
          Input({'type': 'task-compose', 'context': ALL}, 'n_clicks'), Input('tc-cancel', 'n_clicks'),
          Input('tc-save', 'n_clicks'),
          State('app-url', 'pathname'), State('tc-title', 'value'), State('tc-desc', 'value'),
          State('tc-status', 'value'), State('tc-assignees', 'value'), State('tc-assignee-extra', 'value'),
          State('tc-start', 'value'), State('tc-due', 'value'), State('tc-tags', 'value'),
          State('tc-parent', 'value'), State('tc-report', 'value'), State('todo-changed', 'data'),
          prevent_initial_call=True)
def act(_open, _cancel, _save, path, title, desc, status, assignees, extra, start, due, tags, parent, report, tn):
    nu = no_update
    keep = (nu,) * 16
    if not ctx.triggered or not ctx.triggered[0]['value']:
        return keep
    trig = ctx.triggered_id
    if isinstance(trig, dict):  # 열기 — 업무 화면이면 그 업무를 상위 업무로
        opts = [{'label': f"#{t['id']} {t['title']}", 'value': t['id']}
                for t in store.list_tasks(status=store.ACTIVE_STATUSES)]
        me = (auth.current_user() or {}).get('user_id', '')
        return (True, opts, _task_from_path(path), todo_ui.assignee_options(), [f'u:{me}'] if me else [],
                '', '', None, None, '', '', '', False, nu, nu, nu)
    if trig == 'tc-cancel':
        return (False,) + (nu,) * 15
    try:
        t = store.create_task(title, desc or '', status or '진행중', tags=tags or '', parent_id=parent,
                              start_date=start or '', due_date=due or '',
                              assignees=todo_ui.assignees_from(assignees, extra))
        if report:
            reporting.set_target(t['id'], report, auth.current_name())
    except ValueError as e:
        return (nu,) * 11 + (str(e), True, nu, nu, nu)
    toast = [f"#{t['id']} {t['title']} — ", dcc.Link('업무 열기', href=f"/task/{t['id']}")]
    return (False,) + (nu,) * 10 + ('', False, toast, True, (tn or 0) + 1)
