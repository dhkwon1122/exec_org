"""'추적' 업무 화면의 자료 추적 카드 — 업데이트 횟수·마지막 업데이트·평소 간격과, 메일을 모아 주는 조건."""
import dash_bootstrap_components as dbc
from dash import dcc, html

from services import tracking


def card(task_id: int):
    st = tracking.status(task_id)
    rules = tracking.rules(task_id)
    if st['last']:
        since = '오늘' if st['days_since'] == 0 else f"{st['days_since']}일 전"
        line = [html.B(f"업데이트 {st['count']}회"), f" · 마지막 {st['last']} ({since})"]
        if st['interval']:
            line.append(f" · 평소 약 {st['interval']}일마다 · 다음 예상 {st['expected']}")
    else:
        line = ['아직 들어온 업데이트가 없습니다.']
    late = dbc.Badge('평소보다 늦음', color='warning', className='ms-2',
                     title='평소 간격보다 오래 새 자료가 오지 않았습니다') if st['late'] else None
    rule_rows = [html.Div([
        html.I(className='bi bi-envelope-paper me-1'),
        dcc.Link(r['name'], href=f"/mail-series?series={r['id']}"),
        html.Span(' — ' + ' · '.join(x for x in (
            f"제목에 '{r['subject_pattern']}'" if r['subject_pattern'] else '',
            f"보낸 사람에 '{r['sender_pattern']}'" if r['sender_pattern'] else '') if x)
            + f" · 모인 메일 {r['count']}통", className='text-muted'),
    ], className='small') for r in rules]
    return dbc.Card(dbc.CardBody([
        html.Div([html.I(className='bi bi-arrow-repeat me-1'), html.Span('자료 추적', className='fw-semibold'),
                  late], className='d-flex align-items-center mb-1'),
        html.Div(line, className='small mb-2'),
        *(rule_rows or [html.Div('자동으로 모으는 메일이 없습니다 — 아래에서 조건을 걸면 맞는 메일이 오는 대로 '
                                 '이 업무 기록으로 연결되고 메일은 처리 완료됩니다.', className='small text-muted')]),
        dcc.Link([html.I(className='bi bi-plus-lg me-1'), '자동으로 모을 메일 조건 추가'],
                 href=f'/mail-series?task={task_id}', className='small d-inline-block mt-1'),
    ]), className='mt-2 border-info')
