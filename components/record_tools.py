"""모든 기록 화면의 북마크·복사·휴지통·버전 복원."""
import difflib

import dash_bootstrap_components as dbc
from dash import ALL, Input, Output, State, callback, clientside_callback, ctx, dcc, html, no_update

from services import auth, records, store


def layout():
    return html.Div([
        dcc.Store(id='record-last-trash'), dcc.Store(id='revision-entry'), dcc.Store(id='revision-version'),
        dbc.Toast([html.Div(id='record-message'), dbc.Button('실행 취소', id='record-undo', color='link')],
                  id='record-toast', header='기록', is_open=False, duration=8000, dismissable=True,
                  style={'position': 'fixed', 'bottom': 20, 'right': 16, 'zIndex': 1100, 'maxWidth': '90vw'}),
        html.Div(id='record-copy-result', role='status', className='copy-feedback'),
        dbc.Modal([
            dbc.ModalHeader(dbc.ModalTitle('수정 이력')),
            dbc.ModalBody([
                html.P('선택한 버전을 직전 버전과 비교합니다. 복원해도 현재 첨부파일은 유지되며, '
                       '현재 존재하는 업무·임원 연결만 복원합니다.', className='small text-muted'),
                dcc.Dropdown(id='revision-select', clearable=False, className='mb-3'),
                html.Div(id='revision-diff'), dbc.Alert(id='revision-error', color='danger', is_open=False),
            ]),
            dbc.ModalFooter([dbc.Button('닫기', id='revision-close', color='secondary', outline=True),
                             dbc.Button('이 버전으로 복원', id='revision-restore')]),
        ], id='revision-modal', size='lg', is_open=False, scrollable=True),
    ])


@callback(Output('history-changed', 'data', allow_duplicate=True), Output('record-message', 'children'),
          Output('record-toast', 'is_open'), Output('record-last-trash', 'data'), Output('record-undo', 'style'),
          Input({'type': 'record-bookmark', 'id': ALL, 'view': ALL}, 'n_clicks'),
          Input({'type': 'record-trash', 'id': ALL, 'view': ALL}, 'n_clicks'), Input('record-undo', 'n_clicks'),
          State('record-last-trash', 'data'), State('history-changed', 'data'), prevent_initial_call=True)
def act(_b, _d, _u, last, changed):
    if not ctx.triggered or not ctx.triggered[0]['value']:
        return (no_update,) * 5
    trig = ctx.triggered_id
    user = auth.current_user() or {}
    try:
        if trig == 'record-undo' and last:
            records.restore('history', last)
            message, last = '기록을 복원했습니다.', None
        elif isinstance(trig, dict) and trig['type'] == 'record-trash':
            records.trash_history(trig['id'], user.get('name', ''))
            message, last = '휴지통으로 이동했습니다.', trig['id']
        elif isinstance(trig, dict):
            saved = records.toggle_bookmark(trig['id'], user.get('user_id', ''))
            message, last = ('북마크에 저장했습니다.' if saved else '북마크를 해제했습니다.'), None
        else:
            return (no_update,) * 5
    except ValueError as error:
        return no_update, str(error), True, None, {'display': 'none'}
    return (changed or 0) + 1, message, True, last, {} if last else {'display': 'none'}


clientside_callback(
    """async function(clicks) {
        function result(message) { return {namespace: 'dash_html_components', type: 'Span',
            props: {children: message, key: Date.now().toString()}}; }
        const c = dash_clientside.callback_context;
        if (!c.triggered.length || !c.triggered[0].value) return dash_clientside.no_update;
        const id = JSON.parse(c.triggered[0].prop_id.slice(0, -9));
        const url = window.location.origin + '/history/' + id.id;
        try {
            if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(url);
            else {
                const field = document.createElement('textarea'); field.value = url;
                field.style.position = 'fixed'; field.style.top = '0'; document.body.appendChild(field);
                field.select(); const ok = document.execCommand('copy'); field.remove();
                if (!ok) throw new Error('copy');
            }
            return result('링크를 복사했습니다.');
        } catch (_) { return result('복사하지 못했습니다. 기록을 열고 주소창의 링크를 복사하세요.'); }
    }""", Output('record-copy-result', 'children'),
    Input({'type': 'record-copy', 'id': ALL, 'view': ALL}, 'n_clicks'), prevent_initial_call=True)


@callback(Output('revision-modal', 'is_open'), Output('revision-entry', 'data'), Output('revision-version', 'data'),
          Output('revision-select', 'options'), Output('revision-select', 'value'),
          Output('revision-error', 'children'), Output('revision-error', 'is_open'),
          Output('history-changed', 'data', allow_duplicate=True),
          Input({'type': 'record-revisions', 'id': ALL, 'view': ALL}, 'n_clicks'),
          Input('revision-close', 'n_clicks'), Input('revision-restore', 'n_clicks'),
          State('revision-entry', 'data'), State('revision-select', 'value'), State('revision-version', 'data'),
          State('history-changed', 'data'), prevent_initial_call=True)
def version_modal(_o, _c, _r, eid, selected, version, changed):
    nu = no_update
    if not ctx.triggered or not ctx.triggered[0]['value']:
        return (nu,) * 8
    trig = ctx.triggered_id
    if trig == 'revision-close':
        return False, nu, nu, nu, nu, '', False, nu
    if isinstance(trig, dict):
        h = store.get_history(trig['id'])
        if not h:
            return (nu,) * 8
        rows = records.revisions(h['id'])
        opts = [{'label': f"{r['at']} · {r['actor']} · {r['action']}", 'value': r['id']} for r in rows]
        return True, h['id'], h['edit_version'], opts, rows[0]['id'] if rows else None, '', False, nu
    if trig == 'revision-restore' and eid and selected:
        user = auth.current_user() or {}
        try:
            records.restore_revision(eid, selected, version, user.get('name', ''), user.get('user_id', ''))
        except ValueError as error:
            return nu, nu, nu, nu, nu, str(error), True, nu
        return False, nu, nu, nu, nu, '', False, (changed or 0) + 1
    return (nu,) * 8


@callback(Output('revision-diff', 'children'), Input('revision-select', 'value'), State('revision-entry', 'data'))
def show_diff(rid, eid):
    if not rid or not eid:
        return html.P('아직 수정 이력이 없습니다. 다음 수정부터 버전이 보존됩니다.', className='text-muted')
    try:
        before, after = records.revision_pair(eid, rid)
    except ValueError as error:
        return dbc.Alert(str(error), color='warning')
    labels = dict(entry_type='유형', occurred_at='일시', title='제목', summary='요약', keywords='키워드',
                  content='원문', task_ids='업무 번호', person_ids='임원 번호')
    result = []
    for key, label in labels.items():
        old, new = str(before.get(key, '')), str(after.get(key, ''))
        if old == new:
            continue
        lines = list(difflib.ndiff(old.splitlines(), new.splitlines()))
        result.append(html.Section([html.H6(label), html.Pre([
            html.Span(line + '\n', className='diff-add' if line.startswith('+ ') else
                      'diff-remove' if line.startswith('- ') else '') for line in lines if not line.startswith('? ')
        ], className='revision-text')], className='mb-3'))
    return result or html.P('내용 변경이 없습니다.', className='text-muted')


@callback(Output('revision-restore', 'disabled'), Input('revision-select', 'value'))
def disable_empty_restore(selected):
    return selected is None
