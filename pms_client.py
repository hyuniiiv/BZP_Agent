"""
BZP PMS(bzp-pms.webcash.work) 이슈 마감 알림 — Playwright(sync)로 로그인 후
내부 JSON API를 직접 호출한다(화면 스크래핑 아님).

로그인은 폼 기반(#login-id/#login-pw)이고 인증 방식(쿠키/토큰)을 몰라도 되도록,
로그인된 페이지 컨텍스트 안에서 fetch()를 실행해 응답을 받는다.
"""
import logging
from datetime import date
from urllib.parse import quote

from playwright.sync_api import sync_playwright

logger = logging.getLogger("pms_client")

PMS_URL = "https://bzp-pms.webcash.work/"
ISSUE_SECTION = "P10-이슈관리"
PROJECT_TYPE = "수행"
_TIMEOUT = 20000


class PmsError(Exception):
    pass


def _login(page, pms_id: str, pms_pw: str) -> None:
    page.goto(PMS_URL, wait_until="networkidle", timeout=_TIMEOUT)
    page.fill("#login-id", pms_id)
    page.fill("#login-pw", pms_pw)
    page.click("button:has-text('로그인')")
    # 주의: 이 SPA는 로그인 후에도 위젯이 주기적으로 폴링해 networkidle이 조기에(레이스 컨디션으로)
    # 만족되는 경우가 있어, 대신 로그인 성공의 명시적 신호("로그아웃" 버튼 등장)를 기다린다.
    try:
        page.wait_for_selector("text=로그아웃", timeout=_TIMEOUT)
    except Exception:
        raise PmsError("PMS 로그인 실패 — 계정정보를 확인하세요")


def _fetch_json(page, path: str) -> dict:
    return page.evaluate("(p) => fetch(p).then(r => r.json())", path)


def fetch_projects(pms_id: str, pms_pw: str) -> list[dict]:
    """[{code, name, clientName, status, pmoUserName}, ...] 반환."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            _login(page, pms_id, pms_pw)
            resp = _fetch_json(page, "/api/bzp/dashboard/projects")
            return (resp.get("data") or {}).get("projects") or []
        finally:
            browser.close()


def _parse_ymd(s: str | None) -> date | None:
    if not s or len(s) != 8 or not s.isdigit():
        return None
    try:
        return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except ValueError:
        return None


def fetch_issue_alerts(pms_id: str, pms_pw: str, project_codes: list[str], upcoming_days: int = 3) -> dict:
    """선택한 프로젝트(project_codes 비우면 전체)의 이슈 중 지연/임박 항목을 반환.
    반환: {"overdue": [...], "upcoming": [...]}, 각 항목은
    {"project": str, "title": str, "assignee": str, "due": date, "link": str}."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            _login(page, pms_id, pms_pw)
            path = (
                f"/api/bzp/tasks/section?sectionTitle={quote(ISSUE_SECTION)}"
                f"&projectType={quote(PROJECT_TYPE)}&page=1&size=200"
            )
            resp = _fetch_json(page, path)
            items = (resp.get("data") or {}).get("items") or []
        finally:
            browser.close()

    today = date.today()
    code_filter = set(project_codes) if project_codes else None
    overdue, upcoming = [], []
    for it in items:
        code = it.get("projectCode")
        if code_filter is not None and code not in code_filter:
            continue
        if it.get("actualEndDt"):  # 실제 완료됨 — 알림 대상 아님
            continue
        due = _parse_ymd(it.get("endDt"))
        if due is None:
            continue
        entry = {
            "project": it.get("projectName", ""),
            "title": it.get("title", ""),
            "assignee": it.get("author", ""),
            "due": due,
            "link": it.get("link", ""),
        }
        if due < today:
            overdue.append(entry)
        elif (due - today).days <= upcoming_days:
            upcoming.append(entry)

    overdue.sort(key=lambda e: e["due"])
    upcoming.sort(key=lambda e: e["due"])
    return {"overdue": overdue, "upcoming": upcoming}
