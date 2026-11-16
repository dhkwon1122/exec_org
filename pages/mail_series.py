"""정기 메일 — 주간보고처럼 주기적으로 받는 메일을 모음으로 쌓아 지난 회차와 함께 본다 (services/mail_series.py)."""
import dash
import dash_bootstrap_components as dbc
from dash import ALL, Input, Output, State, callback, ctx, dcc, html, no_update

from components import ui
from services import auth, llm, mail_series, mailbox

dash.register_page(__name__, path='/mail-series', name='정기 메일', title='업무 기록 · 정기 메일')

PAGE = 20


def layout(new=None, series=None, **_):
    seed = mailbox.get_mail(int(new)) if str(new or '').isdigit() else None
    return html.Div([
        dcc.Store(id='ms2-sel', data=int(series) if str(series or '').isdigit() else None),
        dcc.Store(id='ms2-edit'),  # 고치는 모음 id (None 이면 새 모음)
        dcc.Store(id='ms2-changed', data=0),
        dcc.Store(id='ms2-seed', data=mail_series.suggest(seed) if seed else None),
        dcc.Download(id='ms2-download'),
        ui.mail_tabs('/mail-series'),
        ui.page_head('정기 메일', '주간보고·월간 실적처럼 주기적으로 받는 메일을 모음으로 쌓아 지난 회차와 함께 봅니다'),
        dbc.Row([
            dbc.Col([
                dbc.Button([html.I(className='bi bi-plus-lg me-1'), '모음 만들기'], id='ms2-new', color='primary',
                           size='sm', className='mb-2'),
                html.Div(id='ms2-list'),
            ], lg=4),
            dbc.Col([html.Div('왼쪽에서 모음을 고르세요.', id='ms2-empty', className='small text-muted'), _detail()],
                    lg=8),
        ]),
        _form(),
    ])


def _form():
    return dbc.Modal([
        dbc.ModalHeader(dbc.ModalTitle(id='ms2-f-title')),
        dbc.ModalBody([
            dbc.Label('모음 이름'),
            dbc.Input(id='ms2-f-name', placeholder='예: 주간보고', className='mb-2'),
            dbc.Label('제목에 들어 있는 글자'),
            dbc.Input(id='ms2-f-subject', placeholder='예: [주간보고]  ·  * 는 아무 글자 (예: 주간*보고)',
                      className='mb-2'),
            dbc.Label('보낸 사람에 들어 있는 글자'),
            dbc.Input(id='ms2-f-sender', placeholder='예: report@company.com (비우면 누가 보내도)', className='mb-2'),
            dbc.Checkbox(id='ms2-f-file', value=True, className='mb-1',
                         label='새로 오는 메일은 AI 처리 없이 바로 모음에 넣고 처리 완료로 (확인 필요에 쌓이지 않음)'),
            dbc.Checkbox(id='ms2-f-replies', value=False, className='mb-1',
                         label='회신·전달 메일(RE:/FW:)도 모으기 — 제목만 맞으면 모으고, 확인 필요로 남겨 둠'),
            html.Div('두 칸을 다 적으면 둘 다 맞는 메일만 모읍니다. 회신·전달은 보내는 사람이 달라 제목 조건만 봅니다.',
                     className='small text-muted'),
            html.Div(id='ms2-f-count', className='small mt-2'),
            dbc.Alert(id='ms2-f-error', color='danger', is_open=False, className='small py-2 mt-2 mb-0'),
        ]),
        dbc.ModalFooter([
            dbc.Button('저장', id='ms2-f-save', color='primary'),
            dbc.Button('닫기', id='ms2-f-close', color='secondary', outline=True),
        ]),
    ], id='ms2-form', is_open=False)


@callback(Output('ms2-list', 'children'), Input('ms2-sel', 'data'), Input('ms2-changed', 'data'))
def render_list(sel, _):
    rows = mail_series.list_series()
    if not rows:
        return html.Div(['아직 모음이 없습니다. "모음 만들기"를 누르거나, 받은 메일 화면에서 메일을 열고 ',
                         html.B('정기 메일 모음 만들기'), '를 누르세요.'], className='small text-muted')
    return dbc.ListGroup([dbc.ListGroupItem([
        html.Div([html.Span(r['name'], className='fw-semibold me-auto text-truncate'),
                  dbc.Badge(f"{r['count']}통", color='light', text_color='dark', className='border')],
                 className='d-flex align-items-center'),
        html.Div(f"최근 {r['latest']}" if r['latest'] else '아직 모인 메일 없음', className='small text-muted'),
    ], id={'type': 'ms2-item', 'id': r['id']}, n_clicks=0, action=True, active=r['id'] == sel, className='py-2')
        for r in rows])


