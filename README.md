# exec_org — 업무 이력·참고자료 관리 시스템

업무마다 **무슨 일이 언제 있었는지(이력)**와 **어떤 자료를 참고했는지(참고자료)**를 남기고,
**업무끼리의 관계(선행/후속/관련/파생)**를 연결해 흐름을 추적하는 Dash 웹앱.

민감한 업무 기록을 다루므로 **임원조직 담당자**만 로그인해서 쓸 수 있다.

주로 기록하는 것은 **업무 관련 피드백**과 **임원 동향**이다. 핵심은 **이력 기록과 검색**이다. 받아 적은 피드백·회의 메모를 다듬지 않고 그대로 붙여넣으면,
원문은 그대로 보존되고 LLM 이 검색용 **제목·요약·키워드(동의어 포함)**를 만들어 붙인다.

## 주요 기능

| 기능 | 내용 |
|---|---|
| 업무 | 제목·설명·상태(예정/진행중/보류/완료)·담당자·태그·시작/마감일·상위 업무 |
| 임원 동향 | `/people` 임원 목록(이름·직위·소속·별칭, 최근 기록 순)과 임원별 화면(`/people/<id>`): 그 임원과 연결된 모든 기록(동향·업무 기록) 타임라인, 자주 나온 키워드, 관련 업무. 동향은 업무 없이 임원만 지정해 기록 |
| 임원 불러오기 | 임원 목록의 'People in SAIT에서 불러오기'(또는 `scripts/sync_people.py`): Researcher-board 가 같은 DB 에 적재한 `researchers` 테이블에서 직급이 임원(`EXEC_ORG_POSITIONS`, 기본 사장·부사장·상무·고문·Master·Fellow — 대소문자 무시)이고 최신 인원(`is_current`≠N)인 사람의 이름·직위·소속을 가져온다(읽기 전용). 바로 바꾸지 않고 **변경 제안**(새 임원 추가 / 직접 등록해 둔 같은 이름에 사번 연결 / 이름·직위·소속이 달라진 항목별 '지금 값 → People in SAIT 값')을 보여 주고, 고른 것만 반영한다. 고르지 않은 덮어쓰기는 지금 값을 유지하고 기억해 두어 다음에는 기본 해제로 제안(People in SAIT 값이 또 바뀌면 다시 기본 선택). 새 임원에는 '성+직위' 별칭(김부사장, Master·Fellow 는 김마스터·김펠로우 — 같은 성·직위가 여럿이면 생략)을 넣는다. 별칭·메모·기록은 건드리지 않고, 최신 인원에서 빠진 임원은 알려만 준다. **과거 임원**(월별 이력 `researchers_history`에서 한 번이라도 임원 직급이었던 사람)도 '추적 중지' 상태로 추가하도록 제안한다. 반영할 때마다 사번이 연결된 모든 임원의 **경력 이력**(직급·소속이 바뀐 달, 인사발령 `hr_orders`)을 `person_career`에 쌓아 임원 화면에 보여 준다 — 원천에서 지워져도 남는다(누적). `scripts/sync_people.py`는 제안만 출력하고 `--apply`면 기본 선택대로 반영 |
| 임원 목록 | `/people` 맨 위에 표: 사번·CL·재직상태·성명·연차·나이·최종학력·입사일·근속·소속·기록 수. 기본은 **재직 중인 임원만**(People in SAIT 현재 임원 + 직접 등록해 추적 중인 임원), "전체 조회" 버튼을 누르면 퇴직·전출(현재 목록에서 빠진/과거 임원)까지. 나이 = 올해 - 출생연도, 연차 = 승격일부터 연차 기준일(매년 3/1)까지(People in SAIT 와 같은 계산), 근속 = 입사일부터 오늘까지. 인사 정보(재직상태·출생연도·입사일·승격일·최종학력)는 불러올 때 비어 있으면 채우고, 값이 바뀌면 덮어쓸지 제안 |
| 주요 이력 | 임원 화면 '주요 이력 → 정리': 경력 이력(People in SAIT 인사발령·직급/소속 변천)으로 초안을 만든다 — 사내 LLM 이 있으면 AI 가 의미 있는 것만 골라 한 줄씩 다듬고, 없거나 실패하면 승진·선임·보임 등 핵심 발령을 규칙으로 고른다. 표에서 고치고 저장하면 `person_highlights`에 남는다 |
| 평가 이력 | 임원 화면 '평가 이력 → 편집': 연도별 임원종합평가·MBO(그 밖의 평가 종류도) 등급·메모. `person_evaluations` |
| 엑셀 일괄 업로드 | 임원 목록 '엑셀 업로드'(`/people/import`): 첫 행 제목을 보고 DB 칸을 맞춘다 — 사번·성명·직위·입사일 같은 정해진 이름과 `25MBO`·`24Mbo`·`2024 임원종합평가` 같은 평가 제목(연도+종류)은 규칙으로, 나머지는 사내 AI 가 제목·예시 값을 보고 판단. 맞는 칸이 없으면 미리 만든다(처음 보는 평가 종류는 새 종류, 그 밖은 같은 이름의 '추가 항목'). 열마다 매칭을 바꿀 수 있고, 미리보기로 바뀌는 건수를 확인한 뒤 반영. 사번(없으면 성명)으로 임원을 찾고(앞자리 0 이 빠진 사번도), 빈 칸은 건드리지 않으며, 없는 임원은 건너뛰거나(기본) 새로 추가 |
| 임원 자동 연결 | 원문에 등록된 임원의 이름·별칭이 나오면 그 임원에게 자동 연결 (업무 피드백 속 임원 언급도 임원 화면에 모인다). 추적 중지한 임원은 제외 |
| 이력 | 메모/피드백/동향/회의/결정/진행/이슈/요청 유형으로 원문 그대로 기록. 실제 일시 지정, 텍스트 파일(.txt 등, UTF-8·CP949) 불러오기 |
| 이력별 관련 자료 | 이력마다 링크·파일·공유폴더 경로(`\\서버\폴더\파일`, 복사 버튼)를 붙인다. 원문 속 URL 은 자동으로 붙고, 이력 수정 창에서 추가·삭제. 이력 검색은 붙은 자료 제목·주소·파일명까지 찾는다 |
| 기록 양식·요령 | 유형별 양식(회의: 참석/논의/결정/후속, 결정: 이유/버린 대안 …)을 빈 칸에 넣는 '양식' 버튼과 '기록 요령' 도움말 |
| AI 정리 | 저장 즉시 원문이 남고, 백그라운드에서 LLM 이 제목·요약·키워드를 채움. 실패해도 기록은 유지되며 "다시 생성" 가능. 결과는 직접 수정 가능 |
| 이력 검색 | `/search` — 원문·AI 제목·요약·키워드·작성자·업무명·연결된 임원(이름·별칭·직위·소속) 전체 검색, 임원 필터. 여러 단어는 AND, `"따옴표"`는 구절. 유형·작성자·기간 필터, 일치 부분 강조, 자주 나온 키워드 |
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

