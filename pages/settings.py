"""설정: 아침 요약 메일, 메일 분류·자동 처리. 용어집·요약 형식과 같은 진입점."""

import dash
import dash_bootstrap_components as dbc
from dash import Input, Output, State, callback, ctx, dash_table, dcc, html, no_update

from components import ui
from components.history_form import task_options
from services import auth, mail_categories, mailbox, morning_digest

dash.register_page(__name__, path='/settings', name='설정', title='업무 기록 · 설정')


def layout(tab='digest', sender='', **_):
    return html.Div(
        [
            ui.page_head('설정'),
            ui.settings_tabs('mail' if tab == 'mail' else 'digest'),
            mail_settings(sender) if tab == 'mail' else digest_settings(),
        ]
    )


def digest_settings():
    config = morning_digest.settings()
    return html.Div(
        [
            dbc.Card(
                dbc.CardBody(
                    [
                        html.H5('아침 요약 메일'),
                        html.P(
                            '지정 수신자에게 전체 업무의 전날 진행 상황과 오늘 마감·시작할 일, 지난 마감을 보냅니다. '
                            '기본은 매일 오전 7시, 한국 시간입니다.',
                            className='text-muted',
                        ),
                        dbc.Label('수신자 (쉼표로 구분)'),
                        dbc.Input(
                            id='dg-to', value=config['recipients'], className='mb-3'
                        ),
                        dbc.Row(
                            [
                                dbc.Col(
                                    [
                                        dbc.Label('발송 시간'),
                                        dbc.Input(
                                            id='dg-time',
                                            type='time',
                                            value=config['send_time'],
                                        ),
                                    ],
                                    md=4,
                                ),
                                dbc.Col(
                                    [
                                        dbc.Label('시간대'),
                                        dbc.Input(
                                            id='dg-zone', value=config['timezone']
                                        ),
                                    ],
                                    md=4,
                                ),
                                dbc.Col(
                                    [
                                        dbc.Checkbox(
                                            id='dg-weekdays',
                                            label='평일에만 발송',
                                            value=config['weekdays_only'],
                                        ),
                                        dbc.Switch(
                                            id='dg-enabled',
                                            label='예약 발송 켜기',
                                            value=config['enabled'],
                                        ),
                                    ],
                                    md=4,
                                ),
                            ],
                            className='g-3 mb-3',
                        ),
                        dbc.Label('앱 접속 주소 (메일의 원문 링크에 사용)'),
                        dbc.Input(
                            id='dg-url',
                            value=config['app_url'],
                            placeholder='http://업무서버:8502',
                            className='mb-3',
                        ),
                        html.Div(
                            [
                                dbc.Button('설정 저장', id='dg-save', color='primary'),
                                dbc.Button(
                                    '오늘 내용 미리보기',
                                    id='dg-preview',
                                    color='secondary',
                                    outline=True,
                                ),
                                dbc.Button(
                                    '미리본 내용을 메일로 작성',
                                    id='dg-compose',
                                    color='secondary',
                                    outline=True,
                                    disabled=True,
                                ),
                            ],
                            className='d-flex flex-wrap gap-2',
                        ),
                        dbc.Alert(
                            id='dg-message', is_open=False, className='mt-3 mb-0'
                        ),
                        html.P(
                            '서버가 계속 실행 중이어야 합니다. 예정 시각 이후 서버가 다시 켜지면 당일분을 발송합니다. '
                            'API 오류·중단으로 발송 여부가 불명확한 경우 중복 방지를 위해 자동 재시도하지 않습니다. '
                            '발송 내역과 실제 수신 여부를 확인한 뒤 미리보기에서 수동 작성할 수 있습니다.',
                            className='small text-muted mt-3 mb-0',
                        ),
                    ]
                ),
                className='mb-3',
            ),
            dcc.Store(id='dg-draft'),
            html.Div(id='dg-preview-view'),
            html.H5('최근 예약 발송 내역', className='mt-3'),
            html.Div(id='dg-history'),
            dcc.Interval(id='dg-refresh', interval=60000),
        ]
    )


