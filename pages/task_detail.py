"""업무 상세: 개요, 이력 타임라인, 참고자료, 연결 업무, 하위 업무."""
import base64

import dash
import dash_bootstrap_components as dbc
from dash import ALL, Input, Output, State, callback, ctx, dcc, html, no_update

from components import doc_ui, todo_ui, ui
from components.history_form import (decode_text_upload, history_form, register_form_callbacks, save_history,
                                     task_options)
from services import auth, confluence, documents, llm, mailbox, outbox, store

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
                dbc.Col([dbc.Label('담당자'), todo_ui.assignee_picker('ed')]),
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


def _import_history_modal():
    """다른 업무·동향에 남긴 기존 기록을 찾아 이 업무에도 건다."""
    return dbc.Modal([
        dbc.ModalHeader(dbc.ModalTitle([html.I(className='bi bi-box-arrow-in-down me-2'), '기존 이력을 이 업무에 연결'])),
        dbc.ModalBody([
            dbc.Input(id='hx-q', type='search', debounce=True, placeholder='검색: 원문·제목·요약·키워드·업무명·임원 이름'),
            html.Div('다른 업무·임원 동향에 남긴 기록을 찾아 고르세요. 고른 기록은 원래 자리에도 그대로 있고, 이 업무 이력에도 보입니다.',
                     className='small text-muted my-2'),
            html.Div(dbc.Checklist(id='hx-pick', options=[], value=[], className='small'),
                     style={'maxHeight': '50vh', 'overflowY': 'auto'}),
            dbc.Alert(id='hx-msg', is_open=False, className='small py-2 mt-2 mb-0'),
        ]),
        dbc.ModalFooter([
            dbc.Button('닫기', id='hx-close', color='secondary', outline=True),
            dbc.Button([html.I(className='bi bi-link-45deg me-1'), '연결'], id='hx-save', color='primary'),
        ]),
    ], id='hx-modal', is_open=False, size='lg', scrollable=True)


def _quick_history_modal():
    """참고자료·연결 업무 탭에서 바로 쓰는 기록 — 자료에 대한 기록이면 그 자료가 붙고, 연결 업무면 두 업무에 걸린다."""
    ai_on = llm.is_configured()
    return dbc.Modal([
        dbc.ModalHeader(dbc.ModalTitle(id='tq-title')),
        dbc.ModalBody([
            dcc.Store(id='tq-meta'),
            html.Div(id='tq-hint', className='small text-muted mb-2'),
            dbc.Row([
                dbc.Col(dbc.Select(id='tq-type', value='메모',
                                   options=[{'label': t, 'value': t} for t in store.ENTRY_TYPES]), md=4),
                dbc.Col(dbc.Input(id='tq-when', type='datetime-local'), md=5),
            ], className='g-2 mb-2'),
            dbc.Textarea(id='tq-content', rows=6, placeholder='기록할 내용 (검토 의견, 변경 사항, 회의 결과 등)'),
            dbc.Switch(id='tq-ai', value=ai_on, disabled=not ai_on, className='mt-2 small',
                       label='AI 요약·키워드 생성' if ai_on else 'AI 요약·키워드 (LLM 미설정)'),
            dbc.Alert(id='tq-error', color='danger', is_open=False, className='small py-2 mt-2 mb-0'),
        ]),
        dbc.ModalFooter([
            dbc.Button('취소', id='tq-cancel', color='secondary', outline=True),
            dbc.Button([html.I(className='bi bi-check2 me-1'), '기록'], id='tq-save', color='primary'),
        ]),
    ], id='tq-modal', is_open=False)


def _history_form(tid: int):
    form = history_form('h')
    # 여러 업무에 걸친 기록이면 다른 업무도 골라 둔다 — 그 업무들의 이력에도 같이 보인다
    form.insert(2, dcc.Dropdown(id='h-also', multi=True, options=task_options(exclude=tid), value=[],
                                placeholder='함께 걸친 다른 업무 (여러 업무에 해당하는 기록이면 선택)',
                                className='mb-2'))
    return dbc.Card(dbc.CardBody(form), className='mb-3')


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


