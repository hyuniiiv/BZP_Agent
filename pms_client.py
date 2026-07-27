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
    """"프로젝트 관리 — 프로젝트 현황"과 동일한 소스(/api/bzp/projects)에서 조회.
    [{code, name, clientName, status, contractDate, pmoUserName, pmUserName}, ...] 반환.
    주의: PMO(pmoUserName)와 PM(pmUserName)은 서로 다른 사람일 수 있다(실측 확인됨) — 혼동 금지."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            _login(page, pms_id, pms_pw)
            resp = _fetch_json(page, "/api/bzp/projects")
            return resp.get("data") or []
        finally:
            browser.close()


def _parse_ymd(s: str | None) -> date | None:
    if not s or len(s) != 8 or not s.isdigit():
        return None
    try:
        return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except ValueError:
        return None


STATUS_LABELS = {"0": "대기", "1": "진행", "2": "완료", "3": "피드백", "4": "보류"}
# 실측(UI 렌더 텍스트 대조)으로 확인된 매핑. 값이 없는 이슈는 "-"로 표시.
PRIORITY_LABELS = {"0": "낮음", "1": "보통", "2": "높음", "3": "긴급"}


def fetch_snapshot(pms_id: str, pms_pw: str, project_codes: list[str], upcoming_days: int = 3) -> dict:
    """한 번 로그인으로 '프로젝트 현황'과 '이슈관리 통합조회'를 함께 가져와 종합한다.
    project_codes 비우면 전체 프로젝트 대상. 반환:
    {"projects": [{code,name,clientName,status,contractDate,pmoUserName,pmUserName}, ...] (필터 적용됨),
     "issues": [...전체 목록...], "issue_total": int, "issue_status_counts": {"대기":N, ...},
     "overdue": [...], "upcoming": [...]} — 각 이슈 항목은
    {"project","title","priority","status","assignee","pm","start","due","actual_start","actual_end","progress","link"}.
    PMO와 PM은 서로 다른 사람일 수 있다(실측 확인됨) — 이슈의 "pm"은 반드시 pmUserName에서 가져온다."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            _login(page, pms_id, pms_pw)
            proj_resp = _fetch_json(page, "/api/bzp/projects")
            path = (
                f"/api/bzp/tasks/section?sectionTitle={quote(ISSUE_SECTION)}"
                f"&projectType={quote(PROJECT_TYPE)}&page=1&size=200"
            )
            issue_resp = _fetch_json(page, path)
        finally:
            browser.close()

    all_projects = proj_resp.get("data") or []
    items = (issue_resp.get("data") or {}).get("items") or []

    code_filter = set(project_codes) if project_codes else None
    projects = [p for p in all_projects if code_filter is None or p.get("code") in code_filter]
    pm_by_code = {p.get("code"): p.get("pmUserName", "") for p in all_projects}
    filtered_items = [it for it in items if code_filter is None or it.get("projectCode") in code_filter]

    today = date.today()
    issues, overdue, upcoming, status_counts = [], [], [], {}
    for it in filtered_items:
        status_label = STATUS_LABELS.get(it.get("taskStatus"), it.get("taskStatus") or "-")
        status_counts[status_label] = status_counts.get(status_label, 0) + 1
        due = _parse_ymd(it.get("endDt"))
        entry = {
            "project": it.get("projectName", ""),
            "title": it.get("title", ""),
            "priority": PRIORITY_LABELS.get(it.get("priority"), "-"),
            "status": status_label,
            "assignee": it.get("author", ""),
            "pm": pm_by_code.get(it.get("projectCode"), ""),
            "start": _parse_ymd(it.get("startDt")),
            "due": due,
            "actual_start": _parse_ymd(it.get("actualStartDt")),
            "actual_end": _parse_ymd(it.get("actualEndDt")),
            "progress": it.get("progress"),
            "link": it.get("link", ""),
        }
        issues.append(entry)
        # taskStatus "2" = 완료. actualEndDt는 완료 건에서도 비어있는 경우가 있어(실측 확인됨)
        # 완료 여부 판정에 신뢰할 수 없다 — taskStatus를 권위 있는 신호로 사용한다.
        if it.get("taskStatus") == "2" or due is None:
            continue
        if due < today:
            overdue.append(entry)
        elif (due - today).days <= upcoming_days:
            upcoming.append(entry)

    issues.sort(key=lambda e: (e["project"], e["title"]))
    overdue.sort(key=lambda e: e["due"])
    upcoming.sort(key=lambda e: e["due"])
    return {
        "projects": projects,
        "issues": issues,
        "issue_total": len(filtered_items),
        "issue_status_counts": status_counts,
        "overdue": overdue,
        "upcoming": upcoming,
    }


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


