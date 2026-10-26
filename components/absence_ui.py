"""임원 부재 — 캘린더 화면의 '임원 부재' 탭 (services/absences.py).

월 보기: 날짜 칸마다 자리를 비운 임원(종류별 색). 날짜를 누르면 오른쪽에 그날 부재 상세.
목록 보기: 그달 부재를 기간 순으로. '부재 등록'에서 여러 임원을 고르면 사람마다 한 건씩 만든다(함께 가는 출장).
'메일로 등록': 부재 안내 메일을 붙여넣으면 임원·종류·기간을 뽑아 표로 보여 주고, 확인한 뒤 등록한다
(POP3 로 받은 메일은 받은 메일 화면에서 같은 방식으로).
"""
from datetime import date

import dash_bootstrap_components as dbc
from dash import ALL, Input, Output, State, callback, ctx, dash_table, dcc, html, no_update

from services import absences, auth, outbox, schedules, store

WEEKDAYS = ['월', '화', '수', '목', '금', '토', '일']
KIND_COLOR = {'휴가': '#16a34a', '출장': '#2563eb', '해외출장': '#7c3aed', '교육': '#d97706', '외부 일정': '#db2777',
              '병가': '#dc2626', '기타': '#6b7280'}
MAX_IN_CELL = 4


def _people_options() -> list[dict]:
    return [{'label': f"{p['name']} {p['position']}".strip(), 'value': p['id']}
            for p in store.list_people(order='rank')]