def _source_modals(tid: int, src: dict, open_kind: str | None):
    """업무를 만든 출처(AI 가 할 일을 찾은 메모, 받은 메일)를 보는 창."""
    out = []
    h = src['history']
    if h:
        go = dcc.Link([html.I(className='bi bi-box-arrow-up-right me-1'), f"그 메모가 있는 업무 #{h['task_id']} 열기"],
                      href=f"/task/{h['task_id']}", className='small me-auto') if h['task_id'] and h['task_id'] != tid \
            else html.Span(className='me-auto')
        out.append(dbc.Modal([
            dbc.ModalHeader(dbc.ModalTitle([html.I(className='bi bi-stars me-2'), 'AI 요약에서 찾은 할 일 — 원래 메모'])),
            dbc.ModalBody(dbc.ListGroup([ui.history_card(h, show_task=True, actions=False,
                                                         added_todos=store.todos_for_history(h['id']))])),
            dbc.ModalFooter([go, dbc.Button('닫기', id='td-src-history-close', color='secondary', outline=True)]),
        ], id='td-src-history', is_open=open_kind == 'history', size='lg', scrollable=True))
    if src['mails']:
        m = src['mails'][0]
        out.append(dbc.Modal([
            dbc.ModalHeader(dbc.ModalTitle([html.I(className='bi bi-envelope me-2'),
                                            html.Span(m['subject'], id='td-src-mail-title')])),
            dbc.ModalBody(_mail_body(m), id='td-src-mail-body'),
            dbc.ModalFooter([
                dcc.Link([html.I(className='bi bi-box-arrow-up-right me-1'), '받은 메일 화면에서 열기'],
                         href=f"/inbox?mail={m['id']}", id='td-src-mail-link', className='small me-auto'),
                dbc.Button('닫기', id='td-src-mail-close', color='secondary', outline=True),
            ]),
        ], id='td-src-mail', is_open=open_kind == 'mail', size='lg', scrollable=True))
    return out


def _mail_body(m: dict):
    return [html.Div(f"보낸 사람 {m['sender']} · {m['mail_date']}", className='small text-muted mb-2'),
            html.Pre(m['body'], className='small mb-0', style={'whiteSpace': 'pre-wrap', 'fontFamily': 'inherit'})]


def _source_card(src: dict):
    if not src['history'] and not src['mails']:
        return None
    items = []
    if src['history']:
        h = src['history']
        items.append(dbc.ListGroupItem([
            html.Div([html.I(className='bi bi-stars me-1'), 'AI 요약에서 찾은 할 일'], className='small text-muted'),
            html.Div(h['title'] or h['content'][:60], className='small fw-semibold text-truncate'),
            html.Div(' · '.join(x for x in [h['occurred_at'], h['author']] if x), className='small text-muted'),
        ], id='td-src-history-open', action=True, n_clicks=0))
    for m in src['mails']:
        items.append(dbc.ListGroupItem([
            html.Div([html.I(className='bi bi-envelope me-1'),
                      '받은 메일에서 만든 업무' if m['created'] else '연결된 메일'], className='small text-muted'),
            html.Div(m['subject'], className='small fw-semibold text-truncate'),
            html.Div(f"{m['sender']} · {m['mail_date']}", className='small text-muted text-truncate'),
        ], id={'type': 'td-src-mail-open', 'id': m['id']}, action=True, n_clicks=0))
    return dbc.Card([dbc.CardHeader([html.I(className='bi bi-link-45deg me-1'), '출처'], className='small'),
                     dbc.ListGroup(items, flush=True)], className='mb-3')