def _config(to, at, zone, weekdays, enabled, url):
    return dict(
        recipients=to or '',
        send_time=at or '07:00',
        timezone=zone or 'Asia/Seoul',
        weekdays_only=bool(weekdays),
        enabled=bool(enabled),
        app_url=url or '',
    )


@callback(
    Output('dg-message', 'children'),
    Output('dg-message', 'is_open'),
    Output('dg-message', 'color'),
    Output('dg-preview-view', 'children'),
    Output('dg-draft', 'data'),
    Output('dg-compose', 'disabled'),
    Input('dg-save', 'n_clicks'),
    Input('dg-preview', 'n_clicks'),
    State('dg-to', 'value'),
    State('dg-time', 'value'),
    State('dg-zone', 'value'),
    State('dg-weekdays', 'value'),
    State('dg-enabled', 'value'),
    State('dg-url', 'value'),
    prevent_initial_call=True,
)
def digest_action(_s, _p, to, at, zone, weekdays, enabled, url):
    config = _config(to, at, zone, weekdays, enabled, url)
    try:
        if ctx.triggered_id == 'dg-save':
            morning_digest.save(config, auth.current_name())
            return (
                '설정을 저장했습니다.',
                True,
                'success',
                no_update,
                no_update,
                no_update,
            )
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(config['timezone'])
        except ZoneInfoNotFoundError:
            raise ValueError('시간대를 확인하세요.') from None
        draft = morning_digest.preview(config=config)
    except ValueError as error:
        return str(error), True, 'danger', no_update, no_update, no_update
    frame = html.Iframe(
        srcDoc=draft['table_html'],
        title='아침 요약 미리보기',
        sandbox='allow-popups allow-popups-to-escape-sandbox',
        className='digest-preview',
        style={
            'width': '100%',
            'height': '600px',
            'background': 'white',
            'border': '1px solid #e8e9ed',
        },
    )
    draft.update(
        to=to or '',
        text='',
        related_kind='digest_preview',
        related_id=draft['day'],
        attachments=[],
    )
    return (
        '미리보기입니다. 발송 설정은 저장 버튼으로 반영하세요.',
        True,
        'info',
        frame,
        draft,
        False,
    )


@callback(
    Output('mail-draft', 'data', allow_duplicate=True),
    Input('dg-compose', 'n_clicks'),
    State('dg-draft', 'data'),
    prevent_initial_call=True,
)
def compose_digest(_, draft):
    return draft or no_update


@callback(Output('dg-history', 'children'), Input('dg-refresh', 'n_intervals'))
def digest_history(_):
    rows = morning_digest.deliveries()
    labels = {
        'preparing': '준비 중 · 오래 지속되면 확인 필요',
        'sending': '발송 중 · 오래 지속되면 수신 확인',
        'sent': '발송 완료',
        'failed': '실패 · 수신 여부 확인 필요',
    }
    return (
        dbc.ListGroup(
            [
                dbc.ListGroupItem(
                    [
                        html.Strong(
                            r['day'] + ' · ' + labels.get(r['status'], r['status'])
                        ),
                        html.Div(r['recipients'], className='small text-muted'),
                        html.Div(r['error'], className='small text-danger')
                        if r['error']
                        else None,
                        dcc.Link('보낸 메일에서 확인', href='/outbox')
                        if r['outbound_id']
                        else None,
                    ]
                )
                for r in rows
            ]
        )
        if rows
        else dbc.Alert('아직 예약 발송 내역이 없습니다.', color='light')
    )


