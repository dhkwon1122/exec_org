"""공통 입력 폼의 서버 자동 저장·명시적 초안 불러오기."""
import json

import dash_bootstrap_components as dbc
from dash import Input, Output, State, callback, ctx, dcc, html, no_update

from services import auth, drafts


def widget(prefix, context):
    return html.Div([
        dcc.Store(id=f'{prefix}-draft-context', data=context), dcc.Store(id=f'{prefix}-draft-meta'),
        dcc.Interval(id=f'{prefix}-draft-tick', interval=5000),
        html.Span(id=f'{prefix}-draft-status', className='small text-muted me-2', role='status'),
        html.Span([
            dbc.Button('초안 불러오기', id=f'{prefix}-draft-load', size='sm', color='link'),
            dbc.Button('저장된 초안 삭제', id=f'{prefix}-draft-discard', size='sm', color='link'),
        ], id=f'{prefix}-draft-actions', style={'display': 'none'}),
        html.Div('본문·선택 항목은 5초마다 자동 저장됩니다. 첨부파일은 다시 선택해 주세요.',
                 className='small text-muted'),
    ], className='mt-2 draft-widget')


def register(prefix, fields):
    """fields 첫 항목은 본문. 등록 성공으로 본문이 비면 해당 탭의 초안만 지운다."""
    @callback(Output(f'{prefix}-draft-meta', 'data'), Output(f'{prefix}-draft-status', 'children'),
        Output(f'{prefix}-draft-actions', 'style'),
        *[Output(field, 'value', allow_duplicate=True) for field in fields],
        Input(f'{prefix}-draft-tick', 'n_intervals'), Input(f'{prefix}-draft-load', 'n_clicks'),
        Input(f'{prefix}-draft-discard', 'n_clicks'), State(f'{prefix}-draft-context', 'data'),
        State(f'{prefix}-draft-meta', 'data'), *[State(field, 'value') for field in fields],
        prevent_initial_call='initial_duplicate')
    def sync(_tick, _load, _discard, context, meta, *values):
        unchanged = (no_update,) * len(fields)
        user_id = (auth.current_user() or {}).get('user_id')
        if not user_id:
            return no_update, '로그인 후 초안을 저장합니다.', {'display': 'none'}, *unchanged
        payload = dict(zip(fields, values))
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        if not meta or meta.get('user') != user_id or meta.get('context') != context:
            saved = drafts.get(user_id, context)
            meta = {'user': user_id, 'context': context, 'raw': raw if saved or not values[0] else None,
                    'token': None, 'paused': bool(saved)}
            return meta, ('저장된 초안이 있습니다. 불러오기 또는 삭제 후 자동 저장됩니다.' if saved else '입력하면 자동 저장됩니다.'), \
                {} if saved else {'display': 'none'}, *unchanged
        try:
            if ctx.triggered_id == f'{prefix}-draft-load':
                saved = drafts.get(user_id, context)
                if not saved:
                    meta.update(paused=False, token=None, raw=raw)
                    return meta, '저장된 초안이 없습니다.', {'display': 'none'}, *unchanged
                restored = {field: saved['payload'].get(field, value) for field, value in zip(fields, values)}
                meta.update(paused=False, token=saved['token'], raw=json.dumps(restored, ensure_ascii=False, sort_keys=True))
                return meta, f"{saved['at']} 초안을 불러왔습니다.", {}, *restored.values()
            if ctx.triggered_id == f'{prefix}-draft-discard':
                saved = drafts.get(user_id, context)
                if saved:
                    drafts.clear(user_id, context, saved['token'])
                meta.update(paused=False, token=None, raw=None if values[0] else raw)
                return meta, '저장된 초안을 삭제했습니다.', {'display': 'none'}, *unchanged
            if meta.get('paused') or raw == meta.get('raw'):
                return (no_update,) * (3 + len(fields))
            if not str(values[0] or '').strip():
                drafts.clear(user_id, context, meta.get('token'))
                meta.update(token=None, raw=raw)
                return meta, '입력하면 자동 저장됩니다.', {'display': 'none'}, *unchanged
            saved = drafts.save(user_id, context, payload, meta.get('token'))
            meta.update(token=saved['token'], raw=raw)
            return meta, f"자동 저장 {saved['at']}", {}, *unchanged
        except ValueError as error:
            meta['paused'] = True
            return meta, str(error), {}, *unchanged
    return sync
