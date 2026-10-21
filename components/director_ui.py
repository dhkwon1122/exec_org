"""원장님 일정 — 캘린더 화면의 '원장님 일정' 탭.

월 보기: 날짜 칸에 일정 제목. 주 보기: 요일별 시간 타임라인(겹치는 일정은 나란히).
일정을 누르면 시작/종료·장소·참석자·세부 사항을 보고 고친다.
'메일로 가져오기': 비서실 일정 메일을 붙여넣거나 파일(.msg .eml .html .txt)로 여러 통 올리면 AI 가 일정으로
정리하고(백그라운드), 확인한 뒤 반영한다. 같은 날짜·제목의 일정은 새로 만들지 않고 바뀐 칸만 고친다.
"""
import calendar as pycal
from datetime import date, timedelta

import dash_bootstrap_components as dbc
from dash import ALL, Input, Output, State, callback, ctx, dash_table, dcc, html, no_update

from services import auth, llm, mail_text, schedules, store

WEEKDAYS = ['월', '화', '수', '목', '금', '토', '일']
HOUR_PX = 44
CAT_COLOR = {'회의': '#0d6efd', '보고': '#6f42c1', '면담': '#20c997', '행사': '#fd7e14', '외부': '#d63384',
             '출장': '#6c757d', '기타': '#adb5bd', '': '#0dcaf0'}
_STATE = {'new': ('신규', 'success'), 'changed': ('변경', 'warning'), 'same': ('동일', 'secondary')}


