"""메일에서 정리한 일정 표 고치기 — 받은 메일 화면(ib-sched)과 캘린더 '메일로 가져오기'(dr-imp-table)가 같이 쓴다.

- '일정 추가' 단추로 새 줄, 줄 맨 앞 ✎ 를 누르면 날짜·시간 고르기가 있는 편집 칸이 열린다 (표 칸을 바로 고쳐도 된다).
- 고치면(편집 칸 저장·표 칸 수정·줄 지우기) 기존 일정과 다시 대조해 상태(신규·변경·동일·확인 필요)를 바꾸고,
  고친 표를 가져온 메일(ScheduleImport)에 저장한다 — 반영 전에 다른 메일을 봤다 와도 고친 내용이 남는다.
"""
import dash_bootstrap_components as dbc
from dash import Input, Output, State, callback, ctx, dcc, html, no_update

from services import schedules

STATE_LABEL = {'new': '신규', 'changed': '변경', 'same': '동일', 'check': '확인 필요'}
EDIT_COLUMN = {'name': '', 'id': 'edit', 'editable': False}
EDIT_STYLE = {'if': {'column_id': 'edit'}, 'cursor': 'pointer', 'color': 'var(--bs-primary)', 'width': '28px',
              'textAlign': 'center'}
_FIELDS = ('date', 'end_date', 'start_time', 'end_time', 'title', 'location', 'attendees', 'category', 'details')


def label(row: dict) -> dict:
    """미리보기 줄 → 표 줄 (상태 글자, 바뀐 칸, ✎)."""
    changed = row.get('changed') or []
    if isinstance(changed, str):
        changed = [c for c in changed.split(', ') if c]
    names = [schedules.LABELS.get(f, f) for f in changed]
    return {**row, 'state_label': STATE_LABEL.get(row.get('state'), '') + (f" ({', '.join(names)})" if names else ''),
            'changed': ', '.join(changed), 'edit': '✎'}


def relabel(rows: list[dict], owner: str) -> list[dict]:
    """사람이 고친 표를 기존 일정과 다시 대조 — 칸 값은 사람이 고친 그대로 두고 상태·대응 일정·검증만 바꾼다."""
    rows = [dict(r) for r in rows or []]
    idx = [i for i, r in enumerate(rows) if schedules.clean_event(r)]
    checked = schedules.preview([rows[i] for i in idx], owner)
    for i, p in zip(idx, checked):
        rows[i].update(state=p['state'], changed=p['changed'], match_id=p['match_id'], note=p['note'])
    for i, r in enumerate(rows):
        if i not in idx:
            r.update(state='check', changed=[], match_id=None, note='날짜와 일정 제목을 적어야 반영됩니다.')
    return [label(r) for r in rows]


def editor(p: str):
    """편집 칸 (표 위에 펼침) + '일정 추가' 단추. p: 표 id."""
    opts = [{'label': '구분 없음', 'value': ''}] + [{'label': c, 'value': c} for c in schedules.CATEGORIES]
    return html.Div([
        html.Div([
            dbc.Button([html.I(className='bi bi-plus-lg me-1'), '일정 추가'], id=f'{p}-add', color='secondary',
                       outline=True, size='sm', className='me-2'),
            html.Span('줄 맨 앞 ✎ 를 누르면 날짜·시간을 골라 고칠 수 있습니다. 표 칸을 바로 고쳐도 되고, 넣지 않을 줄은 ×. '
                      '고치면 기존 일정과 다시 대조합니다.', className='small text-muted'),
        ], className='d-flex align-items-center flex-wrap mb-1'),
        dbc.Collapse(dbc.Card(dbc.CardBody([
            html.Div(id=f'{p}-f-head', className='small fw-semibold mb-2'),
            dbc.Row([
                dbc.Col([dbc.Label('날짜', className='small mb-0'), dbc.Input(id=f'{p}-f-date', type='date', size='sm')], md=3),
                dbc.Col([dbc.Label('마지막 날 (여러 날이면)', className='small mb-0'),
                         dbc.Input(id=f'{p}-f-end-date', type='date', size='sm')], md=3),
                dbc.Col([dbc.Label('시작', className='small mb-0'), dbc.Input(id=f'{p}-f-start', type='time', size='sm')], md=3),
                dbc.Col([dbc.Label('종료', className='small mb-0'), dbc.Input(id=f'{p}-f-end', type='time', size='sm')], md=3),
            ], className='g-2 mb-2'),
            dbc.Row([
                dbc.Col([dbc.Label('일정', className='small mb-0'), dbc.Input(id=f'{p}-f-title', size='sm')], md=6),
                dbc.Col([dbc.Label('장소', className='small mb-0'), dbc.Input(id=f'{p}-f-location', size='sm')], md=3),
                dbc.Col([dbc.Label('구분', className='small mb-0'),
                         dbc.Select(id=f'{p}-f-category', options=opts, value='', size='sm')], md=3),
            ], className='g-2 mb-2'),
            dbc.Row([
                dbc.Col([dbc.Label('참석자', className='small mb-0'), dbc.Input(id=f'{p}-f-attendees', size='sm')], md=6),
                dbc.Col([dbc.Label('세부', className='small mb-0'), dbc.Input(id=f'{p}-f-details', size='sm')], md=6),
            ], className='g-2 mb-2'),
            html.Div([
                dbc.Button('저장', id=f'{p}-f-save', color='primary', size='sm', className='me-2'),
                dbc.Button('취소', id=f'{p}-f-cancel', color='secondary', outline=True, size='sm', className='me-2'),
                dbc.Button([html.I(className='bi bi-trash me-1'), '이 줄 빼기'], id=f'{p}-f-delete', color='link',
                           size='sm', className='text-danger p-0 ms-auto'),
            ], className='d-flex align-items-center'),
            dbc.Alert(id=f'{p}-f-error', color='danger', is_open=False, className='small py-1 px-2 mt-2 mb-0'),
        ]), className='mb-2 border-primary'), id=f'{p}-f-open', is_open=False),
        dcc.Store(id=f'{p}-f-idx'),  # 고치는 줄 번호 ('new' = 새 줄)
    ])


