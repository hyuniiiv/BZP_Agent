# BZP Agent

사내 업무 자동화 트레이 앱 (Windows). 우측 하단 트레이에서 동작하며 다음 기능을 제공한다(설치 직후 전부 꺼짐, 환경설정에서 개별 활성화):

- **네트워크 인증 자동화** — 사내망 인증 페이지 자동 로그인
- **GitLab 동기화** (개발자용, 선택) — 지정 저장소 일일 FF-only 동기화
- **lab dev 서버 감시** (개발자용, 선택) — 3003 포트 다운 시 자동 재실행
- **자동 업데이트** — GitHub Releases 기반 원클릭 반자동 업데이트

## 배포/설치

- 팀원 배포: `BZP_Agent_배포.zip` → `install.bat` 실행 (`%LOCALAPPDATA%\BZP_Agent` 설치 + 시작프로그램 등록)
- 처음 사용: 트레이 아이콘 우클릭 → **환경설정...** → 네트워크 인증 켜고 ID/PW 입력

## 자동 업데이트 구조

- `version.py`의 `APP_VERSION`이 현재 버전
- 앱이 시작 시 + 하루 1회 GitHub `releases/latest` 확인 → 더 높은 버전이면 트레이 알림
- "지금 업데이트" 클릭 → 릴리스 자산 `BZP_Agent_code.zip`(exe + `_internal`만) 다운로드 → PowerShell 업데이터가 앱 종료 대기 후 교체 + 재시작
- `config.yaml` / `.env` / `ms-playwright` 는 교체 대상에서 제외되어 보존됨
- Private 저장소이므로 읽기전용 fine-grained 토큰(`update_token.txt`)을 빌드 시 번들에 포함

## 새 버전 릴리스 절차

1. `version.py`의 `APP_VERSION` 올림 (예: `1.0.1`)
2. `pyinstaller NetworkAuthTray.spec --noconfirm` 로 재빌드
3. 코드 zip 생성: `dist/BZP_Agent/`의 `BZP_Agent.exe` + `_internal/` 만 묶어 `BZP_Agent_code.zip`
4. `gh release create vX.Y.Z BZP_Agent_code.zip --title "vX.Y.Z" --notes "..."`
5. (신규 설치본도 갱신하려면) `ms-playwright` + `config.yaml` 조립 후 `BZP_Agent_배포.zip` 재패키징

> `update_token.txt`, `.env`, `config.yaml`(개인)은 절대 커밋하지 않는다(.gitignore).