def layout():
    today = date.today().isoformat()
    form = [
        dbc.Row([
            dbc.Col([dbc.Label('날짜'), dbc.Input(id='dr-f-date', type='date')], md=4),
            dbc.Col([dbc.Label('시작'), dbc.Input(id='dr-f-start', type='time')], md=4),
            dbc.Col([dbc.Label('종료'), dbc.Input(id='dr-f-end', type='time')], md=4),
        ], className='g-2 mb-2'),
        dbc.Label('일정'), dbc.Input(id='dr-f-title', className='mb-2'),
        dbc.Row([
            dbc.Col([dbc.Label('장소'), dbc.Input(id='dr-f-location')], md=8),
            dbc.Col([dbc.Label('구분'), dbc.Select(id='dr-f-category', options=[{'label': '-', 'value': ''}] + [
                {'label': c, 'value': c} for c in schedules.CATEGORIES])], md=4),
        ], className='g-2 mb-2'),
        dbc.Label('참석자'), dbc.Input(id='dr-f-attendees', className='mb-1', placeholder='쉼표로 구분'),
        html.Div(id='dr-f-people', className='mb-2'),
        dbc.Label('세부 사항·결과'), dbc.Textarea(id='dr-f-details', rows=5),
        html.Div(id='dr-f-meta', className='small text-muted mt-2'),
        dbc.Alert(id='dr-f-error', color='danger', is_open=False, className='mt-2 mb-0 py-2'),
    ]
    return html.Div([
        dcc.Store(id='dr-anchor', data=today),
        dcc.Store(id='dr-refresh', data=0),
        dcc.Store(id='dr-edit-id'),
        dcc.Store(id='dr-imp-sel'),
        dcc.Store(id='dr-imp-shown'),  # 미리보기 표에 지금 보이는 (가져오기 id, 상태, 반영 시각)
        dcc.Interval(id='dr-imp-poll', interval=3000, disabled=True),
        html.Div([
            dbc.Button(html.I(className='bi bi-chevron-left'), id='dr-prev', color='secondary', outline=True, size='sm'),
            html.H5(id='dr-title', className='mb-0 mx-3'),
            dbc.Button(html.I(className='bi bi-chevron-right'), id='dr-next', color='secondary', outline=True,
                       size='sm'),
            dbc.Button('오늘', id='dr-today', color='secondary', outline=True, size='sm', className='ms-2 me-3'),
            dbc.RadioItems(id='dr-view', value='week', inline=True, className='d-flex', inputClassName='btn-check',
                           labelClassName='btn btn-sm btn-outline-secondary', labelCheckedClassName='active',
                           options=[{'label': '주', 'value': 'week'}, {'label': '월', 'value': 'month'}]),
            dbc.Input(id='dr-q', type='search', debounce=True, placeholder='일정 검색 (제목·장소·참석자·내용)',
                      size='sm', className='ms-auto me-2', style={'maxWidth': '260px'}),
            dbc.Button([html.I(className='bi bi-envelope-paper me-1'), '메일로 가져오기'], id='dr-imp-open',
                       color='secondary', outline=True, size='sm', className='me-2'),
            dbc.Button([html.I(className='bi bi-plus-lg me-1'), '일정 추가'], id='dr-add', color='primary', size='sm'),
        ], className='d-flex align-items-center flex-wrap gap-1 mb-2'),
        html.Div(id='dr-body'),
        dbc.Modal([
            dbc.ModalHeader(dbc.ModalTitle(id='dr-modal-title')),
            dbc.ModalBody(form),
            dbc.ModalFooter([
                dbc.Button('삭제', id='dr-del', color='danger', outline=True, className='me-auto'),
                dbc.Button('취소', id='dr-cancel', color='secondary', outline=True),
                dbc.Button('저장', id='dr-save', color='primary'),
            ]),
        ], id='dr-modal', is_open=False, size='lg'),
        dbc.Modal([
            dbc.ModalHeader(dbc.ModalTitle('비서실 메일로 일정 가져오기')),
            dbc.ModalBody([
                html.Div(['비서실 메일 파일(.eml)을 여러 개 한꺼번에 올리거나, 메일 본문을 그대로 붙여넣으세요(표 포함, 보낸 날짜·제목 '
                          '줄도 같이 붙이면 연도·요일 판단이 정확해집니다). .msg .html .txt 도 됩니다. ',
                          'AI 가 일정으로 정리하면 확인한 뒤 반영합니다.' if llm.is_configured() else
                          'LLM 이 설정되지 않아 날짜·시간이 보이는 줄만 규칙으로 읽습니다.'],
                         className='small text-muted mb-2'),
                dbc.Textarea(id='dr-imp-text', rows=6, placeholder='메일 내용 붙여넣기'),
                dbc.Row([
                    dbc.Col(dbc.Input(id='dr-imp-date', type='date', size='sm'), md=4),
                    dbc.Col(html.Div('메일 보낸 날짜 (비우면 본문에서 찾음)', className='small text-muted'), md=8,
                            className='d-flex align-items-center'),
                ], className='g-2 my-2'),
                dcc.Upload(id='dr-imp-files', multiple=True, accept='.eml,.msg,.html,.htm,.txt', children=html.Div(
                    [html.I(className='bi bi-upload me-2'), '메일 파일(.eml) 끌어 놓기 / 선택 — 여러 개 가능 ',
                     html.Span('(첨부가 큰 메일이 많으면 나눠서)', className='text-muted')]),
                    className='border rounded text-center p-3 text-muted small mb-2',
                    style={'borderStyle': 'dashed', 'cursor': 'pointer'}),
                html.Div([
                    dbc.Button([html.I(className='bi bi-magic me-1'), '정리하기'], id='dr-imp-go', color='primary',
                               size='sm', className='me-2'),
                    dbc.Button('정리된 메일 모두 반영 (옛 메일부터)', id='dr-imp-all', color='secondary', outline=True,
                               size='sm'),
                ]),
                dbc.Alert(id='dr-imp-msg', is_open=False, className='small py-2 mt-2 mb-0'),
                html.Hr(),
                html.Div(id='dr-imp-list'),
                html.Div(id='dr-imp-preview', className='mt-3', children=[
                    html.Div(id='dr-imp-preview-head'),
                    dash_table.DataTable(
                        id='dr-imp-table', editable=True, row_deletable=True, data=[],
                        columns=[{'name': '상태', 'id': 'state_label', 'editable': False},
                                 {'name': '날짜', 'id': 'date'}, {'name': '시작', 'id': 'start_time'},
                                 {'name': '종료', 'id': 'end_time'}, {'name': '일정', 'id': 'title'},
                                 {'name': '장소', 'id': 'location'}, {'name': '참석자', 'id': 'attendees'},
                                 {'name': '구분', 'id': 'category'}, {'name': '세부', 'id': 'details'}],
                        style_table={'maxHeight': '40vh', 'overflowY': 'auto', 'overflowX': 'auto'},
                        style_cell={'textAlign': 'left', 'fontSize': '0.8rem', 'fontFamily': 'inherit',
                                    'whiteSpace': 'normal', 'height': 'auto', 'minWidth': '60px', 'maxWidth': '260px'},
                        style_data_conditional=[
                            {'if': {'filter_query': '{state} = "new"'}, 'backgroundColor': 'rgba(25,135,84,.08)'},
                            {'if': {'filter_query': '{state} = "changed"'}, 'backgroundColor': 'rgba(255,193,7,.12)'},
                            {'if': {'filter_query': '{state} = "same"'}, 'color': '#888'}]),
                    dbc.Button([html.I(className='bi bi-check2 me-1'), '이 메일 반영'], id='dr-imp-apply',
                               color='primary', size='sm', className='mt-2'),
                ], style={'display': 'none'}),
            ]),
        ], id='dr-imp-modal', is_open=False, size='xl', scrollable=True),
    ])


