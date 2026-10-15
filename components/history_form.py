"""이력 입력 폼 — 업무 상세와 홈의 "빠른 기록"에서 같이 쓴다."""
import base64

import dash_bootstrap_components as dbc
from dash import dcc, html

from services import llm, store


def _opts(values):
    return [{'label': v, 'value': v} for v in values]


def history_form(prefix: str = 'h', compact: bool = False):
    """이력 입력 폼 (업무 상세·홈 빠른 기록에서 같이 씀). 원문을 그대로 붙여넣는 용도."""
    ai_on = llm.is_configured()
    # compact: 좁은 칸(홈 오른쪽)에서는 유형·일시를 한 줄, 파일 버튼을 다음 줄로
    w = {'type': dict(xs=5), 'when': dict(xs=7), 'file': dict(xs=12)} if compact else \
        {'type': dict(xs=6, md=3), 'when': dict(xs=6, md=4), 'file': dict(xs=12, md=3)}
    return [
        dbc.Row([
            dbc.Col(dbc.Select(id=f'{prefix}-type', value='메모', options=_opts(store.ENTRY_TYPES)), **w['type']),
            dbc.Col(dbc.Input(id=f'{prefix}-when', type='datetime-local'), **w['when']),
            dbc.Col(dcc.Upload(dbc.Button([html.I(className='bi bi-file-earmark-text me-1'), '텍스트 파일'],
                                          color='secondary', outline=True, size='sm', className='w-100 h-100'),
                               id=f'{prefix}-file', accept='.txt,.md,.csv,.log', className='h-100'), **w['file']),
        ], className='g-2 mb-2'),
        dbc.Textarea(id=f'{prefix}-content', rows=6, className='mb-2',
                     placeholder='받아 적은 내용을 그대로 붙여넣으세요 (회의 메모, 피드백, 받아쓰기 등). '
                                 '원문은 그대로 보존되고, AI가 검색용 제목·요약·키워드를 만들어 줍니다.'),
        html.Div([
            dbc.Switch(id=f'{prefix}-ai', value=ai_on, disabled=not ai_on, className='me-3 mb-0',
                       label='AI 요약·키워드 생성' if ai_on else 'AI 요약·키워드 (LLM 미설정)'),
            html.Span('일시를 비우면 지금 시각', className='small text-muted me-auto'),
            dbc.Button('이력 추가', id=f'{prefix}-add', color='primary', size='sm'),
        ], className='d-flex align-items-center flex-wrap'),
        dbc.Alert(id=f'{prefix}-error', color='danger', is_open=False, className='mt-2 mb-0 py-2'),
    ]


def decode_text_upload(contents: str) -> str:
    raw = base64.b64decode(contents.split(',', 1)[1])
    for enc in ('utf-8-sig', 'cp949'):  # 윈도우 메모장 파일(cp949)도 받는다
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode('utf-8', errors='replace')