### Docker (운영 권장)

Researcher-board(People in SAIT)와 같은 구성이다 — 사내 pip 저장소·프록시·CA 를 빌드 인자로 받고(`Dockerfile`),
`.env` 값을 `docker-compose.yml` 이 주입하며, 컨테이너는 비root(uid 10001)로 gunicorn 을 띄운다.

```bash
cp .env.example .env                  # DATABASE_URL = tea-meeting-agenda 의 .env 와 같은 값 (People in SAIT 사용자 DB)
                                      # EXEC_DB_PASSWORD = 이 앱 전용 PostgreSQL 비밀번호 (openssl rand -hex 24)
sudo scripts/fix_data_ownership.sh    # 처음 한 번: ./data 를 컨테이너 사용자 소유로 + 다른 사용자 접근 차단
docker compose up -d --build          # http://<서버>:8502
```

**업무 기록 DB — 이 앱 전용 PostgreSQL(`exec-db`)**

- `docker compose up` 하면 `exec-db`(postgres:16) 컨테이너가 함께 뜨고, 업무 기록·임원 정보·할 일이 모두 여기에 저장된다.
  People in SAIT DB 와는 별개다(그쪽은 로그인·임원 불러오기에 **읽기만**).
- 데이터는 도커 볼륨 `exec_pgdata`에 있다. `docker compose down -v` 는 이 볼륨까지 지우니 쓰지 않는다.
- 포트는 호스트의 `127.0.0.1:5434`(`EXEC_DB_PORT`)에만 열린다 — 서버 안에서 점검·백업용.
- **예전 SQLite 기록 옮기기**(이전 버전에서 쓰던 `./data/exec_org.db`가 있으면 처음 한 번):
  `docker compose exec app python scripts/migrate_to_postgres.py`
  — id 그대로 옮기고 행 수를 대조한다. 대상에 기록이 있으면 멈추므로 두 번 실행해도 안전하고, 원본 파일은 지우지 않는다.
  새 DB 가 비어 있는데 예전 파일이 있으면 앱 로그에 이 안내가 나온다.