def layout():
    today = date.today()
    form = [
        dbc.Label('임원'),
        dcc.Dropdown(id='ab-f-people', multi=True, placeholder='임원 (함께 가는 출장이면 여러 명)', className='mb-1'),
        html.Div(id='ab-f-warn', className='small text-warning-emphasis mb-2'),
        dbc.Row([
            dbc.Col([dbc.Label('종류'), dbc.Select(id='ab-f-kind', value='휴가',
                                                  options=[{'label': k, 'value': k} for k in absences.KINDS])], md=4),
            dbc.Col([dbc.Label('시작일'), dbc.Input(id='ab-f-start', type='date')], md=4),
            dbc.Col([dbc.Label('종료일'), dbc.Input(id='ab-f-end', type='date')], md=4),
        ], className='g-2 mb-2'),
        dbc.Row([
            dbc.Col([dbc.Label('부재 시간대 (선택)'),
                     dbc.Input(id='ab-f-time', placeholder='예: 09:00~12:00, 오후 — 비우면 종일')], md=5),
            dbc.Col([dbc.Label('행선지'), dbc.Input(id='ab-f-place', placeholder='출장지·교육 기관·방문처 등')], md=7),
        ], className='g-2 mb-2'),
        dbc.Label('일정 내용'), dbc.Textarea(id='ab-f-content', rows=2, placeholder='예: 미국 반도체 학회 참석',
                                          className='mb-2'),
        dbc.Label('메모'), dbc.Textarea(id='ab-f-note', rows=2, placeholder='연락처 등'),
        html.Div(id='ab-f-meta', className='small text-muted mt-2'),
        dbc.Alert(id='ab-f-error', color='danger', is_open=False, className='mt-2 mb-0 py-2'),
    ]
    return html.Div([
        dcc.Store(id='ab-month', data=[today.year, today.month]),
        dcc.Store(id='ab-day', data=today.isoformat()),
        dcc.Store(id='ab-refresh', data=0),
        dcc.Store(id='ab-edit-id'),
        html.Div([
            dbc.Button(html.I(className='bi bi-chevron-left'), id='ab-prev', color='secondary', outline=True, size='sm'),
            html.H5(id='ab-title', className='mb-0 mx-3'),
            dbc.Button(html.I(className='bi bi-chevron-right'), id='ab-next', color='secondary', outline=True,
                       size='sm'),
            dbc.Button('오늘', id='ab-today', color='secondary', outline=True, size='sm', className='ms-2 me-3'),
            dbc.RadioItems(id='ab-view', value='month', inline=True, className='d-flex', inputClassName='btn-check',
                           labelClassName='btn btn-sm btn-outline-secondary', labelCheckedClassName='active',
                           options=[{'label': '월', 'value': 'month'}, {'label': '목록', 'value': 'list'}]),
            dcc.Dropdown(id='ab-kinds', multi=True, placeholder='종류 (전체)', options=absences.KINDS,
                         className='ms-auto', style={'minWidth': '200px'}),
            dbc.Input(id='ab-q', type='search', debounce=True, placeholder='이름·행선지·일정 내용 검색', size='sm',
                      style={'maxWidth': '200px'}),
            dbc.Button([html.I(className='bi bi-send me-1'), '부재 현황 메일'], id='ab-mail', color='secondary',
                       outline=True, size='sm', title='고른 날짜가 있는 주(월~일)의 부재 현황을 표로'),
            dbc.Button([html.I(className='bi bi-envelope-paper me-1'), '메일로 등록'], id='ab-imp-open',
                       color='secondary', outline=True, size='sm'),
            dbc.Button([html.I(className='bi bi-plus-lg me-1'), '부재 등록'], id='ab-add', color='primary', size='sm'),
        ], className='d-flex align-items-center flex-wrap gap-2 mb-2'),
        html.Div(id='ab-now', className='mb-2'),
        dbc.Row([
            dbc.Col(html.Div(id='ab-body'), lg=8),
            dbc.Col(html.Div(id='ab-detail'), lg=4),
        ]),
        dbc.Modal([
            dbc.ModalHeader(dbc.ModalTitle(id='ab-modal-title')),
            dbc.ModalBody(form),
            dbc.ModalFooter([
                dbc.Button('삭제', id='ab-del', color='danger', outline=True, className='me-auto'),
                dbc.Button('취소', id='ab-cancel', color='secondary', outline=True),
                dbc.Button('저장', id='ab-save', color='primary'),
            ]),
        ], id='ab-modal', is_open=False, size='lg'),
        dbc.Modal([
            dbc.ModalHeader(dbc.ModalTitle('메일로 임원 부재 등록')),
            dbc.ModalBody([
                html.Div('휴가·출장·교육 안내나 부재 현황 메일을 그대로 붙여넣으세요. 임원 이름·별칭으로 등록된 임원을 찾고, '
                         '종류·기간·시간대·행선지·일정 내용을 뽑습니다.', className='small text-muted mb-2'),
                dbc.Textarea(id='ab-imp-text', rows=7, placeholder='메일 내용 붙여넣기 (제목 줄도 같이 붙이면 좋습니다)'),
                html.Div([
                    dbc.Input(id='ab-imp-date', type='date', size='sm', style={'maxWidth': '170px'}),
                    html.Span('메일 날짜 (연도·요일 표현을 풀 때 씀, 비우면 오늘)', className='small text-muted ms-2 me-auto'),
                    dbc.Button([html.I(className='bi bi-magic me-1'), '읽기'], id='ab-imp-read', color='secondary',
                               size='sm'),
                ], className='d-flex align-items-center my-2'),
                html.Div(id='ab-imp-note', className='small text-muted mb-1'),
                dash_table.DataTable(
                    id='ab-imp-table', editable=True, row_deletable=True, data=[],
                    columns=[{'name': '상태', 'id': 'state_label', 'editable': False}, {'name': '임원', 'id': 'person'},
                             {'name': '종류', 'id': 'kind'}, {'name': '시작', 'id': 'start_date'},
                             {'name': '종료', 'id': 'end_date'}, {'name': '시간대', 'id': 'time_range'},
                             {'name': '행선지', 'id': 'place'}, {'name': '일정 내용', 'id': 'content'},
                             {'name': '메모', 'id': 'note'}],
                    style_table={'overflowX': 'auto'},
                    style_cell={'textAlign': 'left', 'fontSize': '0.8rem', 'whiteSpace': 'normal', 'height': 'auto'},
                    style_data_conditional=[
                        {'if': {'filter_query': '{state} = "unknown"'}, 'backgroundColor': 'rgba(220,53,69,.08)'},
                        {'if': {'filter_query': '{state} = "overlap"'}, 'backgroundColor': 'rgba(255,193,7,.12)'},
                        {'if': {'filter_query': '{state} = "same"'}, 'color': '#888'}]),
                dbc.Alert(id='ab-imp-msg', is_open=False, className='small py-2 mt-2 mb-0'),
            ]),
            dbc.ModalFooter([
                dbc.Button('닫기', id='ab-imp-close', color='secondary', outline=True),
                dbc.Button([html.I(className='bi bi-check2 me-1'), '등록'], id='ab-imp-apply', color='primary'),
            ]),
        ], id='ab-imp-modal', is_open=False, size='xl', scrollable=True),
    ])


