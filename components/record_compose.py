"""공통 기록 작성 창. 업무·사람·자료에서 시작해도 같은 폼과 초안을 사용한다."""

import dash_bootstrap_components as dbc
from dash import ALL, Input, Output, State, callback, ctx, dcc, html, no_update

from components import drafts as draft_ui
from components.history_form import (
    decode_text_upload,
    history_form,
    register_form_callbacks,
    save_history,
    task_options,
    people_options,
)
from services import auth, drafts, store

FIELDS = ('content', 'type', 'when', 'people', 'links', 'ai', 'tasks', 'refs')


def layout():
    return html.Div([_modal(), dbc.Toast(
        id='rc-toast', header='기록 저장', icon='success', is_open=False, dismissable=True,
        duration=6000, style={'position': 'fixed', 'bottom': 16, 'right': 16, 'zIndex': 1080})])


def _modal():
    return dbc.Modal(
        [
            dbc.ModalHeader(dbc.ModalTitle('기록 추가')),
            dbc.ModalBody(
                [
                    html.P(id='rc-context-note', className='small text-muted'),
                    html.Details(
                        [
                            html.Summary(
                                '이전 화면에 저장한 초안', className='small text-muted'
                            ),
                            dcc.Dropdown(id='rc-legacy'),
                            dbc.Button(
                                '불러오기 (현재 입력 대체)',
                                id='rc-legacy-load',
                                color='link',
                                size='sm',
                            ),
                            dbc.Button(
                                '선택한 이전 초안 삭제',
                                id='rc-legacy-delete',
                                color='link',
                                size='sm',
                            ),
                            html.Div(id='rc-legacy-message', className='small'),
                        ],
                        className='mb-3',
                    ),
                    dbc.Label('관련 업무'),
                    dcc.Dropdown(
                        id='rc-tasks',
                        options=task_options(),
                        multi=True,
                        className='mb-2',
                    ),
                    dbc.Label('관련 자료'),
                    dcc.Dropdown(
                        id='rc-refs',
                        options=reference_options(),
                        multi=True,
                        className='mb-3',
                        placeholder='자료 선택 · 자료에서 기록을 시작하면 기본 선택됩니다.',
                    ),
                    *history_form('rc', draft_context='global-record'),
                ]
            ),
            dbc.ModalFooter(
                dbc.Button(
                    '닫기 · 초안 유지', id='rc-close', color='secondary', outline=True
                )
            ),
        ],
        id='rc-modal',
        is_open=False,
        size='lg',
        scrollable=True,
    )


register_form_callbacks('rc')
draft_ui.register('rc', [f'rc-{field}' for field in FIELDS])


OUTS = ('open', 'tasks', 'people', 'refs', 'note', 'content', 'when', 'links', 'attach', 'attach_names',
        'error', 'error_open', 'hn', 'tn', 'dn', 'type', 'task_opts', 'people_opts', 'ref_opts', 'toast',
        'toast_open')


def _out(**values):
    return tuple(values.get(key, no_update) for key in OUTS)


def _merge(current, new):
    """작성 중인 초안의 대상에 새로 고른 대상을 덧붙인다 (기존 대상은 지우지 않음)."""
    current = list(current or [])
    return current + [x for x in new if x not in current]