def mail_settings(sender=''):
    categories = mail_categories.list_categories()
    return html.Div(
        [
            dcc.Store(id='ms-revision', data=0),
            dbc.Alert(id='ms-message', is_open=False),
            html.H5('분류와 처리 방식'),
            html.P(
                '분류별 AI 처리 방식을 정하고, 아래에서 조건·분류·자동 반영 규칙을 한곳에서 관리합니다.',
                className='text-muted',
            ),
            dash_table.DataTable(
                id='ms-categories',
                data=categories,
                editable=True,
                row_deletable=True,
                columns=[
                    {'name': '분류', 'id': 'name'},
                    {'name': '설명', 'id': 'description'},
                    {'name': '처리', 'id': 'action', 'presentation': 'dropdown'},
                ],
                dropdown={
                    'action': {
                        'options': [
                            {'label': v, 'value': k}
                            for k, v in mail_categories.CATEGORY_ACTIONS.items()
                        ]
                    }
                },
                style_cell={
                    'textAlign': 'left',
                    'whiteSpace': 'normal',
                    'height': 'auto',
                    'fontFamily': 'inherit',
                },
                css=[
                    {
                        'selector': '.Select-menu-outer',
                        'rule': 'display:block!important',
                    }
                ],
            ),
            html.Div(
                [
                    dbc.Button(
                        '분류 추가', id='ms-cat-add', color='secondary', outline=True
                    ),
                    dbc.Button('분류 저장', id='ms-cat-save', color='primary'),
                    dbc.Button(
                        'AI 분류 다시',
                        id='ms-cat-redo',
                        color='secondary',
                        outline=True,
                    ),
                ],
                className='d-flex gap-2 my-3 flex-wrap',
            ),
            html.Hr(),
            html.H5('분류·자동 처리 규칙'),
            html.P(
                '분류와 처리 방식은 각각 위에서부터 처음 맞는 규칙을 적용합니다. 분류만 지정하거나 일정·업무 등의 처리와 자동 반영을 함께 지정할 수 있습니다.',
                className='small text-muted',
            ),
            dcc.Dropdown(
                id='ms-rule', options=[], value=0, clearable=False, className='mb-3'
            ),
            dcc.Store(id='ms-sender', data=sender),
            dbc.Row(
                [
                    dbc.Col(
                        [
                            dbc.Label('조건 항목'),
                            dbc.Select(
                                id='ms-field',
                                options=[
                                    {'label': v, 'value': k}
                                    for k, v in mailbox.FIELDS.items()
                                ],
                            ),
                        ],
                        md=3,
                    ),
                    dbc.Col(
                        [dbc.Label('포함할 글자'), dbc.Input(id='ms-pattern')], md=5
                    ),
                    dbc.Col(
                        [
                            dbc.Label('처리'),
                            dbc.Select(
                                id='ms-action',
                                options=[
                                    {'label': v, 'value': k}
                                    for k, v in mailbox.ACTIONS.items()
                                ],
                            ),
                        ],
                        md=4,
                    ),
                    dbc.Col([dbc.Label('분류'), dbc.Select(id='ms-category')], md=4),
                    dbc.Col(
                        [
                            dbc.Label('상위 업무'),
                            dcc.Dropdown(id='ms-parent', options=task_options()),
                        ],
                        md=5,
                    ),
                    dbc.Col([dbc.Label('규칙 이름'), dbc.Input(id='ms-name')], md=3),
                    dbc.Col(
                        dbc.Checkbox(id='ms-auto', label='확인 없이 자동 반영'), md=4
                    ),
                    dbc.Col(
                        dbc.Checkbox(id='ms-active', label='이 규칙 사용', value=True),
                        md=4,
                    ),
                ],
                className='g-3',
            ),
            html.Div(
                [
                    dbc.Button('규칙 저장', id='ms-save', color='primary'),
                    dbc.Button(
                        '규칙 삭제', id='ms-delete', color='danger', outline=True
                    ),
                ],
                className='d-flex gap-2 mt-3',
            ),
            html.Div(id='ms-rules', className='mt-3'),
        ]
    )