# ── 보기 ──────────────────────────────────────────────────────────────

def _period(r: dict) -> str:
    s, e = r['start_date'], r['end_date']
    text = f"{s[5:].replace('-', '/')}" + ('' if s == e else f" ~ {e[5:].replace('-', '/')}")
    return text + (f" {r['time_range']}" if r['time_range'] else '') + f" ({absences.days(r)}일)"


def _kind_badge(kind: str):
    return html.Span(kind, className='badge', style={'background': KIND_COLOR.get(kind, '#6b7280'), 'color': '#fff'})


def _chip(r: dict):
    return html.Div([html.Span(r['person_name'], className='fw-semibold me-1'), r['kind']],
                    className='ab-chip text-truncate',
                    style={'borderLeft': f"3px solid {KIND_COLOR.get(r['kind'], '#6b7280')}"},
                    title=f"{r['person_name']} {r['position']} · {r['kind']} · {_period(r)}"
                          + (f" · {r['place']}" if r['place'] else ''))


def _month_grid(y: int, m: int, rows: list[dict], selected: str):
    today = date.today().isoformat()
    weeks = []
    for week in schedules.month_weeks(y, m):
        cells = []
        for d in week:
            iso = d.isoformat()
            day_rows = absences.on_day(iso, rows)
            cls = 'ab-cell' + (' other' if d.month != m else '') + (' today' if iso == today else '') + \
                (' selected' if iso == selected else '') + (' weekend' if d.weekday() >= 5 else '')
            cells.append(html.Div([
                html.Div([html.Span(d.day), html.Span(f'{len(day_rows)}명', className='ab-count') if day_rows else None],
                         className='ab-daynum'),
                *[_chip(r) for r in day_rows[:MAX_IN_CELL]],
                html.Div(f'외 {len(day_rows) - MAX_IN_CELL}명', className='small text-muted')
                if len(day_rows) > MAX_IN_CELL else None,
            ], id={'type': 'ab-cell', 'day': iso}, n_clicks=0, className=cls))
        weeks.append(html.Div(cells, className='ab-week'))
    head = html.Div([html.Div(w, className='ab-wd') for w in schedules.WEEK_HEAD], className='ab-week ab-head')
    legend = html.Div([html.Span([html.Span(className='ab-dot', style={'background': c}), k], className='me-3')
                       for k, c in KIND_COLOR.items()], className='small text-muted mt-2')
    return html.Div([head, *weeks, legend], className='ab-grid')


def _row_item(r: dict, show_dates: bool = True):
    return dbc.ListGroupItem(html.Div([
        html.Div([
            html.Div([
                _kind_badge(r['kind']),
                dcc.Link(f"{r['person_name']} {r['position']}".strip(), href=f"/people/{r['person_id']}",
                         className='fw-semibold ms-2 me-2'),
                html.Span(_period(r), className='small text-muted') if show_dates else
                html.Span(r['time_range'], className='small text-muted'),
            ]),
            html.Div([html.I(className='bi bi-geo-alt me-1'), r['place']], className='small text-muted')
            if r['place'] else None,
            html.Div(r['content'], className='small', style={'whiteSpace': 'pre-wrap'}) if r['content'] else None,
            html.Div(r['note'], className='small text-muted', style={'whiteSpace': 'pre-wrap'}) if r['note'] else None,
        ], className='flex-grow-1'),
        dbc.Button(html.I(className='bi bi-pencil'), id={'type': 'ab-edit', 'id': r['id']}, color='link', size='sm',
                   className='text-muted p-0', title='고치기'),
    ], className='d-flex align-items-start gap-2'), className='py-2')


