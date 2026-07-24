"""트레이 '사용 설명서' — 임베드된 HTML 매뉴얼을 파일로 써서 기본 브라우저로 연다.
매뉴얼이 코드(_internal)에 포함되므로 자동 업데이트 시 함께 최신화된다."""
import os
from pathlib import Path

from version import APP_VERSION

_MANUAL_HTML = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BZP Agent 사용 설명서</title>
<style>
 body{{font-family:'Malgun Gothic','맑은 고딕',system-ui,sans-serif;line-height:1.7;max-width:860px;margin:0 auto;padding:32px 20px;color:#1f2937;background:#fff}}
 h1{{font-size:1.7rem;border-bottom:3px solid #2563eb;padding-bottom:10px}}
 h2{{font-size:1.2rem;margin-top:34px;color:#1d4ed8}}
 code{{background:#f1f5f9;padding:2px 6px;border-radius:4px;font-size:.9em}}
 .ver{{color:#6b7280;font-size:.9rem}}
 .box{{background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:14px 18px;margin:12px 0}}
 .dot{{display:inline-block;width:12px;height:12px;border-radius:50%;margin-right:8px;vertical-align:middle}}
 table{{border-collapse:collapse;width:100%;margin:10px 0}} td,th{{border:1px solid #e2e8f0;padding:8px 10px;text-align:left}}
 th{{background:#f1f5f9}} ol,ul{{padding-left:22px}}
 @media(prefers-color-scheme:dark){{body{{background:#0f172a;color:#e2e8f0}}.box{{background:#1e293b;border-color:#334155}}code{{background:#1e293b}}th{{background:#1e293b}}td,th{{border-color:#334155}}}}
</style></head><body>
<h1>BZP Agent 사용 설명서</h1>
<p class="ver">버전 v{version} · 사내 업무 자동화 트레이 앱</p>

<h2>1. 이게 뭔가요?</h2>
<p>화면 우측 하단(트레이)에서 조용히 동작하며 아래 기능을 제공합니다. <b>설치 직후에는 모든 기능이 꺼져 있고</b>, 환경설정에서 필요한 것만 켜서 씁니다.</p>
<ul>
 <li><b>네트워크 인증 자동화</b> — 사내망 인증 페이지 자동 로그인</li>
 <li><b>GitLab 동기화</b> (개발자용, 선택) — 지정 시각에 저장소 최신화</li>
 <li><b>lab dev 서버 감시</b> (개발자용, 선택) — 3003 꺼지면 자동 재실행</li>
 <li><b>PMS 이슈 알림</b> (선택) — 선택한 프로젝트의 지연/임박 이슈를 지정 시각에 확인</li>
 <li><b>자동 업데이트</b> — 새 버전 나오면 알림 → 원클릭 설치</li>
</ul>

<h2>2. 처음 사용 설정 (필수)</h2>
<ol>
 <li>트레이 아이콘(원형)을 <b>우클릭</b> → <b>환경설정...</b></li>
 <li>"네트워크 인증"의 <b>[사용]</b> 체크</li>
 <li>맨 위 "계정"에 본인 <b>ID / 비밀번호</b> 입력 (비밀번호를 비우면 기존 값 유지)</li>
 <li><b>저장 후 적용</b> — 트레이가 자동 재시작되며 인증이 시작됩니다</li>
</ol>

<h2>3. 아이콘 색상 (전체 상태 요약)</h2>
<p>아이콘은 켜져 있는 모든 기능(인증·GitLab·lab)을 종합한 상태를 나타냅니다. 세부 내용은 아이콘 우클릭 메뉴에서 볼 수 있습니다.</p>
<table>
 <tr><th>색상</th><th>의미</th></tr>
 <tr><td><span class="dot" style="background:#2ecc71"></span>초록</td><td>정상 — 문제 없음 (인증됨, 또는 인증을 안 써도 다른 기능이 정상 동작 중)</td></tr>
 <tr><td><span class="dot" style="background:#e74c3c"></span>빨강</td><td>주의 필요 — 인증 실패 / GitLab 수동 확인 필요 / lab 서버 오류</td></tr>
 <tr><td><span class="dot" style="background:#f1c40f"></span>노랑</td><td>확인 중 또는 새 버전 있음</td></tr>
 <tr><td><span class="dot" style="background:#95a5a6"></span>회색</td><td>모든 기능이 꺼져 있음 (대기)</td></tr>
</table>

<h2>4. 자동 업데이트</h2>
<p>앱이 시작할 때와 하루 한 번 새 버전을 확인합니다. 새 버전이 있으면 알림이 뜨고, 트레이 메뉴에 <b>"지금 업데이트"</b>가 나타납니다. 누르면 자동으로 받아서 교체·재시작합니다. <b>계정·설정은 그대로 유지</b>됩니다.</p>
<p>환경설정의 "자동 업데이트"에서 자동 확인을 꺼둘 수 있습니다. 꺼도 트레이 메뉴의 <b>"업데이트 확인"</b>으로 언제든 수동 확인은 가능합니다.</p>

<h2>5. 개발자 기능 (선택)</h2>
<div class="box">
 <b>GitLab 동기화</b> — [사용] 체크 후 로컬 저장소 경로를 <b>찾아보기</b>로 지정, 동기화 시각 선택.<br>
 <b>lab dev 감시</b> — [사용] 체크 후 저장소 경로 지정. "콘솔 창 보기"를 켜면 dev 서버 로그가 콘솔 창으로 실시간 표시됩니다.
</div>

<h2>6. PMS 이슈 알림 (선택)</h2>
<div class="box">
 BZP PMS(bzp-pms.webcash.work)에 로그인해 선택한 프로젝트의 이슈 중 <b>마감일이 지난(지연)</b> 또는
 <b>임박한(기준일 이내)</b> 항목을 지정 시각에 확인해 알려줍니다.<br>
 환경설정 → "PMS 이슈 알림"에서 [사용] 체크, PMS 계정 입력, <b>새로고침</b>으로 프로젝트 목록을 불러온 뒤
 모니터링할 프로젝트를 체크하고 저장하세요. 지연 이슈가 있으면 즉시 알림이 뜨고, 없으면 조용히 대기합니다.<br>
 트레이 메뉴의 <b>"PMS 이슈 보기"</b>를 클릭하면 최근 확인 결과를 표(지연/임박)로 볼 수 있고, 각 이슈 제목을 클릭하면 flow.team의 해당 업무로 바로 이동합니다.
</div>

<h2>7. 문제 해결</h2>
<ul>
 <li><b>종료 후 다시 실행하려면</b>: 시작 메뉴에서 <b>"BZP Agent"</b>를 검색해 클릭 (또는 Windows 재로그인 시 자동 실행)</li>
 <li>트레이 우클릭 → <b>로그 폴더 열기</b>에서 실행 로그 확인</li>
 <li>인증이 안 되면: 환경설정에서 ID/PW를 다시 저장</li>
 <li>재설치가 필요하면 배포받은 <code>install.bat</code>을 다시 실행 (계정·설정 유지됨)</li>
</ul>

<h2>8. 참고</h2>
<p>네트워크 인증·PMS 계정정보는 본인 PC의 설치 폴더(<code>%LOCALAPPDATA%\\BZP_Agent\\.env</code>)에만 저장되며, 해당 서비스(사내망 인증 페이지 / BZP PMS) 로그인 용도로만 사용되고 그 외 외부로 전송되지 않습니다.</p>
</body></html>"""


def write_and_open(base_dir: Path) -> None:
    """매뉴얼 HTML을 설치 폴더에 쓰고 기본 브라우저로 연다."""
    path = Path(base_dir) / "manual.html"
    path.write_text(_MANUAL_HTML.format(version=APP_VERSION), encoding="utf-8")
    os.startfile(str(path))
