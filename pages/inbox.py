"""받은 메일 — POP3 로 가져온 메일의 처리 결과(자동 처리)와 추천(확인 필요), 분류 규칙.
추천 항목(업무 후보·원장님 일정·임원 부재·임원 정보 변경)은 항목마다 따로 처리하고, 다 봤으면 '처리 완료'."""
import dash
import dash_bootstrap_components as dbc
from dash import ALL, Input, Output, State, callback, ctx, dash_table, dcc, html, no_update

from components import absence_ui, doc_ui, ui
from services import absences, auth, doc_versions, llm, mailbox, outbox, schedules, store

dash.register_page(__name__, path='/inbox', name='받은 메일', title='업무 기록 · 받은 메일')

KIND = {'schedule': ('원장님 일정', 'primary'), 'task': ('업무', 'success'), 'both': ('일정+업무', 'info'),
        'absence': ('임원 부재', 'warning'), 'ignore': ('무시', 'secondary')}
STATUS_COLOR = {'auto': 'success', 'review': 'warning', 'done': 'primary', 'ignored': 'secondary', 'failed': 'danger'}
FILTERS = [('review', '확인 필요'), ('auto', '자동 처리'), ('done', '처리 완료'), ('ignored', '무시'), ('all', '전체')]


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
            dcc.Link(dbc.Button([html.I(className='bi bi-send me-1'), '보낸 메일'], size='sm', color='secondary',
                                outline=True, className='me-2'), href='/outbox'),
            dbc.Button([html.I(className='bi bi-funnel me-1'), '분류 규칙'], id='ib-rules-open', size='sm',
                       color='secondary', outline=True),
        ], className='d-flex align-items-center flex-wrap gap-1 mb-3'),
        dcc.Loading(dbc.Alert(id='ib-msg', is_open=False, dismissable=True, className='py-2 small'), type='dot'),
        dbc.Row([
            dbc.Col(html.Div(id='ib-list'), lg=5),
            dbc.Col(html.Div([
                html.Div(id='ib-head'),
                html.Div(id='ib-ai-box', style={'display': 'none'}, children=[
                    dbc.Card(dbc.CardBody([
                        html.Div([html.I(className='bi bi-stars me-1 text-warning'), 'AI 에게 맡기기'],
                                 className='fw-semibold small mb-1'),
                        html.Div('이 메일로 무엇을 할지 고르세요. 추천 표시는 AI 분류와 본문 모양을 보고 미리 골라 둔 것입니다. '
                                 '결과는 아래에 항목별로 나오고, 항목마다 확인한 뒤 등록합니다.', className='small text-muted mb-2'),
                        dbc.Checklist(id='ib-ai-actions', options=[], value=[], className='small'),
                        html.Div([
                            dbc.Button([html.I(className='bi bi-magic me-1'), 'AI 분석 실행'], id='ib-ai-run',
                                       color='primary', size='sm', className='me-2'),
                            dcc.Loading(html.Span(id='ib-ai-busy', className='small text-muted'), type='dot'),
                        ], className='d-flex align-items-center mt-2'),
                        html.Div(id='ib-ai-summary', className='mt-2'),
                    ]), className='mt-2'),
                ]),
                html.Div(id='ib-attach'),
                html.Div(id='ib-link-box', style={'display': 'none'}, children=[
                    dbc.Card(dbc.CardBody([
                        html.Div([html.I(className='bi bi-link-45deg me-1'), '이 메일과 연결된 업무'],
                                 className='fw-semibold small mb-1'),
                        html.Div(id='ib-linked', className='mb-2'),
                        dbc.Row([
                            dbc.Col(dcc.Dropdown(id='ib-link-task', placeholder='기존 업무에 연결 — 업무 검색'), md=6),
                            dbc.Col(dbc.Button('연결', id='ib-link', color='secondary', outline=True,
                                               className='w-100'), md=2),
                            dbc.Col(dbc.Button('연결하고 처리 완료', id='ib-link-done', color='secondary', outline=True,
                                               className='w-100', title='업무 이력에 연결만 하고 이 메일은 끝낼 때'),
                                    md=4),
                        ], className='g-2 mb-2'),
                        html.Details([
                            html.Summary('이 메일로 새 업무 만들기', className='small'),
                            html.Div([
                                dbc.Input(id='ib-new-title', placeholder='업무 이름 (비우면 메일 제목)', className='mb-2'),
                                dbc.Row([
                                    dbc.Col(dcc.Dropdown(id='ib-new-parent', placeholder='상위 업무 (선택)'), md=6),
                                    dbc.Col(dbc.Input(id='ib-new-due', type='date'), md=3),
                                    dbc.Col(dbc.Button('만들기', id='ib-new-make', color='primary', className='w-100'),
                                            md=3),
                                ], className='g-2'),
                            ], className='mt-2'),
                        ], id='ib-new-box'),
                        html.Div('연결하거나 만든 업무에는 메일 원문이 "요청" 기록으로 남고, 업무 화면의 출처에 이 메일이 보입니다.',
                                 className='small text-muted mt-2'),
                        html.Hr(className='my-2'),
                        html.Div([html.I(className='bi bi-binoculars me-1'), '임원 동향으로 기록',
                                  html.Span(id='ib-trend-done', className='ms-2')], className='fw-semibold small mb-1'),
                        dbc.Row([
                            dbc.Col(dcc.Dropdown(id='ib-trend-people', multi=True,
                                                 placeholder='임원 (메일에 이름·별칭이 나오면 미리 골라 둠)'), md=8),
                            dbc.Col(dbc.Button('동향으로 기록', id='ib-trend', color='secondary', outline=True,
                                               className='w-100'), md=4),
                        ], className='g-2'),
                        html.Div('고른 임원의 기록(임원 화면)에 메일 원문이 "동향"으로 남습니다. 위에서 연결한 업무가 있으면 '
                                 '그 업무에도 걸립니다.', className='small text-muted mt-1'),
                    ]), className='mt-2'),
                ]),
                html.Div(id='ib-task-box', style={'display': 'none'}, children=[
                    html.H6([html.I(className='bi bi-check2-square me-1'), '업무 후보',
                             html.Span(id='ib-task-done', className='ms-2')], className='mt-3'),
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
                    html.Div([
                        dbc.Button([html.I(className='bi bi-check2 me-1'), '업무로 등록'], id='ib-task-create',
                                   color='primary', size='sm', className='me-2'),
                        html.Span('AI 가 고른 업무입니다 — 칸을 눌러 이름·기한·담당을 바로 고친 뒤 등록하세요. 만들지 않을 줄은 ×, '
                                  '빠진 업무는 "줄 추가". 메일 원문은 만든 업무마다 "요청" 기록으로 남습니다.',
                                  className='small text-muted'),
                    ], className='mt-1 d-flex align-items-center flex-wrap'),
                ]),
                html.Div(id='ib-sched-box', style={'display': 'none'}, children=[
                    html.H6([html.I(className='bi bi-calendar-event me-1'), '원장님 일정',
                             html.Span(id='ib-sched-done', className='ms-2')], className='mt-3'),
                    html.Div(id='ib-sched-note', className='small text-muted mb-1'),
                    dash_table.DataTable(id='ib-sched', editable=True, row_deletable=True, data=[],
                                         columns=[{'name': '상태', 'id': 'state_label', 'editable': False},
                                                  {'name': '날짜', 'id': 'date'}, {'name': '시작', 'id': 'start_time'},
                                                  {'name': '종료', 'id': 'end_time'}, {'name': '일정', 'id': 'title'},
                                                  {'name': '장소', 'id': 'location'},
                                                  {'name': '참석자', 'id': 'attendees'}, {'name': '세부', 'id': 'details'},
                                                  {'name': '검증', 'id': 'note', 'editable': False}],
                                         style_table={'overflowX': 'auto'}, style_cell={**cell, 'fontSize': '0.8rem'}),
                    dbc.Button([html.I(className='bi bi-check2 me-1'), '일정에 반영'], id='ib-sched-apply',
                               color='primary', size='sm', className='mt-1'),
                ]),
                html.Div(id='ib-abs-box', style={'display': 'none'}, children=[
                    html.H6([html.I(className='bi bi-person-dash me-1'), '임원 부재',
                             html.Span(id='ib-abs-done', className='ms-2')], className='mt-3'),
                    html.Div(id='ib-abs-note', className='small text-muted mb-1'),
                    dash_table.DataTable(id='ib-abs', editable=True, row_deletable=True, data=[],
                                         columns=absence_ui.TABLE_COLUMNS,
                                         style_table={'overflowX': 'auto'}, style_cell={**cell, 'fontSize': '0.8rem'},
                                         style_data_conditional=[
                                             {'if': {'filter_query': '{state} = "unknown"'},
                                              'backgroundColor': 'rgba(220,53,69,.08)'},
                                             {'if': {'filter_query': '{state} = "overlap"'},
                                              'backgroundColor': 'rgba(255,193,7,.12)'},
                                             {'if': {'filter_query': '{state} = "same"'}, 'color': '#888'}]),
                    html.Div([
                        dbc.Button([html.I(className='bi bi-check2 me-1'), '부재 일정에 등록'], id='ib-abs-apply',
                                   color='primary', size='sm', className='me-2'),
                        html.Span(f"종류: {', '.join(absences.KINDS)} · 시간대는 선택(예: 09:00~12:00, 오후) · 임원 이름을 고치면 다시 찾습니다. "
                                  '이미 있는 부재와 임원을 못 찾은 줄은 건너뜁니다.', className='small text-muted'),
                    ], className='mt-1 d-flex align-items-center flex-wrap'),
                ]),
                html.Div(id='ib-person-box', style={'display': 'none'}, children=[
                    html.H6([html.I(className='bi bi-person-gear me-1'), '임원 정보 변경',
                             html.Span(id='ib-person-done', className='ms-2')], className='mt-3'),
                    dash_table.DataTable(id='ib-person', editable=True, row_deletable=True, data=[],
                                         columns=[{'name': '상태', 'id': 'state_label', 'editable': False},
                                                  {'name': '임원', 'id': 'person'}, {'name': '항목', 'id': 'field'},
                                                  {'name': '지금 값', 'id': 'current', 'editable': False},
                                                  {'name': '새 값', 'id': 'value'}, {'name': '근거', 'id': 'reason'}],
                                         style_table={'overflowX': 'auto'}, style_cell={**cell, 'fontSize': '0.8rem'},
                                         style_data_conditional=[
                                             {'if': {'filter_query': '{state} = "unknown"'},
                                              'backgroundColor': 'rgba(220,53,69,.08)'},
                                             {'if': {'filter_query': '{state} = "same"'}, 'color': '#888'}]),
                    html.Div([
                        dbc.Button([html.I(className='bi bi-check2 me-1'), '임원 정보에 반영'], id='ib-person-apply',
                                   color='primary', size='sm', className='me-2'),
                        dbc.Button([html.I(className='bi bi-plus me-1'), '줄 추가'], id='ib-person-add', size='sm',
                                   color='secondary', outline=True, className='me-2'),
                        html.Span('항목: 직위·소속은 바꾸고, 별칭은 더하고, 메모는 날짜와 함께 덧붙입니다. 그 밖의 이름은 '
                                  '임원의 "추가 항목"으로. 반영하지 않을 줄은 × 로 지우세요.', className='small text-muted'),
                    ], className='mt-1 d-flex align-items-center flex-wrap'),
                ]),
                html.Div(id='ib-pending', className='small mt-3'),
                html.Div(id='ib-actions', className='mt-2', style={'display': 'none'}, children=[
                    dbc.Button([html.I(className='bi bi-check2-all me-1'), '처리 완료'], id='ib-complete',
                               color='primary', className='me-2',
                               title='이 메일은 다 봤음 — 확인 필요 목록과 알림 숫자에서 빠집니다'),
                    dbc.Button('무시', id='ib-ignore', color='secondary', outline=True, className='me-2'),
                    dbc.Button('다시 확인 필요로', id='ib-reopen', color='secondary', outline=True, className='me-2'),
                    dbc.Button([html.I(className='bi bi-reply me-1'), '회신'], id='ib-reply', color='secondary',
                               outline=True, className='me-2'),
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
          Output('ib-complete', 'style'), Output('ib-ignore', 'style'), Output('ib-reopen', 'style'),
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
        html.Div(['연결된 업무: ', *links], className='small mt-1') if links else None,
        html.Div('이 메일로 이미 등록한 업무는 그대로 두고 새 제목의 업무만 추가합니다. 기존 업무 수정은 업무 화면에서 합니다.',
                 className='small text-muted mt-1') if links and open_ else None,
        html.Div(f"처리: {m['handled_by']} {m['handled_at']}", className='small text-muted mt-1')
        if m['handled_at'] else None,
        dbc.Alert(m['error'], color='danger', className='small py-1 mt-2 mb-0') if m['error'] else None,
    ]))
    wants_task = m['kind'] in ('task', 'both') or not m['kind'] or m['suggestion'].get('requested_tasks')
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
    return (head, show if wants_task and (open_ or tasks) else hide, tasks, _task_options(), parent,
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
          Input('ib-complete', 'n_clicks'), Input('ib-ignore', 'n_clicks'), Input('ib-reopen', 'n_clicks'),
          Input('ib-fetch', 'n_clicks'),
          State('ib-sel', 'data'), State('ib-refresh', 'data'), prevent_initial_call=True)
def act(_a, _i, _r, _f, sel, n):
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
    m = mailbox.get_mail(sel)
    left = mailbox.pending_items(m) if m else []
    mailbox.complete(sel, by)
    return ((n or 0) + 1, '처리 완료했습니다.' + (f" (처리하지 않고 둔 추천: {', '.join(left)})" if left else ''),
            True, 'success')


# ── 업무 직접 연결·만들기 ─────────────────────────────────────────────

@callback(Output('ib-link-box', 'style'), Output('ib-linked', 'children'), Output('ib-link-task', 'options'),
          Output('ib-link-task', 'value'), Output('ib-new-title', 'value'), Output('ib-new-parent', 'options'),
          Output('ib-new-parent', 'value'), Output('ib-new-due', 'value'), Output('ib-attach', 'children'),
          Input('ib-sel', 'data'), Input('ib-refresh', 'data'))
def render_links(sel, _):
    m = mailbox.get_mail(sel) if sel else None
    if m is None:
        return {'display': 'none'}, None, [], None, '', [], None, None, None
    tasks = {t['id']: t for t in store.list_tasks()}
    linked = [tasks[t] for t in m['task_ids'] if t in tasks]
    view = html.Div([
        html.Div([
            ui.status_badge(t['status']), ui.task_link(t), html.Span(className='me-auto'),
            dbc.Button(html.I(className='bi bi-x'), id={'type': 'ib-unlink', 'id': t['id']}, color='link', size='sm',
                       className='text-muted p-0', title='연결 끊기 (업무와 기록은 그대로)'),
        ], className='d-flex align-items-center gap-2 small py-1') for t in linked
    ]) if linked else html.Div('아직 연결된 업무가 없습니다.', className='small text-muted')
    opts = [{'label': f"#{t['id']} {t['title']}" + (' (완료)' if t['status'] == '완료' else ''), 'value': t['id']}
            for t in sorted(tasks.values(), key=lambda t: (t['status'] == '완료', -t['id']))
            if t['id'] not in m['task_ids']]
    parent_opts = [o for o in opts if '(완료)' not in o['label']]
    # 상위 업무 기본값: 받을 때 추천한 상위 업무, 없으면 같은 메일 스레드의 업무 (AI 는 부르지 않음 — 화면이 느려짐)
    suggested = m['suggestion'].get('parent_task_id') or mailbox._thread_parent(m['subject'], m['id'])[0]
    docs = doc_versions.for_mails([m['id']])
    attach = dbc.Card(dbc.CardBody([
        html.Div([html.I(className='bi bi-paperclip me-1'), f'첨부 파일 {sum(len(g["files"]) for g in docs)}개'],
                 className='fw-semibold small mb-2'),
        doc_ui.doc_groups(docs, current_mail=m['id']),
        html.Div('파일 이름의 날짜·_r1·_v2·최종 같은 꼬리표를 떼고 같은 문서끼리 묶어, 메일 날짜 순으로 판을 매깁니다.',
                 className='small text-muted mt-2'),
    ]), className='mt-2') if docs else None
    return ({}, view, opts, None, mailbox.clean_subject(m['subject']), parent_opts,
            suggested if suggested in tasks else None, None, attach)


@callback(Output('ib-refresh', 'data', allow_duplicate=True), Output('ib-msg', 'children', allow_duplicate=True),
          Output('ib-msg', 'is_open', allow_duplicate=True), Output('ib-msg', 'color', allow_duplicate=True),
          Input('ib-link', 'n_clicks'), Input('ib-link-done', 'n_clicks'), Input('ib-new-make', 'n_clicks'),
          Input({'type': 'ib-unlink', 'id': ALL}, 'n_clicks'),
          State('ib-sel', 'data'), State('ib-link-task', 'value'), State('ib-new-title', 'value'),
          State('ib-new-parent', 'value'), State('ib-new-due', 'value'), State('ib-refresh', 'data'),
          prevent_initial_call=True)
def link_actions(_l, _ld, _m, _u, sel, task_id, title, parent, due, n):
    trig = ctx.triggered_id
    if not sel or not ctx.triggered or not ctx.triggered[0]['value']:
        return no_update, no_update, no_update, no_update
    by = auth.current_name()
    try:
        if isinstance(trig, dict):
            mailbox.unlink_task(sel, trig['id'])
            return (n or 0) + 1, f"#{trig['id']} 업무와 연결을 끊었습니다 (업무와 기록은 그대로).", True, 'secondary'
        if trig in ('ib-link', 'ib-link-done'):
            if not task_id:
                return no_update, '연결할 업무를 고르세요.', True, 'warning'
            done = mailbox.link_task(sel, int(task_id), by)
            if trig == 'ib-link-done':  # 업무 이력에 연결만 하고 이 메일은 끝
                mailbox.complete(sel, by)
                return ((n or 0) + 1, ['업무에 연결하고 처리 완료했습니다. ',
                                       dcc.Link('업무 열기', href=f'/task/{task_id}')], True, 'success')
            return ((n or 0) + 1, ['연결했습니다 — 메일 원문을 업무 기록으로 남겼습니다. ',
                                   dcc.Link('업무 열기', href=f'/task/{task_id}')] if done else '이미 연결된 업무입니다.',
                    True, 'success' if done else 'info')
        if trig == 'ib-new-make':
            tid = mailbox.create_task_from_mail(sel, title or '', int(parent) if parent else None, by, due or '')
            return (n or 0) + 1, ['업무를 만들었습니다. ', dcc.Link(f'#{tid} 열기', href=f'/task/{tid}')], True, 'success'
    except ValueError as e:
        return no_update, str(e), True, 'danger'
    return no_update, no_update, no_update, no_update


# ── 임원 부재 ─────────────────────────────────────────────────────────

@callback(Output('ib-abs-box', 'style'), Output('ib-abs', 'data'), Output('ib-abs-note', 'children'),
          Input('ib-sel', 'data'), Input('ib-refresh', 'data'))
def render_abs(sel, _):
    m = mailbox.get_mail(sel) if sel else None
    if m is None or ('absences' not in m['suggestion'] and m['kind'] != 'absence'):
        return {'display': 'none'}, [], ''
    rows = m['suggestion'].get('absences') or []
    note = m['suggestion'].get('absence_note', '')
    if not rows:
        note = (note + ' ' if note else '') + '부재를 찾지 못했습니다 — 아래 표에 직접 적어도 됩니다.'
    return {}, absence_ui.table_rows(rows) or [absence_ui.blank_row(m['mail_date'])], note


@callback(Output('ib-refresh', 'data', allow_duplicate=True), Output('ib-msg', 'children', allow_duplicate=True),
          Output('ib-msg', 'is_open', allow_duplicate=True), Output('ib-msg', 'color', allow_duplicate=True),
          Input('ib-abs-apply', 'n_clicks'),
          State('ib-sel', 'data'), State('ib-abs', 'data'), State('ib-refresh', 'data'), prevent_initial_call=True)
def abs_actions(_a, sel, rows, n):
    if not sel or not ctx.triggered or not ctx.triggered[0]['value']:
        return no_update, no_update, no_update, no_update
    r = mailbox.apply_absences(sel, rows or [], auth.current_name())
    msg = [f"임원 부재 {r['added']}건 등록" + (f", 이미 있음 {r['same']}건" if r['same'] else '')
           + (f", 임원을 못 찾아 건너뜀 {r['unknown']}건" if r['unknown'] else '') + '. ',
           dcc.Link('부재 달력 보기', href='/calendar?tab=absence')]
    return (n or 0) + 1, msg, True, 'success' if r['added'] else 'secondary'


# ── 항목별 처리 ──────────────────────────────────────────────────────

def _done_badge(d: dict | None):
    if not d:
        return None
    return dbc.Badge(f"처리됨 {d['n']}건 · {d['at'][5:]}", color='success', className='fw-normal')


@callback(Output('ib-task-done', 'children'), Output('ib-sched-done', 'children'), Output('ib-abs-done', 'children'),
          Output('ib-person-done', 'children'), Output('ib-trend-done', 'children'),
          Output('ib-person-box', 'style'), Output('ib-person', 'data'),
          Output('ib-trend-people', 'options'), Output('ib-trend-people', 'value'), Output('ib-pending', 'children'),
          Input('ib-sel', 'data'), Input('ib-refresh', 'data'))
def render_items(sel, _):
    """항목마다 처리했는지(처리됨 n건), 임원 정보 변경 표, 동향 임원 기본값, 아직 처리하지 않은 추천."""
    m = mailbox.get_mail(sel) if sel else None
    if m is None:
        return (None,) * 5 + ({'display': 'none'}, [], [], [], None)
    sug = m['suggestion']
    done = sug.get('done') or {}
    rows = [{**r, 'state_label': {'new': '변경', 'same': '같음', 'unknown': '임원 모름'}[r['state']]}
            for r in mailbox.person_update_rows(sug.get('person_updates') or [])]
    show_person = 'person_updates' in sug
    people = [{'label': f"{p['name']} {p['position']}".strip(), 'value': p['id']}
              for p in store.list_people(order='rank')]
    detected = store.detect_people(f"{m['subject']}\n{m['body']}")
    left = mailbox.pending_items(m) if m['status'] in ('review', 'failed') else []
    pending = html.Div([html.I(className='bi bi-hourglass-split me-1'), '아직 처리하지 않은 추천: ',
                        html.B(', '.join(left)), ' — 필요 없으면 그대로 두고 처리 완료해도 됩니다.'],
                       className='text-muted') if left else None
    return (_done_badge(done.get('tasks')), _done_badge(done.get('schedule')), _done_badge(done.get('absence')),
            _done_badge(done.get('person')), _done_badge(done.get('trend')),
            {} if show_person else {'display': 'none'}, rows or [_blank_person()], people, detected, pending)


def _blank_person() -> dict:
    return {'person': '', 'field': '메모', 'current': '', 'value': '', 'reason': '', 'state': 'unknown',
            'state_label': ''}


@callback(Output('ib-person', 'data', allow_duplicate=True), Input('ib-person-add', 'n_clicks'),
          State('ib-person', 'data'), prevent_initial_call=True)
def add_person_row(n, rows):
    return (rows or []) + [_blank_person()] if n else no_update


@callback(Output('ib-refresh', 'data', allow_duplicate=True), Output('ib-msg', 'children', allow_duplicate=True),
          Output('ib-msg', 'is_open', allow_duplicate=True), Output('ib-msg', 'color', allow_duplicate=True),
          Input('ib-task-create', 'n_clicks'), Input('ib-sched-apply', 'n_clicks'),
          Input('ib-person-apply', 'n_clicks'), Input('ib-trend', 'n_clicks'),
          State('ib-sel', 'data'), State('ib-tasks', 'data'), State('ib-parent', 'value'), State('ib-sched', 'data'),
          State('ib-person', 'data'), State('ib-trend-people', 'value'), State('ib-refresh', 'data'),
          prevent_initial_call=True)
def item_actions(_t, _s, _p, _d, sel, tasks, parent, sched, person_rows, trend_people, n):
    """추천 항목 하나씩 처리 — 메일 상태는 그대로 두고, 다 됐으면 '처리 완료'."""
    if not sel or not ctx.triggered or not ctx.triggered[0]['value']:
        return no_update, no_update, no_update, no_update
    by, trig = auth.current_name(), ctx.triggered_id
    try:
        if trig == 'ib-task-create':
            ids = mailbox.add_tasks(sel, tasks or [], int(parent) if parent else None, by)
            msg = ['업무 ', *[dcc.Link(f'#{i} ', href=f'/task/{i}') for i in ids], f'{len(ids)}건을 등록했습니다.']
        elif trig == 'ib-sched-apply':
            r = mailbox.apply_schedule(sel, sched or [], by)
            msg = f"원장님 일정 — 새 일정 {r['added']}건, 바뀐 일정 {r['updated']}건, 이미 있음 {r['same']}건"
        elif trig == 'ib-person-apply':
            r = mailbox.apply_person_updates(sel, person_rows or [], by)
            msg = f"임원 정보 {r['changed']}건 반영" + (f" (건너뜀 {r['skipped']}건 — 같은 값·임원 모름)" if r['skipped'] else '')
        else:
            m = mailbox.get_mail(sel)
            h = mailbox.record_trend(sel, trend_people or [], by, task_id=(m['task_ids'] or [None])[0])
            msg = ['임원 동향으로 기록했습니다 — ',
                   *[dcc.Link(p['name'] + ' ', href=f"/people/{p['id']}") for p in h['people']]]
    except ValueError as e:
        return no_update, str(e), True, 'warning'
    return (n or 0) + 1, msg, True, 'success'


@callback(Output('mail-changed', 'data', allow_duplicate=True), Input('ib-refresh', 'data'),
          State('mail-changed', 'data'), prevent_initial_call=True)
def notify_sidebar(_, n):
    """사이드바의 '확인 필요' 숫자를 다시 세게 한다 (app.py)."""
    return (n or 0) + 1


# ── AI 에게 맡기기 ────────────────────────────────────────────────────

@callback(Output('ib-ai-box', 'style'), Output('ib-ai-actions', 'options'), Output('ib-ai-actions', 'value'),
          Output('ib-ai-summary', 'children'), Input('ib-sel', 'data'), Input('ib-refresh', 'data'))
def render_ai(sel, _):
    m = mailbox.get_mail(sel) if sel else None
    if m is None:
        return {'display': 'none'}, [], [], None
    rec = dict(mailbox.recommend_actions(m))
    asked = m['suggestion'].get('requested') or []
    ai_on = llm.is_configured()
    opts = []
    for key, label in mailbox.AI_ACTIONS.items():
        off = key == 'summary' and not ai_on
        opts.append({'label': html.Span([
            label,
            dbc.Badge('추천', color='warning', text_color='dark', className='ms-2') if key in rec else None,
            dbc.Badge('실행함', color='light', text_color='dark', className='ms-1 border') if key in asked else None,
            html.Span(f' — {rec[key]}', className='text-muted') if key in rec else None,
            html.Span(' (LLM 미설정)', className='text-muted') if off else None,
        ]), 'value': key, 'disabled': off})
    value = [k for k in rec if not (k == 'summary' and not ai_on)] if not asked else []
    sm = m['suggestion'].get('summary')
    summary = dbc.Alert([
        html.Div(sm['title'], className='fw-semibold'),
        html.Div(sm['summary'], style={'whiteSpace': 'pre-wrap'}),
        html.Div([dbc.Badge(f'#{k}', color='light', text_color='dark', className='me-1 border')
                  for k in sm.get('keywords', [])], className='mt-1') if sm.get('keywords') else None,
    ], color='light', className='small mb-0') if sm else None
    return {}, opts, value, summary


@callback(Output('ib-refresh', 'data', allow_duplicate=True), Output('ib-msg', 'children', allow_duplicate=True),
          Output('ib-msg', 'is_open', allow_duplicate=True), Output('ib-msg', 'color', allow_duplicate=True),
          Output('ib-ai-busy', 'children'),
          Input('ib-ai-run', 'n_clicks'), State('ib-sel', 'data'), State('ib-ai-actions', 'value'),
          State('ib-refresh', 'data'), prevent_initial_call=True)
def run_ai(n, sel, actions, refresh):
    """고른 분석을 돌린다 — 일정 표·부재 표·업무 후보·요약이 아래에 추천으로 나온다."""
    if not n or not sel:
        return no_update, no_update, no_update, no_update, ''
    try:
        done = mailbox.run_actions(sel, actions or [], auth.current_name())
    except (ValueError, llm.LLMError) as e:
        return no_update, str(e), True, 'warning', ''
    parts = ['요약 완료' if k == 'summary' else f'{mailbox.AI_ACTIONS[k]} {v}건' for k, v in done.items()]
    return (refresh or 0) + 1, '분석했습니다 — ' + ', '.join(parts) + '. 아래 항목마다 확인하고 등록하세요.', True, 'success', ''


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


@callback(Output('mail-draft', 'data', allow_duplicate=True), Input('ib-reply', 'n_clicks'), State('ib-sel', 'data'),
          prevent_initial_call=True)
def reply(n, sel):
    """회신 — 받는 사람은 보낸 사람, 원문 인용, 원문 첨부를 골라 다시 보낼 수 있다."""
    if not n or not sel:
        return no_update
    return outbox.draft_reply(sel)
