"""로그인 사용자 개인 북마크."""
import dash
import dash_bootstrap_components as dbc
from dash import Input, Output, callback, html
from components import ui
from services import auth, store

dash.register_page(__name__, path='/bookmarks', name='북마크', title='업무 기록 · 북마크')


def layout(**_):
    return html.Div([ui.page_head('북마크', '내가 저장한 기록'),
        dbc.Input(id='bookmarks-q', type='search', debounce=True, placeholder='저장한 기록 검색', className='mb-3'),
        html.Div(id='bookmarks-results')])


@callback(Output('bookmarks-results', 'children'), Input('bookmarks-q', 'value'), Input('history-changed', 'data'))
def render(q, _):
    user = auth.current_user() or {}
    rows = store.search_history(q or '', bookmark_user=user.get('user_id', ''), include_auto=True)
    if not rows:
        return dbc.Alert('저장한 기록이 없습니다. 기록의 북마크 버튼을 눌러 모아보세요.', color='light')
    return [html.P(f'{len(rows)}건 · 최대 200건 표시', className='small text-muted'),
            dbc.ListGroup([ui.history_card(h, show_task=True, view='bookmarks') for h in rows])]
