"""업무 상세: 개요, 이력 타임라인, 참고자료, 연결 업무, 하위 업무."""
import base64

import dash
import dash_bootstrap_components as dbc
from dash import ALL, Input, Output, State, callback, ctx, dcc, html, no_update

from components import ui
from components.history_form import decode_text_upload, history_form
from services import enrich, llm, store

dash.register_page(__name__, path_template='/task/<task_id>', name='업무 상세', title='업무 기록 · 상세')


def _opts(values):
    return [{'label': v, 'value': v} for v in values]


def _edit_modal():
    return dbc.Modal([
        dbc.ModalHeader(dbc.ModalTitle('업무 수정')),
        dbc.ModalBody([
            dbc.Label('제목'), dbc.Input(id='ed-title', className='mb-2'),
            dbc.Label('설명'), dbc.Textarea(id='ed-desc', rows=5, className='mb-2'),
            dbc.Row([
                dbc.Col([dbc.Label('담당자'), dbc.Input(id='ed-owner')]),
                dbc.Col([dbc.Label('태그 (쉼표 구분)'), dbc.Input(id='ed-tags')]),
            ], className='mb-2'),
            dbc.Row([
                dbc.Col([dbc.Label('시작일'), dbc.Input(id='ed-start', type='date')]),
                dbc.Col([dbc.Label('마감일'), dbc.Input(id='ed-due', type='date')]),
            ], className='mb-2'),
            dbc.Label('상위 업무'), dcc.Dropdown(id='ed-parent', placeholder='없음'),
            dbc.Alert(id='ed-error', color='danger', is_open=False, className='mt-3 mb-0'),
        ]),
        dbc.ModalFooter([
            dbc.Button('삭제', id='ed-delete', color='danger', outline=True, className='me-auto'),
            dbc.Button('취소', id='ed-cancel', color='secondary', outline=True),
            dbc.Button('저장', id='ed-save', color='primary'),
        ]),
    ], id='ed-modal', is_open=False, size='lg')


def _history_form():
    return dbc.Card(dbc.CardBody(history_form('h')), className='mb-3')


def _history_edit_modal():
    return dbc.Modal([
        dbc.ModalHeader(dbc.ModalTitle('이력 수정')),
        dbc.ModalBody([
            dcc.Store(id='he-id'),
            dbc.Row([
                dbc.Col([dbc.Label('유형'), dbc.Select(id='he-type', options=_opts(store.ENTRY_TYPES))]),
                dbc.Col([dbc.Label('일시'), dbc.Input(id='he-when', type='datetime-local')]),
            ], className='mb-2'),
            dbc.Label('제목'), dbc.Input(id='he-title', className='mb-2'),
            dbc.Label('요약'), dbc.Textarea(id='he-summary', rows=3, className='mb-2'),
            dbc.Label('키워드 (쉼표 구분)'), dbc.Input(id='he-keywords', className='mb-2'),
            dbc.Label('원문'), dbc.Textarea(id='he-content', rows=8),
            dbc.Alert(id='he-error', color='danger', is_open=False, className='mt-3 mb-0'),
        ]),
        dbc.ModalFooter([
            dbc.Button([html.I(className='bi bi-stars me-1'), 'AI로 다시 생성'], id='he-regen', color='info',
                       outline=True, className='me-auto', disabled=not llm.is_configured(),
                       title='원문을 저장한 뒤 제목·요약·키워드를 다시 만듭니다'),
            dbc.Button('취소', id='he-cancel', color='secondary', outline=True),
            dbc.Button('저장', id='he-save', color='primary'),
        ]),
    ], id='he-modal', is_open=False, size='lg')


def _reference_form():
    return dbc.Card(dbc.CardBody([
        dbc.RadioItems(id='r-kind', value='link', inline=True, className='mb-2',
                       options=[{'label': '링크', 'value': 'link'}, {'label': '파일', 'value': 'file'},
                                {'label': '메모', 'value': 'note'}]),
        dbc.Input(id='r-title', placeholder='제목 (비우면 자동)', className='mb-2'),
        dbc.Input(id='r-url', placeholder='https://…', className='mb-2'),
        dcc.Upload(id='r-file', children=html.Div(['파일을 끌어다 놓거나 ', html.A('선택')]),
                   className='mb-2 border rounded text-center small py-3', style={'borderStyle': 'dashed'},
                   max_size=50 * 1024 * 1024),
        html.Div(id='r-file-name', className='small text-muted mb-2'),
        dbc.Textarea(id='r-note', rows=3, placeholder='설명 / 메모', className='mb-2'),
        html.Div(dbc.Button('자료 추가', id='r-add', color='primary', size='sm'), className='text-end'),
        dbc.Alert(id='r-error', color='danger', is_open=False, className='mt-2 mb-0 py-2'),
    ]), className='mb-3')


