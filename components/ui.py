"""여러 화면에서 같이 쓰는 표시용 조각."""
import dash_bootstrap_components as dbc
from dash import dcc, html

STATUS_COLOR = {'예정': 'secondary', '진행중': 'primary', '보류': 'warning', '완료': 'success'}
ENTRY_ICON = {
    '메모': 'bi-sticky', '회의': 'bi-people', '결정': 'bi-check2-square', '진행': 'bi-arrow-right-circle',
    '이슈': 'bi-exclamation-triangle', '요청': 'bi-envelope', '상태변경': 'bi-flag',
    '연결': 'bi-link-45deg', '자료': 'bi-paperclip',
}
REF_ICON = {'link': 'bi-link-45deg', 'file': 'bi-file-earmark', 'note': 'bi-card-text'}


def status_badge(status: str):
    return dbc.Badge(status, color=STATUS_COLOR.get(status, 'secondary'), className='me-1')


def tag_badges(tags: list[str]):
    return [dbc.Badge(t, color='light', text_color='dark', className='me-1 border') for t in tags]


def task_link(task: dict, prefix: bool = True):
    return dcc.Link(f"#{task['id']} {task['title']}" if prefix else task['title'],
                    href=f"/task/{task['id']}", className='text-decoration-none fw-semibold')


def multiline(text: str):
    return html.Div(text, style={'whiteSpace': 'pre-wrap'})


def ref_target(ref: dict):
    """참고자료의 실제 내용(링크/다운로드/메모 본문)."""
    if ref['kind'] == 'link':
        return html.A(ref['url'], href=ref['url'], target='_blank', rel='noopener noreferrer',
                      className='small text-break')
    if ref['kind'] == 'file':
        return html.A([html.I(className='bi bi-download me-1'), ref['file_name']],
                      href=f"/files/{ref['id']}", className='small')
    return None
