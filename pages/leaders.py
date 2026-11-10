"""리더급 현황 — 보직장·R.Master(People in SAIT 에서 불러옴, services/leader_sync.py)와 지금 임원, 직접 추가한 리더.
표 칸은 services/leaders.py STATUS_COLUMNS. 칸을 고치면 저장되고 불러오기를 다시 해도 유지된다(과제구분 등).
'상세'를 누르면 임원과 같은 화면(/people/<id>)에서 기록·경력·평가·리더십진단을 본다.
엑셀 내보내기는 늘 전체 명단 — 화면의 칸이 기본, 칸·시트를 골라 더해 내려받거나 메일에 첨부한다."""
import dash
import dash_bootstrap_components as dbc
from dash import Input, Output, State, callback, ctx, dash_table, dcc, html, no_update

from components import ui
from services import auth, leader_sync, leaders

dash.register_page(__name__, path='/leaders', name='리더', title='업무 기록 · 리더')

_LINK = '상세'
_PLAN_KINDS = [('add', '새 리더'), ('set', '바뀐 정보'), ('former', '지난 리더로 (리더급에서 빠짐)'),
               ('extras', '경력·발령 · 평가 · 리더십진단')]


def layout(**_):
    return html.Div([
        ui.page_head('리더급 현황', '보직장·R.Master·임원 — 칸을 눌러 고치면 저장되고(과제구분 등), 불러오기를 다시 해도 유지됩니다'),
        dcc.Store(id='ld-refresh', data=0),
        dcc.Download(id='ld-download'),
        dcc.Store(id='lp-plan'),
        dbc.Modal([
            dbc.ModalHeader(dbc.ModalTitle('리더급 현황 엑셀 내보내기')),
            dbc.ModalBody([
                html.Div(id='lx-head', className='small mb-2'),
                html.Div('기본: 화면의 칸 (' + ' · '.join(leaders.STATUS_COLUMNS) + ')', className='small text-muted mb-3'),
                html.Div('칸 더하기', className='fw-semibold small mb-1'),
                dbc.Checklist(id='lx-cols', value=[], className='small ms-2 mb-3',
                              options=[{'label': v[0], 'value': k} for k, v in leaders.EXTRA_COLUMNS.items()]),
                html.Div('시트 더하기', className='fw-semibold small mb-1'),
                dbc.Checklist(id='lx-sheets', value=[], className='small ms-2 mb-3',
                              options=[{'label': v, 'value': k} for k, v in leaders.EXTRA_SHEETS.items()]),
                html.Div(id='lx-file', className='small'),
                html.Div('표에서 고친 칸(과제구분 등)을 고쳐 "엑셀 업로드"로 다시 올릴 수도 있습니다 '
                         '(Platform·그룹·부서명·부서원 수는 사번으로 읽는 칸이라 올리지 않음).', className='small text-muted mt-2'),
            ]),
            dbc.ModalFooter([
                dbc.Button('닫기', id='lx-close', color='secondary', outline=True, className='me-auto'),
                dbc.Button([html.I(className='bi bi-download me-1'), '내려받기'], id='lx-download', color='secondary'),
                dbc.Button([html.I(className='bi bi-envelope me-1'), '메일로 보내기 (엑셀 첨부)'], id='lx-mail',
                           color='primary'),
            ]),
        ], id='lx-modal', is_open=False),
        dbc.Modal([
            dbc.ModalHeader(dbc.ModalTitle('People in SAIT 리더 변경 제안')),
            dbc.ModalBody([
                html.Div(id='lp-head', className='mb-2'),
                html.Div([dbc.Button('전체 선택', id='lp-all', size='sm', color='secondary', outline=True, className='me-1'),
                          dbc.Button('전체 해제', id='lp-none', size='sm', color='secondary', outline=True)],
                         className='mb-2'),
                *[html.Div([html.Div(id=f'lp-title-{k}', className='fw-semibold small mb-1'),
                            dbc.Checklist(id=f'lp-{k}', options=[], value=[], className='small ms-2')],
                           id=f'lp-group-{k}', className='mb-3') for k, _ in _PLAN_KINDS],
            ]),
            dbc.ModalFooter([
                html.Span('고른 것만 반영합니다. 고르지 않은 것은 지금 값 그대로 두고, 다음 불러오기 때 다시 제안합니다.',
                          className='small text-muted me-auto'),
                dbc.Button('취소', id='lp-cancel', color='secondary', outline=True),
                dbc.Button('선택 반영', id='lp-apply', color='primary'),
            ]),
        ], id='lp-modal', is_open=False, size='lg', scrollable=True),
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
                              placeholder='성명·사원번호·조직·과제·Knox ID 검색 (칸별 거르기는 표 머리 아래 칸)'), md=4),
            dbc.Col(dbc.Checkbox(id='ld-former', value=False, label='지난 리더 포함'), md=2,
                    className='d-flex align-items-center'),
            dbc.Col(dbc.Button([html.I(className='bi bi-arrow-repeat me-1'), 'SAIT에서 불러오기'],
                               id='ld-sync', color='secondary', outline=True, className='w-100',
                               disabled=not leader_sync.is_available(),
                               title='조직 직책자(보직장)·R.Master 를 People in SAIT 와 비교해 바뀔 것을 먼저 보여 줍니다 — '
                                     '새 리더·바뀐 정보·지난 리더·경력·평가·리더십진단 중 고른 것만 반영. '
                                     '화면에서 고친 칸은 그대로 둡니다.'),
                    md=2),
            dbc.Col(dbc.ButtonGroup([
                dbc.Button([html.I(className='bi bi-file-earmark-spreadsheet me-1'), '엑셀 업로드'], href='/leaders/import',
                           color='secondary', outline=True,
                           title='리더급 현황 칸·평가·주요 이력 등을 엑셀로 한꺼번에 (임원 엑셀 업로드와 같은 방식)'),
                dbc.Button([html.I(className='bi bi-person-plus me-1'), '직접 추가'], id='ldn-open', color='primary'),
            ], className='w-100'), md=4),
        ], className='g-2 mb-2'),
        dcc.Loading(dbc.Alert(id='ld-msg', is_open=False, dismissable=True, className='py-2 small'), type='dot'),
        html.Div([
            html.Span(id='ld-count', className='small text-muted me-auto'),
            html.Span(id='ld-picked', className='small me-2'),
            dbc.Button([html.I(className='bi bi-trash me-1'), '삭제'], id='ld-delete', size='sm', color='danger',
                       outline=True, className='me-1', disabled=True,
                       title='고른 리더를 명단에서 지웁니다 (임원은 임원 화면에서). 다시 불러와도 생기지 않습니다'),
            dcc.ConfirmDialog(id='ld-delete-confirm'),
            dbc.Button([html.I(className='bi bi-file-earmark-excel me-1'), '엑셀 내보내기'], id='lx-open', size='sm',
                       color='primary', title='전체 명단 — 화면의 칸 그대로, 필요하면 항목을 더해서'),
        ], className='d-flex align-items-center flex-wrap gap-1 mb-2'),
        dash_table.DataTable(
            id='ld-table', data=[], row_selectable='multi', selected_row_ids=[], page_size=50, editable=True,
            sort_action='native', filter_action='native', markdown_options={'link_target': '_self'},
            columns=[{'id': c, 'name': c, **({'editable': False} if c in leaders.FROM_DB else {})}
                     for c in leaders.STATUS_COLUMNS]
            + [{'id': _LINK, 'name': _LINK, 'presentation': 'markdown', 'editable': False}],
            fixed_rows={'headers': True},
            style_table={'overflowX': 'auto', 'maxHeight': '70vh', 'overflowY': 'auto'},
            style_cell={'textAlign': 'left', 'fontSize': '0.8rem', 'fontFamily': 'inherit', 'whiteSpace': 'normal',
                        'height': 'auto', 'padding': '4px 6px', 'minWidth': '64px', 'maxWidth': '220px'},
            style_cell_conditional=[{'if': {'column_id': c}, 'textAlign': 'center'}
                                    for c in ('보직장', '플랫폼장/팀장', '그룹장/PL', 'PM/파트장', 'CL')],
            style_header={'fontWeight': 600, 'backgroundColor': 'var(--bs-tertiary-bg)', 'whiteSpace': 'normal'},
            style_data_conditional=[{'if': {'filter_query': '{former} = 1'}, 'color': '#9aa0a6'}]
            + [{'if': {'column_id': c}, 'backgroundColor': 'var(--bs-tertiary-bg)'} for c in leaders.FROM_DB],
            css=[{'selector': '.dash-cell-value p', 'rule': 'margin: 0'}],
        ),
        html.Div(['칸을 누르고 고친 뒤 Enter — 바로 저장됩니다. 과제구분처럼 불러올 수 없는 칸을 채우세요. ',
                  'Platform·그룹·부서명(회색)은 사번으로 People in SAIT 에서 읽는 칸이라 고치지 않습니다. ',
                  '"상세"는 기록·경력·평가·리더십진단 화면. 표 머리 아래 칸에 글자를 넣으면 그 칸으로 거릅니다 (예: 직책구분에 보직장).'],
                 className='small text-muted mt-2'),
        html.Div(id='ld-saved', className='small text-success'),
    ])


