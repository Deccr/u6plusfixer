#!/usr/bin/env python3
"""
u6plus_fixer_gui.py - tkinter GUI for u6plus_fixer.py (Linux U6+ eMMC recovery).

Usage: python3 u6plus_fixer_gui.py
"""

import glob
import os
import pathlib
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk


class U6PlusFixerGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("U6+ eMMC Recovery")
        self.root.geometry("1040x780")
        self.root.minsize(900, 640)

        self.script_dir = pathlib.Path(__file__).parent.resolve()
        self.fixer_script = self.script_dir / "u6plus_fixer.py"
        self.proc: subprocess.Popen | None = None
        self.reader_thread: threading.Thread | None = None
        self.running = False
        self.warnings = 0
        self.unit_mac = ""

        # Colors
        self.c_neutral = "#e8ecf1"
        self.c_work = "#d6e6fa"
        self.c_prompt = "#ffd60a"
        self.c_good = "#2ea043"
        self.c_bad = "#cf222e"
        self.c_warn = "#bf8700"
        self.c_head = "#0969da"

        self._build_ui()
        self.refresh_ports()

    def _build_ui(self) -> None:
        # Connection frame
        conn_frame = ttk.LabelFrame(self.root, text="Connection", padding=5)
        conn_frame.place(x=12, y=8, width=470, height=96)

        ttk.Label(conn_frame, text="Serial port").place(x=5, y=5)
        self.cb_com = ttk.Combobox(conn_frame, state="readonly", width=30)
        self.cb_com.place(x=110, y=5)
        ttk.Button(conn_frame, text="Refresh", command=self.refresh_ports).place(x=360, y=3)

        ttk.Label(conn_frame, text="Network adapter").place(x=5, y=40)
        self.cb_adapter = ttk.Combobox(conn_frame, state="readonly", width=30)
        self.cb_adapter.place(x=110, y=40)

        # Action frame
        act_frame = ttk.LabelFrame(self.root, text="Action", padding=5)
        act_frame.place(x=494, y=8, width=518, height=96)

        self.rb_mode = tk.StringVar(value="Fix")
        ttk.Radiobutton(act_frame, text="Fix a bricked unit", variable=self.rb_mode, value="Fix").place(x=5, y=5)
        ttk.Radiobutton(act_frame, text="Factory-reset a fixed unit", variable=self.rb_mode, value="Reset").place(x=5, y=30)
        ttk.Radiobutton(act_frame, text="Restore a fixed unit", variable=self.rb_mode, value="Restore").place(x=5, y=55)

        self.chk_factory = tk.BooleanVar()
        ttk.Checkbutton(act_frame, text="Also wipe UniFi settings", variable=self.chk_factory).place(x=5, y=80)

        ttk.Label(act_frame, text="eMMC speed").place(x=230, y=5)
        self.cb_speed = ttk.Combobox(act_frame, state="readonly", width=20)
        self.cb_speed["values"] = ("Auto (recommended)", "Fast: 25 MHz", "Slow: 5 MHz")
        self.cb_speed.current(0)
        self.cb_speed.place(x=320, y=5)

        self.chk_skip = tk.BooleanVar()
        ttk.Checkbutton(act_frame, text="Skip backup (not recommended)", variable=self.chk_skip).place(x=230, y=30)

        ttk.Label(act_frame, text="Backup").place(x=230, y=55)
        self.cb_backup = ttk.Combobox(act_frame, state="readonly", width=20)
        self.cb_backup.place(x=320, y=55)
        self.refresh_backups()

        # Buttons
        self.btn_start = tk.Button(self.root, text="Start", font=("Segoe UI", 10, "bold"), command=self.on_start)
        self.btn_start.place(x=12, y=112, width=140, height=32)

        self.btn_cancel = tk.Button(self.root, text="Cancel", state="disabled", command=self.on_cancel)
        self.btn_cancel.place(x=160, y=112, width=120, height=32)

        ttk.Button(self.root, text="Open logs", command=self.open_logs).place(x=288, y=112, width=120, height=32)
        ttk.Button(self.root, text="Open backups", command=self.open_backups).place(x=416, y=112, width=120, height=32)
        ttk.Button(self.root, text="Instructions", command=self.open_readme).place(x=544, y=112, width=120, height=32)

        # Banner
        self.lbl_banner = tk.Label(
            self.root,
            text="Ready. Wire the serial adapter, cable the PoE injector to this PC, leave the AP unpowered, then press Start.",
            bg=self.c_neutral,
            font=("Segoe UI", 14, "bold"),
            wraplength=1000,
        )
        self.lbl_banner.place(x=12, y=152, width=1000, height=64)

        # Stage list
        stage_frame = ttk.Frame(self.root)
        stage_frame.place(x=12, y=226, width=340, height=480)

        self.lv_stages = ttk.Treeview(stage_frame, columns=("status",), show="tree headings", height=20)
        self.lv_stages.heading("#0", text="Stage")
        self.lv_stages.heading("status", text="Status")
        self.lv_stages.column("#0", width=250)
        self.lv_stages.column("status", width=80)
        self.lv_stages.pack(fill="both", expand=True)

        # Log
        self.rtb = scrolledtext.ScrolledText(
            self.root,
            wrap=tk.NONE,
            state="disabled",
            font=("Consolas", 9),
            bg="white",
        )
        self.rtb.place(x=362, y=226, width=650, height=480)

        # Status bar
        self.status = tk.Label(self.root, text="Idle", bd=1, relief=tk.SUNKEN, anchor=tk.W)
        self.status.place(x=0, y=760, width=1040, height=20)

    def refresh_backups(self) -> None:
        d = self.script_dir / "backups"
        dirs = []
        if d.exists():
            dirs = [str(p.relative_to(self.script_dir)) for p in sorted(d.iterdir()) if p.is_dir()]
        self.cb_backup["values"] = dirs
        if dirs:
            self.cb_backup.current(0)

    def refresh_ports(self) -> None:
        # Serial ports
        ports = sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"))
        self.cb_com["values"] = ports
        if ports:
            self.cb_com.current(0)

        # Network adapters
        adapters = []
        net_path = pathlib.Path("/sys/class/net")
        if net_path.exists():
            for iface in net_path.iterdir():
                if iface.name == "lo":
                    continue
                if not (iface / "device").exists():
                    continue
                type_file = iface / "type"
                if type_file.exists():
                    try:
                        if int(type_file.read_text().strip()) == 1:
                            adapters.append(iface.name)
                    except ValueError:
                        pass
        self.cb_adapter["values"] = adapters
        if adapters:
            self.cb_adapter.current(0)

    def set_banner(self, text: str, bg: str, fg: str = "black") -> None:
        self.lbl_banner.config(text=text, bg=bg, fg=fg)

    def add_log(self, line: str, color: str = "black", bold: bool = False) -> None:
        self.rtb.config(state="normal")
        tag = f"tag_{id(line)}_{time.time()}"
        self.rtb.tag_config(tag, foreground=color, font=("Consolas", 9, "bold" if bold else "normal"))
        self.rtb.insert("end", line + "\n", tag)
        self.rtb.see("end")
        self.rtb.config(state="disabled")

    def set_stage_state(self, n: int, state: str) -> None:
        items = self.lv_stages.get_children()
        if n < 1 or n > len(items):
            return
        item = items[n - 1]
        if state == "RUN":
            self.lv_stages.item(item, values=("running",), tags=("running",))
            self.set_banner(f"Working: {self.lv_stages.item(item, 'text')}", self.c_work)
        elif state == "OK":
            self.lv_stages.item(item, values=("done",), tags=("done",))
        elif state == "FAIL":
            self.lv_stages.item(item, values=("FAILED",), tags=("failed",))
        elif state == "RETRY":
            self.lv_stages.item(item, values=("retrying",), tags=("retrying",))

    def handle_line(self, line: str) -> None:
        if line.startswith("##STAGES "):
            self.lv_stages.delete(*self.lv_stages.get_children())
            for s in line[9:].split("|"):
                self.lv_stages.insert("", "end", text=s, values=("waiting",))
            return
        m = re.match(r"^##STAGE (\d+) (\w+)$", line)
        if m:
            self.set_stage_state(int(m.group(1)), m.group(2))
            return
        if line.startswith("##PROMPT "):
            self.set_banner(line[9:], self.c_prompt)
            self.root.bell()
            self.root.lift()
            return
        if line == "##PROMPTCLEAR":
            self.set_banner("Working... leave the AP and cables alone", self.c_work)
            return
        if line.startswith("##WARN "):
            self.warnings += 1
            self.status.config(text=f"Warnings: {self.warnings}")
            return
        if line.startswith("##UNIT "):
            self.unit_mac = line[7:]
            self.status.config(text=f"Unit MAC: {self.unit_mac}")
            return
        if line.startswith("##RESULT "):
            self.running = False
            if line == "##RESULT SUCCESS":
                if self.rb_mode.get() == "Reset":
                    msg = "DONE: unit reset to factory defaults. Disconnect the serial adapter and adopt it."
                else:
                    msg = "DONE: unit fixed. Disconnect the serial adapter, adopt it, and turn off auto-updates for it."
                self.set_banner(msg, self.c_good, "white")
            else:
                self.set_banner("FAILED: " + line[16:], self.c_bad, "white")
            self.btn_start.config(state="normal")
            self.btn_cancel.config(state="disabled")
            return

        # Plain log line
        if "FAILED" in line:
            self.add_log(line, self.c_bad, True)
        elif "WARNING" in line:
            self.add_log(line, self.c_warn)
        elif "SUCCESS" in line:
            self.add_log(line, self.c_good, True)
        elif "] == " in line:
            self.add_log(line, self.c_head, True)
        elif "[engine error]" in line:
            self.add_log(line, self.c_bad)
        else:
            self.add_log(line)

    def on_start(self) -> None:
        com = self.cb_com.get()
        adapter = self.cb_adapter.get()
        if not com:
            messagebox.showwarning("U6+ Recovery", "No serial port found. Plug in the USB-serial adapter and press Refresh.")
            return
        if not adapter:
            messagebox.showwarning("U6+ Recovery", "No wired network adapter found.")
            return

        mode = self.rb_mode.get()
        speed = ["auto", "fast", "slow"][self.cb_speed.current()]

        checklist = (
            f"Before starting:\n\n"
            f"- Serial adapter wired: RX to AP TX, TX to AP RX, GND to GND (no VCC)\n"
            f"- PoE injector LAN port cabled straight to this PC (no switch or router)\n"
            f"- The AP is UNPOWERED\n\n"
            f"Start {mode.lower()} on {com} / {adapter}?"
        )
        if not messagebox.askokcancel("U6+ Recovery", checklist):
            return

        # Build command
        cmd = [
            sys.executable,
            str(self.fixer_script),
            "--gui",
            "--mode", mode,
            "--com-port", com,
            "--adapter", adapter,
            "--speed", speed,
        ]
        if mode == "Fix" and self.chk_factory.get():
            cmd.append("--factory-reset")
        if mode == "Restore":
            backup = self.cb_backup.get()
            if not backup:
                messagebox.showwarning("U6+ Recovery", "No backup selected. Choose a backup directory.")
                return
            cmd.extend(["--backup-dir", backup])
        if self.chk_skip.get():
            cmd.append("--skip-backup")

        self.rtb.config(state="normal")
        self.rtb.delete(1.0, "end")
        self.rtb.config(state="disabled")
        self.lv_stages.delete(*self.lv_stages.get_children())
        self.warnings = 0
        self.unit_mac = ""
        self.status.config(text="Running...")
        self.set_banner("Starting...", self.c_work)
        self.add_log(" ".join(cmd), "gray")

        self.running = True
        self.btn_start.config(state="disabled")
        self.btn_cancel.config(state="normal")

        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        self.reader_thread = threading.Thread(target=self._read_output, daemon=True)
        self.reader_thread.start()

        self._poll_proc()

    def _read_output(self) -> None:
        if self.proc and self.proc.stdout:
            for line in self.proc.stdout:
                self.root.after(0, lambda l=line.strip(): self.handle_line(l))

    def _poll_proc(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.root.after(150, self._poll_proc)
        else:
            self.running = False
            self.btn_start.config(state="normal")
            self.btn_cancel.config(state="disabled")
            if self.proc and self.proc.returncode != 0:
                # If no result line was emitted, show generic failure
                pass

    def on_cancel(self) -> None:
        if not messagebox.askyesno(
            "U6+ Recovery",
            "Stop the run now? The AP may be left part-way through. Running Fix again is safe.",
        ):
            return
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.stop_loader()
        self.add_log("Cancelled by user.", self.c_bad, True)
        self.running = False
        self.btn_start.config(state="normal")
        self.btn_cancel.config(state="disabled")

    def stop_loader(self) -> None:
        try:
            subprocess.run(["pkill", "-f", "mtk_uartboot"], capture_output=True, check=False)
        except FileNotFoundError:
            pass

    def open_logs(self) -> None:
        d = self.script_dir / "runs"
        d.mkdir(exist_ok=True)
        self._open_dir(d)

    def open_backups(self) -> None:
        d = self.script_dir / "backups"
        d.mkdir(exist_ok=True)
        self._open_dir(d)

    def open_readme(self) -> None:
        readme = self.script_dir / "README.md"
        if readme.exists():
            self._open_file(readme)

    @staticmethod
    def _open_dir(path: pathlib.Path) -> None:
        try:
            subprocess.run(["xdg-open", str(path)], check=False)
        except FileNotFoundError:
            pass

    @staticmethod
    def _open_file(path: pathlib.Path) -> None:
        try:
            subprocess.run(["xdg-open", str(path)], check=False)
        except FileNotFoundError:
            pass

    def on_closing(self) -> None:
        if self.running:
            if not messagebox.askyesno(
                "U6+ Recovery",
                "A run is still in progress. Stop it and close?",
            ):
                return
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            self.stop_loader()
        self.root.destroy()


def main():
    root = tk.Tk()
    app = U6PlusFixerGUI(root)
    root.protocol("WM_DELETE_WINDOW", app.on_closing)
    root.mainloop()


if __name__ == "__main__":
    main()