def layout(task_id=None, src=None, **_):
    try:
        tid = int(task_id)
    except (TypeError, ValueError):
        tid = None
    if tid is None or store.get_task(tid) is None:
        return dbc.Alert(['업무를 찾을 수 없습니다. ', dcc.Link('목록으로', href='/')], color='warning')
    sources = store.task_sources(tid)

    return html.Div([
        dcc.Store(id='td-id', data=tid),
        dcc.Store(id='td-refresh', data=0),
        dcc.Store(id='td-pending', data=[]),
        dcc.Interval(id='td-poll', interval=3000, disabled=True),
        dcc.Location(id='td-redirect', refresh='callback-nav'),
        _edit_modal(),
        _quick_history_modal(),
        _import_history_modal(),
        *_source_modals(tid, sources, src),
        html.Div(id='td-header', className='mb-3'),
        dbc.Row([
            dbc.Col([
                dbc.Tabs([
                    dbc.Tab([_history_form(tid),
                             html.Div(dbc.Button([html.I(className='bi bi-box-arrow-in-down me-1'), '기존 이력 연결'],
                                                 id='hx-open', color='link', size='sm', className='p-0'),
                                      className='text-end mb-1'),
                             html.Div(id='td-history')], label='이력', tab_id='history'),
                    dbc.Tab([dbc.Card(dbc.CardBody(todo_ui.add_form('tt', with_task=False)), className='mb-3'),
                             html.Div(id='td-todos')], label='하위 업무', tab_id='todos', id='td-todo-tab'),
                    dbc.Tab([_reference_form(), html.Div(id='td-refs')], label='참고자료', tab_id='refs'),
                    dbc.Tab([_link_form(), html.Div(id='td-links')], label='연결 업무', tab_id='links'),
                ], id='td-tabs', active_tab='history', className='mb-3'),
            ], lg=8),
            dbc.Col([_source_card(sources), html.Div(id='td-side')], lg=4),
        ]),
    ])


# ── 렌더링 ────────────────────────────────────────────────────────────

@callback(Output('td-header', 'children'), Output('td-side', 'children'), Output('td-history', 'children'),
          Output('td-refs', 'children'), Output('td-links', 'children'), Output('l-target', 'options'),
          Output('td-pending', 'data'), Output('td-poll', 'disabled'),
          Output('td-todos', 'children'), Output('td-todo-tab', 'label'), Output('tt-assignee', 'options'),
          Input('td-refresh', 'data'), Input('todo-changed', 'data'), Input('history-changed', 'data'),
          Input('doc-changed', 'data'), State('td-id', 'data'))