def _row(r: dict) -> dict:
    link = f"[{'지난 리더' if r['former'] else '열기'}](/people/{r['id']})"
    return {**{c: r[c] for c in leaders.STATUS_COLUMNS}, _LINK: link, 'id': r['id'], 'former': 1 if r['former'] else 0}


@callback(Output('ld-table', 'data'), Output('ld-count', 'children'),
          Input('ld-q', 'value'), Input('ld-former', 'value'), Input('ld-refresh', 'data'))
def render(q, former, _):
    rows = leaders.list_status(q or '', include_former=bool(former))
    if not rows:
        hint = ' — "People in SAIT에서 불러오기"나 "직접 추가"로 리더를 넣으세요.' if not q else ''
        return [], '리더가 없습니다' + hint
    kinds: dict[str, int] = {}
    for r in rows:
        kinds[r['직책구분'] or '-'] = kinds.get(r['직책구분'] or '-', 0) + 1
    return [_row(r) for r in rows], f"{len(rows)}명 (" + ' · '.join(f'{k} {n}' for k, n in kinds.items()) + ')'


@callback(Output('ld-saved', 'children'), Input('ld-table', 'data_timestamp'), State('ld-table', 'data'),
          State('ld-table', 'data_previous'), prevent_initial_call=True)
def save_edit(_ts, rows, prev):
    """사람이 칸을 고쳤을 때만(data_timestamp — 표를 새로 그릴 때는 바뀌지 않음) 바뀐 칸을 저장."""
    if not prev or not rows:
        return no_update
    before = {r['id']: r for r in prev}
    saved = []
    for r in rows:
        old = before.get(r['id'])
        if old is None:
            continue
        for c in leaders.STATUS_COLUMNS:
            if (r.get(c) or '') != (old.get(c) or ''):
                leaders.save_status(r['id'], c, r.get(c) or '')
                saved.append(f"{r.get('성명') or ''} {c}")
    return f"저장했습니다: {', '.join(saved)}" if saved else no_update


