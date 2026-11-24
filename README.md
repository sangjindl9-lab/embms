# EUM BMS PRO — 이음BMS

BMS · Power SCADA · EMS · 원격검침 통합 플랫폼. **개발 버전 v0.3.0 — 상용 운영 전 단계.**

## 구조

```
eumbms/            중앙 서버 (FastAPI)
  main.py          앱, 보안 헤더(CSP: unsafe-inline 없음), /health
  models.py        고객사(테넌트)·사용자·세션·감사로그·사업장·건물·게이트웨이·장치·태그·측정값
  deps.py          세션 인증, RBAC, 테넌트 격리 헬퍼
  routers/         auth · admin(테넌트/사용자/감사) · assets(사업장/건물/장치/태그) · gateways
  static/          UI (JS/CSS 분리)
migrations/        Alembic 마이그레이션 (앱 시작 시 upgrade head)
gateway/           현장 엣지 게이트웨이 에이전트 (별도 배포, 현장망 내부)
tests/             pytest — 인증·RBAC·테넌트 격리·로그인 제한·게이트웨이·Modbus 시뮬레이터
```

## 보안 모델

- 계정: scrypt 비밀번호 해시, 세션 토큰(DB에는 SHA-256 해시만 저장), 12시간 만료
- 브라우저: HttpOnly·Secure·SameSite=Strict 쿠키 + CSRF 토큰 헤더 / API: `Authorization: Bearer`
- 권한: `superadmin`(플랫폼) > `tenant_admin` > `operator` > `viewer`
- 테넌트 격리: 모든 데이터에 `tenant_id`, 타 고객사 객체는 404로 응답(존재 여부 비노출)
- 로그인 제한: DB 기반(다중 인스턴스/재시작 공유) — 계정당 10회/5분, IP당 50회/5분
- 프록시 IP: `TRUSTED_PROXY_HOPS`만큼 X-Forwarded-For 오른쪽에서 선택(왼쪽 값은 위조 가능)
- 감사로그: 로그인/실패/로그아웃, 생성·수정·삭제, 토큰 재발급
- 최초 관리자: DB에 superadmin이 없을 때만 `ADMIN_USER`/`ADMIN_PASSWORD`(20자 이상)로 생성. 기존 계정은 덮어쓰지 않음

## 현장 수집 구조 (P2)

중앙 서버는 PLC/RTU에 **절대 직접 접속하지 않습니다.** 현장 게이트웨이가 현장망에서 장치를 읽고(읽기 전용 FC1–4),
로컬 SQLite 버퍼에 저장한 뒤 **아웃바운드 HTTPS**로 `/api/gw/readings`에 업로드합니다.

```bash
pip install -r gateway/requirements.txt
EUM_SERVER_URL=https://<서버> EUM_GATEWAY_TOKEN=gw_... EUM_ALLOWED_NETWORKS=192.168.10.0/24 python gateway/agent.py
```

- 허용 네트워크(`EUM_ALLOWED_NETWORKS`) 밖 주소는 접속하지 않음 — SSRF/사설망 정책은 게이트웨이에서 관리
- 데이터 타입: bool, (u)int16/32/64, float32/64 · 워드/바이트 순서(ABCD/CDAB/BADC/DCBA) · 배율·오프셋
- WAN 장애 시 로컬 버퍼 유지, 지수 백오프 재전송, 서버측 (tag, ts) 중복 제거
- pymodbus 3.8(`slave=`)·3.10+(`device_id=`) 인수 차이를 런타임 감지

## 관제 기능 (P4)

- 관제 대시보드: 현재 전력·금일 사용량·금일 최대전력·장치 통신상태·활성 경보, 사업장 카드, 장치별 실시간 값
- 단선결선도: 사업장별 SVG 결선도(모선·차단기·변압기·계량기·부하), 태그 값과 차단기 상태(투입=적색/개방=녹색) 표시, JSON 편집
- 트렌드: 최대 4개 태그, 1시간~30일, 구간 평균·최소·최대, CSV(엑셀) 내보내기
- 경보: 상한/하한(불감대)·상태값·통신두절, 등급(정보·경·중·중대), 확인(ACK)·이력, 감사로그
- 장비 템플릿: PZEM-016, Eastron SDM120 (문서 기준, 실장비 미검증), 가상 전력계
- 가상 수변전실: 실제 장비 없이 화면·경보를 시험하는 시뮬레이터 장치 (서버에서 값 생성, 현장 장비에 접속하지 않음)
- 데이터 보존: 측정값 READINGS_RETENTION_DAYS(기본 90일), 해제된 경보 365일

## Windows 게이트웨이 패키지

`gateway-v*` 태그를 올리면 GitHub Actions가 Windows exe를 빌드·시험(시뮬레이터로 PZEM-016 프레임 읽기)하고 릴리스에 zip을 올립니다.
다운로드: https://github.com/sangjindl9-lab/embms/releases/latest/download/EUM-Gateway-Windows.zip

- `EUM-Gateway.exe --ports` 포트 찾기 · `--test COM3 --template pzem016` 서버 없이 장비 읽기 · `--check` 서버/토큰 확인
- 설정: 프로그램 폴더의 `gateway.ini` (url, token, allowed_networks) — 환경변수가 있으면 우선
- `gateway/device_templates.py`는 `eumbms/templates.py`의 사본 (테스트로 동기화 확인)

## 원격제어

**비활성.** 쓰기(FC5/6/15/16) 경로가 코드에 없습니다. P6(이중 승인·인터록·실장비 시험) 완료 전까지 구현하지 않습니다.

## 실행

```bash
pip install -r requirements-dev.txt
pytest -q                                   # SQLite
TEST_DATABASE_URL=postgresql+psycopg://... pytest -q   # PostgreSQL
ADMIN_PASSWORD='20자-이상-개발용-비밀번호' docker compose up --build
```

## Render 배포

`render.yaml`은 무료 웹 서비스 `eumbms-web`만 선언하고, 별도로 만든 유료 DB `eumbms-db`를 **참조만** 합니다
(DB를 생성·변경하지 않음). `ADMIN_PASSWORD`는 Render 대시보드 환경변수에만 둡니다.

## 미구현 (로드맵)

P3 프로토콜 센터(제조사·모델·드라이버·공식문서 출처·롤백), BACnet/OPC UA 등 추가 프로토콜,
P4 보고서(일/월 PDF)·결선도 드래그 편집기, P5 원격검침·요금, P6 제한적 원격제어, P7 부하·복구·백업·운영 문서.
실장비 호환성 검증은 아직 없음(시뮬레이터 검증만 완료).