def render(_, _t, _h, _d, tid):
    t = store.get_task(tid)
    if t is None:
        return dbc.Alert('삭제된 업무입니다.', color='warning'), None, None, None, None, [], [], True, None, '하위 업무', []
    parent = store.get_task(t['parent_id']) if t['parent_id'] else None

    header = dbc.Card(dbc.CardBody([
        html.Div([
            html.H4([html.Span(f"#{t['id']} ", className='text-muted'), t['title']], className='mb-0 me-auto'),
            dbc.Select(id='td-status', value=t['status'], options=_opts(store.STATUSES),
                       style={'width': '110px'}, className='me-2'),
            dbc.Button([html.I(className='bi bi-send me-1'), '메일로 공유'], id='td-mail', color='secondary',
                       outline=True, size='sm'),
            dbc.Button([html.I(className='bi bi-pencil me-1'), '수정'], id='ed-open', color='secondary',
                       outline=True, size='sm'),
        ], className='d-flex align-items-center gap-2 mb-2'),
        html.Div([ui.status_badge(t['status']), *ui.tag_badges(t['tags'])], className='mb-2'),
        ui.multiline(t['description']) if t['description'] else html.Div('설명 없음', className='text-muted small'),
    ]))

    def row(label, value):
        return html.Tr([html.Th(label, className='text-muted fw-normal small', style={'width': '35%'}),
                        html.Td(value or '-', className='small')])

    side = [
        dbc.Card(dbc.CardBody(html.Table([html.Tbody([
            row('담당자', todo_ui.assignee_names(t)), row('시작일', t['start_date']), row('마감일', t['due_date']),
            row('상위 업무', ui.task_link(parent) if parent else None),
            row('등록', t['created_at']), row('최종 수정', t['updated_at']),
        ])], className='table table-sm mb-0')), className='mb-3'),
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
    history = dbc.ListGroup([
        ui.history_card(h, added_todos=store.todos_for_history(h['id']) if h['ai_todos'] else None,
                        context_task_id=tid)
        for h in hist]) if hist else \
        html.Div('이력이 없습니다.', className='text-muted small')
    pending = sorted(h['id'] for h in hist if h['ai_status'] == 'pending')

    # 참고자료 — 이 업무·하위 업무에 등록한 자료, 그 업무들의 기록에 붙은 자료, 함께 걸친 기록의 자료
    refs = [r for r in store.references_for_task(tid) if r['kind'] != 'file']  # 파일은 아래 '문서'에 판으로
    ref_counts = store.reference_history_counts(tid, [r['id'] for r in refs])
    hist_titles = store.history_titles(r['history_id'] for r in refs)
    ref_view = dbc.ListGroup([
        dbc.ListGroupItem([
            html.Div([
                html.I(className=f"bi {ui.REF_ICON[r['kind']]} me-2"),
                html.Span(r['title'], className='fw-semibold me-2'),
                dbc.Badge(r['source'], color='light', text_color='dark', className='border me-1'),
                dbc.Badge('하위 업무' if r['via'] == 'child' else '함께 걸친 기록', color='info', className='me-1')
                if r['via'] != 'self' else None,
                html.Span(className='me-auto'),
                html.Span(r['created_at'], className='small text-muted me-2'),
                dbc.Button([html.I(className='bi bi-journal-plus me-1'), '이력'], id={'type': 'r-hist', 'id': r['id']},
                           color='link', size='sm', className='p-0 me-2', title='이 자료에 대한 기록 남기기'),
                dbc.Button(html.I(className='bi bi-x'), id={'type': 'r-del', 'id': r['id']}, color='link',
                           size='sm', className='text-muted p-0', title='삭제'),
            ], className='d-flex align-items-center'),
            ui.ref_target(r),
            html.Div(['출처 업무: ', ui.task_link(r['via_task'])], className='small text-muted')
            if r['via'] != 'self' and r['via_task'] else None,
            ui.confluence_box(r, {'type': 'r-conf', 'id': r['id']}),
            html.Div(r['note'], className='small mt-1', style={'whiteSpace': 'pre-wrap'}) if r['note'] else None,
            html.Div([html.I(className='bi bi-clock-history me-1'), '이력에 첨부: ',
                      hist_titles.get(r['history_id'], f"#{r['history_id']}")]
                     + ([f" · 이 자료에 대한 기록 {ref_counts.get(r['id'], 0)}건"]
                        if ref_counts.get(r['id'], 0) > 1 else []),
                     className='small text-muted mt-1') if r['history_id'] else
            html.Div([html.I(className='bi bi-clock-history me-1'), f"이 자료에 대한 기록 {ref_counts[r['id']]}건"],
                     className='small text-muted mt-1') if ref_counts.get(r['id']) else None,
        ]) for r in refs
    ]) if refs else html.Div('링크·메모가 없습니다.', className='text-muted small')
    # 문서 — 메일 첨부와 올린 파일을 같은 문서끼리 묶어 판이 바뀌어 온 순서대로 (하위 업무·함께 걸친 기록 포함)
    docs = documents.for_task(tid)
    ref_view = html.Div([
        html.H6([html.I(className='bi bi-files me-1'), f'문서 {len(docs)}' if docs else '문서'], className='mb-2'),
        doc_ui.document_cards(docs, delete=True, empty='아직 문서가 없습니다. 메일 첨부나 올린 파일이 문서별로 모입니다.'),
        html.H6([html.I(className='bi bi-link-45deg me-1'), '링크·메모'], className='mt-3 mb-2'),
        ref_view,
    ])

    links = store.linked_tasks(tid)
    link_view = dbc.ListGroup([
        dbc.ListGroupItem([
            html.Div([
                dbc.Badge(l['relation'], color='info', className='me-2'),
                ui.status_badge(l['status']), ui.task_link(l),
                html.Span(className='me-auto'),
                html.Span(f"함께 걸친 기록 {n}건", className='small text-muted me-2')
                if (n := store.shared_history_count(tid, l['id'])) else None,
                dbc.Button([html.I(className='bi bi-journal-plus me-1'), '이력'], id={'type': 'l-hist', 'id': l['id']},
                           color='link', size='sm', className='p-0 me-2',
                           title='두 업무에 함께 걸리는 기록 남기기'),
                dbc.Button(html.I(className='bi bi-x'), id={'type': 'l-del', 'id': l['link_id']}, color='link',
                           size='sm', className='text-muted p-0', title='연결 해제'),
            ], className='d-flex align-items-center'),
            html.Div(l['note'], className='small text-muted mt-1') if l['note'] else None,
        ]) for l in links
    ]) if links else html.Div('연결된 업무가 없습니다.', className='text-muted small')

    linked_ids = {l['id'] for l in links} | {tid}
    targets = [{'label': f"#{o['id']} {o['title']}", 'value': o['id']}
               for o in store.list_tasks() if o['id'] not in linked_ids]
    todos = store.list_todos(task_id=tid)
    n_done = sum(x['done'] for x in todos)
    todo_view = todo_ui.todo_list(todos, show_task=False,
                                  empty='하위 업무가 없습니다 — 위에 적고 Enter. 마감·담당을 정하면 "내 업무"와 캘린더에 나옵니다.')
    return (header, side, history, ref_view, link_view, targets, pending, not pending,
            todo_view, f'하위 업무 ({n_done}/{len(todos)})' if todos else '하위 업무', todo_ui.assignee_options())


todo_ui.register_add_callback('tt', task_id_store='td-id')


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
          State('td-id', 'data'), State('td-refresh', 'data'),
          prevent_initial_call=True)