- **백업**: `scripts/backup_db.sh` → `./backups/exec_org_<시각>.dump` (30일 지난 것은 정리, `KEEP_DAYS`).
  cron 에 걸어 둔다(예: `0 2 * * * cd /path/exec_org && scripts/backup_db.sh`).
  복원: `docker compose exec -T exec-db pg_restore -U exec_org -d exec_org --clean --if-exists < backups/<파일>.dump`
- 다른 PostgreSQL 서버를 쓰려면 `.env`에 `EXEC_ORG_DATABASE_URL`을 적는다(그 값이 우선).

- 기본 운영은 **HTTP**(`http://<서버>:8502`)다. 로그인 쿠키의 Secure 속성은 접속 방식에 맞춰 자동으로 정해지므로
  HTTP 에서 따로 설정할 것이 없다. 쿠키 이름은 `exec_org_session`이라 같은 서버의 People in SAIT 로그인과 겹치지 않는다.

- 사내 CA 가 필요하면 빌드 전에 `certs/`에 넣는다 (`certs/README.md`). People in SAIT 의 `certs/` 파일을 그대로 써도 된다.
- 사외에서 빌드할 때는 `docker-compose.yml`의 build args(사내 pip/프록시)를 빈 값으로 바꾼다.
- (선택) HTTPS 가 가능해지면: `scripts/gen_self_signed_cert.sh <IP>` 로 인증서를 만들고 `.env`에
  `TRUSTED_PROXY_COUNT=1` 을 넣은 뒤 `docker compose --profile proxy up -d` → `https://<서버>:8443`
- People in SAIT 가 같은 서버에서 postgres 컨테이너(bundled-db)를 쓰면 그 네트워크에 붙인다:
  `docker compose -f docker-compose.yml -f docker-compose.people-net.yml up -d --build`
  (이때 `DATABASE_URL`의 호스트는 `db`)
- 업로드 파일·세션 키는 `./data`에 쌓인다 → DB 백업(`backups/`)과 함께 **이 디렉터리도 백업**한다.
- 상태 확인: `curl http://localhost:8502/healthz`, 로그: `docker compose logs -f app`

### 직접 실행 (개발)

```bash
pip install -r requirements.txt
python app.py                # http://localhost:8050
```

- 설정은 `.env.example` 참고. LLM 은 OpenAI 호환 `chat/completions` 엔드포인트(사내 vLLM 등)를 쓰며,
  `LLM_API_URL`(없으면 Researcher-board 의 `LLM2_API_URL`)이 비어 있으면 AI 기능만 꺼지고 나머지는 그대로 동작한다.
- `EXEC_ORG_DATABASE_URL`이 없으면 저장소는 `data/exec_org.db`(SQLite — 개발·테스트용), 업로드 파일은 `data/uploads/`. `data/`는 형상관리에서 제외된다.
- 업무 기록을 PostgreSQL 에 두려면 `EXEC_ORG_DATABASE_URL`을 지정한다 (`DATABASE_URL`은 사용자 DB). 테이블은 시작 시 자동 생성되고,
  이전 버전 DB 에 없는 컬럼은 자동으로 추가된다.

## 로그인과 접근 권한

- **계정**: `DATABASE_URL`(tea-meeting-agenda 와 같은 이름·값, 또는 `USER_DATABASE_URL`)에 People in SAIT 의 PostgreSQL 을 지정하면 그 `app_users` 계정·비밀번호로
  로그인한다. 이 테이블은 다른 시스템도 함께 쓰므로 **읽기만** 하고(쓰기·스키마 변경 없음), 계정 생성·비밀번호 변경은
  People in SAIT 에서 한다. 임시 비밀번호 상태(`must_change_password`)인 계정은 로그인을 막는다.
