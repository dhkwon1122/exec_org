# 업무 기록(exec_org) — 사내 빌드용 이미지 (Researcher-board Dockerfile 과 같은 구성)
#
# 사내망에서 빌드 시 pip 사내 저장소 / CA 인증서 / 프록시가 필요할 수 있어
# 모두 '빌드 인자(ARG)'로 받는다. 기본값은 사내 값이고, 사외에서는 빈 값으로 덮어쓴다.
#
# ── 빌드 예시 (사내망) ─────────────────────────────────────────────
#   docker compose build            # docker-compose.yml 의 build args 사용
#   # 또는
#   docker build -t exec-org:latest .
#
#   # 사내 CA 인증서가 필요하면 빌드 전에 certs/ 에 *.crt 를 넣어둔다 (certs/README.md).
#
# ── 빌드 예시 (사외/공용 PyPI) ─────────────────────────────────────
#   docker build --build-arg PIP_INDEX_URL= --build-arg PIP_TRUSTED_HOST= \
#     --build-arg HTTP_PROXY= --build-arg HTTPS_PROXY= -t exec-org:latest .

FROM python:3.11-slim

# ── 빌드 인자 (사내 기본값 내장. 빌드 시 --build-arg 로 덮어쓸 수 있음) ──
ARG PIP_INDEX_URL=http://repository.samsungds.net/repository/proxy-pypi-files.pythonhosted.org/simple
ARG PIP_TRUSTED_HOST=repository.samsungds.net
ARG PIP_CERT=
ARG HTTP_PROXY=http://12.26.204.100:8080
ARG HTTPS_PROXY=http://12.26.204.100:8080
ARG NO_PROXY=localhost,127.0.0.1,db,::1,samsungds.net,*.samsungds.net,*.samsung.net,12.0.0.0/8,10.0.0.0/8,192.0.0.0/8,172.0.0.0/8

# 한국 표준시(KST) — 이력 일시·할 일 마감(오늘/지난 마감 판정)이 서울 기준이 되게 한다.
# tzdata 패키지(아래 apt-get)가 있어야 TZ가 적용된다. docker-compose.yml 의 TZ로 덮어쓸 수 있다.
ENV TZ=Asia/Seoul
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# ── 1) 사내 CA 인증서 등록 ──
# certs/ 에 아래 중 하나(또는 둘 다)를 둘 수 있다:
#   (a) 개별 사내 CA:  certs/corp-root-ca.crt  → update-ca-certificates 로 등록
#   (b) 전체 CA 번들:  certs/ca-bundle.crt     → 시스템 번들을 통째로 교체
# 둘 다 없으면 컨테이너 기본 CA 로 빌드한다.
COPY certs/ /tmp/corp-certs/
RUN http_proxy="$HTTP_PROXY" https_proxy="$HTTPS_PROXY" no_proxy="$NO_PROXY" \
    apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl tzdata \
    && for f in /tmp/corp-certs/*.crt; do \
         [ -e "$f" ] || continue; \
         case "$f" in */ca-bundle.crt|*/server.crt) continue ;; esac; \
         cp "$f" /usr/local/share/ca-certificates/; \
       done \
    && update-ca-certificates \
    && if [ -f /tmp/corp-certs/ca-bundle.crt ]; then \
         cp /tmp/corp-certs/ca-bundle.crt /etc/ssl/certs/ca-certificates.crt; \
         echo '[build] 사내 CA 번들 적용: /etc/ssl/certs/ca-certificates.crt'; \
       fi \
    && rm -rf /var/lib/apt/lists/* /tmp/corp-certs

# 런타임 파이썬(requests/psycopg2 등)이 사내 CA 를 신뢰하도록 시스템 번들 지정
ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt

# ── 2) 의존성 설치 (사내 pip 인덱스/프록시/인증서 반영) ──
# ${VAR:+--flag $VAR} : VAR 가 비어있지 않을 때만 해당 옵션을 추가.
COPY requirements.txt .
RUN http_proxy="$HTTP_PROXY" https_proxy="$HTTPS_PROXY" no_proxy="$NO_PROXY" \
    pip install --upgrade pip \
      ${PIP_INDEX_URL:+--index-url "$PIP_INDEX_URL"} \
      ${PIP_TRUSTED_HOST:+--trusted-host "$PIP_TRUSTED_HOST"} \
      ${PIP_CERT:+--cert "$PIP_CERT"} \
    && http_proxy="$HTTP_PROXY" https_proxy="$HTTPS_PROXY" no_proxy="$NO_PROXY" \
    pip install \
      ${PIP_INDEX_URL:+--index-url "$PIP_INDEX_URL"} \
      ${PIP_TRUSTED_HOST:+--trusted-host "$PIP_TRUSTED_HOST"} \
      ${PIP_CERT:+--cert "$PIP_CERT"} \
      -r requirements.txt

# ── 3) 앱 소스 복사 ──
# data/ 와 .env 는 .dockerignore 로 제외 → 컨테이너에는 볼륨/환경변수로 주입.
COPY . .

# 애플리케이션은 root 권한이 필요하지 않다. 바인드 마운트하는 ./data 는 호스트의 실제
# 소유권을 그대로 쓰므로, 처음 배포할 때 scripts/fix_data_ownership.sh 를 sudo로 한 번
# 실행해 uid 10001 이 읽고 쓸 수 있게 한다 (안 하면 data/exec_org.db 생성이 Permission denied).
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app/data && chown -R app:app /app
USER app

# 리슨 포트 (기본 8502 — 같은 서버의 People in SAIT(8501)와 겹치지 않게). -e PORT=... 로 변경.
ENV PORT=8502
EXPOSE 8502

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT:-8502}/healthz" || exit 1

# WSGI 서버로 구동 (app.py 의 server = app.server).
# PORT 환경변수를 반영하려 shell 형식 + exec (gunicorn 이 PID 1 로 신호 수신).
# --timeout 300 은 LLM_TIMEOUT 기본값(300)과 맞춘 것 — AI 정리는 백그라운드 스레드에서 돌지만
# 요청 처리 중 LLM 을 기다리는 경우가 생겨도 워커가 강제 종료되지 않게 한다.
CMD exec gunicorn --bind "0.0.0.0:${PORT:-8502}" --workers "${GUNICORN_WORKERS:-2}" --timeout "${GUNICORN_TIMEOUT:-300}" app:server