def change_status(status, tid, n):
    author = auth.current_name()
    t = store.get_task(tid)
    if not t or not status or status == t['status']:
        return no_update
    store.update_task(tid, author=author or '', status=status)
    return (n or 0) + 1


# ── 업무 수정/삭제 ─────────────────────────────────────────────────────

@callback(Output('ed-modal', 'is_open'), Output('ed-title', 'value'), Output('ed-desc', 'value'),
          Output('ed-assignees', 'value'), Output('ed-tags', 'value'), Output('ed-start', 'value'),
          Output('ed-due', 'value'), Output('ed-parent', 'value'), Output('ed-parent', 'options'),
          Output('ed-error', 'children'), Output('ed-error', 'is_open'),
          Output('td-refresh', 'data', allow_duplicate=True), Output('td-redirect', 'pathname'),
          Output('ed-assignees', 'options'), Output('ed-assignee-extra', 'value'),
          Input('ed-open', 'n_clicks'), Input('ed-cancel', 'n_clicks'), Input('ed-save', 'n_clicks'),
          Input('ed-delete', 'n_clicks'),
          State('td-id', 'data'), State('ed-title', 'value'), State('ed-desc', 'value'),
          State('ed-assignees', 'value'), State('ed-assignee-extra', 'value'), State('ed-tags', 'value'),
          State('ed-start', 'value'), State('ed-due', 'value'), State('ed-parent', 'value'),
          State('td-refresh', 'data'), prevent_initial_call=True)
def edit_task(n_open, _c, _s, _d, tid, title, desc, assignees, extra, tags, start, due, parent, n):
    author = auth.current_name()
    nu = no_update
    trig = ctx.triggered_id
    if trig not in ('ed-open', 'ed-cancel', 'ed-save', 'ed-delete'):
        return (nu,) * 15
    if trig == 'ed-open':
        if not n_open:  # 헤더가 다시 그려지며 버튼이 새로 생길 때의 빈 트리거
            return (nu,) * 15
        t = store.get_task(tid)
        opts = [{'label': f"#{o['id']} {o['title']}", 'value': o['id']} for o in store.list_tasks() if o['id'] != tid]
        return (True, t['title'], t['description'], todo_ui.assignee_values(t['assignees']), ', '.join(t['tags']),
                t['start_date'] or None, t['due_date'] or None, t['parent_id'], opts, '', False, nu, nu,
                todo_ui.assignee_options(t['assignees']), '')
    if trig == 'ed-cancel':
        return (False,) + (nu,) * 8 + ('', False, nu, nu, nu, nu)
    if trig == 'ed-delete':
        store.delete_task(tid)
        return (False,) + (nu,) * 8 + ('', False, nu, '/', nu, nu)
    try:
        current = store.get_task(tid)['assignees']
        store.update_task(tid, author=author or '', title=title, description=desc or '',
                          assignees=todo_ui.assignees_from(assignees, extra, current),
                          tags=tags or '', start_date=start or '', due_date=due or '', parent_id=parent)
    except ValueError as e:
        return (True,) + (nu,) * 8 + (str(e), True, nu, nu, nu, nu)
    return (False,) + (nu,) * 8 + ('', False, (n or 0) + 1, nu, nu, nu)