def register(p: str, import_of):
    """콜백 등록. import_of: (State, 그 값 → 가져온 메일 id 또는 None) — 대조할 사람·저장할 곳."""
    import_state, to_import_id = import_of
    fields = [State(f'{p}-f-{k}', 'value') for k in ('date', 'end-date', 'start', 'end', 'title', 'location',
                                                     'category', 'attendees', 'details')]

    def _imp(v):
        iid = to_import_id(v) if v else None
        return schedules.get_import(iid) if iid else None

    def _finish(rows, imp):
        if imp is None:
            return [label(r) for r in rows]
        rows = relabel(rows, imp['owner'])
        schedules.save_import_events(imp['id'], rows)
        return rows

    @callback(Output(f'{p}-f-open', 'is_open'), Output(f'{p}-f-idx', 'data'), Output(f'{p}-f-head', 'children'),
              *[Output(f'{p}-f-{k}', 'value') for k in ('date', 'end-date', 'start', 'end', 'title', 'location',
                                                        'category', 'attendees', 'details')],
              Output(f'{p}-f-error', 'is_open'), Output(f'{p}-f-delete', 'style'),
              Input(f'{p}-add', 'n_clicks'), Input(p, 'active_cell'), State(p, 'data'), import_state,
              prevent_initial_call=True)
    def open_editor(_n, cell, rows, imp_v):
        rows = rows or []
        if ctx.triggered_id == f'{p}-add':
            imp = _imp(imp_v)
            day = (imp or {}).get('mail_date', '') or ''
            return True, 'new', '새 일정', day, '', '', '', '', '', '', '', '', False, {'display': 'none'}
        if not cell or cell.get('column_id') != 'edit' or cell.get('row', -1) >= len(rows):
            return (no_update,) * 14
        r = rows[cell['row']]
        return (True, str(cell['row']), f"{cell['row'] + 1}번째 줄 고치기", *[r.get(k, '') or '' for k in _FIELDS],
                False, {})

    @callback(Output(p, 'data', allow_duplicate=True), Output(f'{p}-f-open', 'is_open', allow_duplicate=True),
              Output(f'{p}-f-error', 'children'), Output(f'{p}-f-error', 'is_open', allow_duplicate=True),
              Output(p, 'active_cell'),
              Input(f'{p}-f-save', 'n_clicks'), Input(f'{p}-f-cancel', 'n_clicks'), Input(f'{p}-f-delete', 'n_clicks'),
              State(f'{p}-f-idx', 'data'), State(p, 'data'), *fields, import_state, prevent_initial_call=True)
    def save(_s, _c, _d, idx, rows, *vals):
        *vals, imp_v = vals
        trig = ctx.triggered_id
        if trig == f'{p}-f-cancel':
            return no_update, False, '', False, None
        rows = list(rows or [])
        pos = int(idx) if str(idx).isdigit() and int(idx) < len(rows) else None
        if trig == f'{p}-f-delete':
            if pos is not None:
                rows.pop(pos)
            return _finish(rows, _imp(imp_v)), False, '', False, None
        row = {**(rows[pos] if pos is not None else {}), **dict(zip(_FIELDS, [(v or '').strip() for v in vals]))}
        if schedules.clean_event(row) is None:
            return no_update, True, '날짜와 일정 제목은 꼭 적어야 합니다.', True, no_update
        row['match_id'] = None  # 고친 내용으로 다시 같은 일정을 찾는다
        if pos is None:
            rows.append(row)
        else:
            rows[pos] = row
        return _finish(rows, _imp(imp_v)), False, '', False, None

    @callback(Output(p, 'data', allow_duplicate=True), Input(p, 'data_timestamp'), State(p, 'data'), import_state,
              prevent_initial_call=True)
    def edited(_ts, rows, imp_v):
        """표 칸을 바로 고치거나 줄을 지웠을 때 — 다시 대조하고 저장."""
        imp = _imp(imp_v)
        if imp is None:
            return no_update
        for r in rows or []:
            r['match_id'] = None
        return _finish(rows or [], imp)