- **접속 권한**: `app_users.role` 이 `executive_org`(임원조직 담당자)인 계정만 로그인된다. 그 외 역할은 계정이 있어도
  막힌다. 허용 역할은 `EXEC_ORG_ROLES`로 바꿀 수 있다(쉼표 구분).
- 역할은 매 요청마다 사용자 DB 에서 다시 확인하므로, People in SAIT 에서 역할이 바뀌면 이미 로그인한 사람도
  다음 클릭부터 막힌다.
- 모든 화면·Dash 콜백·첨부파일 다운로드가 로그인 뒤에만 열린다. 로그인 5회 실패 시 10분 잠금, 세션 8시간,
  HttpOnly·SameSite 쿠키(이름 `exec_org_session`), 로그인 요청 출처(Origin) 확인, 응답 캐시 금지(no-store).
  로그인 전에는 쿠키를 쓰지 않고 성공했을 때만 세션 쿠키를 준다 (tea-meeting-agenda 와 같은 흐름).
- **로그인이 안 될 때**: `docker compose exec app python scripts/auth_check.py <아이디> <비밀번호>` 로 사용자 DB 연결 →
  계정 → 비밀번호 → 역할 → 세션 키를 단계별로 확인하고, `docker compose logs app | grep login` 으로 실패 사유를 본다.
- 단독 실행/개발: `DATABASE_URL`·`USER_DATABASE_URL`을 비우면 로컬 `app_users`를 쓴다 → `python scripts/create_user.py <아이디> <이름>`

## 구조

```
app.py                  앱 진입점, 네비게이션, 파일 다운로드 라우트(/files/<id>)
services/db.py          DB 연결·테이블 정의 (tasks, history, references_, task_links, people, history_people, todos)
                        + 예전 DB 자동 마이그레이션(컬럼 추가, 업무 없는 이력을 위한 NOT NULL 해제)
services/store.py       CRUD + 자동 이력 기록 + 이력 검색 (화면은 이 모듈의 dict만 사용)
services/llm.py         OpenAI 호환 LLM 호출, 원문 → 제목·요약·키워드
services/enrich.py      AI 정리 백그라운드 처리 (스레드풀)
services/users.py       사용자 DB(app_users) 조회·비밀번호 확인 — 공유 DB 는 읽기 전용
services/auth.py        역할 기반 접속 제한, 로그인 세션, 로그인 시도 제한
components/ui.py        배지·아이콘·이력 카드·검색어 강조 등 공용 표시 조각
components/history_form.py  이력 입력 폼 (업무 상세·빠른 기록 공용)
components/todo_ui.py   할 일 목록·추가 폼, 체크/삭제 공용 콜백
components/history_actions.py  이력 수정 창·삭제·AI 다시 생성·할 일로 추가 (모든 화면 공용)
pages/home.py           업무 목록·검색·새 업무·빠른 기록·최근 이력
pages/search.py         이력 검색
pages/people.py         임원 목록·추가, 최근 동향
pages/person.py         임원별 기록 모음·동향 기록
pages/todos.py          할 일
pages/calendar.py       캘린더
pages/task_detail.py    업무 상세 (이력 / 할 일 / 참고자료 / 연결 업무 탭, 하위 업무)
pages/references.py     전체 참고자료 검색
services/people_sync.py People in SAIT researchers 테이블에서 임원 기본 정보 불러오기 (읽기 전용)
scripts/                auth_check.py(로그인 진단), sync_people.py(임원 불러오기), create_user.py(로컬 계정), fix_data_ownership.sh, gen_self_signed_cert.sh
Dockerfile, docker-compose*.yml, deploy/nginx.conf, certs/   Docker 실행 구성
tests/                  저장·검색·LLM 파싱 테스트 (pip install -r requirements-dev.txt; pytest)
```

## 아직 없는 것 (다음 단계 후보)

- 업무별 공개 범위 (특정 업무를 일부 사람만 보게)
- 접근 기록(누가 언제 무엇을 열람·다운로드했는지) 감사 로그
- AI 의미 검색 (질문을 LLM 으로 검색어 확장, 또는 임베딩 기반 유사 이력 찾기)
- 업무 연결 관계 그래프 시각화
- 엑셀 내보내기