@callback(Output('ld-delete', 'disabled'), Input('ld-table', 'selected_row_ids'))
def can_delete(ids):
    return not ids


@callback(Output('ld-delete-confirm', 'displayed'), Output('ld-delete-confirm', 'message'),
          Input('ld-delete', 'n_clicks'), State('ld-table', 'selected_row_ids'), State('ld-table', 'data'),
          prevent_initial_call=True)
def ask_delete(n, ids, rows):
    if not n or not ids:
        return no_update, no_update
    names = [r['성명'] for r in rows or [] if r['id'] in set(ids)]
    shown = ', '.join(names[:10]) + (f' 외 {len(names) - 10}명' if len(names) > 10 else '')
    return True, (f'{len(names)}명을 리더 명단에서 지울까요?\n{shown}\n\n'
                  '기록·경력·평가도 이 사람 화면에서 함께 사라집니다(여러 사람·업무에 걸친 기록은 그대로). '
                  'People in SAIT 에서 다시 불러와도 생기지 않고, 같은 사번으로 "직접 추가"하면 다시 넣을 수 있습니다.')


@callback(Output('ld-msg', 'children', allow_duplicate=True), Output('ld-msg', 'is_open', allow_duplicate=True),
          Output('ld-msg', 'color', allow_duplicate=True), Output('ld-refresh', 'data', allow_duplicate=True),
          Output('ld-table', 'selected_row_ids'),
          Input('ld-delete-confirm', 'submit_n_clicks'), State('ld-table', 'selected_row_ids'),
          State('ld-refresh', 'data'), prevent_initial_call=True)
