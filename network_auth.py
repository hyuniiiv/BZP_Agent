"""
네트워크 인증 자동화 모듈 (Playwright 기반)
http://172.28.200.250/cwp2/faces/common/userAuth.xhtml 에서
세션 만료 시 자동으로 재인증합니다.

Playwright headless 브라우저를 사용하므로 JavaScript, 쿠키, 리다이렉트를
실제 브라우저와 동일하게 처리합니다.
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

_BASE_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
_DEFAULT_URL = "http://172.28.200.250/cwp2/faces/common/userAuth.xhtml"
_AUTHENTICATED_TEXT = "인증사용자"
_SESSION_FILE = _BASE_DIR / "data" / "network_auth_session.json"
_ENV_FILE = _BASE_DIR / ".env"


class NetworkAuthenticator:
    """
    Playwright headless 브라우저로 네트워크 인증 상태를 확인하고
    세션이 만료되면 자동으로 재인증합니다.
    """

    def __init__(self, config: dict):
        cfg = config.get("network_auth", {})
        self._url = cfg.get("url", _DEFAULT_URL)
        self._check_interval = cfg.get("check_interval", 600)  # 기본 10분
        self._run_hour_start = cfg.get("run_hour_start", 8)    # 실행 시작 시각 (기본 8시)
        self._run_hour_end = cfg.get("run_hour_end", 9)        # 실행 종료 시각 (기본 9시)
        self._user_id = ""
        self._password = ""
        self._refresh_credentials()
        self._id_field = cfg.get("id_field", "")
        self._pw_field = cfg.get("pw_field", "")

        self._playwright = None
        self._browser = None
        self._page = None

    def _refresh_credentials(self):
        """.env를 다시 읽어 최신 자격증명을 반영 (재기동 없이 비밀번호 변경 반영)."""
        try:
            load_dotenv(_ENV_FILE, override=True)
        except Exception as e:
            logger.warning(f"자격증명 새로고침 실패: {e}")
        self._user_id = os.environ.get("NETWORK_ID", "")
        self._password = os.environ.get("NETWORK_PW", "")

    # ──────────────────────────────────────────────
    # 브라우저 초기화 / 정리
    # ──────────────────────────────────────────────

    async def _ensure_page(self):
        """Playwright 브라우저와 페이지를 초기화 (한 번만)"""
        if self._page is not None:
            return
        from playwright.async_api import async_playwright
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=True)
        self._page = await self._browser.new_page()
        logger.debug("Playwright 브라우저 초기화 완료")

    async def _reset_browser(self):
        """오류 발생 시 브라우저를 재생성"""
        await self.close()
        await self._ensure_page()

    # ──────────────────────────────────────────────
    # 공개 인터페이스
    # ──────────────────────────────────────────────

    async def check_and_login(self) -> bool:
        """인증 상태 확인 후 필요 시 로그인. 인증 성공 여부 반환.

        1) 저장된 쿠키로 가벼운 HTTP 요청만 시도 (브라우저 미기동) — 대부분 여기서 끝남.
        2) 쿠키가 없거나 만료됐으면 Playwright 브라우저로 실제 확인/로그인
           (이 시점에 한해 .env를 다시 읽어 최신 자격증명을 반영한다).
        """
        if await self._check_via_http():
            logger.info("네트워크 인증 상태(HTTP, 브라우저 미기동): 인증됨")
            return True

        try:
            await self._ensure_page()
        except Exception as e:
            logger.error(f"Playwright 초기화 실패: {e}")
            return False

        try:
            await self._page.goto(self._url, wait_until="domcontentloaded", timeout=15000)
        except Exception as e:
            logger.error(f"네트워크 인증 페이지 접근 실패: {e}")
            await self._reset_browser()
            return False

        # 인증 여부 확인
        if await self._is_authenticated():
            logger.info("네트워크 인증 상태: 인증됨")
            await self._save_cookies()
            return True

        # sessExpired 페이지인 경우 → /cwp2 경유로 세션 생성 후 userAuth.xhtml 재확인
        current_url = self._page.url
        if "sessExpired" in current_url:
            logger.info("세션 만료 감지 — /cwp2 경유 후 인증 페이지 재확인")
            parsed = urlparse(self._url)
            base = f"{parsed.scheme}://{parsed.netloc}"
            try:
                # /cwp2 접근으로 JSESSIONID 쿠키 획득
                await self._page.goto(f"{base}/cwp2", wait_until="domcontentloaded", timeout=15000)
                # userAuth.xhtml 재접근 (이제 쿠키 있음)
                await self._page.goto(self._url, wait_until="domcontentloaded", timeout=15000)
            except Exception as e:
                logger.error(f"세션 재획득 실패: {e}")
                return False

            if await self._is_authenticated():
                logger.info("네트워크 인증 상태: 인증됨")
                await self._save_cookies()
                return True

        logger.info("네트워크 인증 필요 — 로그인 시도 중...")
        ok = await self._do_login()
        if ok:
            await self._save_cookies()
        return ok

    async def check_status_only(self) -> bool | None:
        """브라우저를 절대 띄우지 않는 초경량 상태 확인.
        반환: True=인증됨 / False=미인증(또는 쿠키 없음) / None=요청 자체 실패(네트워크 오류).
        지속적인(짧은 주기) 상태 표시용 — 재로그인은 하지 않는다."""
        cookies = self._load_cookies()
        if not cookies:
            return False
        try:
            async with httpx.AsyncClient(cookies=cookies, timeout=8.0, follow_redirects=True) as client:
                resp = await client.get(self._url)
                return _AUTHENTICATED_TEXT in resp.text
        except Exception as e:
            logger.debug(f"check_status_only 요청 실패: {e}")
            return None

    async def run_loop(self):
        """오전 run_hour_start~run_hour_end 사이에만 check_interval 간격으로 인증 확인"""
        logger.info(
            f"네트워크 인증 감시 시작 "
            f"(운영 시간: {self._run_hour_start}~{self._run_hour_end}시, "
            f"간격: {self._check_interval}초)"
        )
        while True:
            now = datetime.now()
            hour = now.hour

            if self._run_hour_start <= hour < self._run_hour_end:
                # 운영 시간 내 — 인증 확인
                try:
                    await self.check_and_login()
                except Exception as e:
                    logger.error(f"네트워크 인증 감시 오류: {e}")
                await asyncio.sleep(self._check_interval)
            else:
                # 운영 시간 외 — 다음 run_hour_start까지 대기
                next_run = now.replace(hour=self._run_hour_start, minute=0, second=0, microsecond=0)
                if now >= next_run:
                    # 오늘 run_hour_start가 이미 지났으면 내일
                    from datetime import timedelta
                    next_run += timedelta(days=1)
                wait_sec = (next_run - now).total_seconds()
                logger.info(
                    f"네트워크 인증 대기 — 다음 실행: {next_run.strftime('%m/%d %H:%M')} "
                    f"({int(wait_sec // 3600)}시간 {int((wait_sec % 3600) // 60)}분 후)"
                )
                await asyncio.sleep(wait_sec)

    async def close(self):
        """브라우저 리소스 정리"""
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
        self._page = None

    # ──────────────────────────────────────────────
    # 경량 HTTP 체크 (쿠키 재사용, 브라우저 미기동)
    # ──────────────────────────────────────────────

    async def _check_via_http(self) -> bool:
        """저장된 쿠키로 가벼운 HTTP 요청만 시도. 성공(인증됨)이면 True, 그 외에는 False
        (쿠키 없음/만료/네트워크 오류 등 — 모두 브라우저 기반 흐름으로 폴백)."""
        cookies = self._load_cookies()
        if not cookies:
            return False
        try:
            async with httpx.AsyncClient(cookies=cookies, timeout=8.0, follow_redirects=True) as client:
                resp = await client.get(self._url)
                return _AUTHENTICATED_TEXT in resp.text
        except Exception as e:
            logger.debug(f"_check_via_http 실패(브라우저로 폴백): {e}")
            return False

    def _load_cookies(self) -> dict:
        if not _SESSION_FILE.exists():
            return {}
        try:
            data = json.loads(_SESSION_FILE.read_text(encoding="utf-8"))
            return {c["name"]: c["value"] for c in data.get("cookies", []) if c.get("name")}
        except Exception:
            return {}

    async def _save_cookies(self):
        """Playwright 컨텍스트의 쿠키를 저장 — 이후 경량 HTTP 체크에서 재사용."""
        try:
            if self._page is None:
                return
            cookies = await self._page.context.cookies()
            _SESSION_FILE.parent.mkdir(exist_ok=True)
            _SESSION_FILE.write_text(
                json.dumps({"cookies": cookies}, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as e:
            logger.warning(f"쿠키 저장 실패: {e}")

    # ──────────────────────────────────────────────
    # 내부 헬퍼
    # ──────────────────────────────────────────────

    async def _is_authenticated(self) -> bool:
        """현재 페이지에서 인증 여부 확인
        - '인증사용자' 텍스트 존재 OR
        - sessExpired / 로그인 폼이 아닌 정상 페이지(main.xhtml 등)에 착지한 경우
        """
        try:
            content = await self._page.content()
            current_url = self._page.url
            if _AUTHENTICATED_TEXT in content:
                return True
            # sessExpired나 로그인 폼이 아닌 정상 페이지 → 인증됨으로 판단
            if "sessExpired" not in current_url and "userAuth" not in current_url:
                if "sessExpired" not in content:
                    return True
            return False
        except Exception:
            return False

    async def _do_login(self) -> bool:
        """현재 페이지에서 로그인 폼을 찾아 자격증명 입력 후 제출.
        실제 로그인 시도 시점에만 .env를 다시 읽어 최신 자격증명을 반영한다."""
        self._refresh_credentials()
        if not self._user_id or not self._password:
            logger.warning("NETWORK_ID 또는 NETWORK_PW 환경변수가 설정되지 않았습니다")
            return False

        # ID 필드 셀렉터 결정
        if self._id_field:
            id_sel = f'[name="{self._id_field}"]'
        else:
            id_sel = await self._find_selector(
                ['[name*="userId"]', '[name*="loginId"]', '[name*="username"]',
                 '[name*="user_id"]', 'input[type="text"]']
            )

        # PW 필드 셀렉터 결정
        if self._pw_field:
            pw_sel = f'[name="{self._pw_field}"]'
        else:
            pw_sel = 'input[type="password"]'

        if not id_sel:
            logger.error("로그인 폼의 ID 입력 필드를 찾지 못했습니다 — config.yaml의 id_field를 직접 지정하세요")
            return False

        try:
            await self._page.fill(id_sel, self._user_id)
            await self._page.fill(pw_sel, self._password)

            # 제출 버튼 찾기
            submit = await self._page.query_selector(
                'input[type="submit"], button[type="submit"], input[type="button"][value*="인증"], button'
            )
            if submit:
                await submit.click()
            else:
                await self._page.keyboard.press("Enter")

            await self._page.wait_for_load_state("networkidle", timeout=10000)
        except Exception as e:
            logger.error(f"로그인 폼 입력/제출 실패: {e}")
            await self._reset_browser()
            return False

        if await self._is_authenticated():
            logger.info("✅ 네트워크 인증 성공")
            return True
        else:
            logger.warning("⚠️ 네트워크 인증 실패 — ID/PW를 확인하세요")
            return False

    async def _find_selector(self, candidates: list[str]) -> str:
        """후보 셀렉터 중 실제 존재하는 첫 번째 반환"""
        for sel in candidates:
            try:
                el = await self._page.query_selector(sel)
                if el:
                    return sel
            except Exception:
                continue
        return ""