def _list_view(rows: list[dict]):
    if not rows:
        return html.Div('이 달에 등록된 부재가 없습니다.', className='small text-muted')
    return dbc.ListGroup([_row_item(r) for r in rows])


def _day_detail(day: str, rows: list[dict]):
    d = date.fromisoformat(day)
    day_rows = absences.on_day(day, rows)
    return dbc.Card([
        dbc.CardHeader(f"{d.month}월 {d.day}일 ({WEEKDAYS[d.weekday()]}) 부재 {len(day_rows)}명"),
        dbc.ListGroup([_row_item(r) for r in day_rows], flush=True) if day_rows else
        dbc.CardBody('부재 없음 — 모두 자리에 있습니다.', className='small text-muted'),
    ])


def _now_strip(rows_today: list[dict]):
    if not rows_today:
        return html.Div([html.I(className='bi bi-check-circle me-1 text-success'), '오늘 부재 중인 임원이 없습니다.'],
                        className='small text-muted')
    return html.Div([html.Span('오늘 부재 ', className='small fw-semibold me-1')] + [
        html.Span([html.Span(className='ab-dot', style={'background': KIND_COLOR.get(r['kind'], '#6b7280')}),
                   f"{r['person_name']} {r['kind']}" + (f"({r['time_range']})" if r['time_range'] else '')
                   + (f" · {r['place']}" if r['place'] else '')], className='small me-3')
        for r in rows_today], className='d-flex flex-wrap align-items-center')


@callback(Output('ab-month', 'data'), Output('ab-day', 'data', allow_duplicate=True),
          Input('ab-prev', 'n_clicks'), Input('ab-next', 'n_clicks'), Input('ab-today', 'n_clicks'),
          State('ab-month', 'data'), prevent_initial_call=True)
def move_month(_p, _n, _t, ym):
    y, m = ym
    if ctx.triggered_id == 'ab-today':
        t = date.today()
        return [t.year, t.month], t.isoformat()
    m += -1 if ctx.triggered_id == 'ab-prev' else 1
    if m == 0:
        y, m = y - 1, 12
    elif m == 13:
        y, m = y + 1, 1
    return [y, m], date(y, m, 1).isoformat()


@callback(Output('ab-day', 'data', allow_duplicate=True), Input({'type': 'ab-cell', 'day': ALL}, 'n_clicks'),
          prevent_initial_call=True)
def pick_day(_):
    if not isinstance(ctx.triggered_id, dict) or not ctx.triggered[0]['value']:
        return no_update
    return ctx.triggered_id['day']


@callback(Output('ab-title', 'children'), Output('ab-body', 'children'), Output('ab-detail', 'children'),
          Output('ab-now', 'children'),
          Input('ab-month', 'data'), Input('ab-view', 'value'), Input('ab-kinds', 'value'), Input('ab-q', 'value'),
          Input('ab-day', 'data'), Input('ab-refresh', 'data'))
def render(ym, view, kinds, q, day, _):
    y, m = ym
    first = date(y, m, 1)
    weeks = schedules.month_weeks(y, m)
    lo, hi = weeks[0][0].isoformat(), weeks[-1][-1].isoformat()
    rows = absences.list_range(lo, hi, kinds=kinds or None, query=q or '')
    month_rows = [r for r in rows if r['start_date'] <= f'{y:04d}-{m:02d}-31' and r['end_date'] >= first.isoformat()]
    body = _month_grid(y, m, rows, day) if view == 'month' else _list_view(month_rows)
    if day < lo or day > hi:
        day = first.isoformat()
    today = date.today().isoformat()
    return (f'{y}년 {m}월', body, _day_detail(day, rows),
            _now_strip(absences.on_day(today, absences.list_range(today, today))))


