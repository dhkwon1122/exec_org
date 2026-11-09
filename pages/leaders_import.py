"""리더 정보 엑셀 일괄 업로드 — 임원 엑셀 업로드(pages/people_import.py) 화면을 role='leader' 로.
리더급 현황 칸(Platform·그룹·과제구분 등)은 그 칸에, 없는 사람은 리더로 추가할 수 있다."""
import dash

from pages import people_import

dash.register_page(__name__, path='/leaders/import', name='리더 엑셀 업로드', title='업무 기록 · 리더 엑셀 업로드')


def layout(**_):
    return people_import.build('leader')