@callback(
    Output('ms-rule', 'options'),
    Output('ms-category', 'options'),
    Output('ms-rules', 'children'),
    Input('ms-revision', 'data'),
)
def rule_choices(_):
    rules = mailbox.list_rules()
    return (
        [{'label': '새 규칙', 'value': 0}]
        + [{'label': r['name'], 'value': r['id']} for r in rules],
        [{'label': '분류 지정 안 함', 'value': ''}]
        + [
            {'label': c['name'], 'value': str(c['id'])}
            for c in mail_categories.list_categories()
        ],
        dbc.ListGroup(
            [
                dbc.ListGroupItem(
                    f'{i + 1}. {r["name"]} · {mailbox.ACTIONS[r["action"]]} · '
                    + ('자동' if r['auto'] else '확인 후 반영')
                    + ('' if r['active'] else ' · 사용 중지')
                )
                for i, r in enumerate(rules)
            ]
        ),
    )


@callback(
    *[
        Output('ms-' + name, 'value')
        for name in (
            'field',
            'pattern',
            'action',
            'category',
            'parent',
            'name',
            'auto',
            'active',
        )
    ],
    Input('ms-rule', 'value'),
    State('ms-sender', 'data'),
)
def rule_fields(rid, sender):
    row = next((r for r in mailbox.list_rules() if r['id'] == rid), None)
    if not row:
        return 'sender', sender or '', 'classify', '', None, '', False, True
    return (
        row['field'],
        row['pattern'],
        row['action'],
        str(row['category_id'] or ''),
        row['parent_task_id'],
        row['name'],
        row['auto'],
        row['active'],
    )


@callback(
    Output('ms-message', 'children'),
    Output('ms-message', 'is_open'),
    Output('ms-message', 'color'),
    Output('ms-revision', 'data'),
    Output('ms-categories', 'data'),
    Output('ms-rule', 'value'),
    Output('mail-changed', 'data', allow_duplicate=True),
    Input('ms-save', 'n_clicks'),
    Input('ms-delete', 'n_clicks'),
    Input('ms-cat-add', 'n_clicks'),
    Input('ms-cat-save', 'n_clicks'),
    Input('ms-cat-redo', 'n_clicks'),
    State('ms-rule', 'value'),
    *[
        State('ms-' + name, 'value')
        for name in (
            'field',
            'pattern',
            'action',
            'category',
            'parent',
            'name',
            'auto',
            'active',
        )
    ],
    State('ms-categories', 'data'),
    State('ms-revision', 'data'),
    State('mail-changed', 'data'),
    prevent_initial_call=True,
)
def save_mail(
    _s,
    _d,
    _a,
    _c,
    _r,
    rid,
    field,
    pattern,
    action,
    category,
    parent,
    name,
    auto,
    active,
    rows,
    revision,
    changed,
):
    nu = no_update
    try:
        trigger = ctx.triggered_id
        if trigger == 'ms-cat-add':
            return (
                nu,
                False,
                nu,
                nu,
                (rows or []) + [{'name': '', 'description': '', 'action': ''}],
                nu,
                nu,
            )
        if trigger == 'ms-cat-save':
            mail_categories.save_all(rows or [])
        elif trigger == 'ms-cat-redo':
            mail_categories.reset_auto()
        elif trigger == 'ms-delete':
            if not rid:
                raise ValueError('삭제할 규칙을 선택하세요.')
            mailbox.delete_rule(rid)
        elif trigger == 'ms-save':
            mailbox.save_rule(
                dict(
                    field=field,
                    pattern=pattern,
                    action=action,
                    category_id=category,
                    parent_task_id=parent,
                    name=name,
                    auto=auto,
                    active=active,
                ),
                rid or None,
            )
            if category:
                mail_categories.apply_rules()
    except ValueError as error:
        return str(error), True, 'danger', nu, nu, nu, nu
    return (
        '반영했습니다.',
        True,
        'success',
        (revision or 0) + 1,
        mail_categories.list_categories(),
        0,
        (changed or 0) + 1,
    )