# ── 등록·수정 ─────────────────────────────────────────────────────────

@callback(Output('ab-modal', 'is_open'), Output('ab-modal-title', 'children'), Output('ab-edit-id', 'data'),
          Output('ab-f-people', 'options'), Output('ab-f-people', 'value'), Output('ab-f-people', 'disabled'),
          Output('ab-f-kind', 'value'), Output('ab-f-start', 'value'), Output('ab-f-end', 'value'),
          Output('ab-f-time', 'value'), Output('ab-f-place', 'value'), Output('ab-f-content', 'value'),
          Output('ab-f-note', 'value'), Output('ab-f-meta', 'children'), Output('ab-f-error', 'children'),
          Output('ab-f-error', 'is_open'), Output('ab-del', 'style'), Output('ab-refresh', 'data'),
          Input('ab-add', 'n_clicks'), Input({'type': 'ab-edit', 'id': ALL}, 'n_clicks'),
          Input('ab-save', 'n_clicks'), Input('ab-cancel', 'n_clicks'), Input('ab-del', 'n_clicks'),
          State('ab-edit-id', 'data'), State('ab-f-people', 'value'), State('ab-f-kind', 'value'),
          State('ab-f-start', 'value'), State('ab-f-end', 'value'), State('ab-f-time', 'value'),
          State('ab-f-place', 'value'), State('ab-f-content', 'value'), State('ab-f-note', 'value'),
          State('ab-day', 'data'), State('ab-refresh', 'data'), prevent_initial_call=True)
def edit(_a, _e, _s, _c, _d, aid, people, kind, start, end, time_range, place, content, note, day, n):
    nu = no_update
    trig = ctx.triggered_id
    if trig is None or not ctx.triggered[0]['value']:  # 목록이 다시 그려질 때의 빈 트리거
        return (nu,) * 18
    if trig == 'ab-add':
        return (True, '부재 등록', None, _people_options(), [], False, '휴가', day, day, '', '', '', '', '', '', False,
                {'display': 'none'}, nu)
    if isinstance(trig, dict):
        r = absences.get(trig['id'])
        if r is None:
            return (nu,) * 18
        meta = f"등록 {r['created_by']} · 최종 수정 {r['updated_at']}" if r['created_by'] else f"최종 수정 {r['updated_at']}"
        return (True, f"부재 고치기 — {r['person_name']}", r['id'], _people_options(), [r['person_id']], True,
                r['kind'], r['start_date'], r['end_date'], r['time_range'], r['place'], r['content'], r['note'], meta, '',
                False, {}, nu)
    if trig == 'ab-cancel':
        return (False,) + (nu,) * 13 + ('', False, nu, nu)
    if trig == 'ab-del':
        if aid:
            absences.delete(aid)
        return (False,) + (nu,) * 13 + ('', False, nu, (n or 0) + 1)
    try:
        absences.save({'kind': kind, 'start_date': start, 'end_date': end, 'time_range': time_range, 'place': place,
                       'content': content, 'note': note}, person_ids=people, aid=aid, by=auth.current_name())
    except ValueError as e:
        return (True,) + (nu,) * 13 + (str(e), True, nu, nu)
    return (False,) + (nu,) * 13 + ('', False, nu, (n or 0) + 1)


@callback(Output('ab-f-warn', 'children'), Input('ab-f-people', 'value'), Input('ab-f-start', 'value'),
          Input('ab-f-end', 'value'), State('ab-edit-id', 'data'))
def warn_overlap(people, start, end, aid):
    """같은 임원이 그 기간에 이미 부재로 등록돼 있으면 알려 준다 (막지는 않음)."""
    if not people or not start:
        return ''
    msgs = []
    for pid in people:
        for r in absences.overlaps(pid, start, end or start, exclude=aid):
            msgs.append(f"{r['person_name']}: 이미 {r['kind']} {_period(r)}")
    return [html.I(className='bi bi-exclamation-triangle me-1'), '겹치는 부재 — ' + ' / '.join(msgs)] if msgs else ''


