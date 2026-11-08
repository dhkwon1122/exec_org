"""리더급 목록 — 조직 직책자(팀장·그룹장 등). People in SAIT 에서 불러오고(services/leader_sync.py), 직접 추가도 한다.
이름을 누르면 임원과 같은 화면(/people/<id>)에서 기록·경력·평가·리더십진단을 본다.
고른 리더의 자료는 엑셀로 내려받거나 메일에 엑셀로 첨부해 보낸다(services/leaders.py)."""
import dash
import dash_bootstrap_components as dbc
from dash import Input, Output, State, callback, ctx, dash_table, dcc, html, no_update

from components import ui
from services import leader_sync, leaders

dash.register_page(__name__, path='/leaders', name='리더', title='업무 기록 · 리더')

_COLS = [('name', '이름', 'markdown'), ('assignment', '직책', None), ('org', '조직', None),
         ('org_path', '상위 조직', None), ('position', '직급', None), ('grade_years', '연차', None),
         ('tenure', '근속', None), ('eval', '최근 평가', None), ('leadership', '리더십(타인평균)', None),
         ('records', '기록', None), ('source_id', '사번', None)]


def layout(**_):
    return html.Div([
        ui.page_head('리더', '조직 직책자(팀장·그룹장 등) — 임원과 같은 방식으로 기록·경력·평가를 모아 봅니다'),
        dcc.Store(id='ld-refresh', data=0),
        dcc.Download(id='ld-download'),
        dbc.Modal([
            dbc.ModalHeader(dbc.ModalTitle('리더 직접 추가')),
            dbc.ModalBody([
                dbc.Row([
                    dbc.Col([dbc.Label('이름'), dbc.Input(id='ldn-name')], md=4),
                    dbc.Col([dbc.Label('직책'), dbc.Input(id='ldn-assignment', placeholder='예: 팀장')], md=4),
                    dbc.Col([dbc.Label('사번 (선택)'), dbc.Input(id='ldn-sid')], md=4),
                ], className='g-2 mb-2'),
                dbc.Label('조직'), dbc.Input(id='ldn-org', placeholder='예: ○○연구팀'),
                html.Div('사번을 넣으면 다음 불러오기 때 인사 정보 빈 칸·경력·평가·리더십진단이 채워집니다. '
                         '직접 추가한 리더는 직책자 목록에 없어도 그대로 남습니다.', className='small text-muted mt-2'),
                dbc.Alert(id='ldn-error', color='danger', is_open=False, className='mt-3 mb-0'),
            ]),
            dbc.ModalFooter([dbc.Button('취소', id='ldn-cancel', color='secondary', outline=True),
                             dbc.Button('추가', id='ldn-save', color='primary')]),
        ], id='ldn-modal', is_open=False),
        dbc.Row([
            dbc.Col(dbc.Input(id='ld-q', type='search', debounce=True,
                              placeholder='이름·직책·조직·사번·메모 검색'), md=4),
            dbc.Col(dbc.Checkbox(id='ld-former', value=False, label='지난 리더 포함'), md=2,
                    className='d-flex align-items-center'),
            dbc.Col(dbc.Button([html.I(className='bi bi-arrow-repeat me-1'), 'People in SAIT에서 불러오기'],
                               id='ld-sync', color='secondary', outline=True, className='w-100',
                               disabled=not leader_sync.is_available(),
                               title='조직 직책자(team_refer) 중 임원이 아닌 사람을 리더로 추가·갱신하고, '
                                     '경력·평가·리더십진단을 함께 옮깁니다. 직책자에서 빠진 리더는 "지난 리더"로 둡니다.'),
                    md=4),
            dbc.Col(dbc.Button([html.I(className='bi bi-person-plus me-1'), '직접 추가'], id='ldn-open',
                               color='primary', className='w-100'), md=2),
        ], className='g-2 mb-2'),
        dcc.Loading(dbc.Alert(id='ld-msg', is_open=False, dismissable=True, className='py-2 small'), type='dot'),
        html.Div([
            html.Span(id='ld-count', className='small text-muted me-auto'),
            html.Span(id='ld-picked', className='small me-2'),
            dbc.Button([html.I(className='bi bi-file-earmark-excel me-1'), '엑셀 내려받기'], id='ld-xlsx', size='sm',
                       color='secondary', outline=True, className='me-1', disabled=True),
            dbc.Button([html.I(className='bi bi-envelope me-1'), '메일로 보내기 (엑셀 첨부)'], id='ld-mail', size='sm',
                       color='primary', disabled=True),
        ], className='d-flex align-items-center flex-wrap gap-1 mb-2'),
        dash_table.DataTable(
            id='ld-table', data=[], row_selectable='multi', selected_row_ids=[], page_size=50,
            sort_action='native', markdown_options={'link_target': '_self'},
            columns=[{'id': c, 'name': n, **({'presentation': p} if p else {})} for c, n, p in _COLS],
            style_cell={'textAlign': 'left', 'fontSize': '0.85rem', 'fontFamily': 'inherit', 'whiteSpace': 'normal',
                        'height': 'auto', 'padding': '4px 8px'},
            style_header={'fontWeight': 600, 'backgroundColor': 'var(--bs-tertiary-bg)'},
            style_data_conditional=[{'if': {'filter_query': '{former} = 1'}, 'color': '#9aa0a6'}],
            css=[{'selector': '.dash-cell-value p', 'rule': 'margin: 0'}],
        ),
        html.Div('이름을 누르면 기록·경력·평가·리더십진단 화면으로 갑니다. 기록은 그 화면에서 "동향" 등으로 남깁니다.',
                 className='small text-muted mt-2'),
    ])


