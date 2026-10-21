"""주소를 복사해 다시 열 수 있는 단일 기록 화면."""
import dash
import dash_bootstrap_components as dbc
from dash import Input, Output, State, callback, dcc, html
from components import ui
from services import store

dash.register_page(__name__, path_template='/history/<entry_id>', name='기록', title='업무 기록 · 기록')


def layout(entry_id=None, **_):
    return html.Div([dcc.Store(id='record-entry-id', data=entry_id), ui.page_head('기록'),
                     html.Div(id='record-entry-view')])


@callback(Output('record-entry-view', 'children'), Input('history-changed', 'data'),
          State('record-entry-id', 'data'))
def render(_, eid):
    h = store.get_history(int(eid)) if str(eid).isdigit() else None
    if not h:
        return dbc.Alert(['기록이 없거나 휴지통에 있습니다. ', dcc.Link('휴지통 보기', href='/trash')], color='light')
    return dbc.ListGroup([ui.history_card(h, show_task=True, view='permalink',
        added_todos=store.todos_for_history(h['id']) if h['ai_todos'] else None)])
