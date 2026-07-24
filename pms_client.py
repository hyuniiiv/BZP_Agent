"""
BZP PMS(bzp-pms.webcash.work) 이슈 마감 알림 — Playwright(sync)로 로그인 후
내부 JSON API를 직접 호출한다(화면 스크래핑 아님).

로그인은 폼 기반(#login-id/#login-pw)이고 인증 방식(쿠키/토큰)을 몰라도 되도록,
로그인된 페이지 컨텍스트 안에서 fetch()를 실행해 응답을 받는다.
"""
import html
import logging
from datetime import date
from pathlib import Path
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
        # taskStatus "2" = 완료. actualEndDt는 완료 건에서도 비어있는 경우가 있어(실측 확인됨)
        # 완료 여부 판정에 신뢰할 수 없다 — taskStatus를 권위 있는 신호로 사용한다.
        if it.get("taskStatus") == "2":
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


def _rows_html(entries: list[dict], today: date) -> str:
    if not entries:
        return '<tr><td colspan="4" class="empty">해당 없음</td></tr>'
    out = []
    for e in entries:
        days = (e["due"] - today).days
        d_label = f"{-days}일 지남" if days < 0 else ("오늘" if days == 0 else f"{days}일 후")
        title = html.escape(e["title"])
        link = html.escape(e["link"] or "", quote=True)
        title_cell = f'<a href="{link}" target="_blank">{title} ↗</a>' if link else title
        out.append(
            "<tr><td>{proj}</td><td>{title}</td><td>{assignee}</td><td>{due} ({d})</td></tr>".format(
                proj=html.escape(e["project"]), title=title_cell,
                assignee=html.escape(e["assignee"]), due=e["due"].isoformat(), d=d_label,
            )
        )
    return "\n".join(out)


def write_report_html(path, overdue: list[dict], upcoming: list[dict]) -> None:
    """지연/임박 이슈를 HTML 리포트로 저장 — 항목 클릭 시 flow.team 이슈로 바로 이동."""
    today = date.today()
    body = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PMS 이슈 현황</title>
<style>
 body{{font-family:'Malgun Gothic','맑은 고딕',system-ui,sans-serif;max-width:900px;margin:0 auto;padding:28px 20px;color:#1f2937;background:#fff}}
 h1{{font-size:1.4rem;border-bottom:3px solid #2563eb;padding-bottom:8px}}
 h2{{font-size:1.05rem;margin-top:28px}}
 .meta{{color:#6b7280;font-size:.85rem}}
 table{{border-collapse:collapse;width:100%;margin:10px 0}}
 td,th{{border:1px solid #e2e8f0;padding:7px 10px;text-align:left;font-size:.92rem}}
 th{{background:#f1f5f9}}
 .empty{{color:#9ca3af;text-align:center}}
 a{{color:#2563eb;text-decoration:none}} a:hover{{text-decoration:underline}}
 @media(prefers-color-scheme:dark){{body{{background:#0f172a;color:#e2e8f0}}th{{background:#1e293b}}td,th{{border-color:#334155}}a{{color:#60a5fa}}}}
</style></head><body>
<h1>PMS 이슈 현황</h1>
<p class="meta">확인 시각: {today.isoformat()} · 이슈 제목을 클릭하면 flow.team으로 이동합니다</p>

<h2>🔴 지연 ({len(overdue)}건)</h2>
<table><tr><th>프로젝트</th><th>이슈</th><th>담당자</th><th>마감일</th></tr>
{_rows_html(overdue, today)}
</table>

<h2>🟡 임박 ({len(upcoming)}건)</h2>
<table><tr><th>프로젝트</th><th>이슈</th><th>담당자</th><th>마감일</th></tr>
{_rows_html(upcoming, today)}
</table>
</body></html>"""
    Path(path).write_text(body, encoding="utf-8")
