"""받은 메일 — POP3 로 가져온 메일의 처리 결과(자동 반영)와 추천(확인 필요), 분류 규칙."""
import dash
import dash_bootstrap_components as dbc
from dash import ALL, Input, Output, State, callback, ctx, dash_table, dcc, html, no_update

from services import auth, mailbox, schedules, store

dash.register_page(__name__, path='/inbox', name='받은 메일', title='업무 기록 · 받은 메일')

KIND = {'schedule': ('원장님 일정', 'primary'), 'task': ('업무', 'success'), 'both': ('일정+업무', 'info'),
        'ignore': ('무시', 'secondary')}
STATUS_COLOR = {'auto': 'success', 'review': 'warning', 'done': 'primary', 'ignored': 'secondary', 'failed': 'danger'}
FILTERS = [('review', '확인 필요'), ('auto', '자동 반영'), ('done', '반영함'), ('ignored', '무시'), ('all', '전체')]


def _task_options():
    return [{'label': f"#{t['id']} {t['title']}", 'value': t['id']}
            for t in store.list_tasks(status=['예정', '진행중', '보류'])]


def layout(mail=None, **_):
    try:
        mail = int(mail) if mail else None  # 업무의 '출처' 링크(/inbox?mail=…)로 들어오면 그 메일을 바로 연다
    except ValueError:
        mail = None
    cell = {'textAlign': 'left', 'fontSize': '0.85rem', 'fontFamily': 'inherit', 'whiteSpace': 'normal',
            'height': 'auto'}
    return html.Div([
        dcc.Store(id='ib-sel', data=mail),
        dcc.Store(id='ib-refresh', data=0),
        html.Div([
            html.H3('받은 메일', className='mb-0 me-3', style={'fontSize': '1.6rem', 'fontWeight': 750}),
            dbc.RadioItems(id='ib-filter', value='all' if mail else 'review', inline=True, className='d-flex',
                           inputClassName='btn-check', labelClassName='btn btn-sm btn-outline-secondary me-1',
                           labelCheckedClassName='active', options=[]),
            html.Div(id='ib-status', className='small text-muted ms-auto me-2'),
            dbc.Button([html.I(className='bi bi-arrow-repeat me-1'), '지금 가져오기'], id='ib-fetch', size='sm',
                       color='secondary', outline=True, className='me-2'),
            dbc.Button([html.I(className='bi bi-funnel me-1'), '분류 규칙'], id='ib-rules-open', size='sm',
                       color='secondary', outline=True),
        ], className='d-flex align-items-center flex-wrap gap-1 mb-3'),
        dcc.Loading(dbc.Alert(id='ib-msg', is_open=False, dismissable=True, className='py-2 small'), type='dot'),
        dbc.Row([
            dbc.Col(html.Div(id='ib-list'), lg=5),
            dbc.Col(html.Div([
                html.Div(id='ib-head'),
                html.Div(id='ib-task-box', style={'display': 'none'}, children=[
                    html.H6([html.I(className='bi bi-check2-square me-1'), '업무로 만들기'], className='mt-3'),
                    dbc.Row([
                        dbc.Col(dcc.Dropdown(id='ib-parent', placeholder='상위 업무 (비우면 최상위 업무)'), md=8),
                        dbc.Col(dbc.Button([html.I(className='bi bi-plus me-1'), '줄 추가'], id='ib-task-add',
                                           size='sm', color='secondary', outline=True), md=4),
                    ], className='g-2 mb-1'),
                    html.Div(id='ib-parent-hint', className='small mb-2'),
                    dash_table.DataTable(id='ib-tasks', editable=True, row_deletable=True, data=[],
                                         columns=[{'name': '업무', 'id': 'title'}, {'name': '기한(YYYY-MM-DD)', 'id': 'due'},
                                                  {'name': '담당 (쉼표로 여러 명)', 'id': 'assignee'}],
                                         style_cell=cell),
                    html.Div('메일 원문은 만든 업무마다 "요청" 기록으로 남습니다. 만들지 않을 줄은 × 로 지우세요.',
                             className='small text-muted mt-1'),
                ]),
                html.Div(id='ib-sched-box', style={'display': 'none'}, children=[
                    html.H6([html.I(className='bi bi-calendar-event me-1'), '원장님 일정'], className='mt-3'),
                    html.Div(id='ib-sched-note', className='small text-muted mb-1'),
                    dash_table.DataTable(id='ib-sched', editable=True, row_deletable=True, data=[],
                                         columns=[{'name': '상태', 'id': 'state_label', 'editable': False},
                                                  {'name': '날짜', 'id': 'date'}, {'name': '시작', 'id': 'start_time'},
                                                  {'name': '종료', 'id': 'end_time'}, {'name': '일정', 'id': 'title'},
                                                  {'name': '장소', 'id': 'location'},
                                                  {'name': '참석자', 'id': 'attendees'}, {'name': '세부', 'id': 'details'},
                                                  {'name': '검증', 'id': 'note', 'editable': False}],
                                         style_table={'overflowX': 'auto'}, style_cell={**cell, 'fontSize': '0.8rem'}),
                ]),
                html.Div(id='ib-actions', className='mt-3', style={'display': 'none'}, children=[
                    dbc.Button([html.I(className='bi bi-check2 me-1'), '반영'], id='ib-apply', color='primary',
                               className='me-2'),
                    dbc.Button('무시', id='ib-ignore', color='secondary', outline=True, className='me-2'),
                    dbc.Button('다시 확인 필요로', id='ib-reopen', color='secondary', outline=True, className='me-2'),
                    dbc.Button([html.I(className='bi bi-funnel me-1'), '이 보낸 사람으로 규칙 만들기'], id='ib-mkrule',
                               color='link', size='sm'),
                ]),
                html.Details([html.Summary('메일 본문', className='small mt-3'),
                              html.Pre(id='ib-body', className='small bg-body-tertiary p-2 rounded',
                                       style={'whiteSpace': 'pre-wrap', 'maxHeight': '50vh', 'overflowY': 'auto'})],
                             id='ib-body-box', style={'display': 'none'}),
            ]), lg=7),
        ]),
        dbc.Modal([
            dbc.ModalHeader(dbc.ModalTitle('메일 분류 규칙')),
            dbc.ModalBody([
                html.Div('위에서부터 처음 맞는 규칙을 씁니다 (글자 포함, 대소문자 무시). "자동"이면 확인 없이 바로 반영하고, '
                         '아니면 받은 메일에 추천으로 남깁니다. 맞는 규칙이 없으면 AI 가 추천합니다.',
                         className='small text-muted mb-2'),
                html.Div(id='ib-rule-list'),
                html.Hr(),
                dbc.Row([
                    dbc.Col(dbc.Select(id='ib-r-field', value='sender', options=[
                        {'label': v, 'value': k} for k, v in mailbox.FIELDS.items()]), md=3),
                    dbc.Col(dbc.Input(id='ib-r-pattern', placeholder='에 이 글자가 있으면 (예: 비서실, secretary@)'), md=5),
                    dbc.Col(dbc.Select(id='ib-r-action', value='schedule', options=[
                        {'label': f'→ {v}', 'value': k} for k, v in mailbox.ACTIONS.items()]), md=4),
                ], className='g-2 mb-2'),
                dbc.Row([
                    dbc.Col(dcc.Dropdown(id='ib-r-parent', placeholder='업무면: 상위 업무 (선택)'), md=6),
                    dbc.Col(dbc.Input(id='ib-r-name', placeholder='규칙 이름 (선택)'), md=3),
                    dbc.Col(dbc.Checkbox(id='ib-r-auto', value=True, label='자동 반영'), md=3,
                            className='d-flex align-items-center'),
                ], className='g-2 mb-2'),
                dbc.Button('규칙 추가', id='ib-r-add', color='primary', size='sm'),
                dbc.Alert(id='ib-r-error', color='danger', is_open=False, className='py-2 small mt-2 mb-0'),
            ]),
        ], id='ib-rules-modal', is_open=False, size='lg'),
    ])


