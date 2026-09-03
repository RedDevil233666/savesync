# -*- coding: utf-8 -*-
"""
存档同步 — 图形界面（4 键极简版）

按钮:
  1. 扫描游戏   扫描本机所有游戏存档（Windows 原生 + macOS CrossOver bottle），自动加入同步清单
  2. 上传存档   把本机所有游戏的存档上传到云端
  3. 下载覆盖   用云端存档覆盖本机（覆盖前自动备份本地到 ~/.savesync/backups/）
  4. 查看存档   查看已配置游戏清单与本地/云端同步状态

配置与命令行版（savesync.py）共用 ~/.savesync/。
"""
import io
import queue
import sys
import threading
import contextlib

import tkinter as tk
from tkinter import messagebox
from tkinter.scrolledtext import ScrolledText

import os
if os.name == "nt":
    FONT_UI = ("Microsoft YaHei UI", 15, "bold")
    FONT_SUB = ("Microsoft YaHei UI", 10)
    FONT_MONO = ("Consolas", 11)
else:
    FONT_UI = ("PingFang SC", 15, "bold")
    FONT_SUB = ("PingFang SC", 10)
    FONT_MONO = ("Menlo", 12)

# ---------------------------------------------------------------- 配色（简约淡绿）
C_BG      = "#E4F0E5"   # 窗口背景：淡绿
C_BTN     = "#FDFBF3"   # 按钮：乳白
C_BTN_ACT = "#F3EEDF"   # 按钮：按下/悬停
C_TEXT    = "#1A1A1A"   # 主文字：黑
C_SUB     = "#4A5A4C"   # 次要文字：深灰绿
C_LOG_BG  = "#FBFBF7"   # 日志区：近白