# ── 이력 ─────────────────────────────────────────────────────────────

@callback(Output('h-content', 'value', allow_duplicate=True), Input('h-file', 'contents'),
          State('h-content', 'value'), prevent_initial_call=True)
def load_text_file(contents, current):
    if not contents:
        return no_update
    text = decode_text_upload(contents)
    return f'{current.rstrip()}\n\n{text}' if (current or '').strip() else text


register_form_callbacks('h')


@callback(Output('td-refresh', 'data', allow_duplicate=True), Output('h-content', 'value'),
          Output('h-when', 'value'), Output('h-links', 'value'), Output('h-attach', 'contents'),
          Output('h-attach', 'filename'), Output('h-attach-names', 'children', allow_duplicate=True),
          Output('h-error', 'children'), Output('h-error', 'is_open'), Output('h-people', 'value'),
          Output('h-also', 'value'),
          Input('h-add', 'n_clicks'), State('td-id', 'data'), State('h-type', 'value'),
          State('h-content', 'value'), State('h-when', 'value'), State('h-ai', 'value'),
          State('h-links', 'value'), State('h-attach', 'contents'), State('h-attach', 'filename'),
          State('h-people', 'value'), State('h-also', 'value'), State('td-refresh', 'data'),
          prevent_initial_call=True)
def add_history(_, tid, etype, content, when, use_ai, links, att, att_names, people, also, n):
    try:
        save_history(tid, etype, content, when, use_ai, links, att, att_names, auth.current_name(), people,
                     also_task_ids=also)
    except ValueError as e:
        return (no_update,) * 7 + (str(e), True, no_update, no_update)
    return (n or 0) + 1, '', None, '', None, None, '', '', False, [], []


@callback(Output('td-refresh', 'data', allow_duplicate=True),
          Input({'type': 'r-del', 'id': ALL}, 'n_clicks'), Input({'type': 'l-del', 'id': ALL}, 'n_clicks'),
          State('td-refresh', 'data'), prevent_initial_call=True)
def delete_item(_r, _l, n):
    """참고자료 삭제·업무 연결 해제 (이력 삭제는 components/history_actions.py)"""
    trig = ctx.triggered_id
    if not trig or not ctx.triggered[0]['value']:  # 목록이 새로 그려질 때(n_clicks=None) 무시
        return no_update
    {'r-del': store.delete_reference, 'l-del': store.unlink_tasks}[trig['type']](trig['id'])
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
          State('r-file', 'filename'), State('td-refresh', 'data'),
          prevent_initial_call=True)
def add_reference(_, tid, kind, title, url, note, contents, fname, n):
    author = auth.current_name()
    data = None
    if kind == 'file' and contents:
        data = base64.b64decode(contents.split(',', 1)[1])
    try:
        ref = store.add_reference(tid, kind, title or '', url or '', note or '', fname or '', data, author or '')
    except ValueError as e:
        return (no_update,) * 6 + (str(e), True)
    if kind == 'link' and confluence.is_configured() and confluence.is_confluence_url(ref['url']):
        try:  # 컨플루언스 페이지면 바로 읽어 붙인다 (실패해도 자료는 남고, 목록에서 이유와 '다시 읽기')
            confluence.refresh_reference(ref['id'])
        except confluence.ConfluenceError:
            pass
    return (n or 0) + 1, '', '', '', None, None, '', False


@callback(Output('td-refresh', 'data', allow_duplicate=True), Input({'type': 'r-conf', 'id': ALL}, 'n_clicks'),
          State('td-refresh', 'data'), prevent_initial_call=True)