# ── 목록 ──────────────────────────────────────────────────────────────

@callback(Output('ib-list', 'children'), Output('ib-filter', 'options'), Output('ib-status', 'children'),
          Input('ib-filter', 'value'), Input('ib-refresh', 'data'), Input('ib-sel', 'data'))
def render_list(flt, _, sel):
    c = mailbox.counts()
    opts = [{'label': f'{label} {c.get(k, 0)}' if k != 'all' else label, 'value': k} for k, label in FILTERS]
    mails = mailbox.list_mails(None if flt == 'all' else [flt])
    if mailbox.is_configured():
        lr = mailbox.last_run()
        st = (f"POP3 {mailbox.settings()['user']} · {mailbox.settings()['minutes']}분마다"
              + (f" · 마지막 확인 {lr['at']}" + ('' if lr.get('ok') else f" (실패: {lr.get('error', '')[:60]})")
                 if lr else ''))
    else:
        st = 'POP3 미설정 (.env 의 MAIL_POP3_*) — 자동으로 가져오지 않습니다'
    if not mails:
        return html.Div('메일이 없습니다.', className='small text-muted'), opts, st
    items = []
    for m in mails:
        kind = KIND.get(m['kind'])
        items.append(dbc.ListGroupItem([
            html.Div([
                dbc.Badge(mailbox.STATUS.get(m['status'], m['status']), color=STATUS_COLOR.get(m['status'], 'light'),
                          className='me-1'),
                dbc.Badge(kind[0], color=kind[1], className='me-1') if kind else None,
                html.Span(m['mail_date'], className='small text-muted ms-auto'),
            ], className='d-flex align-items-center mb-1'),
            html.Div(m['subject'], className='fw-semibold text-truncate'),
            html.Div(mailbox.sender_name(m['sender']), className='small text-muted text-truncate'),
        ], id={'type': 'ib-mail', 'id': m['id']}, n_clicks=0, action=True, active=m['id'] == sel, className='py-2'))
    return dbc.ListGroup(items), opts, st


