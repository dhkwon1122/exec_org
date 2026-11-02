"""업무 AI 요약 카드 (services/task_summary.py) — 업무 화면 위와 간트에서 업무를 눌렀을 때 옆 창에."""
import dash_bootstrap_components as dbc
from dash import MATCH, Input, Output, State, callback, html, no_update

from services import llm, task_summary


def card(task_id: int, flush: bool = False):
    """요약 카드 — 없으면 '쉬는 시간에 만들어집니다'와 '지금 요약' 단추."""
    return html.Div(_body(task_id, task_summary.get(task_id)), id={'type': 'ts-box', 'id': task_id},
                    className='' if flush else 'mb-3')


def _body(task_id: int, s: dict | None, error: str = ''):
    can = llm.is_configured()
    run = dbc.Button([html.I(className='bi bi-arrow-repeat me-1'), '다시 요약' if s and s['has'] else '지금 요약'],
                     id={'type': 'ts-run', 'id': task_id}, color='link', size='sm',
                     className='p-0 ms-auto text-decoration-none') if can else None
    head = html.Div([html.I(className='bi bi-stars me-1'), html.Span('AI 요약', className='fw-semibold me-2'),
                     html.Span(f"{s['at']} 기준" + (' · 이후 바뀐 내용이 있어 쉬는 시간에 다시 요약합니다' if s['stale'] else ''),
                               className='small text-muted') if s and s['has'] else None, run],
                    className='d-flex align-items-center mb-1')
    msg = error or (s or {}).get('error', '')
    if not s or not s['has']:
        note = ('하위 업무·이력·참고자료를 묶은 요약이 쉬는 시간에 만들어집니다.' if can
                else '사내 LLM 이 설정되지 않아 요약을 만들 수 없습니다.')
        return dbc.Card(dbc.CardBody([head, html.Div(note, className='small text-muted'),
                                      dbc.Alert(msg, color='warning', className='small py-1 px-2 mt-2 mb-0') if msg else None]),
                        className='task-summary')
    return dbc.Card(dbc.CardBody([
        head,
        html.Div(s['overview'], className='small', style={'whiteSpace': 'pre-wrap'}) if s['overview'] else None,
        html.Div([html.Div('주요 경과', className='small fw-semibold mt-2'),
                  html.Ul([html.Li([html.Span(p['date'], className='text-muted me-2') if p['date'] else None, p['what']])
                           for p in s['progress']], className='small mb-0 ps-3')]) if s['progress'] else None,
        html.Div([html.Div('남은 일', className='small fw-semibold mt-2'),
                  html.Ul([html.Li(x) for x in s['next']], className='small mb-0 ps-3')]) if s['next'] else None,
        html.Div([html.Div([html.I(className='bi bi-folder2 me-1'), '참고자료'], className='small fw-semibold mt-2'),
                  html.Div(s['references'], className='small')]) if s['references'] else None,
        dbc.Alert(msg, color='warning', className='small py-1 px-2 mt-2 mb-0') if error else None,
    ]), className='task-summary')


@callback(Output({'type': 'ts-box', 'id': MATCH}, 'children'), Input({'type': 'ts-run', 'id': MATCH}, 'n_clicks'),
          State({'type': 'ts-run', 'id': MATCH}, 'id'), prevent_initial_call=True)
def run(n, ident):
    if not n:
        return no_update
    tid = ident['id']
    try:
        s = task_summary.generate(tid, force=True)
    except llm.LLMError as e:
        return _body(tid, task_summary.get(tid), str(e))
    return _body(tid, s)
