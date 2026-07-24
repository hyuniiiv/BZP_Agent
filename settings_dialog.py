"""
환경설정 다이얼로그 — 트레이 앱과 별도 프로세스로 실행 (tkinter).
계정·위치·기능 온오프·실행 주기를 한 화면에서 설정한다.

저장 성공 시 종료 코드 42(RESTART_CODE)로 종료 → 트레이가 감지해 자동 재시작.
로직은 settings_store(순수 함수)에 위임하고, 여기서는 폼만 담당한다.
"""
import os
import sys
import tkinter as tk
from tkinter import ttk, filedialog

import settings_store as store
import pms_client
from version import APP_VERSION

RESTART_CODE = 42


def _add_browse(parent, row, label_text, var):
    """저장소 경로 입력줄 + '찾아보기...' 폴더 선택 버튼을 한 행에 배치."""
    pad = {"padx": 8, "pady": 3}
    ttk.Label(parent, text=label_text).grid(row=row, column=0, sticky="e", **pad)
    frm = ttk.Frame(parent)
    frm.grid(row=row, column=1, sticky="w", **pad)
    ttk.Entry(frm, textvariable=var, width=38).pack(side="left")

    def browse():
        current = var.get().strip()
        initial = current if current and os.path.isdir(current) else os.path.expanduser("~")
        chosen = filedialog.askdirectory(title=f"{label_text} 선택", initialdir=initial)
        if chosen:
            var.set(os.path.normpath(chosen))  # Windows 표기(역슬래시)로 정규화

    ttk.Button(frm, text="찾아보기...", width=10, command=browse).pack(side="left", padx=(6, 0))