@callback(Output('ib-sel', 'data'), Input({'type': 'ib-mail', 'id': ALL}, 'n_clicks'), prevent_initial_call=True)
def pick(_):
    if not isinstance(ctx.triggered_id, dict) or not ctx.triggered[0]['value']:
        return no_update
    return ctx.triggered_id['id']


# ── 상세 ──────────────────────────────────────────────────────────────

@callback(Output('ib-head', 'children'), Output('ib-task-box', 'style'), Output('ib-tasks', 'data'),
          Output('ib-parent', 'options'), Output('ib-parent', 'value'), Output('ib-sched-box', 'style'),
          Output('ib-sched', 'data'), Output('ib-sched-note', 'children'), Output('ib-actions', 'style'),
          Output('ib-apply', 'style'), Output('ib-ignore', 'style'), Output('ib-reopen', 'style'),
          Output('ib-body', 'children'), Output('ib-body-box', 'style'), Output('ib-parent-hint', 'children'),
          Input('ib-sel', 'data'), Input('ib-refresh', 'data'))
def render_detail(sel, _):
    hide, show = {'display': 'none'}, {}
    m = mailbox.get_mail(sel) if sel else None
    if m is None:
        return (html.Div('왼쪽에서 메일을 고르세요.', className='text-muted small'), hide, [], [], None, hide, [], '',
                hide, hide, hide, hide, '', hide, None)
    open_ = m['status'] in ('review', 'failed')
    kind = KIND.get(m['kind'])
    links = [dcc.Link(f'#{tid}', href=f'/task/{tid}', className='me-2') for tid in m['task_ids']]
    head = dbc.Card(dbc.CardBody([
        html.H5(m['subject'], className='mb-1'),
        html.Div(f"{m['sender']} · {m['mail_date']} · 받은 시각 {m['received_at']}", className='small text-muted mb-2'),
        html.Div([dbc.Badge(mailbox.STATUS.get(m['status'], m['status']), color=STATUS_COLOR.get(m['status'], 'light'),
                            className='me-1'),
                  dbc.Badge(kind[0], color=kind[1], className='me-2') if kind else None,
                  html.Span(m['reason'], className='small')]),
        html.Div(['만든 업무: ', *links], className='small mt-1') if links else None,
        html.Div(f"처리: {m['handled_by']} {m['handled_at']}", className='small text-muted mt-1')
        if m['handled_at'] else None,
        dbc.Alert(m['error'], color='danger', className='small py-1 mt-2 mb-0') if m['error'] else None,
    ]))
    wants_task = m['kind'] in ('task', 'both') or not m['kind']
    tasks = m['suggestion'].get('tasks') or []
    sched_rows, note = [], ''
    if m['schedule_import_id']:
        imp = schedules.get_import(m['schedule_import_id'])
        if imp and imp['status'] == 'done':
            sched_rows = schedules.preview(imp['events'])
            for r in sched_rows:
                r['state_label'] = {'new': '신규', 'changed': '변경', 'same': '동일', 'check': '확인 필요'}[r['state']] + (
                    f" ({', '.join(schedules.LABELS[f] for f in r['changed'])})" if r['changed'] else '')
                r['changed'] = ', '.join(r['changed'])
            note = imp['error'] or ''
        elif imp:
            note = f"일정 정리 실패: {imp['error']}"
    show_sched = bool(m['schedule_import_id']) and (open_ or bool(sched_rows))
    sug = m['suggestion']
    parent = sug.get('parent_task_id')
    return (head, show if (open_ and wants_task) else hide, tasks, _task_options(), parent,
            show if show_sched else hide, sched_rows, note, show,
            show if open_ else hide, show if open_ else hide, hide if open_ else show,
            m['body'], show, _parent_hint(sug))


