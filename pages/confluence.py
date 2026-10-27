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
        dcc.Interval(id='cf-poll', interval=4000, disabled=True),  # 백그라운드 확인이 도는 동안 다시 그리기
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
                dbc.Tabs(id='cf-tabs', active_tab='all', className='mb-2',
                         children=[dbc.Tab(label='전체', tab_id='all')]),  # 지켜보는 곳마다 탭 (render 가 채움)
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


def _older_row(u: dict):
    return html.Li([
        dbc.Badge(f"{u['version']}판", color='light', text_color='dark', className='me-1 border'),
        html.Span(f"{u['modified_by']} · {u['modified_at']}", className='text-muted me-1'),
        html.Span(u['summary'] or (f"+{u['added']} −{u['removed']}줄" if u['diff'] else '')),
    ], className='mb-1')


def _group_item(group: list[dict], show_watch: bool):
    """한 페이지의 변경 묶음 — 최근 판을 크게, 그 앞 변경은 접어서. '확인'은 이 페이지의 안 본 변경 전부."""
    u, older = group[0], group[1:]
    unseen = sum(1 for x in group if not x['seen'])
    item = _update_item(u, show_watch, unseen, len(group))
    if older:
        item.children.append(html.Details([
            html.Summary(f'이 페이지의 앞선 변경 {len(older)}건', className='small'),
            html.Ul([_older_row(x) for x in older], className='list-unstyled small mt-1 mb-0 ps-2'),
        ], className='mt-1'))
    return item


def _update_item(u: dict, show_watch: bool = True, unseen: int = 0, n: int = 1):
    new_page = u['version'] == 1
    stat = '새 페이지' if new_page else (f"+{u['added']} −{u['removed']}줄" if u['diff'] else '')
    return dbc.ListGroupItem([
        html.Div([
            html.A(u['title'], href=u['link'] or None, target='_blank', rel='noopener noreferrer',
                   className='fw-semibold me-2'),
            dbc.Badge('새 페이지' if new_page else f"{u['version']}판", color='success' if new_page else 'light',
                      text_color=None if new_page else 'dark', className='me-1 border'),
            html.Span(stat if not new_page else '', className='small text-muted me-1'),
            dbc.Badge(f'{n}번 수정', color='warning', text_color='dark', className='me-1') if n > 1 else None,
            html.Span(className='me-auto'),
            dbc.Button('확인' + (f' ({unseen})' if unseen > 1 else ''),
                       id={'type': 'cf-seen', 'id': f"{u['watch_id']}:{u['page_id']}"}, size='sm',
                       color='secondary', outline=True, className='py-0') if unseen else
            html.Span('확인함', className='small text-muted'),
        ], className='d-flex align-items-center flex-wrap'),
        html.Div(f"{u['space']} · {u['modified_by']} · {u['modified_at']}" + (f" · 메모: {u['message']}" if u['message'] else '')
                 + (f" · {u['watch_title']}" if show_watch else ''), className='small text-muted'),
        html.Div(u['summary'], className='small mt-1' + (' text-danger' if u['summary_status'] == 'failed' else ''),
                 style={'whiteSpace': 'pre-wrap'}) if u['summary'] else
        html.Div('AI 요약 대기 중' if u['summary_status'] == '' else '', className='small text-muted mt-1'),
        html.Details([html.Summary('바뀐 줄 보기', className='small'),
                      html.Pre(u['diff'], className='small bg-body-tertiary p-2 rounded mb-0',
                               style={'whiteSpace': 'pre-wrap', 'maxHeight': '40vh', 'overflowY': 'auto'})])
        if u['diff'] else None,
    ], className='cf-seen' if not unseen else '', style={'opacity': .65} if not unseen else None)


@callback(Output('conf-changed', 'data', allow_duplicate=True), Input('cf-refresh', 'data'),
          State('conf-changed', 'data'), prevent_initial_call=True)
def notify_sidebar(_, n):
    """확인하거나 새로 모으면 사이드바 숫자를 다시 센다."""
    return (n or 0) + 1


def _tab_watch(tab) -> int | None:
    return int(tab[1:]) if isinstance(tab, str) and tab[1:].isdigit() else None


def _updates_view(ups: list[dict], watch_id: int | None, watches: list[dict]):
    """페이지마다 묶어서 — '전체' 탭은 지켜보는 곳마다 제목을 달아 나눈다."""
    groups = cw.group_by_page(ups)
    if watch_id is not None:
        return dbc.ListGroup([_group_item(g, False) for g in groups])
    out = []
    for w in watches:
        mine = [g for g in groups if g[0]['watch_id'] == w['id']]
        if mine:
            out.append(html.H6([w['title'] or w['space_key'],
                                html.Span(f' · 페이지 {len(mine)}개', className='small text-muted')],
                               className='mt-3 mb-1'))
            out.append(dbc.ListGroup([_group_item(g, False) for g in mine]))
    return html.Div(out)


@callback(Output('cf-config', 'children'), Output('cf-watches', 'children'), Output('cf-updates', 'children'),
          Output('cf-last', 'children'), Output('cf-poll', 'disabled'), Output('cf-tabs', 'children'),
          Input('cf-refresh', 'data'), Input('cf-filter', 'value'), Input('cf-poll', 'n_intervals'),
          Input('cf-tabs', 'active_tab'))
