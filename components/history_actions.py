"""이력 카드의 버튼(수정·삭제·AI 다시 생성·할 일로 추가)과 이력 수정 창 — 모든 화면 공용.

수정 창은 앱 레이아웃에 한 번만 들어가고(app.py), 무언가 바뀌면 전역 Store 'history-changed' 값을
올린다. 이력을 보여주는 화면(업무 상세·임원)은 그 Store 를 Input 으로 받아 다시 그린다.
"""
import re

import dash_bootstrap_components as dbc
from dash import ALL, Input, Output, State, callback, ctx, dash_table, dcc, html, no_update

from components import todo_ui, ui
from components.history_form import parse_uploads, people_options, register_form_callbacks, task_options
from services import auth, db, enrich, llm, store


def _opts(values):
    return [{'label': v, 'value': v} for v in values]


def history_edit_modal():
    return dbc.Modal([
        dbc.ModalHeader(dbc.ModalTitle('이력 수정')),
        dbc.ModalBody([
            dcc.Store(id='he-id'),
            dbc.Row([
                dbc.Col([dbc.Label('유형'), dbc.Select(id='he-type', options=_opts(store.ENTRY_TYPES))]),
                dbc.Col([dbc.Label('일시'), dbc.Input(id='he-when', type='datetime-local')]),
            ], className='mb-2'),
            dbc.Label('관련 업무 (여러 업무에 걸치면 모두)'), dcc.Dropdown(id='he-tasks', multi=True, className='mb-2'),
            dbc.Label('관련 임원'), dcc.Dropdown(id='he-people', multi=True, className='mb-2'),
            dbc.Label('제목'), dbc.Input(id='he-title', className='mb-2'),
            dbc.Label('요약'), dbc.Textarea(id='he-summary', rows=3, className='mb-2'),
            dbc.Label('키워드 (쉼표 구분)'), dbc.Input(id='he-keywords', className='mb-2'),
            dbc.Label('원문'), dbc.Textarea(id='he-content', rows=8, className='mb-3'),
            dbc.Label('관련 자료'),
            html.Div(id='he-refs', className='mb-2'),
            dbc.Textarea(id='he-links', rows=2, className='mb-1 small',
                         placeholder='추가할 링크 — 한 줄에 하나, 앞뒤 글자는 제목'),
            html.Div([
                dcc.Upload(dbc.Button([html.I(className='bi bi-paperclip me-1'), '파일 첨부'], color='secondary',
                                      outline=True, size='sm'), id='he-attach', multiple=True,
                           max_size=50 * 1024 * 1024),
                html.Span(id='he-attach-names', className='small text-muted ms-2'),
            ], className='d-flex align-items-center'),
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


register_form_callbacks('he', template=False)


def _modal_refs(entry_id: int):
    h = store.get_history(entry_id)
    if not h or not h['refs']:
        return html.Div('붙은 자료가 없습니다.', className='small text-muted')
    return dbc.ListGroup([
        dbc.ListGroupItem(html.Div([
            html.Div(ui.history_refs([r]), className='flex-grow-1 text-truncate'),
            dbc.Button(html.I(className='bi bi-trash'), id={'type': 'he-ref-del', 'id': r['id']}, color='link',
                       size='sm', className='text-danger p-0 ms-2', title='이 자료 삭제'),
        ], className='d-flex align-items-center'), className='py-1')
        for r in h['refs']], className='small')


@callback(Output('he-modal', 'is_open'), Output('he-id', 'data'), Output('he-type', 'value'),
          Output('he-when', 'value'), Output('he-title', 'value'), Output('he-summary', 'value'),
          Output('he-keywords', 'value'), Output('he-content', 'value'), Output('he-error', 'children'),
          Output('he-error', 'is_open'), Output('history-changed', 'data', allow_duplicate=True),
          Output('he-refs', 'children'), Output('he-links', 'value'), Output('he-attach', 'contents'),
          Output('he-attach', 'filename'), Output('he-attach-names', 'children', allow_duplicate=True),
          Output('he-people', 'value'), Output('he-people', 'options'),
          Output('he-tasks', 'value'), Output('he-tasks', 'options'),
          Input({'type': 'h-edit', 'id': ALL}, 'n_clicks'), Input('he-cancel', 'n_clicks'),
          Input('he-save', 'n_clicks'), Input('he-regen', 'n_clicks'),
          Input({'type': 'he-ref-del', 'id': ALL}, 'n_clicks'),
          State('he-id', 'data'), State('he-type', 'value'), State('he-when', 'value'), State('he-title', 'value'),
          State('he-summary', 'value'), State('he-keywords', 'value'), State('he-content', 'value'),
          State('he-links', 'value'), State('he-attach', 'contents'), State('he-attach', 'filename'),
          State('he-people', 'value'), State('he-tasks', 'value'), State('history-changed', 'data'),
          prevent_initial_call=True)
def edit_history(_e, _c, _s, _r, _d, eid, etype, when, title, summary, keywords, content, links, att, att_names,
                 people, tasks, n):
    nu = no_update
    none = (nu,) * 20
    clear_new = (None, None, '')  # 첨부 입력칸 비우기
    trig = ctx.triggered_id
    if trig is None:  # 화면 이동으로 이력 카드가 다시 그려질 때 트리거 없이 호출된다 — 아무것도 하지 않는다
        return none
    if isinstance(trig, dict) and trig['type'] == 'he-ref-del':
        if not ctx.triggered[0]['value']:
            return none
        store.delete_reference(trig['id'])
        return (nu,) * 10 + ((n or 0) + 1, _modal_refs(eid)) + (nu,) * 8
    if isinstance(trig, dict):
        if not ctx.triggered[0]['value']:  # 목록이 다시 그려질 때의 빈 트리거
            return none
        h = store.get_history(trig['id'])
        if h is None:
            return none
        return ((True, h['id'], h['entry_type'], h['occurred_at'].replace(' ', 'T'), h['title'], h['summary'],
                 ', '.join(h['keywords']), h['content'], '', False, nu, _modal_refs(h['id']), '') + clear_new
                + ([p['id'] for p in h['people']], people_options(),
                   ([h['task_id']] if h['task_id'] else []) + [t['id'] for t in h['also_tasks']], task_options()))
    if trig == 'he-cancel':
        return (False,) + (nu,) * 7 + ('', False, nu, nu, '') + clear_new + (nu,) * 4
    if trig not in ('he-save', 'he-regen') or not eid:
        return none
    try:
        h = store.get_history(eid)
        if h is None:
            raise ValueError('이력을 찾을 수 없습니다.')
        with db.transaction() as s:
            h = store.update_history(eid, entry_type=etype, occurred_at=when or None, title=title or '',
                                     summary=summary or '', keywords=keywords or '', content=content or '',
                                     person_ids=people or [], task_ids=tasks or [], session=s)
            author = auth.current_name()
            store.attach_links(h['task_id'], eid, store.parse_links(links or ''), author=author, session=s)
            for name, data in parse_uploads(att, att_names):
                store.add_reference(h['task_id'], 'file', title=name, file_name=name, file_bytes=data, author=author,
                                    history_id=eid, session=s)
    except ValueError as e:
        return (True,) + (nu,) * 7 + (str(e), True) + (nu,) * 10
    if trig == 'he-regen':
        enrich.submit(eid)
    return (False,) + (nu,) * 7 + ('', False, (n or 0) + 1, nu, '') + clear_new + (nu,) * 4


@callback(Output('history-changed', 'data', allow_duplicate=True),
          Input({'type': 'h-regen', 'id': ALL}, 'n_clicks'), Input({'type': 'h-del', 'id': ALL}, 'n_clicks'),
          Input({'type': 'h-unlink', 'id': ALL, 'task': ALL}, 'n_clicks'),
          State('history-changed', 'data'), prevent_initial_call=True)
def regen_or_delete(_r, _d, _u, n):
    trig = ctx.triggered_id
    if not isinstance(trig, dict) or not ctx.triggered[0]['value']:  # 목록이 다시 그려질 때 무시
        return no_update
    if trig['type'] == 'h-regen':
        enrich.submit(trig['id'])
    elif trig['type'] == 'h-unlink':  # 여러 업무에 걸친 기록을 이 업무에서만 뺀다
        store.unlink_history_task(trig['id'], trig['task'])
    else:
        store.delete_history(trig['id'])
    return (n or 0) + 1


def todo_review_modal():
    """AI 가 찾은 할 일을 고쳐서 업무로 등록하는 창 — 앱 레이아웃에 한 번(app.py)."""
    return dbc.Modal([
        dbc.ModalHeader(dbc.ModalTitle([html.I(className='bi bi-list-check me-2'), 'AI 가 찾은 할 일 — 고쳐서 등록'])),
        dbc.ModalBody([
            dcc.Store(id='ht-meta'),
            html.Div('칸을 눌러 업무 이름·마감·담당을 고치고, 등록하지 않을 줄은 × 로 지우세요. '
                     '마감은 YYYY-MM-DD, 담당은 쉼표로 여러 명(사용자 이름과 같으면 그 사용자로 지정).',
                     className='small text-muted mb-2'),
            dash_table.DataTable(
                id='ht-table', editable=True, row_deletable=True, data=[],
                columns=[{'name': '업무', 'id': 'title'}, {'name': '마감 (YYYY-MM-DD)', 'id': 'due'},
                         {'name': '담당', 'id': 'assignee'}],
                style_cell={'textAlign': 'left', 'fontSize': '0.85rem', 'whiteSpace': 'normal', 'height': 'auto'},
                style_cell_conditional=[{'if': {'column_id': 'title'}, 'width': '55%'}]),
            dbc.Label('상위 업무', className='small mt-3 mb-1'),
            dcc.Dropdown(id='ht-parent', placeholder='없음 (최상위 업무로)'),
            dbc.Alert(id='ht-error', color='danger', is_open=False, className='small py-2 mt-2 mb-0'),
        ]),
        dbc.ModalFooter([
            dbc.Button('취소', id='ht-cancel', color='secondary', outline=True),
            dbc.Button([html.I(className='bi bi-check2 me-1'), '등록'], id='ht-save', color='primary'),
        ]),
    ], id='ht-modal', is_open=False, size='lg')


@callback(Output('ht-modal', 'is_open'), Output('ht-table', 'data'), Output('ht-parent', 'options'),
          Output('ht-parent', 'value'), Output('ht-meta', 'data'), Output('ht-error', 'is_open'),
          Output('todo-changed', 'data', allow_duplicate=True),
          Output('history-changed', 'data', allow_duplicate=True), Output('ht-error', 'children'),
          Input({'type': 'h-todo', 'hid': ALL, 'idx': ALL, 'task': ALL}, 'n_clicks'),
          Input({'type': 'h-todo-all', 'hid': ALL, 'task': ALL}, 'n_clicks'),
          Input('ht-cancel', 'n_clicks'), Input('ht-save', 'n_clicks'),
          State('ht-table', 'data'), State('ht-parent', 'value'), State('ht-meta', 'data'),
          State('todo-changed', 'data'), State('history-changed', 'data'), prevent_initial_call=True)
def review_todos(_one, _all, _c, _s, rows, parent, meta, n, hn):
    """AI 가 찾은 할 일 → 확인 창에서 고쳐 등록. 상위 업무 기본값은 보고 있는 업무 화면의 업무, 아니면 그 기록의 업무."""
    nu = no_update
    trig = ctx.triggered_id
    if trig is None or not ctx.triggered[0]['value']:  # 목록이 다시 그려질 때의 빈 트리거
        return (nu,) * 9
    if trig == 'ht-cancel':
        return False, nu, nu, nu, nu, False, nu, nu, ''
    if isinstance(trig, dict):
        h = store.get_history(trig['hid'])
        if h is None:
            return (nu,) * 9
        idxs = [trig['idx']] if trig['type'] == 'h-todo' else \
            [i for i, t in enumerate(h['ai_todos']) if not t.get('task_id')]
        done = store.todo_titles_for_history(h['id'])
        rows = [{'idx': i, 'title': h['ai_todos'][i]['title'], 'due': h['ai_todos'][i].get('due', ''),
                 'assignee': h['ai_todos'][i].get('assignee', '')} for i in idxs
                if i < len(h['ai_todos']) and (trig['type'] == 'h-todo' or h['ai_todos'][i]['title'] not in done)]
        opts = [{'label': f"#{t['id']} {t['title']}", 'value': t['id']}
                for t in store.list_tasks(status=['예정', '진행중', '보류'])]
        default = trig.get('task') or h['task_id']
        if default and default not in {o['value'] for o in opts}:
            t = store.get_task(default)
            if t:
                opts.insert(0, {'label': f"#{t['id']} {t['title']}", 'value': t['id']})
        return True, rows, opts, default or None, {'hid': h['id']}, False, nu, nu, ''
    # 등록
    rows = [r for r in rows or [] if str(r.get('title') or '').strip()]
    if not rows:
        return True, nu, nu, nu, nu, True, nu, nu, '등록할 업무가 없습니다.'
    bad = [r['title'] for r in rows if str(r.get('due') or '').strip()
           and not re.match(r'^\d{4}-\d{2}-\d{2}$', str(r['due']).strip())]
    if bad:  # 하나라도 틀리면 아무것도 만들지 않는다
        return True, nu, nu, nu, nu, True, nu, nu, f"마감은 YYYY-MM-DD 로 적으세요: {', '.join(bad)}"
    by = auth.current_name()
    try:
        for r in rows:
            t = store.create_todo(str(r['title']).strip(), task_id=int(parent) if parent else None,
                                  due_date=str(r.get('due') or '').strip(),
                                  assignees=todo_ui.assignees_from([], str(r.get('assignee') or '')),
                                  created_by=by, source_history_id=(meta or {}).get('hid'))
            if r.get('idx') is not None and meta:
                store.mark_ai_todo(meta['hid'], int(r['idx']), t['id'])
    except ValueError as e:  # 마감 형식 등 — 앞 줄은 이미 등록됐을 수 있어 목록을 새로 그린다
        return True, nu, nu, nu, nu, True, (n or 0) + 1, (hn or 0) + 1, f"'{r['title']}': {e}"
    return False, [], nu, nu, nu, False, (n or 0) + 1, (hn or 0) + 1, ''