# ── 메일로 등록 ───────────────────────────────────────────────────────

_ABS_STATE = {'new': '신규', 'same': '이미 있음', 'overlap': '겹침', 'unknown': '임원 모름'}


def table_rows(rows: list[dict]) -> list[dict]:
    """부재 후보 → 표 줄 (상태 칸 포함) — 받은 메일 화면과 같이 씀."""
    return [{k: r.get(k, '') or '' for k in TABLE_FIELDS + ('state',)} | {'state_label': _ABS_STATE.get(r['state'], '')}
            for r in absences.preview(rows)]


TABLE_FIELDS = ('person', 'kind', 'start_date', 'end_date', 'time_range', 'place', 'content', 'note')
TABLE_COLUMNS = [{'name': '상태', 'id': 'state_label', 'editable': False}, {'name': '임원', 'id': 'person'},
                 {'name': '종류', 'id': 'kind'}, {'name': '시작', 'id': 'start_date'}, {'name': '종료', 'id': 'end_date'},
                 {'name': '시간대', 'id': 'time_range'}, {'name': '행선지', 'id': 'place'},
                 {'name': '일정 내용', 'id': 'content'}, {'name': '메모', 'id': 'note'}]


def blank_row(day: str = '') -> dict:
    return {**{k: '' for k in TABLE_FIELDS}, 'kind': '휴가', 'start_date': day, 'state': 'unknown', 'state_label': ''}


@callback(Output('ab-imp-modal', 'is_open'), Output('ab-imp-table', 'data'), Output('ab-imp-note', 'children'),
          Output('ab-imp-msg', 'children'), Output('ab-imp-msg', 'is_open'), Output('ab-imp-msg', 'color'),
          Output('ab-refresh', 'data', allow_duplicate=True),
          Input('ab-imp-open', 'n_clicks'), Input('ab-imp-close', 'n_clicks'), Input('ab-imp-read', 'n_clicks'),
          Input('ab-imp-apply', 'n_clicks'),
          State('ab-imp-text', 'value'), State('ab-imp-date', 'value'), State('ab-imp-table', 'data'),
          State('ab-refresh', 'data'), prevent_initial_call=True)
def import_mail(_o, _c, _r, _a, text, mail_date, rows, n):
    nu = no_update
    trig = ctx.triggered_id
    if not ctx.triggered or not ctx.triggered[0]['value']:
        return (nu,) * 7
    if trig == 'ab-imp-open':
        return True, nu, nu, '', False, nu, nu
    if trig == 'ab-imp-close':
        return False, nu, nu, '', False, nu, nu
    if trig == 'ab-imp-read':
        if not (text or '').strip():
            return nu, nu, nu, '메일 내용을 붙여넣으세요.', True, 'warning', nu
        found, how = absences.extract(text, mail_date or date.today().isoformat())
        msg = '' if found else '부재를 찾지 못했습니다 — 표에 직접 적어도 됩니다.'
        data = table_rows(found) or [blank_row(mail_date or '')]
        return nu, data, how, msg, bool(msg), 'warning', nu
    r = absences.apply_rows(rows or [], auth.current_name())
    msg = (f"{r['added']}건 등록" + (f", 이미 있음 {r['same']}건" if r['same'] else '')
           + (f", 임원을 못 찾아 건너뜀 {r['unknown']}건" if r['unknown'] else ''))
    return nu, table_rows([x for x in rows or [] if x.get('person')]), nu, msg, True, \
        'success' if r['added'] else 'secondary', (n or 0) + 1


@callback(Output('mail-draft', 'data', allow_duplicate=True), Input('ab-mail', 'n_clicks'), State('ab-day', 'data'),
          prevent_initial_call=True)
def mail_absences(n, day):
    """고른 날짜가 있는 주(월~일)의 임원 부재 현황을 메일로."""
    if not n:
        return no_update
    lo, hi = absences.week_of(date.fromisoformat(day))
    return outbox.draft_absence(lo, hi)
