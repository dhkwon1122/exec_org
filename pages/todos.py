"""할 일 — 마감일 기준으로 묶어 보는 체크리스트."""
from datetime import date, timedelta

import dash
import dash_bootstrap_components as dbc
from dash import Input, Output, callback, html

from components import todo_ui, ui
from services import auth, store

dash.register_page(__name__, path='/todos', name='할 일', title='업무 기록 · 할 일')


def layout(**_):
    return html.Div([
        dbc.Row([
            dbc.Col([
                dbc.Card(dbc.CardBody(todo_ui.add_form('tp')), className='mb-3'),
                html.Div([
                    dbc.RadioItems(id='tp-scope', value='mine', inline=True, className='me-4',
                                   options=[{'label': '내 할 일', 'value': 'mine'},
                                            {'label': '전체', 'value': 'all'}]),
                    dbc.Checkbox(id='tp-show-done', label='최근 7일 완료 보기', value=False),
                ], className='d-flex align-items-center mb-3'),
                html.Div(id='tp-groups'),
            ], lg=8),
            dbc.Col([
                html.H6('다가오는 업무 마감 (2주)', className='text-muted'),
                html.Div(id='tp-task-due'),
            ], lg=4),
        ]),
    ])


todo_ui.register_add_callback('tp')


@callback(Output('tp-groups', 'children'), Output('tp-task-due', 'children'), Output('tp-assignee', 'options'),
          Output('tp-task', 'options'), Output('tp-assignee', 'value'),
          Input('tp-scope', 'value'), Input('tp-show-done', 'value'), Input('todo-changed', 'data'))
def render(scope, show_done, _):
    me = (auth.current_user() or {}).get('user_id', '')
    today = date.today()
    week = today + timedelta(days=7)
    todos = store.list_todos(assignee_id=me if scope == 'mine' else None,
                             done=None if show_done else False,
                             done_since=(today - timedelta(days=7)).isoformat() if show_done else '')

    groups = [
        ('지난 마감', 'text-danger', lambda t: not t['done'] and t['overdue']),
        ('오늘', 'text-warning-emphasis', lambda t: not t['done'] and t['due_date'] == today.isoformat()),
        ('7일 이내', '', lambda t: not t['done'] and today.isoformat() < t['due_date'] <= week.isoformat()),
        ('이후', '', lambda t: not t['done'] and t['due_date'] > week.isoformat()),
        ('날짜 없음', 'text-muted', lambda t: not t['done'] and not t['due_date']),
        ('최근 완료', 'text-success', lambda t: t['done']),
    ]
    sections = []
    for label, cls, pred in groups:
        items = [t for t in todos if pred(t)]
        if items:
            sections.append(html.Div([
                html.H6([label, html.Span(f' {len(items)}', className='text-muted fw-normal')],
                        className=f'mt-3 {cls}'),
                todo_ui.todo_list(items),
            ]))
    if not sections:
        empty = '내게 배정된 할 일이 없습니다.' if scope == 'mine' else '할 일이 없습니다.'
        sections = [html.Div(empty, className='text-muted small')]

    due_tasks = store.tasks_due_between('0000-01-01', (today + timedelta(days=14)).isoformat())
    task_view = dbc.ListGroup([
        dbc.ListGroupItem([
            html.Div([ui.status_badge(t['status']), ui.task_link(t)]),
            todo_ui.due_badge(t['due_date']),
        ], className='py-2') for t in due_tasks
    ]) if due_tasks else html.Div('2주 안에 마감되는 업무가 없습니다.', className='small text-muted')

    task_opts = [{'label': f"#{t['id']} {t['title']}", 'value': t['id']}
                 for t in store.list_tasks(status=['예정', '진행중', '보류'])]
    # 새 할 일의 담당자 기본값은 나
    return sections, task_view, todo_ui.member_options(), task_opts, me or None
