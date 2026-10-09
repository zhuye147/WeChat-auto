"""
客服数据监听 - 启动控制面板
提供开始/停止按钮，方便测试
"""
import os
import sys
import subprocess
import threading
import tkinter as tk
from tkinter import scrolledtext
from datetime import datetime

PY_EXE = sys.executable
WORK_DIR = os.path.dirname(os.path.abspath(__file__))
MAIN_SCRIPT = os.path.join(WORK_DIR, "main.py")


class ControlPanel:
    def __init__(self, root):
        self.root = root
        self.root.title("客服数据监听 - 控制面板")
        self.root.geometry("700x500")
        self.root.resizable(True, True)

        self.process = None
        self.output_thread = None

        # 顶部按钮区
        btn_frame = tk.Frame(root)
        btn_frame.pack(fill=tk.X, padx=10, pady=8)

        self.start_btn = tk.Button(
            btn_frame, text="▶ 开始监听", command=self.start_program,
            bg="#4CAF50", fg="white", font=("Microsoft YaHei", 12, "bold"),
            width=14, height=1, relief=tk.FLAT, cursor="hand2"
        )
        self.start_btn.pack(side=tk.LEFT, padx=5)

        self.stop_btn = tk.Button(
            btn_frame, text="■ 停止监听", command=self.stop_program,
            bg="#f44336", fg="white", font=("Microsoft YaHei", 12, "bold"),
            width=14, height=1, relief=tk.FLAT, cursor="hand2",
            state=tk.DISABLED
        )
        self.stop_btn.pack(side=tk.LEFT, padx=5)

        # 状态标签
        self.status_label = tk.Label(
            btn_frame, text="● 已停止", fg="gray",
            font=("Microsoft YaHei", 11)
        )
        self.status_label.pack(side=tk.RIGHT, padx=10)

        # 日志输出区
        log_frame = tk.Frame(root)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        tk.Label(log_frame, text="运行日志：", anchor=tk.W,
                 font=("Microsoft YaHei", 10)).pack(fill=tk.X)

        self.log_text = scrolledtext.ScrolledText(
            log_frame, wrap=tk.WORD, font=("Consolas", 10),
            bg="#1e1e1e", fg="#d4d4d4", insertbackground="white",
            state=tk.DISABLED
        )
        self.log_text.pack(fill=tk.BOTH, expand=True, pady=(4, 0))

        # 配置日志颜色
        self.log_text.tag_configure("info", foreground="#9CDCFE")
        self.log_text.tag_configure("error", foreground="#F44747")
        self.log_text.tag_configure("success", foreground="#6A9955")
        self.log_text.tag_configure("system", foreground="#DCDCAA")

    def _append_log(self, text, tag=None):
        self.log_text.configure(state=tk.NORMAL)
        if tag:
            self.log_text.insert(tk.END, text + "\n", tag)
        else:
            self.log_text.insert(tk.END, text + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def start_program(self):
        if self.process and self.process.poll() is None:
            return

        self.start_btn.configure(state=tk.DISABLED)
        self.stop_btn.configure(state=tk.NORMAL)
        self.status_label.configure(text="● 运行中", fg="#4CAF50")

        now = datetime.now().strftime("%H:%M:%S")
        self._append_log(f"[{now}] ====== 启动监听程序 ======", "system")

        try:
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"

            self.process = subprocess.Popen(
                [PY_EXE, MAIN_SCRIPT],
                cwd=WORK_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1
            )

            self.output_thread = threading.Thread(
                target=self._read_output, daemon=True
            )
            self.output_thread.start()

        except Exception as e:
            self._append_log(f"启动失败: {e}", "error")
            self._reset_buttons()

    def stop_program(self):
        if not self.process or self.process.poll() is not None:
            self._reset_buttons()
            return

        now = datetime.now().strftime("%H:%M:%S")
        self._append_log(f"[{now}] ====== 正在停止... ======", "system")

        self.process.terminate()

        def wait_stop():
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
            self.root.after(0, self._on_stopped)

        threading.Thread(target=wait_stop, daemon=True).start()

    def _on_stopped(self):
        self._append_log("程序已停止\n", "system")
        self._reset_buttons()

    def _reset_buttons(self):
        self.start_btn.configure(state=tk.NORMAL)
        self.stop_btn.configure(state=tk.DISABLED)
        self.status_label.configure(text="● 已停止", fg="gray")

    def _read_output(self):
        try:
            for line in self.process.stdout:
                line = line.rstrip("\n")
                if not line:
                    continue

                tag = None
                if "[ERROR]" in line:
                    tag = "error"
                elif "[INFO]" in line and ("写入成功" in line or "发现" in line):
                    tag = "success"
                elif "[INFO]" in line or "[DEBUG]" in line:
                    tag = "info"

                self.root.after(0, self._append_log, line, tag)

        except Exception:
            pass

        if self.process and self.process.poll() is not None:
            self.root.after(0, self._on_stopped)


def main():
    root = tk.Tk()
    app = ControlPanel(root)

    def on_close():
        if app.process and app.process.poll() is None:
            app.process.terminate()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
