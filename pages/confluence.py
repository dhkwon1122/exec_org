"""컨플루언스 워치 — 자주 보는 공간·페이지를 등록해 두고, 바뀐 페이지와 무엇이 바뀌었는지(AI 요약)를 본다.
로직은 services/confluence_watch.py. 안 본 업데이트 수는 사이드바 '컨플루언스' 옆 숫자 (app.py)."""
import dash
import dash_bootstrap_components as dbc
from dash import ALL, Input, Output, State, callback, ctx, dcc, html, no_update

from components import ui
from services import auth, confluence, confluence_watch as cw, llm, outbox

dash.register_page(__name__, path='/confluence', name='컨플루언스', title='업무 기록 · 컨플루언스 워치')


def layout(**_):
    return html.Div([
        ui.page_head('컨플루언스', '지켜보는 공간·페이지의 변경'),
        dcc.Store(id='cf-refresh', data=0),
        html.Div(id='cf-config', className='mb-2'),
        dbc.Row([
            dbc.Col(dbc.Card(dbc.CardBody([
                html.Div([html.I(className='bi bi-eye me-1'), '지켜보는 곳'], className='fw-semibold small mb-2'),
                dbc.Input(id='cf-target', size='sm', placeholder='공간 키(예: EXEC) 또는 공간·페이지 주소'),
                dbc.Checkbox(id='cf-children', value=True, label='페이지면 하위 페이지까지', className='small mt-1'),
                dbc.Button([html.I(className='bi bi-plus-lg me-1'), '추가'], id='cf-add', size='sm', color='primary',
                           className='mt-1'),
                dbc.Alert(id='cf-add-msg', is_open=False, className='small py-1 px-2 mt-2 mb-0'),
                html.Hr(),
                html.Div(id='cf-watches'),
                html.Div([
                    dbc.Button([html.I(className='bi bi-arrow-repeat me-1'), '지금 확인'], id='cf-check', size='sm',
                               color='secondary', outline=True),
                    dcc.Loading(html.Span(id='cf-check-msg', className='small text-muted ms-2'), type='dot'),
                ], className='d-flex align-items-center mt-2'),
                html.Div(id='cf-last', className='small text-muted mt-1'),
            ])), lg=4),
            dbc.Col([
                html.Div([
                    dbc.RadioItems(id='cf-filter', value='unseen', inline=True, className='small me-auto',
                                   options=[{'label': '안 본 것', 'value': 'unseen'}, {'label': '전체', 'value': 'all'}]),
                    dbc.Button([html.I(className='bi bi-stars me-1'), 'AI 로 묶어 요약'], id='cf-digest', size='sm',
                               color='secondary', outline=True, className='me-1'),
                    dbc.Button([html.I(className='bi bi-send me-1'), '메일로 보내기'], id='cf-mail', size='sm',
                               color='secondary', outline=True, className='me-1'),
                    dbc.Button([html.I(className='bi bi-check2-all me-1'), '모두 확인함'], id='cf-seen-all', size='sm',
                               color='secondary', outline=True),
                ], className='d-flex align-items-center flex-wrap gap-1 mb-2'),
                dcc.Loading(html.Div(id='cf-digest-box'), type='dot'),
                html.Div(id='cf-updates'),
            ], lg=8),
        ], className='g-3'),
    ])


def _config_note():
    if confluence.is_configured():
        return None
    return dbc.Alert('컨플루언스가 설정되지 않아 확인할 수 없습니다 — .env 의 CONFLUENCE_URL·CONFLUENCE_API_TOKEN '
                     '(tea-meeting-agenda 와 같은 값).', color='warning', className='small py-2 mb-0')