def render(_, flt, _poll, tab):
    watches = cw.list_watches()
    wv = html.Div([_watch_row(w) for w in watches]) if watches else \
        html.Div('아직 지켜보는 곳이 없습니다. 공간 키나 페이지 주소를 넣으세요.', className='small text-muted')
    total = sum(w['unseen'] for w in watches)
    tabs = [dbc.Tab(label=f'전체 ({total})' if total else '전체', tab_id='all')] + [
        dbc.Tab(label=(w['title'] or w['space_key'])[:20] + (f" ({w['unseen']})" if w['unseen'] else ''),
                tab_id=f"w{w['id']}") for w in watches]
    watch_id = _tab_watch(tab)
    ups = cw.list_updates(watch_id=watch_id, unseen_only=flt == 'unseen')
    uv = _updates_view(ups, watch_id, watches) if ups else \
        dbc.Alert('안 본 변경이 없습니다.' if flt == 'unseen' else '모은 변경이 없습니다.', color='light', className='small')
    lr = cw.last_run()
    every = cw.interval_minutes()
    last = (f"{every}분마다 자동 확인" if every and confluence.is_configured() else '자동 확인 꺼짐') + \
        (f" · 마지막 {lr['at']} 새 변경 {lr.get('new', 0)}건" if lr else '') + \
        (f" · 실패: {'; '.join(lr.get('errors', []))[:120]}" if lr and lr.get('errors') else '')
    running = cw.is_running()
    if running:
        last = [dbc.Spinner(size='sm', spinner_class_name='me-1'),
                f"확인 중 — 호출 한도(분당 {confluence.rate_per_minute()}회)를 지키느라 몇 분 걸릴 수 있습니다. ", last]
    return _config_note(), wv, uv, last, not running, tabs


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
    started = cw.run_later(w['id'])  # 최근 며칠 변경을 백그라운드로 모은다 (호출 한도 때문에 화면이 기다리지 않게)
    msg = (f"추가했습니다 — 최근 {cw.first_days()}일 변경을 모으는 중입니다." if started else
           '추가했습니다 — 지금 도는 확인이 끝난 뒤 다음 확인 때 함께 모읍니다.')
    return (r or 0) + 1, msg, True, 'success', ''


@callback(Output('cf-refresh', 'data', allow_duplicate=True), Output('cf-check-msg', 'children'),
          Input('cf-check', 'n_clicks'), Input({'type': 'cf-del', 'id': ALL}, 'n_clicks'),
          Input({'type': 'cf-active', 'id': ALL}, 'value'), Input({'type': 'cf-seen', 'id': ALL}, 'n_clicks'),
          Input('cf-seen-all', 'n_clicks'), State('cf-filter', 'value'), State('cf-tabs', 'active_tab'),
          State('cf-refresh', 'data'), prevent_initial_call=True)
def actions(_c, _d, _a, _s, _all, flt, tab, r):
    trig = ctx.triggered_id
    if trig is None or (isinstance(trig, dict) and trig['type'] != 'cf-active' and not ctx.triggered[0]['value']):
        return no_update, no_update
    msg = no_update
    if trig == 'cf-check':
        msg = '확인을 시작했습니다.' if cw.run_later() else '이미 확인하는 중입니다.'
    elif trig == 'cf-seen-all':
        cw.mark_seen([u['id'] for u in cw.list_updates(watch_id=_tab_watch(tab), unseen_only=True, limit=10000)])
    elif trig['type'] == 'cf-del':
        cw.delete_watch(trig['id'])
    elif trig['type'] == 'cf-active':
        active = ctx.triggered[0]['value']
        if active is None or active == next((w['active'] for w in cw.list_watches() if w['id'] == trig['id']), None):
            return no_update, no_update  # 다시 그려질 때 같은 값
        cw.set_active(trig['id'], active)
    else:  # 'cf-seen' — 그 페이지의 안 본 변경 전부
        watch_id, page_id = str(trig['id']).split(':', 1)
        cw.mark_seen_page(int(watch_id), page_id)
    return (r or 0) + 1, msg


def _shown(flt, tab) -> list[dict]:
    """지금 탭·보기에 보이는 변경 (최대 50건) — 묶어 요약·메일 대상."""
    return cw.list_updates(watch_id=_tab_watch(tab), unseen_only=flt == 'unseen', limit=50)


@callback(Output('cf-digest-box', 'children'), Input('cf-digest', 'n_clicks'), State('cf-filter', 'value'),
          State('cf-tabs', 'active_tab'), prevent_initial_call=True)
def make_digest(n, flt, tab):
    if not n:
        return no_update
    ups = _shown(flt, tab)
    try:
        text = cw.digest([u['id'] for u in ups])
    except (ValueError, llm.LLMError) as e:
        return dbc.Alert(str(e), color='warning', className='small py-2')
    return dbc.Alert([html.Div([html.I(className='bi bi-stars me-1'), f'변경 {len(ups)}건 요약'],
                               className='fw-semibold mb-1'),
                      html.Div(text, style={'whiteSpace': 'pre-wrap'})], color='light', className='small')


@callback(Output('mail-draft', 'data', allow_duplicate=True), Input('cf-mail', 'n_clicks'), State('cf-filter', 'value'),
          State('cf-tabs', 'active_tab'), prevent_initial_call=True)
def mail(n, flt, tab):
    """보이는 변경(최대 50건)을 메일로 — AI 가 있으면 묶음 요약을 본문에."""
    if not n:
        return no_update
    ups = _shown(flt, tab)
    if not ups:
        return no_update
    try:
        text = cw.digest([u['id'] for u in ups]) if llm.is_configured() else ''
    except llm.LLMError:
        text = ''
    return outbox.draft_confluence(ups, text)
