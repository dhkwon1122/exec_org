#!/usr/bin/env bash
# reverse-proxy(nginx) 컨테이너용 자체 서명(self-signed) TLS 인증서를
# certs/ 아래에 생성한다.
#
# 어디까지나 임시/개발용이다 — 브라우저가 "신뢰할 수 없는 인증서" 경고를
# 띄운다(사내 PKI가 서명한 게 아니므로). 실제 운영 배포에서는 사내
# 보안팀에서 발급받은 인증서로 certs/server.crt·certs/server.key를
# 교체할 것 — deploy/nginx.conf가 참조하는 파일명은 그대로 두면 된다.
#
# 사용법:
#   scripts/gen_self_signed_cert.sh [도메인_또는_IP]
#   생략하면 localhost로 발급한다. 다른 PC에서 워크스테이션의 LAN IP로
#   접속할 거라면(도메인 없이 IP로 직접 붙는 경우) 그 IP를 인자로 준다 —
#   예: scripts/gen_self_signed_cert.sh 192.168.0.42
#   (IPv4/IPv6는 자동 감지해 SAN을 DNS: 대신 IP:로 넣는다 — 그래야 브라우저가
#   "호스트 이름 불일치"로 추가 거부하지 않는다.)

set -euo pipefail

DOMAIN="${1:-localhost}"
CERTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/certs"
CRT_PATH="$CERTS_DIR/server.crt"
KEY_PATH="$CERTS_DIR/server.key"

if ! command -v openssl >/dev/null 2>&1; then
  echo "[gen_self_signed_cert] openssl이 필요합니다." >&2
  exit 1
fi

mkdir -p "$CERTS_DIR"

if [ -f "$CRT_PATH" ] || [ -f "$KEY_PATH" ]; then
  echo "[gen_self_signed_cert] 이미 인증서가 있습니다: $CRT_PATH" >&2
  echo "[gen_self_signed_cert] 새로 만들려면 기존 파일을 먼저 지우세요." >&2
  exit 1
fi

# SAN(Subject Alternative Name)은 접속 방식과 타입이 맞아야 한다 — IP로
# 접속하는데 SAN을 DNS:로 넣으면 브라우저가 "자체 서명이라 신뢰 안 됨"과는
# 별개로 "호스트 이름 불일치"까지 추가로 띄운다.
if [[ "$DOMAIN" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]] || [[ "$DOMAIN" == *:* ]]; then
  SAN="IP:${DOMAIN}"
else
  SAN="DNS:${DOMAIN}"
fi

openssl req -x509 -nodes -newkey rsa:2048 \
  -keyout "$KEY_PATH" -out "$CRT_PATH" -days 825 \
  -subj "/CN=${DOMAIN}" \
  -addext "subjectAltName=${SAN}"

chmod 600 "$KEY_PATH"

echo "[gen_self_signed_cert] 생성 완료:"
echo "  $CRT_PATH"
echo "  $KEY_PATH"
echo "[gen_self_signed_cert] 브라우저는 신뢰하지 않는 인증서로 표시됩니다(임시/개발용)."
echo "[gen_self_signed_cert] 운영 배포에서는 사내 PKI 발급 인증서로 같은 파일명으로 교체하세요."