def _watch_row(w: dict):
    return html.Div([
        html.Div([
            dbc.Badge('공간' if w['kind'] == 'space' else '페이지', color='info' if w['kind'] == 'space' else 'primary',
                      className='me-1'),
            html.Span(w['title'] or w['space_key'], className='fw-semibold'),
            dbc.Badge(w['unseen'], color='danger', pill=True, className='ms-1') if w['unseen'] else None,
            html.Span(className='me-auto'),
            html.Span(dbc.Switch(id={'type': 'cf-active', 'id': w['id']}, value=w['active'], className='mb-0'),
                      title='자동 확인 켜고 끄기', className='me-1'),
            dbc.Button(html.I(className='bi bi-trash'), id={'type': 'cf-del', 'id': w['id']}, color='link', size='sm',
                       className='text-muted p-0', title='지우기 (모은 업데이트도 함께)'),
        ], className='d-flex align-items-center'),
        html.Div([w['space_key'] + (' · 하위 페이지 포함' if w['kind'] == 'page' and w['children'] else ''),
                  f" · 확인 {w['last_checked_at']}" if w['last_checked_at'] else ' · 아직 확인 전'],
                 className='small text-muted'),
        html.Div(w['last_error'], className='small text-danger') if w['last_error'] else None,
    ], className='py-2 border-bottom')


def _update_item(u: dict):
    new_page = u['version'] == 1
    stat = '새 페이지' if new_page else (f"+{u['added']} −{u['removed']}줄" if u['diff'] else '')
    return dbc.ListGroupItem([
        html.Div([
            html.A(u['title'], href=u['link'] or None, target='_blank', rel='noopener noreferrer',
                   className='fw-semibold me-2'),
            dbc.Badge('새 페이지' if new_page else f"{u['version']}판", color='success' if new_page else 'light',
                      text_color=None if new_page else 'dark', className='me-1 border'),
            html.Span(stat if not new_page else '', className='small text-muted me-auto'),
            dbc.Button('확인', id={'type': 'cf-seen', 'id': u['id']}, size='sm', color='secondary', outline=True,
                       className='py-0') if not u['seen'] else html.Span('확인함', className='small text-muted'),
        ], className='d-flex align-items-center flex-wrap'),
        html.Div(f"{u['space']} · {u['modified_by']} · {u['modified_at']}" + (f" · 메모: {u['message']}" if u['message'] else '')
                 + f" · {u['watch_title']}", className='small text-muted'),
        html.Div(u['summary'], className='small mt-1' + (' text-danger' if u['summary_status'] == 'failed' else ''),
                 style={'whiteSpace': 'pre-wrap'}) if u['summary'] else
        html.Div('AI 요약 대기 중' if u['summary_status'] == '' else '', className='small text-muted mt-1'),
        html.Details([html.Summary('바뀐 줄 보기', className='small'),
                      html.Pre(u['diff'], className='small bg-body-tertiary p-2 rounded mb-0',
                               style={'whiteSpace': 'pre-wrap', 'maxHeight': '40vh', 'overflowY': 'auto'})])
        if u['diff'] else None,
    ], className='cf-seen' if u['seen'] else '', style={'opacity': .65} if u['seen'] else None)


@callback(Output('conf-changed', 'data', allow_duplicate=True), Input('cf-refresh', 'data'),
          State('conf-changed', 'data'), prevent_initial_call=True)
def notify_sidebar(_, n):
    """확인하거나 새로 모으면 사이드바 숫자를 다시 센다."""
    return (n or 0) + 1


@callback(Output('cf-config', 'children'), Output('cf-watches', 'children'), Output('cf-updates', 'children'),
          Output('cf-last', 'children'), Input('cf-refresh', 'data'), Input('cf-filter', 'value'))
def render(_, flt):
    watches = cw.list_watches()
    wv = html.Div([_watch_row(w) for w in watches]) if watches else \
        html.Div('아직 지켜보는 곳이 없습니다. 공간 키나 페이지 주소를 넣으세요.', className='small text-muted')
    ups = cw.list_updates(unseen_only=flt == 'unseen')
    uv = dbc.ListGroup([_update_item(u) for u in ups]) if ups else \
        dbc.Alert('안 본 변경이 없습니다.' if flt == 'unseen' else '모은 변경이 없습니다.', color='light', className='small')
    lr = cw.last_run()
    every = cw.interval_minutes()
    last = (f"{every}분마다 자동 확인" if every and confluence.is_configured() else '자동 확인 꺼짐') + \
        (f" · 마지막 {lr['at']} 새 변경 {lr.get('new', 0)}건" if lr else '') + \
        (f" · 실패: {'; '.join(lr.get('errors', []))[:120]}" if lr and lr.get('errors') else '')
    return _config_note(), wv, uv, last