# ── 보기 ──────────────────────────────────────────────────────────────

def _range(view: str, anchor: date) -> tuple[date, date]:
    if view == 'month':
        weeks = pycal.Calendar(firstweekday=0).monthdatescalendar(anchor.year, anchor.month)
        return weeks[0][0], weeks[-1][-1]
    start = schedules.week_start(anchor)
    return start, start + timedelta(days=6)


def _ev_box(e: dict, style: dict | None = None, compact: bool = False):
    color = CAT_COLOR.get(e['category'], CAT_COLOR[''])
    time = f"{e['start_time']}" + (f"~{e['end_time']}" if e['end_time'] else '') if e['start_time'] else '종일'
    body = [html.Span(time, className='me-1 opacity-75') if not compact or not e['start_time'] else None,
            html.Span(e['title'], className='fw-semibold')]
    if not compact and e['location']:
        body += [html.Div(e['location'], className='opacity-75 text-truncate')]
    cancelled = '취소' in e['details'] or '연기' in e['details']
    return html.Div(body, id={'type': 'dr-ev', 'id': e['id']}, n_clicks=0, title=f"{time} {e['title']} {e['location']}",
                    className='rounded px-1 text-truncate', style={
                        'background': color + '22', 'borderLeft': f'3px solid {color}', 'cursor': 'pointer',
                        'fontSize': '0.75rem', 'lineHeight': '1.25',
                        'textDecoration': 'line-through' if cancelled else None, **(style or {})})


def _minutes(t: str) -> int:
    h, m = t.split(':')
    return int(h) * 60 + int(m)


def _lanes(events: list[dict]) -> list[tuple[dict, int, int]]:
    """겹치는 일정을 나란히 — (일정, 몇 번째 칸, 그 묶음의 칸 수)."""
    out, group, lane_end, gmax = [], [], [], 0

    def flush():
        for ev, ln in group:
            out.append((ev, ln, gmax))

    for e in sorted(events, key=lambda x: (x['start_time'], x['end_time'])):
        s = _minutes(e['start_time'])
        end = _minutes(e['end_time']) if e['end_time'] else s + 60
        if group and s >= max(lane_end):
            flush()
            group, lane_end, gmax = [], [], 0
        for i, le in enumerate(lane_end):
            if s >= le:
                lane_end[i] = end
                lane = i
                break
        else:
            lane_end.append(end)
            lane = len(lane_end) - 1
        group.append((e, lane))
        gmax = max(gmax, len(lane_end))
    flush()
    return out