def _parent_hint(sug: dict):
    """상위 업무 추천 근거와 다른 후보 — 후보를 누르면 위 칸이 바뀐다."""
    cands = sug.get('parent_candidates') or []
    pid = sug.get('parent_task_id')
    if not cands and not pid:
        return html.Span('추천할 상위 업무가 없습니다 — 비워 두면 최상위 업무로 만듭니다.', className='text-muted')
    head = [html.I(className='bi bi-stars me-1 text-warning'),
            html.Span(f"추천: {next((c['title'] for c in cands if c['id'] == pid), '')}" if pid else '뚜렷한 추천 없음',
                      className='fw-semibold me-1'),
            html.Span(f"({sug.get('parent_reason')})" if sug.get('parent_reason') else '', className='text-muted')]
    others = [c for c in cands if c['id'] != pid]
    return html.Div([
        html.Div(head),
        html.Div([html.Span('다른 후보: ', className='text-muted me-1')] + [
            dbc.Button(f"#{c['id']} {c['title'][:30]}", id={'type': 'ib-pc', 'id': c['id']}, size='sm', color='light',
                       className='border me-1 mb-1 py-0', title=f"겹치는 낱말: {', '.join(c['hits'])}" if c['hits'] else None)
            for c in others]) if others else None,
    ])


@callback(Output('ib-parent', 'value', allow_duplicate=True), Input({'type': 'ib-pc', 'id': ALL}, 'n_clicks'),
          prevent_initial_call=True)
def pick_candidate(_):
    if not isinstance(ctx.triggered_id, dict) or not ctx.triggered[0]['value']:
        return no_update
    return ctx.triggered_id['id']


@callback(Output('ib-tasks', 'data', allow_duplicate=True), Input('ib-task-add', 'n_clicks'),
          State('ib-tasks', 'data'), prevent_initial_call=True)
def add_task_row(n, rows):
    return (rows or []) + [{'title': '', 'due': '', 'assignee': ''}] if n else no_update


@callback(Output('ib-refresh', 'data'), Output('ib-msg', 'children'), Output('ib-msg', 'is_open'),
          Output('ib-msg', 'color'),
          Input('ib-apply', 'n_clicks'), Input('ib-ignore', 'n_clicks'), Input('ib-reopen', 'n_clicks'),
          Input('ib-fetch', 'n_clicks'),
          State('ib-sel', 'data'), State('ib-tasks', 'data'), State('ib-parent', 'value'), State('ib-sched', 'data'),
          State('ib-task-box', 'style'), State('ib-sched-box', 'style'), State('ib-refresh', 'data'),
          prevent_initial_call=True)