@callback(Output('cf-refresh', 'data', allow_duplicate=True), Output('cf-add-msg', 'children'),
          Output('cf-add-msg', 'is_open'), Output('cf-add-msg', 'color'), Output('cf-target', 'value'),
          Input('cf-add', 'n_clicks'), State('cf-target', 'value'), State('cf-children', 'value'),
          State('cf-refresh', 'data'), prevent_initial_call=True)
def add(n, target, children, r):
    if not n:
        return (no_update,) * 5
    try:
        w = cw.add_watch(target or '', bool(children), auth.current_name())
    except (ValueError, confluence.ConfluenceError) as e:
        return no_update, str(e), True, 'warning', no_update
    try:  # 바로 한 번 확인 (최근 며칠 변경)
        k = cw.check_watch(w['id'])
        msg = f"추가했습니다 — 최근 {cw.first_days()}일 변경 {k}건"
    except confluence.ConfluenceError as e:
        msg = f'추가했지만 확인은 실패: {e}'
    return (r or 0) + 1, msg, True, 'success', ''


@callback(Output('cf-refresh', 'data', allow_duplicate=True), Output('cf-check-msg', 'children'),
          Input('cf-check', 'n_clicks'), Input({'type': 'cf-del', 'id': ALL}, 'n_clicks'),
          Input({'type': 'cf-active', 'id': ALL}, 'value'), Input({'type': 'cf-seen', 'id': ALL}, 'n_clicks'),
          Input('cf-seen-all', 'n_clicks'), State('cf-filter', 'value'), State('cf-refresh', 'data'),
          prevent_initial_call=True)
def actions(_c, _d, _a, _s, _all, flt, r):
    trig = ctx.triggered_id
    if trig is None or (isinstance(trig, dict) and trig['type'] != 'cf-active' and not ctx.triggered[0]['value']):
        return no_update, no_update
    msg = no_update
    if trig == 'cf-check':
        res = cw.run_once()
        msg = f"새 변경 {res.get('new', 0)}건" + (f" · 실패 {len(res['errors'])}곳" if res.get('errors') else '')
    elif trig == 'cf-seen-all':
        ids = [u['id'] for u in cw.list_updates(unseen_only=True)] if flt == 'unseen' else None
        cw.mark_seen(ids)
    elif trig['type'] == 'cf-del':
        cw.delete_watch(trig['id'])
    elif trig['type'] == 'cf-active':
        active = ctx.triggered[0]['value']
        if active is None or active == next((w['active'] for w in cw.list_watches() if w['id'] == trig['id']), None):
            return no_update, no_update  # 다시 그려질 때 같은 값
        cw.set_active(trig['id'], active)
    else:
        cw.mark_seen([trig['id']])
    return (r or 0) + 1, msg


def _shown(flt) -> list[dict]:
    return cw.list_updates(unseen_only=flt == 'unseen', limit=50)


@callback(Output('cf-digest-box', 'children'), Input('cf-digest', 'n_clicks'), State('cf-filter', 'value'),
          prevent_initial_call=True)
def make_digest(n, flt):
    if not n:
        return no_update
    ups = _shown(flt)
    try:
        text = cw.digest([u['id'] for u in ups])
    except (ValueError, llm.LLMError) as e:
        return dbc.Alert(str(e), color='warning', className='small py-2')
    return dbc.Alert([html.Div([html.I(className='bi bi-stars me-1'), f'변경 {len(ups)}건 요약'],
                               className='fw-semibold mb-1'),
                      html.Div(text, style={'whiteSpace': 'pre-wrap'})], color='light', className='small')


@callback(Output('mail-draft', 'data', allow_duplicate=True), Input('cf-mail', 'n_clicks'), State('cf-filter', 'value'),
          State('cf-digest-box', 'children'), prevent_initial_call=True)
def mail(n, flt, _box):
    """보이는 변경(최대 50건)을 메일로 — AI 가 있으면 묶음 요약을 본문에."""
    if not n:
        return no_update
    ups = _shown(flt)
    if not ups:
        return no_update
    try:
        text = cw.digest([u['id'] for u in ups]) if llm.is_configured() else ''
    except llm.LLMError:
        text = ''
    return outbox.draft_confluence(ups, text)