def _link_form():
    return dbc.Card(dbc.CardBody([
        dbc.Row([
            dbc.Col(dbc.Select(id='l-rel', value='관련', options=_opts(store.RELATIONS)), xs=4, md=3),
            dbc.Col(dcc.Dropdown(id='l-target', placeholder='연결할 업무 선택'), xs=8, md=9),
        ], className='g-2 mb-2'),
        dbc.Input(id='l-note', placeholder='연결 사유 (선택)', className='mb-2'),
        html.Div([
            html.Span('"선행"은 이 업무가 선택한 업무보다 먼저라는 뜻입니다.', className='small text-muted me-auto'),
            dbc.Button('연결', id='l-add', color='primary', size='sm'),
        ], className='d-flex align-items-center'),
        dbc.Alert(id='l-error', color='danger', is_open=False, className='mt-2 mb-0 py-2'),
    ]), className='mb-3')


def layout(task_id=None, **_):
    try:
        tid = int(task_id)
    except (TypeError, ValueError):
        tid = None
    if tid is None or store.get_task(tid) is None:
        return dbc.Alert(['업무를 찾을 수 없습니다. ', dcc.Link('목록으로', href='/')], color='warning')

    return html.Div([
        dcc.Store(id='td-id', data=tid),
        dcc.Store(id='td-refresh', data=0),
        dcc.Store(id='td-pending', data=[]),
        dcc.Interval(id='td-poll', interval=3000, disabled=True),
        dcc.Location(id='td-redirect', refresh='callback-nav'),
        _edit_modal(),
        _history_edit_modal(),
        html.Div(id='td-header', className='mb-3'),
        dbc.Row([
            dbc.Col([
                dbc.Tabs([
                    dbc.Tab([_history_form(), html.Div(id='td-history')], label='이력', tab_id='history'),
                    dbc.Tab([_reference_form(), html.Div(id='td-refs')], label='참고자료', tab_id='refs'),
                    dbc.Tab([_link_form(), html.Div(id='td-links')], label='연결 업무', tab_id='links'),
                ], id='td-tabs', active_tab='history', className='mb-3'),
            ], lg=8),
            dbc.Col(html.Div(id='td-side'), lg=4),
        ]),
    ])


# ── 렌더링 ────────────────────────────────────────────────────────────

@callback(Output('td-header', 'children'), Output('td-side', 'children'), Output('td-history', 'children'),
          Output('td-refs', 'children'), Output('td-links', 'children'), Output('l-target', 'options'),
          Output('td-pending', 'data'), Output('td-poll', 'disabled'),
          Input('td-refresh', 'data'), State('td-id', 'data'))