@callback(
    Output('rc-modal', 'is_open'),
    Output('rc-tasks', 'value', allow_duplicate=True),
    Output('rc-people', 'value', allow_duplicate=True),
    Output('rc-refs', 'value', allow_duplicate=True),
    Output('rc-context-note', 'children'),
    Output('rc-content', 'value', allow_duplicate=True),
    Output('rc-when', 'value', allow_duplicate=True),
    Output('rc-links', 'value', allow_duplicate=True),
    Output('rc-attach', 'contents'),
    Output('rc-attach', 'filename'),
    Output('rc-error', 'children'),
    Output('rc-error', 'is_open'),
    Output('history-changed', 'data', allow_duplicate=True),
    Output('todo-changed', 'data', allow_duplicate=True),
    Output('doc-changed', 'data', allow_duplicate=True),
    Output('rc-type', 'value', allow_duplicate=True),
    Output('rc-tasks', 'options'),
    Output('rc-people', 'options'),
    Output('rc-refs', 'options'),
    Output('rc-toast', 'children'),
    Output('rc-toast', 'is_open'),
    Input(
        {
            'type': 'record-compose',
            'task': ALL,
            'person': ALL,
            'ref': ALL,
            'other': ALL,
            'context': ALL,
        },
        'n_clicks',
    ),
    Input('rc-close', 'n_clicks'),
    Input('rc-add', 'n_clicks'),
    *[State('rc-' + field, 'value') for field in FIELDS],
    State('rc-attach', 'contents'),
    State('rc-attach', 'filename'),
    State('history-changed', 'data'),
    State('todo-changed', 'data'),
    State('doc-changed', 'data'),
    prevent_initial_call=True,
)
def act(
    _open,
    _close,
    _save,
    content,
    kind,
    when,
    people,
    links,
    ai,
    tasks,
    refs,
    attachments,
    names,
    hn,
    tn,
    dn,
):
    if not ctx.triggered or not ctx.triggered[0]['value']:
        return _out()
    trigger = ctx.triggered_id
    if trigger == 'rc-close':
        return _out(open=False)
    if isinstance(trigger, dict):
        new_tasks = list(dict.fromkeys(t for t in (trigger['task'], trigger['other']) if t))
        new_people = [trigger['person']] if trigger['person'] else []
        new_refs = [trigger['ref']] if trigger['ref'] else []
        options = {'task_opts': task_options(), 'people_opts': people_options(), 'ref_opts': reference_options()}
        if (content or '').strip():
            merged = {'tasks': _merge(tasks, new_tasks), 'people': _merge(people, new_people),
                      'refs': _merge(refs, new_refs)}
            added = merged != {'tasks': list(tasks or []), 'people': list(people or []), 'refs': list(refs or [])}
            return _out(open=True, **merged, **options, note=(
                '작성 중인 초안을 이어서 엽니다. 이번에 고른 대상을 관련 목록에 덧붙였습니다 — '
                '이전 대상이 이 기록에도 맞는지 확인하고, 아니면 빼 주세요.' if added else
                '작성 중인 초안을 이어서 엽니다. 관련 업무·사람·자료를 확인하세요.'))
        return _out(open=True, tasks=new_tasks, people=new_people, refs=new_refs, **options,
                    note='선택한 대상과 연결해 기록합니다. 초안과 첨부는 저장 전에 확인하세요.',
                    type='동향' if new_people and not new_tasks else '메모')
    try:
        tasks = tasks or []
        saved = save_history(
            tasks[0] if tasks else None,
            kind or '메모',
            content,
            when,
            bool(ai),
            links,
            attachments,
            names,
            auth.current_name(),
            person_ids=people or [],
            also_task_ids=tasks[1:],
            ref_ids=refs or [],
        )
    except ValueError as error:
        return _out(open=True, error=str(error), error_open=True)
    task_id = saved.get('task_id')
    toast = [f'기록 #{saved["id"]} 저장 완료. ',
             dcc.Link('업무 열기', href=f'/task/{task_id}') if task_id else None]
    return _out(open=False, tasks=[], people=[], refs=[], note='', content='', when=None, links='',
                attach=None, attach_names=None, error='', error_open=False, hn=(hn or 0) + 1,
                tn=(tn or 0) + 1, dn=(dn or 0) + 1, type='메모', toast=toast, toast_open=True)


@callback(
    Output('rc-legacy', 'options'),
    Input('rc-modal', 'is_open'),
    Input('history-changed', 'data'),
)
def options(_open, _changed):
    rows = drafts.legacy_records((auth.current_user() or {}).get('user_id', ''))
    return [
        {
            'label': f'{r["at"]} · {r["context"]} · '
            + next(
                (
                    str(v)[:40]
                    for k, v in r['payload'].items()
                    if k.endswith('-content')
                ),
                '',
            ),
            'value': r['context'],
        }
        for r in rows
    ]


def reference_options():
    return [
        {'label': r['title'] or f'자료 #{r["id"]}', 'value': r['id']}
        for r in store.list_references()
    ]


@callback(
    *[Output('rc-' + field, 'value', allow_duplicate=True) for field in FIELDS],
    Output('rc-legacy-message', 'children'),
    Input('rc-legacy-load', 'n_clicks'),
    Input('rc-legacy-delete', 'n_clicks'),
    State('rc-legacy', 'value'),
    prevent_initial_call=True,
)
def legacy(_load, _delete, context):
    uid = (auth.current_user() or {}).get('user_id', '')
    saved = next(
        (r for r in drafts.legacy_records(uid) if r['context'] == context), None
    )
    if not saved:
        return *(no_update,) * len(FIELDS), '선택한 초안을 찾을 수 없습니다.'
    if ctx.triggered_id == 'rc-legacy-delete':
        try:
            drafts.clear(uid, context, saved['token'])
        except ValueError as error:
            return *(no_update,) * len(FIELDS), str(error)
        return *(no_update,) * len(FIELDS), '이전 초안을 삭제했습니다.'
    payload = drafts.migrate_record_payload(context, saved['payload'])
    return *(
        payload.get('rc-' + field) for field in FIELDS
    ), '불러왔습니다. 이전 초안 원본은 유지됩니다.'


@callback(
    Output('rc-content', 'value', allow_duplicate=True),
    Input('rc-file', 'contents'),
    State('rc-content', 'value'),
    prevent_initial_call=True,
)
def load_text(contents, text):
    if not contents:
        return no_update
    return ((text or '').rstrip() + '\n\n' + decode_text_upload(contents)).strip()
