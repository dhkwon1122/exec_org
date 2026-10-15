# exec_org — 업무 이력·참고자료 관리 시스템

업무마다 **무슨 일이 언제 있었는지(이력)**와 **어떤 자료를 참고했는지(참고자료)**를 남기고,
**업무끼리의 관계(선행/후속/관련/파생)**를 연결해 흐름을 추적하는 Dash 웹앱.

민감한 업무 기록을 다루므로 **로그인 + 접근 허용 목록**에 있는 사람만 쓸 수 있다.

핵심은 **이력 기록과 검색**이다. 받아 적은 피드백·회의 메모를 다듬지 않고 그대로 붙여넣으면,
원문은 그대로 보존되고 LLM 이 검색용 **제목·요약·키워드(동의어 포함)**를 만들어 붙인다.

## 주요 기능

| 기능 | 내용 |
|---|---|
| 업무 | 제목·설명·상태(예정/진행중/보류/완료)·담당자·태그·시작/마감일·상위 업무 |
| 이력 | 메모/피드백/회의/결정/진행/이슈/요청 유형으로 원문 그대로 기록. 실제 일시 지정, 텍스트 파일(.txt 등, UTF-8·CP949) 불러오기 |
| AI 정리 | 저장 즉시 원문이 남고, 백그라운드에서 LLM 이 제목·요약·키워드를 채움. 실패해도 기록은 유지되며 "다시 생성" 가능. 결과는 직접 수정 가능 |
| 이력 검색 | `/search` — 원문·AI 제목·요약·키워드·작성자·업무명 전체 검색. 여러 단어는 AND, `"따옴표"`는 구절. 유형·작성자·기간 필터, 일치 부분 강조, 자주 나온 키워드 |
| 빠른 기록 | 홈 화면에서 업무만 골라 바로 붙여넣기 |
| 자동 이력 | 업무 생성, 상태 변경, 참고자료 추가, 업무 연결 시 이력에 자동으로 남음 |
| 참고자료 | 링크 / 파일 업로드(최대 50MB) / 메모. 업무 화면과 전체 자료 검색 화면에서 조회 |
| 업무 연결 | A→B "선행"으로 연결하면 B 쪽에는 "후속"으로 표시. 양쪽 이력에 모두 기록 |
| 업무 검색 | 업무 제목·설명·태그뿐 아니라 **이력(원문·AI 요약·키워드)과 참고자료**까지 검색 |
| 할 일 | 업무별/단독 할 일, 마감일·담당자. `/todos`에서 지난 마감·오늘·7일 이내·이후·날짜 없음으로 묶어 보기. 완료하면 업무 이력에 남음 |
| AI 할 일 추출 | 원문에서 요청·지시·후속 조치를 찾아 할 일 제안("금요일까지" → 실제 날짜). 버튼 하나로 할 일에 추가 |
| 캘린더 | `/calendar` 월 달력에 할 일 마감·업무 시작/마감·이력 건수. 날짜를 누르면 그날의 할 일·업무·이력 |
| 로그인·권한 | 아래 "로그인과 접근 권한" 참고. 작성자는 로그인한 사용자로 자동 기록 |

## 실행

```bash
pip install -r requirements.txt
python app.py                # http://localhost:8050
# 운영
gunicorn app:server -b 0.0.0.0:8050 --workers 2
```

- 설정은 `.env.example` 참고. LLM 은 OpenAI 호환 `chat/completions` 엔드포인트(사내 vLLM 등)를 쓰며,
  `LLM_API_URL`(없으면 Researcher-board 의 `LLM2_API_URL`)이 비어 있으면 AI 기능만 꺼지고 나머지는 그대로 동작한다.
- 기본 저장소는 `data/exec_org.db`(SQLite), 업로드 파일은 `data/uploads/`. `data/`는 형상관리에서 제외된다.
- `DATABASE_URL` 환경변수(또는 `.env`)를 지정하면 PostgreSQL 등 다른 DB를 사용한다. 테이블은 시작 시 자동 생성되고,
  이전 버전 DB 에 없는 컬럼은 자동으로 추가된다.