@callback(Output('ms2-sel', 'data'), Input({'type': 'ms2-item', 'id': ALL}, 'n_clicks'), prevent_initial_call=True)
def pick(_):
    if not isinstance(ctx.triggered_id, dict) or not ctx.triggered[0]['value']:
        return no_update
    return ctx.triggered_id['id']


def _detail():
    """오른쪽 모음 화면 — 단추·검색 칸은 처음부터 두고(콜백 입력), 모음을 고르면 보인다."""
    return html.Div(id='ms2-detail-box', style={'display': 'none'}, children=dbc.Card(dbc.CardBody([
        html.Div([
            html.H5(id='ms2-name', className='mb-0 me-auto'),
            dbc.Button([html.I(className='bi bi-graph-up me-1'), f'최근 {mail_series.TREND_LIMIT}통 흐름 요약'],
                       id='ms2-trend', size='sm', color='primary', outline=True, className='me-1'),
            dbc.Button([html.I(className='bi bi-file-earmark-excel me-1'), '엑셀'], id='ms2-export', size='sm',
                       color='secondary', outline=True, className='me-1'),
            dbc.Button([html.I(className='bi bi-pencil me-1'), '조건'], id='ms2-edit-open', size='sm',
                       color='secondary', outline=True, className='me-1'),
            dcc.ConfirmDialogProvider(
                dbc.Button(html.I(className='bi bi-trash'), size='sm', color='link', className='text-danger',
                           title='모음 지우기 (메일은 받은 메일에 남음)'),
                id='ms2-delete', message='이 모음을 지울까요? 메일은 받은 메일에 그대로 남습니다.'),
        ], className='d-flex align-items-center flex-wrap gap-1 mb-1'),
        html.Div(id='ms2-cond', className='small text-muted mb-2'),
        dcc.Loading(html.Div(id='ms2-trend-out'), type='dot'),
        dbc.Input(id='ms2-q', placeholder='이 모음에서 찾기 (제목·본문)', type='search', debounce=True, size='sm',
                  className='mb-2'),
        html.Div([
            dbc.Button([html.I(className='bi bi-box-arrow-right me-1'), '선택한 메일 모음에서 빼기'], id='ms2-exclude',
                       size='sm', color='danger', outline=True, disabled=True, className='me-2'),
            html.Span('잘못 모인 메일은 왼쪽 칸을 골라 빼세요. 메일은 받은 메일에 남고, 다시 모을 때도 넣지 않습니다.',
                      className='small text-muted me-2'),
            dbc.Button(id='ms2-include', size='sm', color='link', className='p-0', style={'display': 'none'}),
        ], className='d-flex align-items-center flex-wrap mb-2'),
        dbc.Alert(id='ms2-msg', is_open=False, duration=6000, className='small py-2'),
        html.Div(id='ms2-mails'),
        ui.pager('ms2-page'),
    ])))


@callback(Output('ms2-detail-box', 'style'), Output('ms2-empty', 'style'), Output('ms2-name', 'children'),
          Output('ms2-cond', 'children'), Output('ms2-trend', 'disabled'), Output('ms2-trend', 'title'),
          Output('ms2-trend-out', 'children', allow_duplicate=True),
          Output('ms2-include', 'children'), Output('ms2-include', 'style'),
          Input('ms2-sel', 'data'), Input('ms2-changed', 'data'), prevent_initial_call='initial_duplicate')
def render_detail(sel, _):
    series = mail_series.get(sel)
    if series is None:
        return {'display': 'none'}, {}, None, None, True, None, None, None, {'display': 'none'}
    cond = ' · '.join(x for x in (f"제목에 '{series['subject_pattern']}'" if series['subject_pattern'] else '',
                                  f"보낸 사람에 '{series['sender_pattern']}'" if series['sender_pattern'] else '') if x)
    cond += ' · 새 메일은 바로 처리 완료' if series['file_away'] else ' · 새 메일도 확인 필요로'
    cond += ' · 회신·전달도 모음 (확인 필요로 남김)' if series['include_replies'] else ''
    ok = llm.is_configured()
    n_out = len(series['excluded'])
    return {}, {'display': 'none'}, series['name'], cond, not ok, None if ok else 'LLM 이 설정돼 있어야 합니다', None, \
        f'뺀 메일 {n_out}통 다시 넣기', {} if n_out else {'display': 'none'}