def main():
    data = store.load_settings()
    root = tk.Tk()
    root.title(f"환경설정 — {store.APP_NAME} v{APP_VERSION}")
    root.resizable(False, False)

    pad = {"padx": 8, "pady": 3}
    poll_labels = [label for _sec, label in store.POLL_OPTIONS]
    hhmm_list = store.hhmm_options()

    # ── 네트워크 인증 (계정 포함) ─────────
    f_auth = ttk.LabelFrame(root, text="네트워크 인증")
    f_auth.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 4))
    auth_on = tk.BooleanVar(value=data["auth_enabled"])
    ttk.Checkbutton(f_auth, text="사용 (끄면 자동 인증 안 함)", variable=auth_on).grid(row=0, column=0, columnspan=2, sticky="w", **pad)
    # 계정 (인증 전용이라 이 프레임 안에 둔다)
    ttk.Label(f_auth, text="ID").grid(row=1, column=0, sticky="e", **pad)
    id_var = tk.StringVar(value=data["account_id"])
    ttk.Entry(f_auth, textvariable=id_var, width=32).grid(row=1, column=1, sticky="w", **pad)
    ttk.Label(f_auth, text="비밀번호").grid(row=2, column=0, sticky="e", **pad)
    pw_var = tk.StringVar()
    ttk.Entry(f_auth, textvariable=pw_var, show="*", width=32).grid(row=2, column=1, sticky="w", **pad)
    ttk.Label(f_auth, text="(비워두면 기존 비밀번호 유지)", foreground="gray").grid(row=3, column=1, sticky="w", padx=8)
    ttk.Separator(f_auth, orient="horizontal").grid(row=4, column=0, columnspan=2, sticky="ew", padx=8, pady=6)
    ttk.Label(f_auth, text="인증 URL").grid(row=5, column=0, sticky="e", **pad)
    auth_url = tk.StringVar(value=data["auth_url"])
    ttk.Entry(f_auth, textvariable=auth_url, width=46).grid(row=5, column=1, sticky="w", **pad)
    ttk.Label(f_auth, text="평상시 체크 주기").grid(row=6, column=0, sticky="e", **pad)
    auth_iv = tk.StringVar(value=store.interval_label(data["auth_interval"]))
    ttk.Combobox(f_auth, textvariable=auth_iv, values=poll_labels, state="readonly", width=10).grid(row=6, column=1, sticky="w", **pad)
    ttk.Label(f_auth, text="민감 시간대").grid(row=7, column=0, sticky="e", **pad)
    peak_frame = ttk.Frame(f_auth)
    peak_frame.grid(row=7, column=1, sticky="w", **pad)
    peak_win = tk.StringVar(value=data["auth_peak_window"])
    ttk.Entry(peak_frame, textvariable=peak_win, width=14).pack(side="left")
    ttk.Label(peak_frame, text="주기").pack(side="left", padx=(8, 4))
    peak_iv = tk.StringVar(value=store.interval_label(data["auth_peak_interval"]))
    ttk.Combobox(peak_frame, textvariable=peak_iv, values=poll_labels, state="readonly", width=8).pack(side="left")
    ttk.Label(f_auth, text='형식 "HH:MM-HH:MM" (비우면 미사용)', foreground="gray").grid(row=8, column=1, sticky="w", padx=8)

    # ── GitLab 동기화 ─────────────────────
    f_git = ttk.LabelFrame(root, text="GitLab 동기화")
    f_git.grid(row=1, column=0, sticky="ew", padx=12, pady=4)
    git_on = tk.BooleanVar(value=data["git_enabled"])
    ttk.Checkbutton(f_git, text="사용 (매일 지정 시각에 동기화)", variable=git_on).grid(row=0, column=0, columnspan=2, sticky="w", **pad)
    git_repo = tk.StringVar(value=data["git_repo"])
    _add_browse(f_git, 1, "저장소 경로", git_repo)
    ttk.Label(f_git, text="동기화 시각").grid(row=2, column=0, sticky="ne", **pad)
    git_time_frame = ttk.Frame(f_git)
    git_time_frame.grid(row=2, column=1, sticky="w", **pad)
    time_choices = [store.GIT_TIME_NONE] + hhmm_list
    git_time_vars = []
    for slot_idx in range(store.GIT_TIME_SLOTS):
        tvar = tk.StringVar(value=data["git_times"][slot_idx])
        ttk.Combobox(git_time_frame, textvariable=tvar, values=time_choices,
                     state="readonly", width=12).grid(row=slot_idx, column=0, sticky="w", pady=2)
        git_time_vars.append(tvar)
    ttk.Label(f_git, text="최대 3개 (사용 안 함 = 그 슬롯 비활성)", foreground="gray").grid(row=3, column=1, sticky="w", padx=8)

    # ── lab dev 감시 ──────────────────────
    f_lab = ttk.LabelFrame(root, text="lab dev 서버 감시")
    f_lab.grid(row=2, column=0, sticky="ew", padx=12, pady=4)
    lab_on = tk.BooleanVar(value=data["lab_enabled"])
    ttk.Checkbutton(f_lab, text="사용 (3003 꺼지면 자동 재실행)", variable=lab_on).grid(row=0, column=0, columnspan=2, sticky="w", **pad)
    lab_console = tk.BooleanVar(value=data["lab_show_console"])
    ttk.Checkbutton(f_lab, text="콘솔 창 보기 (dev 서버 실시간 로그 표시 · 다음 재실행부터 적용)", variable=lab_console).grid(row=4, column=0, columnspan=2, sticky="w", **pad)
    lab_repo = tk.StringVar(value=data["lab_repo"])
    _add_browse(f_lab, 1, "저장소 경로", lab_repo)
    ttk.Label(f_lab, text="서버 URL").grid(row=2, column=0, sticky="e", **pad)
    lab_url = tk.StringVar(value=data["lab_url"])
    ttk.Entry(f_lab, textvariable=lab_url, width=46).grid(row=2, column=1, sticky="w", **pad)
    ttk.Label(f_lab, text="확인 주기").grid(row=3, column=0, sticky="e", **pad)
    lab_iv = tk.StringVar(value=store.interval_label(data["lab_interval"]))
    ttk.Combobox(f_lab, textvariable=lab_iv, values=poll_labels, state="readonly", width=10).grid(row=3, column=1, sticky="w", **pad)

    # ── 자동 업데이트 ─────────────────────
    f_update = ttk.LabelFrame(root, text="자동 업데이트")
    f_update.grid(row=3, column=0, sticky="ew", padx=12, pady=4)
    auto_update = tk.BooleanVar(value=data["auto_update_enabled"])
    ttk.Checkbutton(
        f_update, text="사용 (시작 시 + 매일 1회 확인, 새 버전 발견 시 예고 없이 즉시 적용/재시작. 끄면 수동으로만 확인·적용)",
        variable=auto_update,
    ).grid(row=0, column=0, columnspan=2, sticky="w", **pad)

    # ── PMS 이슈 알림 ─────────────────────
    f_pms = ttk.LabelFrame(root, text="PMS 이슈 알림 (bzp-pms.webcash.work)")
    f_pms.grid(row=0, column=1, rowspan=4, sticky="new", padx=(4, 12), pady=(12, 4))
    pms_on = tk.BooleanVar(value=data["pms_enabled"])
    ttk.Checkbutton(f_pms, text="사용 (지정 시각에 선택 프로젝트의 지연/임박 이슈 확인)", variable=pms_on).grid(
        row=0, column=0, columnspan=2, sticky="w", **pad
    )
    ttk.Label(f_pms, text="PMS ID").grid(row=1, column=0, sticky="e", **pad)
    pms_id_var = tk.StringVar(value=data["pms_account_id"])
    ttk.Entry(f_pms, textvariable=pms_id_var, width=32).grid(row=1, column=1, sticky="w", **pad)
    ttk.Label(f_pms, text="비밀번호").grid(row=2, column=0, sticky="e", **pad)
    pms_pw_var = tk.StringVar()
    ttk.Entry(f_pms, textvariable=pms_pw_var, show="*", width=32).grid(row=2, column=1, sticky="w", **pad)
    ttk.Label(f_pms, text="(비워두면 기존 비밀번호 유지)", foreground="gray").grid(row=3, column=1, sticky="w", padx=8)

    ttk.Label(f_pms, text="임박 기준(일 이내)").grid(row=4, column=0, sticky="e", **pad)
    pms_days_var = tk.StringVar(value=str(data["pms_upcoming_days"]))
    ttk.Entry(f_pms, textvariable=pms_days_var, width=6).grid(row=4, column=1, sticky="w", **pad)

    ttk.Label(f_pms, text="확인 시각").grid(row=5, column=0, sticky="ne", **pad)
    pms_time_frame = ttk.Frame(f_pms)
    pms_time_frame.grid(row=5, column=1, sticky="w", **pad)
    pms_time_vars = []
    for slot_idx in range(store.GIT_TIME_SLOTS):
        tvar = tk.StringVar(value=data["pms_check_times"][slot_idx])
        ttk.Combobox(pms_time_frame, textvariable=tvar, values=time_choices,
                     state="readonly", width=12).grid(row=slot_idx, column=0, sticky="w", pady=2)
        pms_time_vars.append(tvar)

    ttk.Label(f_pms, text="모니터링 프로젝트").grid(row=6, column=0, sticky="ne", **pad)
    pms_project_outer = ttk.Frame(f_pms)
    pms_project_outer.grid(row=6, column=1, sticky="w", **pad)
    pms_btn_row = ttk.Frame(pms_project_outer)
    pms_btn_row.pack(anchor="w")
    pms_refresh_status = ttk.Label(pms_project_outer, text="", foreground="gray")
    pms_checklist_frame = ttk.Frame(pms_project_outer)
    pms_checklist_frame.pack(anchor="w", pady=(4, 0))

    current_projects = list(data["pms_known_projects"])
    project_vars: dict[str, tk.BooleanVar] = {}

    def render_projects():
        for w in pms_checklist_frame.winfo_children():
            w.destroy()
        project_vars.clear()
        selected = set(data["pms_project_codes"])
        if not current_projects:
            ttk.Label(pms_checklist_frame, text="(새로고침으로 프로젝트 목록을 불러오세요)", foreground="gray").grid(row=0, column=0)
            return
        for i, proj in enumerate(current_projects):
            code = proj["code"]
            var = tk.BooleanVar(value=code in selected)
            project_vars[code] = var
            ttk.Checkbutton(pms_checklist_frame, text=f"{code} {proj['name']}", variable=var).grid(
                row=i // 2, column=i % 2, sticky="w", padx=(0, 12)
            )

    def select_all():
        for v in project_vars.values():
            v.set(True)

    def select_none():
        for v in project_vars.values():
            v.set(False)

    def do_refresh():
        pid = pms_id_var.get().strip()
        pw = pms_pw_var.get() or store.load_env_value("PMS_PW")
        if not pid or not pw:
            pms_refresh_status.config(text="ID/비밀번호를 먼저 입력하세요", foreground="red")
            return
        pms_refresh_status.pack(anchor="w")
        pms_refresh_status.config(text="조회 중...", foreground="gray")
        root.update_idletasks()
        try:
            fetched = pms_client.fetch_projects(pid, pw)
        except Exception as exc:
            pms_refresh_status.config(text=f"조회 실패: {exc}", foreground="red")
            return
        current_projects[:] = [
            {"code": p.get("code", ""), "name": p.get("name", "")} for p in fetched
        ]
        render_projects()
        pms_refresh_status.config(text=f"{len(current_projects)}개 프로젝트 불러옴", foreground="green")

    ttk.Button(pms_btn_row, text="새로고침", command=do_refresh).pack(side="left")
    ttk.Button(pms_btn_row, text="전체 선택", command=select_all).pack(side="left", padx=(6, 0))
    ttk.Button(pms_btn_row, text="전체 해제", command=select_none).pack(side="left", padx=(6, 0))
    render_projects()

    # ── 상태 + 버튼 ───────────────────────
    status = ttk.Label(root, text="", foreground="green")
    status.grid(row=4, column=0, columnspan=2, pady=(6, 0))

    def on_save():
        values = {
            "account_id": id_var.get().strip(),
            "account_pw": pw_var.get(),
            "auth_enabled": auth_on.get(),
            "auth_url": auth_url.get().strip(),
            "auth_interval": store.label_interval(auth_iv.get(), 180),
            "auth_peak_interval": store.label_interval(peak_iv.get(), 60),
            "auth_peak_window": peak_win.get().strip(),
            "git_enabled": git_on.get(),
            "git_repo": git_repo.get().strip(),
            "git_times": [v.get() for v in git_time_vars],
            "lab_enabled": lab_on.get(),
            "lab_repo": lab_repo.get().strip(),
            "lab_url": lab_url.get().strip(),
            "lab_interval": store.label_interval(lab_iv.get(), 60),
            "lab_show_console": lab_console.get(),
            "auto_update_enabled": auto_update.get(),
            "pms_enabled": pms_on.get(),
            "pms_account_id": pms_id_var.get().strip(),
            "pms_account_pw": pms_pw_var.get(),
            "pms_project_codes": [code for code, v in project_vars.items() if v.get()],
            "pms_known_projects": current_projects,
            "pms_upcoming_days": int(pms_days_var.get().strip() or 3),
            "pms_check_times": [v.get() for v in pms_time_vars],
        }
        try:
            store.save_settings(values)
        except Exception as exc:
            status.config(text=f"저장 실패: {exc}", foreground="red")
            return
        status.config(text="저장 완료. 트레이를 재시작합니다...", foreground="green")
        root.after(800, lambda: sys.exit(RESTART_CODE))

    btns = ttk.Frame(root)
    btns.grid(row=5, column=0, columnspan=2, pady=(6, 12))
    ttk.Button(btns, text="저장 후 적용", width=14, command=on_save).pack(side="left", padx=5)
    ttk.Button(btns, text="취소", width=10, command=root.destroy).pack(side="left", padx=5)

    root.update_idletasks()
    w, h = root.winfo_width(), root.winfo_height()
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"+{(sw - w) // 2}+{(sh - h) // 3}")
    root.attributes("-topmost", True)
    root.lift()
    root.focus_force()
    root.mainloop()


if __name__ == "__main__":
    main()