def delete(n, ids, r):
    if not n or not ids:
        return (no_update,) * 5
    res = leaders.delete_leaders(ids, by=auth.current_name())
    parts = []
    if res['deleted']:
        parts.append(f"{len(res['deleted'])}명을 지웠습니다: {', '.join(res['deleted'][:10])}")
    if res['execs']:
        parts.append(f"임원은 지우지 않았습니다(임원 화면에서): {', '.join(res['execs'])}")
    for name, why in res['blocked']:
        parts.append(f'{name} — {why}')
    color = 'success' if res['deleted'] and not res['blocked'] else 'warning'
    return html.Div([html.Div(x) for x in parts]), True, color, (r or 0) + 1, []


@callback(Output('ld-picked', 'children'), Input('ld-table', 'selected_row_ids'))
def picked(ids):
    n = len(ids or [])
    return f'{n}명 고름' if n else ''


@callback(Output('lx-modal', 'is_open'), Output('lx-head', 'children'), Output('lx-file', 'children'),
          Input('lx-open', 'n_clicks'), Input('lx-close', 'n_clicks'), Input('lx-mail', 'n_clicks'),
          State('ld-former', 'value'), prevent_initial_call=True)
def export_window(n_open, _c, _m, former):
    if ctx.triggered_id != 'lx-open' or not n_open:
        return False, no_update, no_update  # 닫기·메일(메일 창이 열림)
    n = len(leaders.list_status(include_former=bool(former)))
    head = f'전체 명단 {n}명 — 화면 순서(조직 편제 순)' + (' · 지난 리더 포함' if former else '')
    return True, head, ['파일 이름: ', html.Code(leaders.export_filename())]


@callback(Output('ld-download', 'data'), Input('lx-download', 'n_clicks'),
          State('lx-cols', 'value'), State('lx-sheets', 'value'), State('ld-former', 'value'), prevent_initial_call=True)
def download(n, cols, sheets, former):
    if not n:
        return no_update
    return dcc.send_bytes(leaders.export_xlsx((cols or []) + (sheets or []), include_former=bool(former)),
                          leaders.export_filename())


@callback(Output('mail-draft', 'data', allow_duplicate=True), Input('lx-mail', 'n_clicks'),
          State('lx-cols', 'value'), State('lx-sheets', 'value'), State('ld-former', 'value'), prevent_initial_call=True)
def mail(n, cols, sheets, former):
    if not n:
        return no_update
    return leaders.draft_mail((cols or []) + (sheets or []), include_former=bool(former))


_PLAN_OUT = [Output('lp-head', 'children')] + [
    o for k, _ in _PLAN_KINDS for o in (Output(f'lp-group-{k}', 'style'), Output(f'lp-title-{k}', 'children'),
                                        Output(f'lp-{k}', 'options'), Output(f'lp-{k}', 'value'))]


