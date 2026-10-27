"""여러 화면에서 같이 쓰는 표시용 조각."""
import re
from urllib.parse import quote

import dash_bootstrap_components as dbc
from dash import dcc, html

STATUS_COLOR = {'예정': 'secondary', '진행중': 'primary', '보류': 'warning', '완료': 'success'}
ENTRY_ICON = {
    '메모': 'bi-sticky', '피드백': 'bi-chat-left-quote', '동향': 'bi-binoculars', '회의': 'bi-people', '결정': 'bi-check2-square',
    '진행': 'bi-arrow-right-circle',
    '이슈': 'bi-exclamation-triangle', '요청': 'bi-envelope', '상태변경': 'bi-flag',
    '연결': 'bi-link-45deg', '자료': 'bi-paperclip', '할일': 'bi-check2-circle',
}
REF_ICON = {'link': 'bi-link-45deg', 'file': 'bi-file-earmark', 'note': 'bi-card-text'}


def status_badge(status: str):
    return dbc.Badge(status, color=STATUS_COLOR.get(status, 'secondary'), className='me-1')


def page_head(title: str, sub=None):
    """화면 맨 위 큰 제목 (assets/style.css 의 .page-head)."""
    return html.Div([html.H3(title), html.Span(sub, className='page-sub') if sub else None], className='page-head')


def tag_badges(tags: list[str]):
    return [dbc.Badge(t, color='light', text_color='dark', className='me-1 border') for t in tags]


def task_link(task: dict, prefix: bool = True):
    label = [html.Span(f"#{task['id']}", className='task-no'), task['title']] if prefix else task['title']
    return dcc.Link(label, href=f"/task/{task['id']}", className='task-title')


def multiline(text: str):
    return html.Div(text, style={'whiteSpace': 'pre-wrap'})


def ref_target(ref: dict):
    """참고자료의 실제 내용(링크/다운로드/메모 본문)."""
    if ref['kind'] == 'link' and ref['url'].startswith('\\\\'):
        # 공유 폴더 경로는 웹 링크로 열리지 않으므로 복사해서 탐색기에 붙여넣게 한다
        return unc_path(ref['url'])
    if ref['kind'] == 'link':
        from services import confluence
        url = confluence.display_url(ref['url'])  # 컨플루언스 API 주소는 브라우저로 여는 주소로
        return html.A(url, href=url, target='_blank', rel='noopener noreferrer', className='small text-break')
    if ref['kind'] == 'file':
        return html.A([html.I(className='bi bi-download me-1'), ref['file_name']],
                      href=f"/files/{ref['id']}", className='small')
    return None


def confluence_box(ref: dict, button_id: dict | None = None):
    """컨플루언스 링크 참고자료 — AI 요약, 읽어 온 원문(펼쳐 보기)과 '다시 읽기'. 컨플루언스 링크가 아니면 None."""
    from services import confluence
    if ref['kind'] != 'link' or not confluence.is_confluence_url(ref['url']):
        return None
    btn = dbc.Button([html.I(className='bi bi-arrow-repeat me-1'), '다시 읽기' if ref['content_at'] else '읽어 오기'],
                     id=button_id, color='link', size='sm', className='p-0 ms-2') if button_id else None
    extra = None
    if ref['content_error']:
        tries = ref.get('content_tries') or 0
        status = html.Span([f"읽지 못함 — {ref['content_error']}",
                            html.Span(f" (쉬는 시간에 다시 시도 · {tries}/{confluence.RETRY_MAX})"
                                      if tries < confluence.RETRY_MAX else ' (자동 재시도 멈춤 — 다시 읽기를 누르세요)',
                                      className='text-muted')], className='text-danger')
    elif ref['content']:
        status = html.Span(f"{ref['content_at']} 읽음", className='text-muted')
        extra = html.Div([
            html.Div([html.I(className='bi bi-stars me-1 text-warning'), ref['content_summary']],
                     className='mt-1', style={'whiteSpace': 'pre-wrap'}) if ref['content_summary'] else None,
            html.Details([
                html.Summary(f"원문 보기 ({len(ref['content']):,}자)"),
                html.Pre(ref['content'][:20000], className='small bg-body-tertiary p-2 rounded mb-0',
                         style={'whiteSpace': 'pre-wrap', 'maxHeight': '40vh', 'overflowY': 'auto'}),
            ], className='mt-1'),
        ])
    elif ref['content_at']:
        status = html.Span(f"본문이 비어 있음 ({ref['content_at']} 읽음)", className='text-muted')
    else:
        status = html.Span('아직 읽지 않음' if confluence.is_configured() else
                           '연결 설정 전 — .env 의 CONFLUENCE_*', className='text-muted')
    return html.Div([
        html.Div([dbc.Badge('컨플루언스', color='info', className='me-1'), status,
                  btn if confluence.is_configured() else None]),
        extra,
    ], className='small mt-1')