## 로그인과 접근 권한

- **계정**: `USER_DATABASE_URL`에 People in SAIT(Researcher-board)의 PostgreSQL 을 지정하면 그 `app_users` 계정·비밀번호로
  로그인한다. 이 테이블은 다른 시스템도 함께 쓰므로 **읽기만** 하고(쓰기·스키마 변경 없음), 계정 생성·비밀번호 변경은
  People in SAIT 에서 한다. 임시 비밀번호 상태(`must_change_password`)인 계정은 로그인을 막는다.
- **접근 허용 목록**: 계정이 있다고 들어올 수 있는 것은 아니다. 이 앱 DB 의 `exec_members`에 등록된 사람만 로그인된다.
  - 관리자: 업무 기록 전체 + '접근 권한' 화면에서 사용자 추가/회수
  - 사용자: 업무 기록 전체
  - 첫 관리자는 `EXEC_ORG_ADMINS=아이디` 환경변수로 지정하거나 `python scripts/grant_access.py <아이디> admin`
- 권한은 매 요청마다 다시 확인하므로, 회수하면 이미 로그인한 사람도 다음 클릭부터 막힌다.
- 모든 화면·Dash 콜백·첨부파일 다운로드가 로그인 뒤에만 열린다. 로그인 5회 실패 시 10분 잠금, 세션 8시간,
  HttpOnly·SameSite 쿠키, 로그인 폼 CSRF 토큰, 응답 캐시 금지(no-store).
- 단독 실행/개발: `USER_DATABASE_URL`을 비우면 로컬 `app_users`를 쓴다 → `python scripts/create_user.py <아이디> <이름> --admin`

## 구조

```
app.py                  앱 진입점, 네비게이션, 파일 다운로드 라우트(/files/<id>)
services/db.py          DB 연결·테이블 정의 (tasks, history, references_, task_links)
services/store.py       CRUD + 자동 이력 기록 + 이력 검색 (화면은 이 모듈의 dict만 사용)
services/llm.py         OpenAI 호환 LLM 호출, 원문 → 제목·요약·키워드
services/enrich.py      AI 정리 백그라운드 처리 (스레드풀)
services/users.py       사용자 DB(app_users) 조회·비밀번호 확인 — 공유 DB 는 읽기 전용
services/auth.py        접근 허용 목록, 로그인 세션, 로그인 시도 제한
components/ui.py        배지·아이콘·이력 카드·검색어 강조 등 공용 표시 조각
components/history_form.py  이력 입력 폼 (업무 상세·빠른 기록 공용)
components/todo_ui.py   할 일 목록·추가 폼, 체크/삭제 공용 콜백
pages/home.py           업무 목록·검색·새 업무·빠른 기록·최근 이력
pages/search.py         이력 검색
pages/todos.py          할 일
pages/calendar.py       캘린더
pages/admin.py          접근 권한 관리 (관리자)
scripts/                create_user.py(로컬 계정), grant_access.py(접근 권한 부여)
pages/task_detail.py    업무 상세 (이력 / 참고자료 / 연결 업무 탭, 하위 업무)
pages/references.py     전체 참고자료 검색
tests/                  저장·검색·LLM 파싱 테스트 (pip install -r requirements-dev.txt; pytest)
```

## 아직 없는 것 (다음 단계 후보)

- 업무별 공개 범위 (특정 업무를 일부 사람만 보게)
- 접근 기록(누가 언제 무엇을 열람·다운로드했는지) 감사 로그
- AI 의미 검색 (질문을 LLM 으로 검색어 확장, 또는 임베딩 기반 유사 이력 찾기)
- 업무 연결 관계 그래프 시각화
- 엑셀 내보내기