def render(_, tid):
    t = store.get_task(tid)
    if t is None:
        return dbc.Alert('삭제된 업무입니다.', color='warning'), None, None, None, None, [], [], True
    parent = store.get_task(t['parent_id']) if t['parent_id'] else None

    header = dbc.Card(dbc.CardBody([
        html.Div([
            html.H4([html.Span(f"#{t['id']} ", className='text-muted'), t['title']], className='mb-0 me-auto'),
            dbc.Select(id='td-status', value=t['status'], options=_opts(store.STATUSES),
                       style={'width': '110px'}, className='me-2'),
            dbc.Button([html.I(className='bi bi-pencil me-1'), '수정'], id='ed-open', color='secondary',
                       outline=True, size='sm'),
        ], className='d-flex align-items-center gap-2 mb-2'),
        html.Div([ui.status_badge(t['status']), *ui.tag_badges(t['tags'])], className='mb-2'),
        ui.multiline(t['description']) if t['description'] else html.Div('설명 없음', className='text-muted small'),
    ]))

    def row(label, value):
        return html.Tr([html.Th(label, className='text-muted fw-normal small', style={'width': '35%'}),
                        html.Td(value or '-', className='small')])

    children = store.child_tasks(tid)
    side = [
        dbc.Card(dbc.CardBody(html.Table([html.Tbody([
            row('담당자', t['owner']), row('시작일', t['start_date']), row('마감일', t['due_date']),
            row('상위 업무', ui.task_link(parent) if parent else None),
            row('등록', t['created_at']), row('최종 수정', t['updated_at']),
        ])], className='table table-sm mb-0')), className='mb-3'),
        dbc.Card([
            dbc.CardHeader(f'하위 업무 ({len(children)})', className='small'),
            dbc.ListGroup([dbc.ListGroupItem([ui.status_badge(c['status']), ui.task_link(c)]) for c in children],
                          flush=True) if children else dbc.CardBody('없음', className='small text-muted'),
        ], className='mb-3'),
    ]
    kw = store.keyword_stats(25, task_id=tid)
    if kw:
        side.append(dbc.Card([
            dbc.CardHeader('이 업무의 키워드', className='small'),
            dbc.CardBody(html.Div([
                dcc.Link(f'#{k} {n}' if n > 1 else f'#{k}', href=ui.search_href(k),
                         className='badge rounded-pill text-bg-light border text-decoration-none me-1 mb-1')
                for k, n in kw])),
        ]))

    hist = store.list_history(tid)
    history = dbc.ListGroup([ui.history_card(h) for h in hist]) if hist else \
        html.Div('이력이 없습니다.', className='text-muted small')
    pending = sorted(h['id'] for h in hist if h['ai_status'] == 'pending')

    refs = store.list_references(task_id=tid)
    ref_view = dbc.ListGroup([
        dbc.ListGroupItem([
            html.Div([
                html.I(className=f"bi {ui.REF_ICON[r['kind']]} me-2"),
                html.Span(r['title'], className='fw-semibold me-auto'),
                html.Span(r['created_at'], className='small text-muted me-2'),
                dbc.Button(html.I(className='bi bi-x'), id={'type': 'r-del', 'id': r['id']}, color='link',
                           size='sm', className='text-muted p-0', title='삭제'),
            ], className='d-flex align-items-center'),
            ui.ref_target(r),
            html.Div(r['note'], className='small mt-1', style={'whiteSpace': 'pre-wrap'}) if r['note'] else None,
        ]) for r in refs
    ]) if refs else html.Div('참고자료가 없습니다.', className='text-muted small')

    links = store.linked_tasks(tid)
    link_view = dbc.ListGroup([
        dbc.ListGroupItem([
            html.Div([
                dbc.Badge(l['relation'], color='info', className='me-2'),
                ui.status_badge(l['status']), ui.task_link(l),
                html.Span(className='me-auto'),
                dbc.Button(html.I(className='bi bi-x'), id={'type': 'l-del', 'id': l['link_id']}, color='link',
                           size='sm', className='text-muted p-0', title='연결 해제'),
            ], className='d-flex align-items-center'),
            html.Div(l['note'], className='small text-muted mt-1') if l['note'] else None,
        ]) for l in links
    ]) if links else html.Div('연결된 업무가 없습니다.', className='text-muted small')

    linked_ids = {l['id'] for l in links} | {tid}
    targets = [{'label': f"#{o['id']} {o['title']}", 'value': o['id']}
               for o in store.list_tasks() if o['id'] not in linked_ids]
    return header, side, history, ref_view, link_view, targets, pending, not pending


@callback(Output('td-refresh', 'data', allow_duplicate=True), Input('td-poll', 'n_intervals'),
          State('td-id', 'data'), State('td-pending', 'data'), State('td-refresh', 'data'),
          prevent_initial_call=True)
def poll_ai(_, tid, pending, n):
    """AI 정리가 끝난 이력이 생기면 화면을 다시 그린다."""
    if sorted(store.pending_ai_ids(tid)) == sorted(pending or []):
        return no_update
    return (n or 0) + 1


# ── 상태 변경 ─────────────────────────────────────────────────────────

@callback(Output('td-refresh', 'data', allow_duplicate=True), Input('td-status', 'value'),
          State('td-id', 'data'), State('author-store', 'data'), State('td-refresh', 'data'),
          prevent_initial_call=True)
def change_status(status, tid, author, n):
    t = store.get_task(tid)
    if not t or not status or status == t['status']:
        return no_update
    store.update_task(tid, author=author or '', status=status)
    return (n or 0) + 1


# ── 업무 수정/삭제 ─────────────────────────────────────────────────────

