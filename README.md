# exec_org — 업무 이력·참고자료 관리 시스템

업무마다 **무슨 일이 언제 있었는지(이력)**와 **어떤 자료를 참고했는지(참고자료)**를 남기고,
**업무끼리의 관계(선행/후속/관련/파생)**를 연결해 흐름을 추적하는 Dash 웹앱.

## 주요 기능

| 기능 | 내용 |
|---|---|
| 업무 | 제목·설명·상태(예정/진행중/보류/완료)·담당자·태그·시작/마감일·상위 업무 |
| 이력 | 메모/회의/결정/진행/이슈/요청 유형으로 기록. 실제 일시를 지정할 수 있음 |
| 자동 이력 | 업무 생성, 상태 변경, 참고자료 추가, 업무 연결 시 이력에 자동으로 남음 |
| 참고자료 | 링크 / 파일 업로드(최대 50MB) / 메모. 업무 화면과 전체 자료 검색 화면에서 조회 |
| 업무 연결 | A→B "선행"으로 연결하면 B 쪽에는 "후속"으로 표시. 양쪽 이력에 모두 기록 |
| 검색 | 업무 제목·설명·태그뿐 아니라 **이력 내용과 참고자료**까지 검색 |
| 작성자 | 상단 이름 칸에 입력하면 브라우저에 기억되어 이력·자료에 함께 기록 |

## 실행

```bash
pip install -r requirements.txt
python app.py                # http://localhost:8050
# 운영
gunicorn app:server -b 0.0.0.0:8050 --workers 2
```

- 기본 저장소는 `data/exec_org.db`(SQLite), 업로드 파일은 `data/uploads/`. `data/`는 형상관리에서 제외된다.
- `DATABASE_URL` 환경변수(또는 `.env`)를 지정하면 PostgreSQL 등 다른 DB를 사용한다. 테이블은 시작 시 자동 생성.

## 구조

```
app.py                  앱 진입점, 네비게이션, 파일 다운로드 라우트(/files/<id>)
services/db.py          DB 연결·테이블 정의 (tasks, history, references_, task_links)
services/store.py       CRUD + 자동 이력 기록 (화면은 이 모듈의 dict만 사용)
components/ui.py        배지·아이콘 등 공용 표시 조각
pages/home.py           업무 목록·검색·새 업무·최근 이력
pages/task_detail.py    업무 상세 (이력 / 참고자료 / 연결 업무 탭, 하위 업무)
pages/references.py     전체 참고자료 검색
tests/test_store.py     저장 계층 테스트 (pip install -r requirements-dev.txt; pytest)
```

## 아직 없는 것 (다음 단계 후보)

- 로그인/권한 (현재는 작성자 이름만 기록)
- 업무 연결 관계 그래프 시각화
- 엑셀 내보내기, 이력 수정 기능