@callback(Output('ms2-mails', 'children'), Output('ms2-page', 'max_value'), Output('ms2-page', 'style'),
          Input('ms2-sel', 'data'), Input('ms2-q', 'value'), Input('ms2-page', 'active_page'),
          Input('ms2-changed', 'data'))
def render_mails(sel, q, page, _):
    if not sel:
        return None, 1, {'display': 'none'}
    if ctx.triggered_id in ('ms2-q', 'ms2-sel'):
        page = 1
    page = page or 1
    rows, total = mail_series.mails(sel, q or '', limit=PAGE, offset=(page - 1) * PAGE)
    pages = max(1, -(-total // PAGE))
    if not rows:
        return html.Div('찾는 메일이 없습니다.' if q else '아직 모인 메일이 없습니다.', className='small text-muted'), \
            pages, {'display': 'none'}
    items = [html.Div([
        dbc.Checkbox(id={'type': 'ms2-pick', 'id': r['id']}, value=False, className='mt-2 me-2',
                     input_class_name='border-secondary'),
        dbc.Accordion(dbc.AccordionItem([
            html.Div([html.Span(r['sender'], className='me-2'),
                      dcc.Link('받은 메일에서 열기', href=f"/inbox?mail={r['id']}")], className='small text-muted mb-2'),
            html.Pre(r['body'], className='small mb-0', style={'whiteSpace': 'pre-wrap', 'maxHeight': '60vh',
                                                              'overflowY': 'auto'}),
        ], title=f"{r['mail_date']} · " + (f"↪ {r['reply']} · " if r['reply'] else '') + r['subject'],
            item_id=str(r['id'])), start_collapsed=True,
            className='flex-grow-1', style={'minWidth': 0}),
    ], className='d-flex align-items-start mb-1') for r in rows]
    head = html.Div(f'{total}통' + (f' · {(page - 1) * PAGE + 1}–{min(page * PAGE, total)}번째' if pages > 1 else ''),
                    className='small text-muted mb-1')
    return [head, *items], pages, ({} if pages > 1 else {'display': 'none'})


@callback(Output('ms2-exclude', 'disabled'), Output('ms2-exclude', 'children'),
          Input({'type': 'ms2-pick', 'id': ALL}, 'value'))
def pick_count(values):
    n = sum(bool(v) for v in values or [])
    label = f'선택한 {n}통 모음에서 빼기' if n else '선택한 메일 모음에서 빼기'
    return not n, [html.I(className='bi bi-box-arrow-right me-1'), label]


@callback(Output('ms2-changed', 'data', allow_duplicate=True), Output('ms2-msg', 'children'),
          Output('ms2-msg', 'is_open'), Output('ms2-msg', 'color'),
          Input('ms2-exclude', 'n_clicks'), Input('ms2-include', 'n_clicks'),
          State({'type': 'ms2-pick', 'id': ALL}, 'value'), State({'type': 'ms2-pick', 'id': ALL}, 'id'),
          State('ms2-sel', 'data'), State('ms2-changed', 'data'), prevent_initial_call=True)
def exclude(_e, _i, values, ids, sel, changed):
    if not sel or not ctx.triggered or not ctx.triggered[0]['value']:
        return no_update, no_update, no_update, no_update
    try:
        if ctx.triggered_id == 'ms2-include':
            n = mail_series.include_again(sel)
            return (changed or 0) + 1, f'뺀 메일을 다시 받아들였습니다 — 조건에 맞는 {n}통이 모음에 있습니다.', True, 'info'
        picked = [i['id'] for i, v in zip(ids or [], values or []) if v]
        n = mail_series.exclude(sel, picked)
    except ValueError as e:
        return no_update, str(e), True, 'warning'
    return ((changed or 0) + 1, [f'{n}통을 모음에서 뺐습니다. 모음이 처리 완료로 둔 메일은 ',
                                 dcc.Link('받은 메일', href='/inbox'), '의 확인 필요로 돌려 놓았습니다.'], True, 'success')


@callback(Output('ms2-trend-out', 'children'), Input('ms2-trend', 'n_clicks'), State('ms2-sel', 'data'),
          prevent_initial_call=True)
def show_trend(n, sel):
    if not n or not sel:
        return no_update
    try:
        text = mail_series.trend(sel)
    except (ValueError, llm.LLMError) as e:
        return dbc.Alert(str(e), color='warning', className='small py-2')
    return dbc.Card(dbc.CardBody(dcc.Markdown(text, className='small mb-0')), className='mb-2 border-primary')


@callback(Output('ms2-download', 'data'), Input('ms2-export', 'n_clicks'), State('ms2-sel', 'data'),
          prevent_initial_call=True)
def export(n, sel):
    series = mail_series.get(sel)
    if not n or series is None:
        return no_update
    return dcc.send_bytes(mail_series.export_xlsx(sel), f"정기메일_{series['name']}.xlsx")


@callback(Output('ms2-form', 'is_open'), Output('ms2-edit', 'data'), Output('ms2-f-title', 'children'),
          Output('ms2-f-name', 'value'), Output('ms2-f-subject', 'value'), Output('ms2-f-sender', 'value'),
          Output('ms2-f-file', 'value'), Output('ms2-f-replies', 'value'), Output('ms2-f-error', 'is_open'),
          Input('ms2-new', 'n_clicks'), Input('ms2-edit-open', 'n_clicks'), Input('ms2-f-close', 'n_clicks'),
          Input('ms2-seed', 'data'), State('ms2-sel', 'data'))
def open_form(_n, _e, _c, seed, sel):
    trig = ctx.triggered_id
    if trig == 'ms2-f-close':
        return False, no_update, no_update, no_update, no_update, no_update, no_update, no_update, False
    if trig == 'ms2-edit-open':
        s = mail_series.get(sel)
        if s is None:
            return (no_update,) * 9
        return True, s['id'], f"'{s['name']}' 조건 고치기", s['name'], s['subject_pattern'], s['sender_pattern'], \
            s['file_away'], s['include_replies'], False
    if trig == 'ms2-new' or (trig == 'ms2-seed' and seed) or (trig is None and seed):
        seed = seed if trig != 'ms2-new' else None
        seed = seed or {'name': '', 'subject_pattern': '', 'sender_pattern': '', 'file_away': True}
        return True, None, '정기 메일 모음 만들기', seed['name'], seed['subject_pattern'], seed['sender_pattern'], \
            seed['file_away'], bool(seed.get('include_replies')), False
    return (no_update,) * 9


@callback(Output('ms2-f-count', 'children'), Input('ms2-f-subject', 'value'), Input('ms2-f-sender', 'value'),
          Input('ms2-f-replies', 'value'))
def count_preview(subject, sender, replies):
    if not (subject or '').strip('* ') and not (sender or '').strip('* '):
        return None
    n = mail_series.preview_count({'subject_pattern': subject, 'sender_pattern': sender, 'include_replies': replies})
    return html.Span([html.I(className='bi bi-collection me-1'), f'지금까지 받은 메일 중 {n}통이 모입니다.'],
                     className='text-primary')


@callback(Output('ms2-form', 'is_open', allow_duplicate=True), Output('ms2-f-error', 'children'),
          Output('ms2-f-error', 'is_open', allow_duplicate=True), Output('ms2-sel', 'data', allow_duplicate=True),
          Output('ms2-changed', 'data'),
          Input('ms2-f-save', 'n_clicks'), State('ms2-edit', 'data'), State('ms2-f-name', 'value'),
          State('ms2-f-subject', 'value'), State('ms2-f-sender', 'value'), State('ms2-f-file', 'value'),
          State('ms2-f-replies', 'value'), State('ms2-changed', 'data'), prevent_initial_call=True)
def save(n, sid, name, subject, sender, file_away, replies, changed):
    if not n:
        return (no_update,) * 5
    try:
        s = mail_series.save({'name': name, 'subject_pattern': subject, 'sender_pattern': sender,
                              'file_away': file_away, 'include_replies': replies}, sid, auth.current_name())
    except ValueError as e:
        return no_update, str(e), True, no_update, no_update
    return False, '', False, s['id'], (changed or 0) + 1


@callback(Output('ms2-sel', 'data', allow_duplicate=True), Output('ms2-changed', 'data', allow_duplicate=True),
          Input('ms2-delete', 'submit_n_clicks'), State('ms2-sel', 'data'), State('ms2-changed', 'data'),
          prevent_initial_call=True)
def remove(n, sel, changed):
    if not n or not sel:
        return no_update, no_update
    mail_series.delete(sel)
    return None, (changed or 0) + 1