def _plan_view(plan: dict) -> list:
    items = plan['items']
    head = (f"리더급 {plan['total']}명 확인" + (f" (임원 {plan['execs']}명 포함 — 임원은 현황 칸만)" if plan['execs'] else '')
            + (f" · 제안 {len(items)}건" if items else ' · 바뀐 것이 없습니다.'))
    if plan['skipped_exec']:
        head += f" · 아직 임원 목록에 없는 임원 {len(plan['skipped_exec'])}명은 임원 불러오기에서"
    out = [head]
    for k, title in _PLAN_KINDS:
        opts = [{'label': i['label'], 'value': i['key']} for i in items if i['kind'] == k]
        out += [None if opts else {'display': 'none'}, f'{title} ({len(opts)})', opts, [o['value'] for o in opts]]
    return out


@callback(Output('lp-modal', 'is_open'), Output('lp-plan', 'data'), Output('lp-apply', 'disabled'), *_PLAN_OUT,
          Output('ld-msg', 'children'), Output('ld-msg', 'is_open'), Output('ld-msg', 'color'),
          Output('ld-refresh', 'data', allow_duplicate=True),
          Input('ld-sync', 'n_clicks'), Input('lp-cancel', 'n_clicks'), Input('lp-apply', 'n_clicks'),
          State('lp-plan', 'data'), *[State(f'lp-{k}', 'value') for k, _ in _PLAN_KINDS], State('ld-refresh', 'data'),
          prevent_initial_call=True)
def sync(n_sync, _c, n_apply, plan, *chosen_and_r):
    """불러오기 → 제안 창(고르기) → 선택 반영. 제안을 만들 때는 DB 를 바꾸지 않는다."""
    nu = no_update
    keep = (nu,) * len(_PLAN_OUT)
    trig = ctx.triggered_id
    *chosen, r = chosen_and_r
    if trig == 'lp-cancel':
        return (False, None, nu, *keep, nu, nu, nu, nu)
    if trig == 'ld-sync' and n_sync:
        try:
            plan = leader_sync.plan_leaders()
        except leader_sync.SyncError as e:
            return (nu, nu, nu, *keep, str(e), True, 'danger', nu)
        if not plan['items']:
            return (False, None, nu, *keep, _plan_view(plan)[0], True, 'secondary', (r or 0) + 1)
        return (True, {'n': len(plan['items'])}, False, *_plan_view(plan), nu, nu, nu, nu)
    if trig != 'lp-apply' or not n_apply or not plan:
        return (nu, nu, nu, *keep, nu, nu, nu, nu)
    keys = [k for vals in chosen for k in (vals or [])]
    try:
        res = leader_sync.apply_leaders(keys)
    except leader_sync.SyncError as e:
        return (nu, nu, nu, *keep, str(e), True, 'danger', nu)
    parts = []
    if res['added']:
        parts.append(f"새 리더 {len(res['added'])}명")
    if res['updated']:
        parts.append(f"정보 바뀜 {len(res['updated'])}명")
    if res['former']:
        parts.append(f"지난 리더로 {len(res['former'])}명")
    if 'extras' in keys:
        parts.append(f"경력 {res['career_added']}건 · 평가 {res['eval_changed']}건 · 리더십진단 {res['leadership_changed']}건")
    msg = ('반영했습니다 — ' + ' · '.join(parts)) if parts else '고른 제안이 없어 바꾸지 않았습니다.'
    return (False, None, True, *keep, msg, True, 'success' if parts else 'secondary', (r or 0) + 1)


@callback(*[Output(f'lp-{k}', 'value', allow_duplicate=True) for k, _ in _PLAN_KINDS],
          Input('lp-all', 'n_clicks'), Input('lp-none', 'n_clicks'),
          *[State(f'lp-{k}', 'options') for k, _ in _PLAN_KINDS], prevent_initial_call=True)
def select_all(_a, _n, *opts):
    on = ctx.triggered_id == 'lp-all'
    return [[o['value'] for o in (x or [])] if on else [] for x in opts]


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