@callback(Output('ed-modal', 'is_open'), Output('ed-title', 'value'), Output('ed-desc', 'value'),
          Output('ed-owner', 'value'), Output('ed-tags', 'value'), Output('ed-start', 'value'),
          Output('ed-due', 'value'), Output('ed-parent', 'value'), Output('ed-parent', 'options'),
          Output('ed-error', 'children'), Output('ed-error', 'is_open'),
          Output('td-refresh', 'data', allow_duplicate=True), Output('td-redirect', 'pathname'),
          Input('ed-open', 'n_clicks'), Input('ed-cancel', 'n_clicks'), Input('ed-save', 'n_clicks'),
          Input('ed-delete', 'n_clicks'),
          State('td-id', 'data'), State('ed-title', 'value'), State('ed-desc', 'value'),
          State('ed-owner', 'value'), State('ed-tags', 'value'), State('ed-start', 'value'),
          State('ed-due', 'value'), State('ed-parent', 'value'), State('author-store', 'data'),
          State('td-refresh', 'data'), prevent_initial_call=True)
def edit_task(n_open, _c, _s, _d, tid, title, desc, owner, tags, start, due, parent, author, n):
    nu = no_update
    trig = ctx.triggered_id
    if trig == 'ed-open':
        if not n_open:  # 헤더가 다시 그려지며 버튼이 새로 생길 때의 빈 트리거
            return (nu,) * 13
        t = store.get_task(tid)
        opts = [{'label': f"#{o['id']} {o['title']}", 'value': o['id']} for o in store.list_tasks() if o['id'] != tid]
        return (True, t['title'], t['description'], t['owner'], ', '.join(t['tags']), t['start_date'] or None,
                t['due_date'] or None, t['parent_id'], opts, '', False, nu, nu)
    if trig == 'ed-cancel':
        return (False,) + (nu,) * 8 + ('', False, nu, nu)
    if trig == 'ed-delete':
        store.delete_task(tid)
        return (False,) + (nu,) * 8 + ('', False, nu, '/')
    try:
        store.update_task(tid, author=author or '', title=title, description=desc or '', owner=owner or '',
                          tags=tags or '', start_date=start or '', due_date=due or '', parent_id=parent)
    except ValueError as e:
        return (True,) + (nu,) * 8 + (str(e), True, nu, nu)
    return (False,) + (nu,) * 8 + ('', False, (n or 0) + 1, nu)


# ── 이력 ─────────────────────────────────────────────────────────────

@callback(Output('h-content', 'value', allow_duplicate=True), Input('h-file', 'contents'),
          State('h-content', 'value'), prevent_initial_call=True)
def load_text_file(contents, current):
    if not contents:
        return no_update
    text = decode_text_upload(contents)
    return f'{current.rstrip()}\n\n{text}' if (current or '').strip() else text


@callback(Output('td-refresh', 'data', allow_duplicate=True), Output('h-content', 'value'),
          Output('h-when', 'value'), Output('h-error', 'children'), Output('h-error', 'is_open'),
          Input('h-add', 'n_clicks'), State('td-id', 'data'), State('h-type', 'value'),
          State('h-content', 'value'), State('h-when', 'value'), State('h-ai', 'value'),
          State('author-store', 'data'), State('td-refresh', 'data'), prevent_initial_call=True)
def add_history(_, tid, etype, content, when, use_ai, author, n):
    try:
        h = store.add_history(tid, content, etype, author or '', when or None)
    except ValueError as e:
        return no_update, no_update, no_update, str(e), True
    if use_ai:
        enrich.submit(h['id'])
    return (n or 0) + 1, '', None, '', False


@callback(Output('he-modal', 'is_open'), Output('he-id', 'data'), Output('he-type', 'value'),
          Output('he-when', 'value'), Output('he-title', 'value'), Output('he-summary', 'value'),
          Output('he-keywords', 'value'), Output('he-content', 'value'), Output('he-error', 'children'),
          Output('he-error', 'is_open'), Output('td-refresh', 'data', allow_duplicate=True),
          Input({'type': 'h-edit', 'id': ALL}, 'n_clicks'), Input('he-cancel', 'n_clicks'),
          Input('he-save', 'n_clicks'), Input('he-regen', 'n_clicks'),
          State('he-id', 'data'), State('he-type', 'value'), State('he-when', 'value'), State('he-title', 'value'),
          State('he-summary', 'value'), State('he-keywords', 'value'), State('he-content', 'value'),
          State('td-refresh', 'data'), prevent_initial_call=True)
