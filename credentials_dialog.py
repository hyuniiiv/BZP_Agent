"""
네트워크 인증 계정 설정 다이얼로그 — 트레이 앱과 별도 프로세스로 실행.
트레이(pystray)의 메시지 루프와 섞이지 않아 키보드 포커스 문제가 없다.
"""
import sys
import tkinter as tk
from pathlib import Path

BASE_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
ENV_FILE = BASE_DIR / ".env"


def load_current_id() -> str:
    if not ENV_FILE.exists():
        return ""
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("NETWORK_ID="):
            return line.split("=", 1)[1]
    return ""


def load_current_pw() -> str:
    if not ENV_FILE.exists():
        return ""
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("NETWORK_PW="):
            return line.split("=", 1)[1]
    return ""


def save_credentials(user_id: str, password: str):
    lines = []
    seen_id = seen_pw = False
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if line.startswith("NETWORK_ID="):
                lines.append(f"NETWORK_ID={user_id}")
                seen_id = True
            elif line.startswith("NETWORK_PW="):
                lines.append(f"NETWORK_PW={password}")
                seen_pw = True
            else:
                lines.append(line)
    if not seen_id:
        lines.append(f"NETWORK_ID={user_id}")
    if not seen_pw:
        lines.append(f"NETWORK_PW={password}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    root = tk.Tk()
    root.title("네트워크 인증 계정 설정")
    root.resizable(False, False)

    def on_save():
        new_id = id_var.get().strip()
        new_pw = pw_var.get()
        if not new_id:
            status_label.config(text="ID를 입력하세요.", fg="red")
            return
        final_pw = new_pw if new_pw else load_current_pw()
        save_credentials(new_id, final_pw)
        status_label.config(text="저장 완료! 창을 닫습니다...", fg="green")
        root.after(1000, root.destroy)

    tk.Label(root, text="NETWORK_ID").grid(row=0, column=0, padx=10, pady=(12, 4), sticky="e")
    id_var = tk.StringVar(value=load_current_id())
    id_entry = tk.Entry(root, textvariable=id_var, width=28)
    id_entry.grid(row=0, column=1, padx=10, pady=(12, 4))

    tk.Label(root, text="NETWORK_PW").grid(row=1, column=0, padx=10, pady=4, sticky="e")
    pw_var = tk.StringVar()
    pw_entry = tk.Entry(root, textvariable=pw_var, show="*", width=28)
    pw_entry.grid(row=1, column=1, padx=10, pady=4)
    tk.Label(root, text="(비워두면 기존 비밀번호 유지)", fg="gray").grid(row=2, column=0, columnspan=2, pady=(0, 6))

    status_label = tk.Label(root, text="", fg="green")
    status_label.grid(row=3, column=0, columnspan=2)

    btn_frame = tk.Frame(root)
    btn_frame.grid(row=4, column=0, columnspan=2, pady=(4, 12))
    tk.Button(btn_frame, text="저장", width=10, command=on_save).pack(side="left", padx=5)
    tk.Button(btn_frame, text="취소", width=10, command=root.destroy).pack(side="left", padx=5)

    root.bind("<Return>", lambda _e: on_save())

    # 창을 화면 중앙 부근에, 항상 위로
    root.update_idletasks()
    w, h = root.winfo_width(), root.winfo_height()
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"+{(sw - w) // 2}+{(sh - h) // 3}")
    root.attributes("-topmost", True)
    root.lift()
    root.focus_force()
    id_entry.focus_set()

    root.mainloop()


if __name__ == "__main__":
    main()
