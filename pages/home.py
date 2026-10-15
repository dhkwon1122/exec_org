"""업무 목록: 검색·필터, 새 업무 등록, 전체 최근 이력."""
import dash
import dash_bootstrap_components as dbc
from dash import Input, Output, State, callback, ctx, dcc, html, no_update

from components import ui
from services import store

dash.register_page(__name__, path='/', name='업무', title='업무 기록 · 업무')


def _new_task_modal():
    return dbc.Modal([
        dbc.ModalHeader(dbc.ModalTitle('새 업무')),
        dbc.ModalBody([
            dbc.Label('제목'), dbc.Input(id='new-title', className='mb-2'),
            dbc.Label('설명'), dbc.Textarea(id='new-desc', rows=4, className='mb-2'),
            dbc.Row([
                dbc.Col([dbc.Label('상태'), dbc.Select(id='new-status', value='진행중',
                                                      options=[{'label': s, 'value': s} for s in store.STATUSES])]),
                dbc.Col([dbc.Label('담당자'), dbc.Input(id='new-owner')]),
            ], className='mb-2'),
            dbc.Row([
                dbc.Col([dbc.Label('시작일'), dbc.Input(id='new-start', type='date')]),
                dbc.Col([dbc.Label('마감일'), dbc.Input(id='new-due', type='date')]),
            ], className='mb-2'),
            dbc.Label('태그 (쉼표로 구분)'), dbc.Input(id='new-tags', className='mb-2'),
            dbc.Label('상위 업무 (선택)'), dcc.Dropdown(id='new-parent', placeholder='없음'),
            dbc.Alert(id='new-error', color='danger', is_open=False, className='mt-3 mb-0'),
        ]),
        dbc.ModalFooter([
            dbc.Button('취소', id='new-cancel', color='secondary', outline=True),
            dbc.Button('등록', id='new-save', color='primary'),
        ]),
    ], id='new-modal', is_open=False, size='lg')


def layout(**_):
    return html.Div([
        dcc.Store(id='home-refresh', data=0),
        dcc.Location(id='home-redirect', refresh='callback-nav'),
        _new_task_modal(),
        dbc.Row([
            dbc.Col([
                dbc.Row([
                    dbc.Col(dbc.Input(id='q', placeholder='검색: 제목·설명·태그·이력·참고자료', debounce=True,
                                      type='search'), md=5),
                    dbc.Col(dcc.Dropdown(id='f-status', multi=True, placeholder='상태 (전체)',
                                         options=store.STATUSES, value=[]), md=3),
                    dbc.Col(dcc.Dropdown(id='f-tag', placeholder='태그'), md=2),
                    dbc.Col(dbc.Button([html.I(className='bi bi-plus-lg me-1'), '새 업무'], id='new-open',
                                       color='primary', className='w-100'), md=2),
                ], className='g-2 mb-3'),
                html.Div(id='task-list'),
            ], lg=8),
            dbc.Col([
                html.H6('최근 이력', className='text-muted'),
                html.Div(id='recent-history'),
            ], lg=4),
        ]),
    ])


@callback(Output('task-list', 'children'), Output('recent-history', 'children'), Output('f-tag', 'options'),
          Input('q', 'value'), Input('f-status', 'value'), Input('f-tag', 'value'), Input('home-refresh', 'data'))
def render_list(q, statuses, tag, _):
    tasks = store.list_tasks(query=q or '', status=statuses or None, tag=tag or '')
    if tasks:
        items = []
        for t in tasks:
            meta = [m for m in [t['owner'] and f"담당 {t['owner']}",
                                t['due_date'] and f"마감 {t['due_date']}",
                                f"수정 {t['updated_at']}"] if m]
            items.append(dbc.ListGroupItem([
                html.Div([ui.status_badge(t['status']), ui.task_link(t)], className='mb-1'),
                html.Div(t['description'][:140] + ('…' if len(t['description']) > 140 else ''),
                         className='small text-muted mb-1') if t['description'] else None,
                html.Div([*ui.tag_badges(t['tags']), html.Span(' · '.join(meta), className='small text-muted')]),
            ]))
        task_view = dbc.ListGroup(items)
    else:
        task_view = dbc.Alert('조건에 맞는 업무가 없습니다. 오른쪽 위 "새 업무"로 등록하세요.', color='light')

    recent = store.recent_history(15)
    history_view = dbc.ListGroup([
        dbc.ListGroupItem([
            html.Div([html.I(className=f"bi {ui.ENTRY_ICON.get(h['entry_type'], 'bi-dot')} me-1"),
                      html.Span(h['entry_type'], className='fw-semibold me-2'),
                      dcc.Link(h['task_title'], href=f"/task/{h['task_id']}", className='small')]),
            html.Div(h['content'][:120], className='small', style={'whiteSpace': 'pre-wrap'}),
            html.Div(f"{h['occurred_at']} {h['author']}", className='small text-muted'),
        ], className='py-2') for h in recent
    ], flush=True) if recent else html.Div('아직 이력이 없습니다.', className='small text-muted')

    return task_view, history_view, store.all_tags()


@callback(Output('new-modal', 'is_open'), Output('new-parent', 'options'), Output('new-error', 'children'),
          Output('new-error', 'is_open'), Output('home-redirect', 'pathname'),
          Input('new-open', 'n_clicks'), Input('new-cancel', 'n_clicks'), Input('new-save', 'n_clicks'),
          State('new-title', 'value'), State('new-desc', 'value'), State('new-status', 'value'),
          State('new-owner', 'value'), State('new-start', 'value'), State('new-due', 'value'),
          State('new-tags', 'value'), State('new-parent', 'value'), State('author-store', 'data'),
          prevent_initial_call=True)
def new_task(_o, _c, _s, title, desc, status, owner, start, due, tags, parent, author):
    if ctx.triggered_id == 'new-open':
        opts = [{'label': f"#{t['id']} {t['title']}", 'value': t['id']} for t in store.list_tasks()]
        return True, opts, '', False, no_update
    if ctx.triggered_id == 'new-cancel':
        return False, no_update, '', False, no_update
    try:
        t = store.create_task(title, desc or '', status or '진행중', owner or author or '', tags or '',
                              parent, start or '', due or '')
    except ValueError as e:
        return True, no_update, str(e), True, no_update
    return False, no_update, '', False, f"/task/{t['id']}"