def edit_history(_e, _c, _s, _r, eid, etype, when, title, summary, keywords, content, n):
    nu = no_update
    trig = ctx.triggered_id
    if isinstance(trig, dict):
        if not ctx.triggered[0]['value']:  # 목록이 다시 그려질 때의 빈 트리거
            return (nu,) * 11
        h = store.get_history(trig['id'])
        if h is None:
            return (nu,) * 11
        return (True, h['id'], h['entry_type'], h['occurred_at'].replace(' ', 'T'), h['title'], h['summary'],
                ', '.join(h['keywords']), h['content'], '', False, nu)
    if trig == 'he-cancel':
        return (False,) + (nu,) * 7 + ('', False, nu)
    try:
        store.update_history(eid, entry_type=etype, occurred_at=when or None, title=title or '',
                             summary=summary or '', keywords=keywords or '', content=content or '')
    except ValueError as e:
        return (True,) + (nu,) * 7 + (str(e), True, nu)
    if trig == 'he-regen':
        enrich.submit(eid)
    return (False,) + (nu,) * 7 + ('', False, (n or 0) + 1)


@callback(Output('td-refresh', 'data', allow_duplicate=True), Input({'type': 'h-regen', 'id': ALL}, 'n_clicks'),
          State('td-refresh', 'data'), prevent_initial_call=True)
def regen_history(_, n):
    if not ctx.triggered_id or not ctx.triggered[0]['value']:
        return no_update
    enrich.submit(ctx.triggered_id['id'])
    return (n or 0) + 1


@callback(Output('td-refresh', 'data', allow_duplicate=True), Input({'type': 'h-del', 'id': ALL}, 'n_clicks'),
          Input({'type': 'r-del', 'id': ALL}, 'n_clicks'), Input({'type': 'l-del', 'id': ALL}, 'n_clicks'),
          State('td-refresh', 'data'), prevent_initial_call=True)
def delete_item(_h, _r, _l, n):
    trig = ctx.triggered_id
    if not trig or not ctx.triggered[0]['value']:  # 목록이 새로 그려질 때(n_clicks=None) 무시
        return no_update
    {'h-del': store.delete_history, 'r-del': store.delete_reference,
     'l-del': store.unlink_tasks}[trig['type']](trig['id'])
    return (n or 0) + 1


# ── 참고자료 ──────────────────────────────────────────────────────────

@callback(Output('r-url', 'style'), Output('r-file', 'style'), Output('r-file-name', 'children'),
          Input('r-kind', 'value'), Input('r-file', 'filename'))
def toggle_ref_inputs(kind, fname):
    hide = {'display': 'none'}
    return (None if kind == 'link' else hide,
            {'borderStyle': 'dashed'} if kind == 'file' else hide,
            f'선택된 파일: {fname}' if kind == 'file' and fname else '')


@callback(Output('td-refresh', 'data', allow_duplicate=True), Output('r-title', 'value'), Output('r-url', 'value'),
          Output('r-note', 'value'), Output('r-file', 'contents'), Output('r-file', 'filename'),
          Output('r-error', 'children'), Output('r-error', 'is_open'),
          Input('r-add', 'n_clicks'), State('td-id', 'data'), State('r-kind', 'value'), State('r-title', 'value'),
          State('r-url', 'value'), State('r-note', 'value'), State('r-file', 'contents'),
          State('r-file', 'filename'), State('author-store', 'data'), State('td-refresh', 'data'),
          prevent_initial_call=True)
def add_reference(_, tid, kind, title, url, note, contents, fname, author, n):
    data = None
    if kind == 'file' and contents:
        data = base64.b64decode(contents.split(',', 1)[1])
    try:
        store.add_reference(tid, kind, title or '', url or '', note or '', fname or '', data, author or '')
    except ValueError as e:
        return (no_update,) * 6 + (str(e), True)
    return (n or 0) + 1, '', '', '', None, None, '', False


# ── 업무 연결 ─────────────────────────────────────────────────────────

@callback(Output('td-refresh', 'data', allow_duplicate=True), Output('l-target', 'value'), Output('l-note', 'value'),
          Output('l-error', 'children'), Output('l-error', 'is_open'),
          Input('l-add', 'n_clicks'), State('td-id', 'data'), State('l-target', 'value'), State('l-rel', 'value'),
          State('l-note', 'value'), State('author-store', 'data'), State('td-refresh', 'data'),
          prevent_initial_call=True)
def add_link(_, tid, target, rel, note, author, n):
    if not target:
        return no_update, no_update, no_update, '연결할 업무를 선택하세요.', True
    try:
        store.link_tasks(tid, int(target), rel, note or '', author or '')
    except ValueError as e:
        return no_update, no_update, no_update, str(e), True
    return (n or 0) + 1, None, '', '', False