def _issue_rows_html(issues: list[dict]) -> str:
    if not issues:
        return '<tr><td colspan="11" class="empty">해당 없음</td></tr>'
    out = []
    for e in issues:
        title = html.escape(e["title"])
        link = html.escape(e["link"] or "", quote=True)
        title_cell = f'<a href="{link}" target="_blank">{title} ↗</a>' if link else title
        fmt = lambda d: d.isoformat() if d else "-"  # noqa: E731
        progress = f"{e['progress']}%" if e.get("progress") is not None else "-"
        out.append(
            "<tr><td>{proj}</td><td>{title}</td><td>{pri}</td><td>{status}</td><td>{assignee}</td>"
            "<td>{pm}</td><td>{start}</td><td>{due}</td><td>{astart}</td><td>{aend}</td><td>{prog}</td></tr>".format(
                proj=html.escape(e["project"]), title=title_cell, pri=html.escape(e["priority"]),
                status=html.escape(e["status"]), assignee=html.escape(e["assignee"]), pm=html.escape(e["pm"]),
                start=fmt(e["start"]), due=fmt(e["due"]), astart=fmt(e["actual_start"]), aend=fmt(e["actual_end"]),
                prog=progress,
            )
        )
    return "\n".join(out)


def _project_rows_html(projects: list[dict]) -> str:
    if not projects:
        return '<tr><td colspan="6" class="empty">해당 없음</td></tr>'
    out = []
    for p in projects:
        out.append(
            "<tr><td>{code}</td><td>{client}</td><td>{name}</td><td>{status}</td>"
            "<td>{contract}</td><td>{pmo}</td><td>{pm}</td></tr>".format(
                code=html.escape(p.get("code", "")), client=html.escape(p.get("clientName", "")),
                name=html.escape(p.get("name", "")), status=html.escape(p.get("status", "")),
                contract=html.escape(p.get("contractDate") or "-"), pmo=html.escape(p.get("pmoUserName", "")),
                pm=html.escape(p.get("pmUserName", "")),
            )
        )
    return "\n".join(out)


def write_report_html(path, snapshot: dict) -> None:
    """PMS 조회 결과를 HTML 리포트로 저장 — 프로젝트 현황 + 이슈관리 통합조회 요약 + 지연/임박 상세.
    이슈 제목 클릭 시 flow.team 해당 업무로 바로 이동."""
    today = date.today()
    projects = snapshot["projects"]
    overdue, upcoming = snapshot["overdue"], snapshot["upcoming"]
    status_line = " · ".join(f"{k} {v}" for k, v in snapshot["issue_status_counts"].items()) or "-"

    body = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PMS 이슈 현황</title>
<style>
 body{{font-family:'Malgun Gothic','맑은 고딕',system-ui,sans-serif;max-width:960px;margin:0 auto;padding:28px 20px;color:#1f2937;background:#fff}}
 h1{{font-size:1.4rem;border-bottom:3px solid #2563eb;padding-bottom:8px}}
 h2{{font-size:1.05rem;margin-top:28px}}
 .meta{{color:#6b7280;font-size:.85rem}}
 .summary{{background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:10px 14px;margin:8px 0;font-size:.9rem}}
 table{{border-collapse:collapse;width:100%;margin:10px 0}}
 td,th{{border:1px solid #e2e8f0;padding:7px 10px;text-align:left;font-size:.92rem}}
 th{{background:#f1f5f9}}
 .empty{{color:#9ca3af;text-align:center}}
 a{{color:#2563eb;text-decoration:none}} a:hover{{text-decoration:underline}}
 @media(prefers-color-scheme:dark){{body{{background:#0f172a;color:#e2e8f0}}.summary{{background:#1e293b;border-color:#334155}}th{{background:#1e293b}}td,th{{border-color:#334155}}a{{color:#60a5fa}}}}
</style></head><body>
<h1>PMS 이슈 현황</h1>
<p class="meta">확인 시각: {today.isoformat()} · 이슈 제목을 클릭하면 flow.team으로 이동합니다</p>

<h2>프로젝트 관리 — 프로젝트 현황</h2>
<p class="summary">모니터링 대상 {len(projects)}개 프로젝트</p>
<table><tr><th>코드</th><th>고객명</th><th>프로젝트명</th><th>상태</th><th>계약일자</th><th>PMO</th><th>PM</th></tr>
{_project_rows_html(projects)}
</table>

<h2>프로젝트 관리 — 이슈관리 통합조회</h2>
<p class="summary">전체 {snapshot['issue_total']}건 · {status_line}</p>
<table><tr><th>프로젝트</th><th>이슈</th><th>우선순위</th><th>상태</th><th>담당자</th><th>PM</th>
<th>시작일</th><th>완료예정일</th><th>착수일</th><th>완료일</th><th>진척도</th></tr>
{_issue_rows_html(snapshot['issues'])}
</table>

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