def reread_confluence(_, n):
    """컨플루언스 링크 참고자료 다시 읽기 (실패 이유는 목록에 남는다)."""
    trig = ctx.triggered_id
    if not trig or not ctx.triggered[0]['value']:
        return no_update
    try:
        confluence.refresh_reference(trig['id'])
    except confluence.ConfluenceError:
        pass
    return (n or 0) + 1


# ── 업무 연결 ─────────────────────────────────────────────────────────

@callback(Output('td-refresh', 'data', allow_duplicate=True), Output('l-target', 'value'), Output('l-note', 'value'),
          Output('l-error', 'children'), Output('l-error', 'is_open'),
          Input('l-add', 'n_clicks'), State('td-id', 'data'), State('l-target', 'value'), State('l-rel', 'value'),
          State('l-note', 'value'), State('td-refresh', 'data'),
          prevent_initial_call=True)
def add_link(_, tid, target, rel, note, n):
    author = auth.current_name()
    if not target:
        return no_update, no_update, no_update, '연결할 업무를 선택하세요.', True
    try:
        store.link_tasks(tid, int(target), rel, note or '', author or '')
    except ValueError as e:
        return no_update, no_update, no_update, str(e), True
    return (n or 0) + 1, None, '', '', False


# ── 출처 보기 ─────────────────────────────────────────────────────────

@callback(Output('td-src-history', 'is_open'), Input('td-src-history-open', 'n_clicks'),
          Input('td-src-history-close', 'n_clicks'), prevent_initial_call=True)
def _toggle_history_source(_o, _c):
    if not ctx.triggered or not ctx.triggered[0]['value']:
        return no_update
    return ctx.triggered_id == 'td-src-history-open'


@callback(Output('td-src-mail', 'is_open'), Output('td-src-mail-title', 'children'),
          Output('td-src-mail-body', 'children'), Output('td-src-mail-link', 'href'),
          Input({'type': 'td-src-mail-open', 'id': ALL}, 'n_clicks'), Input('td-src-mail-close', 'n_clicks'),
          prevent_initial_call=True)
def _toggle_mail_source(_o, _c):
    """출처 카드의 메일을 누르면 그 메일을 창에 보여 준다 (연결된 메일이 여럿일 수 있음)."""
    if not ctx.triggered or not ctx.triggered[0]['value']:
        return no_update, no_update, no_update, no_update
    if ctx.triggered_id == 'td-src-mail-close':
        return False, no_update, no_update, no_update
    m = mailbox.get_mail(ctx.triggered_id['id'])
    if m is None:
        return no_update, no_update, no_update, no_update
    return True, m['subject'], _mail_body(m), f"/inbox?mail={m['id']}"


@callback(Output('mail-draft', 'data', allow_duplicate=True), Input('td-mail', 'n_clicks'), State('td-id', 'data'),
          prevent_initial_call=True)
def mail_task(n, tid):
    """업무 공유 메일 — 상태·담당·하위 업무·최근 기록, 첨부는 이 업무의 파일·메일 첨부(최신 판)에서 고른다."""
    if not n:  # 머리가 다시 그려질 때의 빈 트리거
        return no_update
    return outbox.draft_task(tid)


# ── 참고자료·연결 업무에서 바로 기록 ───────────────────────────────────

@callback(Output('tq-modal', 'is_open'), Output('tq-title', 'children'), Output('tq-hint', 'children'),
          Output('tq-meta', 'data'), Output('tq-content', 'value'), Output('tq-when', 'value'),
          Output('tq-error', 'children'), Output('tq-error', 'is_open'),
          Output('td-refresh', 'data', allow_duplicate=True), Output('tq-type', 'value'),
          Input({'type': 'r-hist', 'id': ALL}, 'n_clicks'), Input({'type': 'l-hist', 'id': ALL}, 'n_clicks'),
          Input('tq-cancel', 'n_clicks'), Input('tq-save', 'n_clicks'),
          State('td-id', 'data'), State('tq-meta', 'data'), State('tq-type', 'value'), State('tq-when', 'value'),
          State('tq-content', 'value'), State('tq-ai', 'value'), State('td-refresh', 'data'),
          prevent_initial_call=True)