def _row(p: dict) -> dict:
    name = f"[{p['name']}](/people/{p['id']})" + (' · 지난 리더' if p['former'] else '') + \
        (' · 추적 중지' if not p['active'] else '')
    return {'id': p['id'], 'name': name, 'assignment': p['assignment'], 'org': p['org'], 'org_path': p['org_path'],
            'position': p['position'], 'grade_years': p['grade_years'] if p['grade_years'] is not None else '',
            'tenure': p['tenure'] if p['tenure'] is not None else '', 'eval': p['eval'],
            'leadership': p['leadership'], 'records': p['count'] or '', 'source_id': p['source_id'],
            'former': 1 if p['former'] else 0}


@callback(Output('ld-table', 'data'), Output('ld-count', 'children'),
          Input('ld-q', 'value'), Input('ld-former', 'value'), Input('ld-refresh', 'data'))
def render(q, former, _):
    rows = leaders.list_leaders(q or '', include_former=bool(former))
    if not rows:
        hint = ' — "People in SAIT에서 불러오기"나 "직접 추가"로 리더를 넣으세요.' if not q else ''
        return [], '리더가 없습니다' + hint
    return [_row(p) for p in rows], f'리더 {len(rows)}명'


@callback(Output('ld-picked', 'children'), Output('ld-xlsx', 'disabled'), Output('ld-mail', 'disabled'),
          Input('ld-table', 'selected_row_ids'))
def picked(ids):
    n = len(ids or [])
    return (f'{n}명 고름' if n else '내보낼 리더를 왼쪽 칸에서 고르세요'), not n, not n


@callback(Output('ld-download', 'data'), Input('ld-xlsx', 'n_clicks'), State('ld-table', 'selected_row_ids'),
          prevent_initial_call=True)
def download(n, ids):
    if not n or not ids:
        return no_update
    return dcc.send_bytes(leaders.export_xlsx(ids), leaders.export_filename(len(ids)))


@callback(Output('mail-draft', 'data', allow_duplicate=True), Input('ld-mail', 'n_clicks'),
          State('ld-table', 'selected_row_ids'), prevent_initial_call=True)
def mail(n, ids):
    if not n or not ids:
        return no_update
    return leaders.draft_mail(ids)


@callback(Output('ld-msg', 'children'), Output('ld-msg', 'is_open'), Output('ld-msg', 'color'),
          Output('ld-refresh', 'data', allow_duplicate=True),
          Input('ld-sync', 'n_clicks'), State('ld-refresh', 'data'), prevent_initial_call=True)
def sync(n, r):
    if not n:
        return no_update, no_update, no_update, no_update
    try:
        res = leader_sync.sync_leaders()
    except leader_sync.SyncError as e:
        return str(e), True, 'danger', no_update
    parts = [f"직책자 {res['total']}명"]
    if res['added']:
        parts.append(f"새 리더 {len(res['added'])}명")
    if res['updated']:
        parts.append(f"정보 바뀜 {len(res['updated'])}명")
    if res['former']:
        parts.append(f"지난 리더로 {len(res['former'])}명 ({', '.join(res['former'][:5])}"
                     + (' 등' if len(res['former']) > 5 else '') + ')')
    if res['skipped_exec']:
        parts.append(f"임원이라 건너뜀 {len(res['skipped_exec'])}명")
    parts.append(f"경력 {res['career_added']}건 · 평가 {res['eval_changed']}건 · 리더십진단 {res['leadership_changed']}건 반영")
    return '불러왔습니다 — ' + ' · '.join(parts), True, 'success', (r or 0) + 1


@callback(Output('ldn-modal', 'is_open'), Output('ldn-error', 'children'), Output('ldn-error', 'is_open'),
          Output('ld-refresh', 'data', allow_duplicate=True), Output('ldn-name', 'value'),
          Output('ldn-assignment', 'value'), Output('ldn-sid', 'value'), Output('ldn-org', 'value'),
          Input('ldn-open', 'n_clicks'), Input('ldn-cancel', 'n_clicks'), Input('ldn-save', 'n_clicks'),
          State('ldn-name', 'value'), State('ldn-assignment', 'value'), State('ldn-sid', 'value'),
          State('ldn-org', 'value'), State('ld-refresh', 'data'), prevent_initial_call=True)
def add(_o, _c, _s, name, assignment, sid, org, r):
    nu = no_update
    trig = ctx.triggered_id
    if trig == 'ldn-open':
        return True, '', False, nu, '', '', '', ''
    if trig == 'ldn-cancel':
        return False, '', False, nu, nu, nu, nu, nu
    try:
        leaders.add_leader(name or '', assignment or '', org or '', sid or '')
    except ValueError as e:
        return nu, str(e), True, nu, nu, nu, nu, nu
    return False, '', False, (r or 0) + 1, nu, nu, nu, nu
