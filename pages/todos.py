"""내 업무 — 마감일 기준으로 묶어 보는 업무 체크 목록 (예전 '할 일'은 업무로 합쳐졌다)."""
from datetime import date, timedelta

import dash
import dash_bootstrap_components as dbc
from dash import Input, Output, callback, html

from components import todo_ui
from services import auth, store

dash.register_page(__name__, path='/todos', name='내 업무', title='업무 기록 · 내 업무')


def layout(**_):
    return html.Div([
        dbc.Card(dbc.CardBody(todo_ui.add_form('tp')), className='mb-3'),
        html.Div([
            dbc.RadioItems(id='tp-scope', value='mine', inline=True, className='me-4',
                           options=[{'label': '내 업무', 'value': 'mine'}, {'label': '전체', 'value': 'all'}]),
            dbc.Checkbox(id='tp-show-done', label='최근 7일 완료 보기', value=False),
        ], className='d-flex align-items-center mb-1'),
        html.Div('업무·하위 업무를 마감일별로 모아 봅니다. 체크하면 완료, 제목을 누르면 업무 화면.',
                 className='small text-muted mb-2'),
        html.Div(id='tp-groups'),
    ])


todo_ui.register_add_callback('tp')


@callback(Output('tp-groups', 'children'), Output('tp-assignee', 'options'),
          Output('tp-task', 'options'), Output('tp-assignee', 'value'),
          Input('tp-scope', 'value'), Input('tp-show-done', 'value'), Input('todo-changed', 'data'))
def render(scope, show_done, _):
    me = (auth.current_user() or {}).get('user_id', '')
    today = date.today()
    week = today + timedelta(days=7)
    todos = store.list_todos(assignee_id=me if scope == 'mine' else None,
                             assignee_name=auth.current_name() if scope == 'mine' else '',
                             done=None if show_done else False,
                             done_since=(today - timedelta(days=7)).isoformat() if show_done else '')

    groups = [
        ('지난 마감', 'text-danger', lambda t: not t['done'] and t['overdue']),
        ('오늘', 'text-warning-emphasis', lambda t: not t['done'] and t['due_date'] == today.isoformat()),
        ('7일 이내', '', lambda t: not t['done'] and today.isoformat() < t['due_date'] <= week.isoformat()),
        ('이후', '', lambda t: not t['done'] and t['due_date'] > week.isoformat()),
        ('마감 없음', 'text-muted', lambda t: not t['done'] and not t['due_date']),
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
        empty = '내가 담당인 업무가 없습니다.' if scope == 'mine' else '업무가 없습니다.'
        sections = [html.Div(empty, className='text-muted small')]

    task_opts = [{'label': f"#{t['id']} {t['title']}", 'value': t['id']}
                 for t in store.list_tasks(status=['예정', '진행중', '보류'])]
    # 새 업무의 담당자 기본값은 나
    return sections, todo_ui.member_options(), task_opts, me or None