def quick_history(_r, _l, _c, _s, tid, meta, etype, when, content, use_ai, n):
    nu = no_update
    trig = ctx.triggered_id
    if trig is None or not ctx.triggered[0]['value']:  # 목록이 다시 그려질 때의 빈 트리거
        return (nu,) * 10
    if trig == 'tq-cancel':
        return False, nu, nu, nu, nu, nu, '', False, nu, nu
    if isinstance(trig, dict) and trig['type'] == 'r-hist':
        r = store.get_reference(trig['id'])
        if r is None:
            return (nu,) * 10
        return (True, [html.I(className=f"bi {ui.REF_ICON[r['kind']]} me-2"), r['title']],
                '이 자료에 대한 기록 — 이 업무의 이력에 남고, 기록에 이 자료가 함께 붙습니다.',
                {'kind': 'ref', 'id': r['id']}, '', None, '', False, nu, '메모')
    if isinstance(trig, dict) and trig['type'] == 'l-hist':
        t = store.get_task(trig['id'])
        if t is None:
            return (nu,) * 10
        return (True, [html.I(className='bi bi-diagram-2 me-2'), f"#{t['id']} {t['title']} 와 함께 기록"],
                '두 업무에 함께 걸리는 기록 — 이 업무와 연결 업무 양쪽 이력에 보입니다.',
                {'kind': 'link', 'id': t['id']}, '', None, '', False, nu, '메모')
    if not meta:
        return (nu,) * 10
    try:
        save_history(tid, etype, content, when, use_ai, '', None, None, auth.current_name(),
                     also_task_ids=[meta['id']] if meta['kind'] == 'link' else None,
                     ref_ids=[meta['id']] if meta['kind'] == 'ref' else None)
    except ValueError as e:
        return True, nu, nu, nu, nu, nu, str(e), True, nu, nu
    return False, nu, nu, None, '', None, '', False, (n or 0) + 1, nu


# ── 기존 이력 연결 ────────────────────────────────────────────────────

@callback(Output('hx-modal', 'is_open'), Input('hx-open', 'n_clicks'), Input('hx-close', 'n_clicks'),
          prevent_initial_call=True)
def toggle_import(_o, _c):
    return ctx.triggered_id == 'hx-open'


@callback(Output('hx-pick', 'options'), Output('hx-pick', 'value'),
          Input('hx-q', 'value'), Input('hx-modal', 'is_open'), Input('td-refresh', 'data'), State('td-id', 'data'))
def search_existing(q, is_open, _, tid):
    """이 업무에 아직 걸리지 않은 기록 — 검색어가 없으면 최근 기록."""
    if not is_open:
        return no_update, no_update
    mine = {h['id'] for h in store.list_history(tid)}
    rows = [h for h in store.search_history(q or '', limit=60) if h['id'] not in mine][:40]
    opts = [{'label': html.Span([
        html.Span(f"{h['occurred_at'][:10]} ", className='text-muted'),
        dbc.Badge(h['entry_type'], color='light', text_color='dark', className='me-1 border'),
        html.B(h['title'] or h['content'][:50]),
        html.Span(f" — #{h['task_id']} {h['task_title']}" if h['task_id'] else
                  (' — ' + ', '.join(p['name'] for p in h['people']) + ' 동향' if h['people'] else ''),
                  className='text-muted'),
    ]), 'value': h['id']} for h in rows]
    return opts, []


@callback(Output('hx-msg', 'children'), Output('hx-msg', 'is_open'), Output('hx-msg', 'color'),
          Output('td-refresh', 'data', allow_duplicate=True),
          Input('hx-save', 'n_clicks'), State('hx-pick', 'value'), State('td-id', 'data'),
          State('td-refresh', 'data'), prevent_initial_call=True)
def import_history(n, picked, tid, r):
    if not n:
        return no_update, no_update, no_update, no_update
    if not picked:
        return '연결할 기록을 고르세요.', True, 'warning', no_update
    k = sum(store.link_history_task(int(hid), tid) for hid in picked)
    return f'기록 {k}건을 이 업무에 연결했습니다.', True, 'success', (r or 0) + 1