def act(_a, _i, _r, _f, sel, tasks, parent, sched, task_style, sched_style, n):
    trig = ctx.triggered_id
    if not ctx.triggered or not ctx.triggered[0]['value']:
        return no_update, no_update, no_update, no_update
    by = auth.current_name()
    if trig == 'ib-fetch':
        if not mailbox.is_configured():
            return no_update, 'POP3 가 설정되지 않았습니다 (.env 의 MAIL_POP3_HOST·USER·PASSWORD).', True, 'warning'
        st = mailbox.run_once()
        if not st['ok']:
            return (n or 0) + 1, f"가져오기 실패: {st['error']}", True, 'danger'
        if st.get('busy'):
            return (n or 0) + 1, '지금 다른 곳(자동 확인)에서 가져오는 중입니다. 잠시 뒤 목록을 보세요.', True, 'info'
        return (n or 0) + 1, f"새 메일 {st['new']}통 (오래돼 건너뜀 {st['skipped']}통)", True, 'success'
    if not sel:
        return no_update, no_update, no_update, no_update
    if trig == 'ib-ignore':
        mailbox.ignore(sel, by)
        return (n or 0) + 1, '무시했습니다.', True, 'secondary'
    if trig == 'ib-reopen':
        mailbox.reopen(sel)
        return (n or 0) + 1, no_update, False, no_update
    use_tasks = tasks if task_style != {'display': 'none'} else []
    use_sched = sched if sched_style != {'display': 'none'} else []
    r = mailbox.apply_review(sel, use_tasks, parent, use_sched, by)
    return (n or 0) + 1, (f"반영 — 업무 {r['tasks']}건, 새 일정 {r['added']}건, 바뀐 일정 {r['updated']}건"), True, 'success'


# ── 규칙 ──────────────────────────────────────────────────────────────

def _rules_view():
    rules = mailbox.list_rules()
    if not rules:
        return html.Div('규칙이 없습니다. 예: 보낸 사람에 "비서실" → 원장님 일정(자동).', className='small text-muted')
    titles = {t['value']: t['label'] for t in _task_options()}
    return dbc.Table(html.Tbody([html.Tr([
        html.Td(r['name']),
        html.Td(f"{mailbox.FIELDS[r['field']]}에 '{r['pattern']}'", className='small'),
        html.Td([f"→ {mailbox.ACTIONS[r['action']]}",
                 html.Div(titles.get(r['parent_task_id'], ''), className='small text-muted')
                 if r['parent_task_id'] else None]),
        html.Td(dbc.Badge('자동' if r['auto'] else '추천', color='success' if r['auto'] else 'warning')),
        html.Td(dbc.Button(html.I(className='bi bi-trash'), id={'type': 'ib-r-del', 'id': r['id']}, size='sm',
                           color='secondary', outline=True), className='text-end'),
    ]) for r in rules]), size='sm', className='align-middle small')


@callback(Output('ib-rules-modal', 'is_open'), Output('ib-rule-list', 'children'), Output('ib-r-parent', 'options'),
          Output('ib-r-error', 'children'), Output('ib-r-error', 'is_open'), Output('ib-r-pattern', 'value'),
          Output('ib-r-field', 'value'), Output('ib-r-action', 'value'),
          Input('ib-rules-open', 'n_clicks'), Input('ib-mkrule', 'n_clicks'), Input('ib-r-add', 'n_clicks'),
          Input({'type': 'ib-r-del', 'id': ALL}, 'n_clicks'),
          State('ib-sel', 'data'), State('ib-r-field', 'value'), State('ib-r-pattern', 'value'),
          State('ib-r-action', 'value'), State('ib-r-auto', 'value'), State('ib-r-parent', 'value'),
          State('ib-r-name', 'value'), prevent_initial_call=True)
def rules(_o, _m, _a, _d, sel, field, pattern, action, auto, parent, name):
    nu = no_update
    trig = ctx.triggered_id
    if trig is None or not ctx.triggered[0]['value']:
        return (nu,) * 8
    pat_out, field_out, action_out = nu, nu, nu
    if trig == 'ib-mkrule' and sel:
        m = mailbox.get_mail(sel)
        from email.utils import parseaddr
        pat_out, field_out = parseaddr(m['sender'])[1] or m['sender'], 'sender'
        action_out = 'task' if m['kind'] == 'task' else 'ignore' if m['kind'] == 'ignore' else 'schedule'
    elif trig == 'ib-r-add':
        try:
            mailbox.save_rule({'field': field, 'pattern': pattern, 'action': action, 'auto': auto,
                               'parent_task_id': parent, 'name': name})
        except ValueError as e:
            return True, nu, nu, str(e), True, nu, nu, nu
        pat_out = ''
    elif isinstance(trig, dict):
        mailbox.delete_rule(trig['id'])
    return True, _rules_view(), _task_options(), '', False, pat_out, field_out, action_out