AUTO_TYPES = ('상태변경', '연결', '자료', '할일')


def unc_path(path: str):
    return html.Span([
        html.Code(path, className='small text-break me-1'),
        dcc.Clipboard(content=path, title='경로 복사 (탐색기 주소창에 붙여넣기)',
                      style={'display': 'inline-block', 'cursor': 'pointer', 'fontSize': '0.85em'}),
    ])


def history_refs(refs: list[dict], terms: list[str] | None = None):
    """이력에 붙은 관련 자료 — 링크는 새 탭, 파일은 다운로드, 공유 폴더 경로는 복사."""
    items = []
    for r in refs:
        icon = html.I(className=f"bi {REF_ICON.get(r['kind'], 'bi-link-45deg')} me-1 text-muted")
        if r['kind'] == 'link' and r['url'].startswith('\\\\'):
            label = [highlight(r['title'], terms), ' '] if r['title'] != r['url'] else []
            items.append(html.Div([icon, *label, unc_path(r['url'])]))
        elif r['kind'] == 'link':
            from services import confluence
            url = confluence.display_url(r['url'])  # 컨플루언스 API 주소는 페이지 주소로
            items.append(html.Div([icon, html.A(highlight(r['title'], terms), href=url, target='_blank',
                                                rel='noopener noreferrer', title=url, className='text-break')]))
        elif r['kind'] == 'file':
            items.append(html.Div([icon, html.A(highlight(r['file_name'] or r['title'], terms),
                                                href=f"/files/{r['id']}")]))
        else:
            items.append(html.Div([icon, highlight(r['title'], terms)]))
    return html.Div(items, className='small mt-1 ps-2 border-start border-2')


def highlight(text: str, terms: list[str] | None, multiline_text: bool = False):
    """검색어가 들어간 부분을 <mark> 로 강조한 children."""
    style = {'whiteSpace': 'pre-wrap'} if multiline_text else None
    if not terms or not text:
        return html.Span(text, style=style)
    pattern = re.compile('(' + '|'.join(re.escape(t) for t in sorted(terms, key=len, reverse=True)) + ')',
                         re.IGNORECASE)
    parts = [html.Mark(p, className='p-0') if i % 2 else p for i, p in enumerate(pattern.split(text)) if p]
    return html.Span(parts, style=style)


def _matches(text: str, terms: list[str] | None) -> bool:
    return bool(terms) and any(t.lower() in (text or '').lower() for t in terms)


def search_href(q: str) -> str:
    return '/search?q=' + quote(f'"{q}"' if ' ' in q else q)


def keyword_chips(keywords: list[str], terms: list[str] | None = None):
    return html.Div([
        dcc.Link(f'#{k}', href=search_href(k),
                 className='badge rounded-pill text-decoration-none me-1 mb-1 '
                           + ('text-bg-warning' if _matches(k, terms) else 'text-bg-light border'))
        for k in keywords
    ], className='mt-1')


def people_chips(people: list[dict], terms: list[str] | None = None):
    """이력에 연결된 임원 — 누르면 그 임원의 기록 모음으로."""
    return html.Div([
        dcc.Link([html.I(className='bi bi-person-badge me-1'), f"{p['name']} {p['position']}".strip()],
                 href=f"/people/{p['id']}",
                 className='badge rounded-pill text-decoration-none me-1 mb-1 '
                           + ('text-bg-warning' if _matches(p['name'], terms) else 'text-bg-primary'))
        for p in people], className='mb-1')