# ---------------------------------------------------------------- 记录输出
class TextEmitter(io.StringIO):
    def __init__(self, q):
        super().__init__()
        self.q = q

    def write(self, s):
        if s:
            self.q.put(s)
        return len(s)

    def flush(self):
        pass


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("存档同步")
        self.geometry("780x560")
        self.minsize(640, 480)

        self.out_q = queue.Queue()
        self.busy = False

        self._build_ui()
        self.after(100, self._drain_queue)
        self._hello()

    # ---------------------------------------------------------------- UI
    def _build_ui(self):
        self.configure(bg=C_BG)

        head = tk.Frame(self, bg=C_BG)
        head.pack(fill="x", padx=14, pady=(12, 4))
        tk.Label(head, text="游戏存档云同步", font=(FONT_SUB[0], 16, "bold"),
                 bg=C_BG, fg=C_TEXT).pack(side="left")
        self.status_var = tk.StringVar(value="就绪")
        tk.Label(head, textvariable=self.status_var, fg=C_SUB, bg=C_BG).pack(side="right")

        btns = tk.Frame(self, bg=C_BG)
        btns.pack(fill="x", padx=14, pady=10)

        spec = [
            ("🔍  扫描游戏", "扫描本机所有游戏存档\n(Windows / macOS CrossOver)", self.on_scan),
            ("⬆️  上传存档", "本机存档上传到云端\n(玩完游戏后点这个)", self.on_push),
            ("⬇️  下载覆盖", "云端存档覆盖到本机\n(换机器开玩前点这个)", self.on_pull),
            ("📋  查看存档", "同步清单与状态对比", self.on_status),
        ]
        for i, (text, sub, cmd) in enumerate(spec):
            cell = tk.Frame(btns, bd=1, relief="solid", highlightthickness=0,
                            bg=C_BTN)
            cell.grid(row=i // 2, column=i % 2, sticky="nsew", padx=5, pady=5)
            b = tk.Button(cell, text=text, command=cmd, font=FONT_UI,
                          bd=0, cursor="hand2", activebackground=C_BTN_ACT,
                          activeforeground=C_TEXT,
                          bg=C_BTN, fg=C_TEXT, pady=10, highlightthickness=0)
            b.pack(fill="x", padx=2, pady=(2, 0))
            tk.Label(cell, text=sub, font=FONT_SUB, fg=C_SUB, bg=C_BTN,
                     wraplength=330, justify="left").pack(fill="x", padx=6, pady=(2, 6))
        btns.columnconfigure(0, weight=1)
        btns.columnconfigure(1, weight=1)

        # 次要操作行：配置云端
        minor = tk.Frame(self, bg=C_BG)
        minor.pack(fill="x", padx=14, pady=(0, 8))
        tk.Button(minor, text="⚙  配置云端（坚果云 WebDAV）", command=self.on_setup,
                  font=FONT_SUB, cursor="hand2", relief="groove", pady=3,
                  bg=C_BTN, fg=C_TEXT, activebackground=C_BTN_ACT,
                  activeforeground=C_TEXT, bd=1).pack(side="left")
        tk.Label(minor, text="每台机器配置一次即可", fg=C_SUB, bg=C_BG).pack(side="left", padx=8)

        tk.Label(self, text="运行日志", anchor="w", fg=C_TEXT, bg=C_BG).pack(fill="x", padx=14)
        self.log = ScrolledText(self, height=14, font=FONT_MONO, state="disabled",
                                bg=C_LOG_BG, fg=C_TEXT, relief="groove")
        self.log.pack(fill="both", expand=True, padx=14, pady=(2, 12))

    def _hello(self):
        self.log_print("欢迎使用存档同步。首次使用：先点「扫描游戏」，"
                       "再点左下角「配置云端」填入坚果云账号，之后即可上传/下载。\n")

    # ---------------------------------------------------------------- 配置云端
    def on_setup(self):
        win = tk.Toplevel(self)
        win.title("配置云端 — 坚果云 WebDAV")
        win.resizable(False, False)
        win.transient(self)
        win.grab_set()

        body = tk.Frame(win, padx=16, pady=14, bg=C_BTN)
        body.pack()
        tk.Label(body, text="坚果云网页端 → 账户信息 → 安全选项 → 添加应用密码，\n"
                            "把邮箱和应用密码填在下面（不是登录密码）。",
                 font=FONT_SUB, fg=C_SUB, justify="left", bg=C_BTN).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))

        fields = [
            ("服务器地址", "https://dav.jianguoyun.com/dav/"),
            ("账号（邮箱）", ""),
            ("应用密码", ""),
        ]
        entries = {}
        for i, (label, default) in enumerate(fields, start=1):
            tk.Label(body, text=label, font=FONT_SUB, fg=C_TEXT, bg=C_BTN).grid(row=i, column=0, sticky="w", pady=4)
            e = tk.Entry(body, width=40, font=FONT_SUB,
                         show="*" if "密码" in label else "")
            e.insert(0, default)
            e.grid(row=i, column=1, padx=(10, 0), pady=4, sticky="we")
            entries[label] = e
        entries["账号（邮箱）"].focus_set()

        def save():
            url = entries["服务器地址"].get().strip().rstrip("/")
            user = entries["账号（邮箱）"].get().strip()
            pwd = entries["应用密码"].get().strip()
            if not url or not user or not pwd:
                messagebox.showwarning("缺信息", "三项都要填。", parent=win)
                return
            win.destroy()
            self._run("配置云端", ["setup-webdav", "--url", url,
                                  "--user", user, "--password", pwd])

        btns2 = tk.Frame(body, bg=C_BTN)
        btns2.grid(row=len(fields) + 1, column=0, columnspan=2, pady=(12, 0))
        tk.Button(btns2, text="保存并测试连接", command=save, font=FONT_SUB,
                  bg=C_BTN, fg=C_TEXT, bd=1, cursor="hand2",
                  activebackground=C_BTN_ACT, activeforeground=C_TEXT,
                  padx=14, pady=4).pack(side="left", padx=4)
        tk.Button(btns2, text="取消", command=win.destroy, font=FONT_SUB,
                  relief="groove", padx=10, pady=4,
                  bg=C_BTN, fg=C_TEXT, activebackground=C_BTN_ACT,
                  activeforeground=C_TEXT).pack(side="left", padx=4)

    # ---------------------------------------------------------------- 日志
    def log_print(self, s=""):
        self.log.configure(state="normal")
        self.log.insert("end", s + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _drain_queue(self):
        try:
            while True:
                s = self.out_q.get_nowait()
                self.log_print(s.rstrip("\n"))
        except queue.Empty:
            pass
        self.after(100, self._drain_queue)

    # ---------------------------------------------------------------- 执行器
    def _run(self, label, argv, confirm=None):
        if self.busy:
            messagebox.showwarning("忙", "有任务正在运行，请稍候。")
            return
        if confirm and not messagebox.askyesno("确认", confirm, icon="warning", parent=self):
            return

        self.busy = True
        self.status_var.set(label + " 中 ...")
        self.log_print("\n===== %s =====" % label)

        def worker():
            import savesync
            buf = TextEmitter(self.out_q)
            try:
                with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                    savesync.run_cli([a for a in argv if a])
                self.out_q.put("[完成] %s\n" % label)
            except SystemExit:
                self.out_q.put("[完成] %s\n" % label)
            except Exception as e:
                self.out_q.put("[出错] %s: %s\n" % (type(e).__name__, e))
            finally:
                self.after(0, self._done)

        threading.Thread(target=worker, daemon=True).start()

    def _done(self):
        self.busy = False
        self.status_var.set("就绪")

    # ---------------------------------------------------------------- 按钮
    def on_scan(self):
        self._run("扫描游戏", ["scan", "--add-known"])

    def on_push(self):
        self._run("上传存档", ["push"])

    def on_pull(self):
        self._run(
            "下载覆盖", ["pull", "--force"],
            confirm="将用云端存档覆盖本机存档。\n\n覆盖前会自动备份当前本地存档\n"
                    "（保留最近 10 份，位置 ~/.savesync/backups/）。\n\n确定继续？")

    def on_status(self):
        self._run("查看存档", ["status"])


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
