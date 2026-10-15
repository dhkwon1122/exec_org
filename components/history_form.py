"""이력 입력 폼 — 업무 상세와 홈의 "빠른 기록"에서 같이 쓴다."""
import base64

import dash_bootstrap_components as dbc
from dash import Input, Output, State, callback, dcc, html, no_update

from services import enrich, llm, store

# 유형별 기록 양식 — 빈 칸에서 '양식' 버튼을 누르면 채워진다. 나중에 검색·회고할 때 필요한 것
# (누가/무엇을/왜/다음에 할 일)이 빠지지 않게 하는 용도이고, 받아 적은 원문을 붙여넣을 때는 쓰지 않아도 된다.
TEMPLATES = {
    '피드백': '누가(이름/직책):\n받은 피드백:\n- \n요청 사항(기한):\n- \n내 대응/생각:\n',
    '회의': '회의명/참석:\n논의:\n- \n결정:\n- \n후속 조치(담당·기한):\n- \n',
    '결정': '결정 내용:\n배경/이유:\n검토했지만 택하지 않은 안:\n결정자:\n영향·후속 조치:\n',
    '진행': '한 일:\n- \n다음 할 일:\n- \n막힌 점/도움 필요:\n',
    '이슈': '현상:\n원인(추정):\n영향:\n대응·담당:\n',
    '요청': '요청자:\n요청 내용:\n기한:\n',
    '메모': '',
}

TIPS = [
    '한 건 = 한 사건: 회의 1번, 피드백 1건, 결정 1건씩 따로 남기면 나중에 찾기 쉽다.',
    '받아 적은 원문은 다듬지 말고 그대로 — 요약·키워드는 AI가 붙인다.',
    '일시는 "일이 있었던 때"로 (나중에 몰아서 적어도 날짜순으로 정렬된다).',
    '사람 이름·조직·문서명은 원문에 그대로 — 검색 키워드가 된다.',
    '"결정"은 이유와 버린 대안까지 — 몇 달 뒤 "왜 이렇게 했지?"에 답해준다.',
    '근거 자료(메일·컨플·공유폴더 경로)는 아래 "관련 자료"에 붙인다. 원문 속 링크는 자동으로 붙는다.',
]


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
        dbc.Textarea(id=f'{prefix}-content', rows=6, className='mb-1',
                     placeholder='받아 적은 내용을 그대로 붙여넣으세요 (회의 메모, 피드백, 받아쓰기 등). '
                                 '원문은 그대로 보존되고, AI가 검색용 제목·요약·키워드를 만들어 줍니다.'),
        html.Div([
            dbc.Button([html.I(className='bi bi-layout-text-sidebar me-1'), '양식'], id=f'{prefix}-tpl',
                       color='link', size='sm', className='p-0 me-3',
                       title='빈 칸일 때 선택한 유형의 기록 양식을 넣습니다'),
            dbc.Button([html.I(className='bi bi-lightbulb me-1'), '기록 요령'], id=f'{prefix}-tips-btn',
                       color='link', size='sm', className='p-0'),
            dbc.Popover(dbc.PopoverBody(html.Ul([html.Li(t) for t in TIPS], className='small mb-0 ps-3')),
                        target=f'{prefix}-tips-btn', trigger='legacy', placement='bottom'),
        ], className='mb-2'),
        dbc.Textarea(id=f'{prefix}-links', rows=2, className='mb-1 small',
                     placeholder='관련 자료 링크 — 한 줄에 하나, 앞뒤 글자는 제목 (예: 회의록 https://conf.../123, '
                                 '\\\\공유폴더\\보고서.pptx 최종본). 원문 속 링크는 자동으로 붙습니다.'),
        html.Div([
            dcc.Upload(dbc.Button([html.I(className='bi bi-paperclip me-1'), '파일 첨부'], color='secondary',
                                  outline=True, size='sm'), id=f'{prefix}-attach', multiple=True,
                       max_size=50 * 1024 * 1024),
            html.Span(id=f'{prefix}-attach-names', className='small text-muted ms-2'),
        ], className='d-flex align-items-center mb-2'),
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


def parse_uploads(contents, names) -> list[tuple[str, bytes]]:
    """dcc.Upload(multiple=True) 값 → [(파일명, 바이트)]"""
    if not contents:
        return []
    if isinstance(contents, str):
        contents, names = [contents], [names]
    return [(n or 'file', base64.b64decode(c.split(',', 1)[1])) for c, n in zip(contents, names or [])]


def save_history(task_id: int, etype: str, content: str, when, use_ai: bool, links_text: str,
                 attach_contents, attach_names, author: str) -> dict:
    """이력 저장 + 관련 자료(입력한 링크·원문 속 링크·첨부 파일) 연결 + AI 정리 요청.
    업무 상세와 홈 빠른 기록이 같이 쓴다. 실패하면 ValueError."""
    files = parse_uploads(attach_contents, attach_names)
    links = store.parse_links(links_text or '')
    h = store.add_history(task_id, content, etype, author, when or None)
    known = {l['url'] for l in links}
    links += [{'title': u, 'url': u} for u in store.urls_in_text(content) if u not in known]
    store.attach_links(task_id, h['id'], links, author=author)
    for name, data in files:
        store.add_reference(task_id, 'file', title=name, file_name=name, file_bytes=data, author=author,
                            history_id=h['id'])
    if use_ai:
        enrich.submit(h['id'])
    return h


def register_form_callbacks(prefix: str, template: bool = True):
    """양식 넣기·첨부 파일 이름 표시 콜백 (폼을 쓰는 화면마다 한 번 등록)."""
    if template:
        _register_template(prefix)

    @callback(Output(f'{prefix}-attach-names', 'children'), Input(f'{prefix}-attach', 'filename'))
    def _names(names):
        if not names:
            return ''
        names = [names] if isinstance(names, str) else names
        return '첨부: ' + ', '.join(names)


def _register_template(prefix: str):
    @callback(Output(f'{prefix}-content', 'value', allow_duplicate=True), Input(f'{prefix}-tpl', 'n_clicks'),
              State(f'{prefix}-type', 'value'), State(f'{prefix}-content', 'value'), prevent_initial_call=True)
    def _template(n, etype, content):
        if not n or (content or '').strip():  # 이미 쓴 내용(붙여넣은 원문)은 덮어쓰지 않는다
            return no_update
        return TEMPLATES.get(etype, '') or no_update