def source_icons(t: dict):
    """AI 요약·받은 메일에서 만든 업무 표시 — 누르면 업무 화면에서 그 메모/메일을 연다."""
    out = []
    if t.get('source_history_id'):
        out.append(dcc.Link(html.I(className='bi bi-stars'), href=f"/task/{t['id']}?src=history",
                            className='text-info me-1', title='AI 요약에서 찾은 할 일 — 원래 메모 보기'))
    if t.get('source_mail_id'):
        out.append(dcc.Link(html.I(className='bi bi-envelope'), href=f"/task/{t['id']}?src=mail",
                            className='text-info me-1', title='받은 메일에서 만든 업무 — 메일 보기'))
    return out


def history_card(h: dict, terms: list[str] | None = None, show_task: bool = False, actions: bool = True,
                 added_todos: set[str] | dict[str, int] | None = None, context_task_id: int | None = None):
    """이력 한 건. 원문은 그대로 보존하고, AI 정리본(제목·요약·키워드)이 있으면 위에 보여준다.
    added_todos 가 {제목: 업무 id} 면 이미 업무로 만든 AI 할 일은 그 업무로 가는 링크가 된다.
    context_task_id: 업무 화면에서 그릴 때 그 업무 — 여러 업무에 걸친 기록이면 나머지 업무를 보여 주고,
    삭제 대신 '이 업무에서 빼기'를 쓸 수 있다."""
    auto = h['entry_type'] in AUTO_TYPES
    header = [
        html.I(className=f"bi {ENTRY_ICON.get(h['entry_type'], 'bi-dot')} me-2"),
        dbc.Badge(h['entry_type'], color='light' if auto else 'dark', text_color='dark' if auto else None,
                  className='me-2'),
        html.Span(h['occurred_at'], className='small text-muted me-2 text-nowrap'),
        html.Span(h['author'], className='small me-2 text-nowrap'),
    ]
    if show_task and h['task_id']:
        header.append(dcc.Link(f"#{h['task_id']} {h.get('task_title', '')}", href=f"/task/{h['task_id']}",
                               className='small text-truncate me-2'))
    all_tasks = ([{'id': h['task_id'], 'title': h.get('task_title', '')}] if h['task_id'] else []) \
        + h.get('also_tasks', [])
    shown = {h['task_id']} if show_task else set()
    others = [t for t in all_tasks if t['id'] != context_task_id and t['id'] not in shown]
    if others:
        header.append(html.Span([
            html.I(className='bi bi-diagram-2 me-1', title='여러 업무에 걸친 기록'),
            *[dcc.Link(f"#{t['id']} {t['title']}", href=f"/task/{t['id']}", className='me-2') for t in others],
        ], className='small text-muted text-truncate me-2'))
    header.append(html.Span(className='me-auto'))
    shared = context_task_id is not None and len(all_tasks) > 1
    if actions and shared:
        header.append(dbc.Button(html.I(className='bi bi-box-arrow-right'),
                                 id={'type': 'h-unlink', 'id': h['id'], 'task': context_task_id}, color='link',
                                 size='sm', className='text-muted p-0 me-2',
                                 title='이 업무에서 빼기 (다른 업무에는 그대로 남음)'))
    if actions and not auto:
        header.append(dbc.Button(html.I(className='bi bi-link-45deg'), id={'type': 'h-link', 'id': h['id']},
                                 color='link', size='sm', className='text-muted p-0 me-2',
                                 title='업무에 연결 (이 기록이 관련된 업무 고르기)'))
        header.append(dbc.Button(html.I(className='bi bi-pencil'), id={'type': 'h-edit', 'id': h['id']},
                                 color='link', size='sm', className='text-muted p-0 me-2', title='수정'))
    if actions:
        header.append(dbc.Button(html.I(className='bi bi-x'), id={'type': 'h-del', 'id': h['id']}, color='link',
                                 size='sm', className='text-muted p-0', title='삭제'))

    body = []
    if h.get('people'):
        body.append(people_chips(h['people'], terms))
    if h['title']:
        body.append(html.Div(highlight(h['title'], terms), className='fw-semibold'))
    if h['summary']:
        body.append(html.Div(highlight(h['summary'], terms, True), className='small'))
    if h['keywords']:
        body.append(keyword_chips(h['keywords'], terms))
    if h['ai_todos']:
        added_todos = added_todos or set()

        def added_id(t):  # 만든 업무 id — 확인 창에서 고쳐 등록했으면 표시(task_id), 예전 것은 제목으로
            if t.get('task_id'):
                return t['task_id']
            if isinstance(added_todos, dict):
                return added_todos.get(t['title'])
            return True if t['title'] in added_todos else None
        left = [i for i, t in enumerate(h['ai_todos']) if not added_id(t)]
        body.append(html.Div([
            html.Div([html.I(className='bi bi-list-check me-1'), 'AI가 찾은 할 일' + (' — + 를 누르면 고쳐서 업무로 등록' if actions else ''),
                      html.Span(f" · 기한 기준: {h['ai_reference_date']}", className='ms-1')
                      if h.get('ai_reference_date') and any(t.get('due') for t in h['ai_todos']) else None,
                      dbc.Button(f'{len(left)}건 한꺼번에 검토', id={'type': 'h-todo-all', 'hid': h['id'],
                                                               'task': context_task_id or 0},
                                 color='link', size='sm', className='p-0 ms-auto')
                      if actions and len(left) > 1 else None],
                     className='text-muted mb-1 d-flex align-items-center'),
            *[html.Div([
                html.I(className='bi bi-check-lg text-success me-1') if added_id(t) else
                dbc.Button(html.I(className='bi bi-plus-lg'), id={'type': 'h-todo', 'hid': h['id'], 'idx': i, 'task': context_task_id or 0},
                           color='link', size='sm', className='p-0 me-1', title='고쳐서 업무로 등록')
                if actions else html.I(className='bi bi-dot me-1'),
                dcc.Link(t['title'], href=f"/task/{added_id(t)}", className='me-2', title='이 할 일로 만든 업무 열기')
                if isinstance(added_id(t), int) and not isinstance(added_id(t), bool) else
                html.Span(t['title'], className='me-2'),
                html.Span(t['due'], className='badge text-bg-light border me-1') if t['due'] else None,
                html.Span(t['assignee'], className='text-muted') if t['assignee'] else None,
            ], className='d-flex align-items-center') for i, t in enumerate(h['ai_todos'])],
        ], className='small border rounded px-2 py-1 my-1 bg-body-tertiary'))
    if h['ai_status'] == 'pending':
        body.append(html.Div([dbc.Spinner(size='sm', spinner_class_name='me-2'),
                              'AI가 요약·키워드를 만드는 중…'], className='small text-muted my-1'))
    elif h['ai_status'] == 'failed':
        body.append(html.Div([
            html.I(className='bi bi-exclamation-circle me-1'), f"AI 정리 실패: {h['ai_error']} ",
            dbc.Button('다시 생성', id={'type': 'h-regen', 'id': h['id']}, color='link', size='sm',
                       className='p-0 align-baseline') if actions else None,
        ], className='small text-danger my-1'))

    if h['title'] or h['summary']:
        # 원문은 접어 두되, 검색어가 원문에만 있으면 펼쳐서 보여준다
        only_in_raw = _matches(h['content'], terms) and not any(
            _matches(x, terms) for x in [h['title'], h['summary'], ', '.join(h['keywords'])])
        body.append(html.Details([
            html.Summary('원문 보기', className='small text-muted'),
            html.Div(highlight(h['content'], terms, True), className='small border-start ps-2 mt-1'),
        ], open=only_in_raw, className='mt-1'))
    else:
        body.append(html.Div(highlight(h['content'], terms, True)))

    if h.get('refs'):  # 관련 자료는 항상 내용 뒤에
        body.append(history_refs(h['refs'], terms))

    return dbc.ListGroupItem([html.Div(header, className='d-flex align-items-center flex-wrap mb-1'), *body])
