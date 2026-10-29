# 인증서

Researcher-board 의 `certs/` 와 같은 용도의 두 종류가 들어간다 (둘 다 `.gitignore` 로 커밋 제외).

1. **사내 CA 인증서** — 이미지 빌드 시 pip 설치·사내 프록시 HTTPS 를 신뢰하게 하는 용도. Dockerfile 이 사용.
   - 개별 CA: PEM 형식 `.crt` 를 넣는다 (예: `corp-root-ca.crt`) → `update-ca-certificates` 로 등록
   - 전체 번들: `ca-bundle.crt` (WSL 의 `/etc/ssl/certs/ca-certificates.crt` 를 그대로 복사) → 시스템 번들 교체
   - People in SAIT 에서 쓰던 파일을 그대로 복사해 와도 된다.
2. **리버스 프록시 서버 인증서** — `server.crt` / `server.key`. `docker compose --profile proxy` 의 nginx 가 마운트.
   - 개발/임시: `scripts/gen_self_signed_cert.sh [도메인_또는_IP]`
   - 운영: 사내 PKI 발급 인증서를 같은 파일명으로 교체
3. **ADFS 토큰 서명 인증서** (AD SSO, 선택) — `adfs-signing.pem` 또는 `.cer`(DER) 로 넣고 `.env` 에
   `SSO_CERT_FILE=/app/certs/adfs-signing.pem`. `.crt` 로 두면 1번처럼 CA 로 등록되므로 확장자를 다르게 한다.
   비워 두면 JWKS 엔드포인트(`https://{SSO_HOST}/adfs/discovery/keys`)에서 공개키를 받아 온다.