def week_view(start: date, events: list[dict]):
    days = [start + timedelta(days=i) for i in range(7)]
    timed = [e for e in events if e['start_time']]
    h0 = min([7] + [_minutes(e['start_time']) // 60 for e in timed])
    h1 = max([21] + [(_minutes(e['end_time'] or e['start_time']) + 59) // 60 for e in timed])
    height = (h1 - h0) * HOUR_PX
    today = date.today()
    head = html.Div([html.Div(style={'width': '48px'})] + [
        html.Div([html.Span(WEEKDAYS[d.weekday()], className='me-1'), html.Span(f'{d.month}/{d.day}')],
                 className='flex-fill text-center small fw-semibold' + (' text-primary' if d == today else '') +
                 (' text-danger' if d.weekday() == 6 else ''), style={'width': 0})
        for d in days], className='d-flex border-bottom pb-1')
    allday = html.Div([html.Div('종일', className='small text-muted', style={'width': '48px'})] + [
        html.Div([_ev_box(e, {'marginBottom': '2px'}, compact=True) for e in events
                  if e['date'] == d.isoformat() and not e['start_time']],
                 className='flex-fill px-1', style={'width': 0}) for d in days],
        className='d-flex border-bottom py-1', style={'minHeight': '26px'})
    hours = html.Div([html.Div(f'{h:02d}:00', className='small text-muted text-end pe-1',
                               style={'position': 'absolute', 'top': f'{(h - h0) * HOUR_PX - 7}px', 'width': '44px'})
                      for h in range(h0, h1 + 1)], style={'position': 'relative', 'width': '48px', 'height': f'{height}px'})
    cols = []
    for d in days:
        boxes = []
        for e, lane, n in _lanes([e for e in timed if e['date'] == d.isoformat()]):
            s = _minutes(e['start_time'])
            end = _minutes(e['end_time']) if e['end_time'] else s + 60
            boxes.append(_ev_box(e, {
                'position': 'absolute', 'top': f'{(s - h0 * 60) * HOUR_PX / 60}px',
                'height': f'{max(22, (end - s) * HOUR_PX / 60 - 2)}px', 'left': f'{lane * 100 / n}%',
                'width': f'calc({100 / n}% - 2px)', 'whiteSpace': 'normal', 'overflow': 'hidden'}))
        lines = [html.Div(style={'position': 'absolute', 'top': f'{(h - h0) * HOUR_PX}px', 'left': 0, 'right': 0,
                                 'borderTop': '1px solid var(--bs-border-color-translucent)'}) for h in range(h0, h1)]
        cols.append(html.Div(lines + boxes, className='flex-fill border-start', style={
            'position': 'relative', 'height': f'{height}px', 'width': 0,
            'background': 'rgba(13,110,253,.04)' if d == today else None}))
    return html.Div([head, allday, html.Div([hours] + cols, className='d-flex pt-2')])


def month_view(anchor: date, events: list[dict]):
    by_day: dict[str, list] = {}
    for e in events:
        by_day.setdefault(e['date'], []).append(e)
    weeks = pycal.Calendar(firstweekday=0).monthdatescalendar(anchor.year, anchor.month)
    today = date.today().isoformat()
    rows = []
    for week in weeks:
        cells = []
        for d in week:
            items = by_day.get(d.isoformat(), [])
            cells.append(html.Td(html.Div([
                html.Div(d.day, id={'type': 'dr-day', 'd': d.isoformat()}, n_clicks=0, title='이 주 보기',
                         className='small fw-bold' + (' text-primary' if d.isoformat() == today else '') +
                         (' text-muted' if d.month != anchor.month else ''), style={'cursor': 'pointer'}),
                *[_ev_box(e, {'marginBottom': '2px'}, compact=True) for e in items[:4]],
                html.Div(f'+{len(items) - 4}', id={'type': 'dr-day', 'd': d.isoformat() + '#'}, n_clicks=0,
                         className='small text-muted', style={'cursor': 'pointer'}) if len(items) > 4 else None,
            ], className='p-1 h-100 border rounded', style={'minHeight': '96px', 'background':
                                                            'var(--bs-tertiary-bg)' if d.month != anchor.month else None}),
                className='p-1', style={'width': '14.28%', 'verticalAlign': 'top'}))
        rows.append(html.Tr(cells))
    return html.Table([html.Thead(html.Tr([html.Th(w, className='text-center small text-muted fw-normal')
                                           for w in WEEKDAYS])), html.Tbody(rows)],
                      className='w-100', style={'tableLayout': 'fixed'})


def search_view(q: str):
    found = schedules.search(q)
    if not found:
        return html.Div('검색 결과가 없습니다.', className='text-muted small')
    return html.Div([html.Div(f'일정 {len(found)}건', className='small text-muted mb-1'), dbc.ListGroup([
        dbc.ListGroupItem(html.Div([
            html.Span(f"{e['date']} ({WEEKDAYS[date.fromisoformat(e['date']).weekday()]})", className='me-2 text-nowrap'),
            html.Div(_ev_box(e), className='flex-grow-1')], className='d-flex align-items-center'), className='py-1')
        for e in found])])


@callback(Output('dr-anchor', 'data'), Output('dr-view', 'value'),
          Input('dr-prev', 'n_clicks'), Input('dr-next', 'n_clicks'), Input('dr-today', 'n_clicks'),
          Input({'type': 'dr-day', 'd': ALL}, 'n_clicks'),
          State('dr-anchor', 'data'), State('dr-view', 'value'), prevent_initial_call=True)
def move(_p, _n, _t, _d, anchor, view):
    trig = ctx.triggered_id
    if trig is None or not ctx.triggered[0]['value']:
        return no_update, no_update
    if isinstance(trig, dict):  # 월 보기에서 날짜를 누르면 그 주로
        return trig['d'].rstrip('#'), 'week'
    a = date.fromisoformat(anchor)
    if trig == 'dr-today':
        return date.today().isoformat(), no_update
    step = -1 if trig == 'dr-prev' else 1
    if view == 'month':
        y, m = a.year, a.month + step
        y, m = (y - 1, 12) if m == 0 else (y + 1, 1) if m == 13 else (y, m)
        return date(y, m, 1).isoformat(), no_update
    return (a + timedelta(days=7 * step)).isoformat(), no_update


@callback(Output('dr-body', 'children'), Output('dr-title', 'children'),
          Input('dr-anchor', 'data'), Input('dr-view', 'value'), Input('dr-refresh', 'data'), Input('dr-q', 'value'))
def render(anchor, view, _, q):
    a = date.fromisoformat(anchor)
    if (q or '').strip():
        return search_view(q.strip()), f'검색: {q.strip()}'
    start, end = _range(view, a)
    events = schedules.list_range(start.isoformat(), end.isoformat())
    if view == 'month':
        return month_view(a, events), f'{a.year}년 {a.month}월'
    title = f'{start.year}년 {start.month}월 {start.day}일 – {end.month}월 {end.day}일'
    return week_view(start, events), title


# ── 일정 보기·수정 ────────────────────────────────────────────────────

def _people_chips(text: str):
    ids = store.detect_people(text or '')
    if not ids:
        return None
    people = [store.get_person(i) for i in ids]
    return html.Div([html.Span('연결된 임원: ', className='small text-muted me-1')] + [
        dcc.Link(f"{p['name']} {p['position']}".strip(), href=f"/people/{p['id']}",
                 className='badge rounded-pill text-bg-light border text-decoration-none me-1') for p in people if p])


@callback(Output('dr-modal', 'is_open'), Output('dr-modal-title', 'children'), Output('dr-edit-id', 'data'),
          Output('dr-f-date', 'value'), Output('dr-f-start', 'value'), Output('dr-f-end', 'value'),
          Output('dr-f-title', 'value'), Output('dr-f-location', 'value'), Output('dr-f-category', 'value'),
          Output('dr-f-attendees', 'value'), Output('dr-f-details', 'value'), Output('dr-f-meta', 'children'),
          Output('dr-del', 'style'), Output('dr-f-error', 'children'), Output('dr-f-error', 'is_open'),
          Output('dr-refresh', 'data', allow_duplicate=True),
          Input({'type': 'dr-ev', 'id': ALL}, 'n_clicks'), Input('dr-add', 'n_clicks'), Input('dr-cancel', 'n_clicks'),
          Input('dr-save', 'n_clicks'), Input('dr-del', 'n_clicks'),
          State('dr-edit-id', 'data'), State('dr-anchor', 'data'), State('dr-f-date', 'value'),
          State('dr-f-start', 'value'), State('dr-f-end', 'value'), State('dr-f-title', 'value'),
          State('dr-f-location', 'value'), State('dr-f-category', 'value'), State('dr-f-attendees', 'value'),
          State('dr-f-details', 'value'), State('dr-refresh', 'data'), prevent_initial_call=True)
def edit(_e, _a, _c, _s, _d, sid, anchor, d, start, end, title, loc, cat, att, details, n):
    nu = no_update
    trig = ctx.triggered_id
    if trig is None or not ctx.triggered[0]['value']:
        return (nu,) * 16  # 보기가 다시 그려지며 일정 상자가 새로 생길 때의 빈 트리거
    if isinstance(trig, dict):
        e = schedules.get(trig['id'])
        if e is None:
            return (nu,) * 15 + ((n or 0) + 1,)
        meta = f"{'메일에서 가져옴' if e['source'] == 'mail' else '직접 입력'} · 최근 수정 {e['updated_at']}"
        return (True, f"{e['date']} 일정", e['id'], e['date'], e['start_time'], e['end_time'], e['title'],
                e['location'], e['category'], e['attendees'], e['details'], meta, {}, '', False, nu)
    if trig == 'dr-add':
        return (True, '일정 추가', None, anchor, '', '', '', '', '', '', '', '', {'display': 'none'}, '', False, nu)
    if trig == 'dr-cancel':
        return (False,) + (nu,) * 12 + ('', False, nu)
    if trig == 'dr-del' and sid:
        schedules.delete(sid)
        return (False,) + (nu,) * 12 + ('', False, (n or 0) + 1)
    if trig == 'dr-save':
        try:
            schedules.save({'date': d, 'start_time': start, 'end_time': end, 'title': title, 'location': loc,
                            'category': cat, 'attendees': att, 'details': details}, sid, by=auth.current_name())
        except ValueError as ex:
            return (nu,) * 13 + (str(ex), True, nu)
        return (False,) + (nu,) * 12 + ('', False, (n or 0) + 1)
    return (nu,) * 16


@callback(Output('dr-f-people', 'children'), Input('dr-f-attendees', 'value'), Input('dr-f-title', 'value'))
def show_people(att, title):
    return _people_chips(f'{att or ""} {title or ""}')


# ── 메일로 가져오기 ───────────────────────────────────────────────────

def _imports_view(imps: list[dict], selected):
    if not imps:
        return html.Div('아직 가져온 메일이 없습니다.', className='small text-muted')
    rows = []
    for i in imps[:30]:
        if i['status'] == 'pending':
            badge = dbc.Badge([dbc.Spinner(size='sm', spinner_style={'width': '.7rem', 'height': '.7rem'}),
                               ' 정리 중'], color='info')
        elif i['status'] == 'failed':
            badge = dbc.Badge('실패', color='danger')
        elif i['applied']:
            badge = dbc.Badge('반영됨', color='success')
        else:
            badge = dbc.Badge(f"일정 {len(i['events'])}건 · 확인 전", color='warning', text_color='dark')
        rows.append(html.Tr([
            html.Td(badge, className='text-nowrap'),
            html.Td([html.Div(i['name'], className='fw-semibold'),
                     html.Div(i['error'], className='small text-muted') if i['error'] else None]),
            html.Td(i['mail_date'] or '-', className='small text-nowrap'),
            html.Td([
                dbc.Button('확인', id={'type': 'dr-imp-view', 'id': i['id']}, size='sm', color='primary',
                           outline=i['id'] != selected, className='me-1', disabled=i['status'] == 'pending'),
                dbc.Button(html.I(className='bi bi-arrow-repeat'), id={'type': 'dr-imp-redo', 'id': i['id']}, size='sm',
                           color='secondary', outline=True, className='me-1', title='다시 정리',
                           disabled=i['status'] == 'pending'),
                dbc.Button(html.I(className='bi bi-trash'), id={'type': 'dr-imp-del', 'id': i['id']}, size='sm',
                           color='secondary', outline=True, title='가져온 메일 지우기 (반영된 일정은 그대로)'),
            ], className='text-nowrap text-end'),
        ]))
    return dbc.Table(html.Tbody(rows), size='sm', className='align-middle small mb-0')


@callback(Output('dr-imp-modal', 'is_open'), Input('dr-imp-open', 'n_clicks'), prevent_initial_call=True)
def open_import(n):
    return bool(n) or no_update


@callback(Output('dr-imp-list', 'children'), Output('dr-imp-poll', 'disabled'), Output('dr-imp-msg', 'children'),
          Output('dr-imp-msg', 'is_open'), Output('dr-imp-msg', 'color'), Output('dr-imp-text', 'value'),
          Output('dr-imp-sel', 'data'), Output('dr-refresh', 'data', allow_duplicate=True),
          Input('dr-imp-modal', 'is_open'), Input('dr-imp-go', 'n_clicks'), Input('dr-imp-files', 'contents'),
          Input('dr-imp-poll', 'n_intervals'), Input('dr-imp-all', 'n_clicks'),
          Input({'type': 'dr-imp-redo', 'id': ALL}, 'n_clicks'), Input({'type': 'dr-imp-del', 'id': ALL}, 'n_clicks'),
          Input({'type': 'dr-imp-view', 'id': ALL}, 'n_clicks'), Input('dr-imp-apply', 'n_clicks'),
          State('dr-imp-text', 'value'), State('dr-imp-date', 'value'), State('dr-imp-files', 'filename'),
          State('dr-imp-sel', 'data'), State('dr-imp-table', 'data'), State('dr-refresh', 'data'),
          prevent_initial_call=True)
def imports(is_open, _go, files, _poll, _all, _redo, _del, _view, _apply, text, mail_date, names, sel, rows, n):
    nu = no_update
    trig = ctx.triggered_id
    msg, color, clear, refresh = nu, 'info', nu, nu
    value = ctx.triggered[0]['value'] if ctx.triggered else None
    if isinstance(trig, dict) and not value:
        trig = None  # 목록이 다시 그려질 때의 빈 트리거
    by = auth.current_name()
    if trig == 'dr-imp-go':
        if not (text or '').strip():
            msg, color = '붙여넣은 내용이 없습니다.', 'warning'
        else:
            m = mail_text.from_pasted(text)
            iid = schedules.create_import(m['name'], mail_date or m['mail_date'], m['text'], by)
            schedules.submit_import(iid)
            msg, clear, sel = '정리를 시작했습니다. 끝나면 "확인"을 눌러 반영하세요.', '', iid
    elif trig == 'dr-imp-files' and files:
        ok, errors = 0, []
        for c, fn in zip(files, names or []):
            try:
                m = mail_text.from_upload(c, fn)
            except ValueError as e:
                errors.append(str(e))
                continue
            schedules.submit_import(schedules.create_import(m['name'], m['mail_date'], m['text'], by))
            ok += 1
        msg = f'메일 {ok}통 정리를 시작했습니다.' + (' 읽지 못한 파일: ' + ' / '.join(errors) if errors else '')
        color = 'warning' if errors else 'info'
    elif trig == 'dr-imp-all':
        r = schedules.apply_all_pending(by)
        msg, color, refresh = (f"메일 {r['mails']}통 반영 — 새 일정 {r['added']}건, 바뀐 일정 {r['updated']}건, "
                               f"이미 있음 {r['same']}건"), 'success', (n or 0) + 1
    elif isinstance(trig, dict) and trig['type'] == 'dr-imp-redo':
        schedules.reparse(trig['id'])
        sel = trig['id']
    elif isinstance(trig, dict) and trig['type'] == 'dr-imp-del':
        schedules.delete_import(trig['id'])
        sel = None if sel == trig['id'] else sel
    elif isinstance(trig, dict) and trig['type'] == 'dr-imp-view':
        sel = trig['id']
    elif trig == 'dr-imp-apply' and sel and value:
        r = schedules.apply_events(rows or [], sel, by)
        msg, color, refresh = (f"반영 — 새 일정 {r['added']}건, 바뀐 일정 {r['updated']}건, 이미 있음 {r['same']}건",
                               'success', (n or 0) + 1)
    elif trig not in ('dr-imp-modal', 'dr-imp-poll'):
        return (nu,) * 8
    imps = schedules.list_imports()
    pending = any(i['status'] == 'pending' for i in imps)
    sel_out = sel if trig not in ('dr-imp-modal', 'dr-imp-poll') else nu  # 주기 갱신으로는 고치던 표를 건드리지 않음
    return (_imports_view(imps, sel), not pending, msg, msg is not nu and bool(msg), color, clear, sel_out, refresh)


@callback(Output('dr-imp-preview', 'style'), Output('dr-imp-preview-head', 'children'), Output('dr-imp-table', 'data'),
          Output('dr-imp-shown', 'data'),
          Input('dr-imp-sel', 'data'), Input('dr-imp-list', 'children'), State('dr-imp-shown', 'data'))
def show_preview(sel, _list, shown):
    imp = schedules.get_import(sel) if sel else None
    if imp is None or imp['status'] != 'done':
        return {'display': 'none'}, None, [], None
    key = [imp['id'], imp['status'], imp['applied_at']]
    if ctx.triggered_id == 'dr-imp-list' and shown == key:
        return no_update, no_update, no_update, no_update  # 목록만 다시 그려진 것 — 고치던 표는 그대로
    rows = schedules.preview(imp['events'])
    for r in rows:
        label, _ = _STATE[r['state']]
        r['state_label'] = label + (f" ({', '.join(r['changed'])})" if r['changed'] else '')
        r['changed'] = ', '.join(r['changed'])  # 표 칸에는 글자만 넣을 수 있다
    counts = {k: sum(r['state'] == k for r in rows) for k in _STATE}
    head = html.Div([
        html.Span(imp['name'], className='fw-semibold me-2'),
        *[dbc.Badge(f'{_STATE[k][0]} {v}', color=_STATE[k][1], className='me-1') for k, v in counts.items() if v],
        dbc.Badge('반영됨', color='success', className='ms-1') if imp['applied'] else None,
        html.Div('칸을 눌러 고치고, 넣지 않을 줄은 × 로 지운 뒤 반영하세요. 같은 날짜·제목의 일정은 바뀐 칸만 고칩니다.',
                 className='small text-muted'),
        html.Details([html.Summary('메일 원문 보기', className='small'),
                      html.Pre(imp['raw_text'][:20000], className='small bg-body-tertiary p-2 rounded',
                               style={'whiteSpace': 'pre-wrap', 'maxHeight': '30vh', 'overflowY': 'auto'})]),
    ], className='mb-2')
    return {}, head, rows, key
