"""Offline Persian Windows desktop operator console."""
from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

import qrcode
from PIL import ImageTk

from .network import local_ips, start_hotspot, stop_hotspot, wifi_qr
from .server import LocalServer
from .transfer import Limits, TransferError, TransferManager

APP_NAME = "آنی وای‌فای | انتقال فایل بدون اینترنت"
CONFIG_DIR = Path(os.environ.get("APPDATA", str(Path.home() / ".config"))) / "AniWiFi"
CONFIG_PATH = CONFIG_DIR / "config.json"


def read_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(data: dict):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    temp = CONFIG_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, CONFIG_PATH)


def setup_logs():
    directory = Path(os.environ.get("LOCALAPPDATA", str(CONFIG_DIR))) / "AniWiFi"
    directory.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.WARNING, handlers=[RotatingFileHandler(directory / "errors.log", maxBytes=2_000_000, backupCount=2, encoding="utf-8")])


class Desktop(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1040x760")
        self.minsize(800, 680)
        self.configure(bg="#f1f5f9")
        family = "Vazirmatn" if "Vazirmatn" in tkfont.families(self) else "Tahoma"
        tkfont.nametofont("TkDefaultFont").configure(family=family, size=10)
        self.option_add("*Font", (family, 10))
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        cfg = read_config()
        self.path_var = tk.StringVar(value=cfg.get("destination", str(Path.home() / "Downloads" / "AniWiFi")))
        self.file_limit = tk.StringVar(value=str(cfg.get("max_file_gb", 20)))
        self.session_limit = tk.StringVar(value=str(cfg.get("max_session_gb", 100)))
        self.files_limit = tk.StringVar(value=str(cfg.get("max_files", 1000)))
        self.disk_reserve = tk.StringVar(value=str(cfg.get("reserve_disk_gb", 1)))
        self.ip_var = tk.StringVar(value="")
        self.status_var = tk.StringVar(value="آماده‌سازی سرور محلی...")
        self.url_var = tk.StringVar(value="")
        self.hotspot_text = tk.StringVar(value="هات‌اسپات هنوز ساخته نشده است")
        self.hotspot_credentials: tuple[str, str] | None = None
        self.hotspot_started = False
        self.qr_image = None
        self.wifi_qr_image = None
        try:
            self.manager = TransferManager(Path(self.path_var.get()), self.read_limits())
            self.server = LocalServer(self.manager)
            self.server.start()
        except Exception as exc:
            messagebox.showerror("خطای راه‌اندازی", str(exc))
            self.destroy()
            return
        self._build()
        self.refresh_ips()
        self.render_qr()
        self.after(700, self.tick)

    def read_limits(self) -> Limits:
        try:
            limits = Limits(int(self.file_limit.get()), int(self.session_limit.get()),
                            int(self.files_limit.get()), int(self.disk_reserve.get()))
            limits.validate()
            return limits
        except ValueError as exc:
            raise TransferError("لطفاً محدودیت‌ها را با عدد صحیح وارد کنید.") from exc

    def _build(self):
        header = tk.Frame(self, bg="#0f172a", padx=22, pady=18)
        header.pack(fill="x")
        tk.Label(header, text="آنی وای‌فای  •  دریافت فایل از گوشی بدون اینترنت", font=("Tahoma", 16, "bold"),
                 fg="white", bg="#0f172a", anchor="e").pack(fill="x")
        tk.Label(header, text="۱) وای‌فای مشترک  ←  ۲) اسکن کد  ←  ۳) ارسال فایل", bg="#0f172a", fg="#a5f3fc", anchor="e").pack(fill="x")
        body = tk.Frame(self, bg="#f1f5f9", padx=16, pady=14)
        body.pack(fill="both", expand=True)
        right = tk.Frame(body, bg="white", padx=16, pady=12, highlightbackground="#cbd5e1", highlightthickness=1)
        right.pack(side="right", fill="y", padx=(12, 0))
        tk.Label(right, text="QR ارسال فایل مشتری", font=("Tahoma", 12, "bold"), bg="white").pack(pady=(0, 9))
        self.qr_label = tk.Label(right, text="در انتظار شبکه محلی", width=33, height=15, bg="#f8fafc")
        self.qr_label.pack()
        tk.Label(right, textvariable=self.url_var, bg="white", fg="#0369a1", wraplength=260).pack(pady=6)
        tk.Button(right, text="نوبت جدید / QR جدید", command=self.new_customer, bg="#0d9488", fg="white", padx=14, pady=9).pack(fill="x", pady=8)
        tk.Label(right, text="شبکه وای‌فای یا IP انتخابی", bg="white").pack(anchor="e", pady=(8, 0))
        self.ip_combo = ttk.Combobox(right, textvariable=self.ip_var, width=33)
        self.ip_combo.pack(fill="x", pady=5)
        self.ip_combo.bind("<<ComboboxSelected>>", lambda _: self.render_qr())
        self.ip_combo.bind("<Return>", lambda _: self.render_qr())
        tk.Button(right, text="بازخوانی شبکه / QR", command=self.refresh_ips).pack(fill="x")
        tk.Label(right, text="اگر وای‌فای ندارید، هات‌اسپات بسازید:", bg="white").pack(pady=(16, 3))
        tk.Button(right, text="تلاش برای ایجاد هات‌اسپات ویندوز", command=self.create_hotspot).pack(fill="x")
        tk.Label(right, textvariable=self.hotspot_text, wraplength=265, bg="white", justify="right", fg="#334155").pack(pady=5)
        self.wifi_label = tk.Label(right, bg="white")
        self.wifi_label.pack()
        tk.Button(right, text="تنظیمات هات‌اسپات ویندوز", command=self.open_hotspot_settings).pack(fill="x", pady=4)

        left = tk.Frame(body, bg="#f1f5f9")
        left.pack(side="left", fill="both", expand=True)
        tk.Label(left, textvariable=self.status_var, anchor="e", bg="#dbeafe", fg="#1e3a8a", padx=12, pady=10).pack(fill="x", pady=(0, 8))
        settings = ttk.LabelFrame(left, text="تنظیمات و محل ذخیره")
        settings.pack(fill="x")
        line = tk.Frame(settings)
        line.pack(fill="x", padx=10, pady=6)
        tk.Button(line, text="انتخاب پوشه", command=self.choose_folder).pack(side="left")
        tk.Entry(line, textvariable=self.path_var, justify="right").pack(side="left", fill="x", expand=True, padx=8)
        tk.Label(line, text="پوشه:").pack(side="right")
        limit_row = tk.Frame(settings)
        limit_row.pack(fill="x", pady=6, padx=10)
        for title, var in [("گیگابایت هر فایل", self.file_limit), ("گیگابایت هر نوبت", self.session_limit),
                           ("تعداد فایل", self.files_limit), ("فضای آزاد (GB)", self.disk_reserve)]:
            part = tk.Frame(limit_row)
            part.pack(side="right", padx=5)
            tk.Label(part, text=title).pack()
            tk.Entry(part, width=9, textvariable=var, justify="center").pack()
        buttons = tk.Frame(settings)
        buttons.pack(fill="x", padx=10, pady=(0, 10))
        tk.Button(buttons, text="ذخیره تنظیمات (از نوبت بعد)", command=self.apply_settings).pack(side="right", padx=4)
        tk.Button(buttons, text="باز کردن پوشه مشتری", command=self.open_folder).pack(side="right", padx=4)
        tk.Label(left, text="وضعیت فایل‌های نوبت جاری", font=("Tahoma", 12, "bold"), bg="#f1f5f9", anchor="e").pack(fill="x", pady=(20, 5))
        columns = ("status", "progress", "size", "name")
        self.table = ttk.Treeview(left, columns=columns, show="headings", height=14)
        for col, heading, width in [("status", "وضعیت", 95), ("progress", "پیشرفت", 85),
                                    ("size", "حجم", 95), ("name", "نام فایل", 330)]:
            self.table.heading(col, text=heading)
            self.table.column(col, width=width, anchor="e")
        scroll = ttk.Scrollbar(left, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.table.pack(side="left", fill="both", expand=True)
        tk.Label(self, text="فایل‌های کامل‌شده حفظ می‌شوند؛ بخش‌های ناقص هنگام پایان نوبت حذف می‌شوند.  •  فقط در شبکه مطمئن استفاده کنید.",
                 bg="#f1f5f9", fg="#475569", pady=10).pack(fill="x")

    def choose_folder(self):
        selected = filedialog.askdirectory(initialdir=self.path_var.get())
        if selected:
            self.path_var.set(selected)

    def apply_settings(self):
        try:
            limits = self.read_limits()
            new_path = Path(self.path_var.get()).expanduser()
            new_path.mkdir(parents=True, exist_ok=True)
            if not new_path.is_dir():
                raise OSError("مسیر پوشه نامعتبر است.")
            save_config({"destination": str(new_path), "max_file_gb": limits.max_file_gb,
                         "max_session_gb": limits.max_session_gb, "max_files": limits.max_files,
                         "reserve_disk_gb": limits.reserve_disk_gb})
            messagebox.showinfo("ذخیره شد", "تنظیمات ذخیره شد و با شروع نوبت بعد اعمال می‌شود.")
        except (TransferError, OSError) as exc:
            messagebox.showerror("تنظیمات نامعتبر", str(exc))

    def new_customer(self):
        if not messagebox.askyesno("نوبت جدید", "انتقال‌های نیمه‌کاره نوبت قبل پاک شوند و QR قبلی غیرفعال شود؟\nفایل‌های کامل‌شده باقی می‌مانند."):
            return
        try:
            self.manager.new_session(Path(self.path_var.get()), self.read_limits())
            self.render_qr()
            self.tick()
        except (OSError, TransferError) as exc:
            messagebox.showerror("خطا", str(exc))

    def refresh_ips(self):
        choices = local_ips()
        self.ip_combo["values"] = choices
        if self.ip_var.get() not in choices:
            self.ip_var.set(choices[0] if choices else "")
        self.render_qr()

    def render_qr(self):
        ip = self.ip_var.get().strip()
        try:
            import ipaddress
            addr = ipaddress.ip_address(ip)
            if addr.version != 4 or addr.is_loopback or not addr.is_private:
                raise ValueError()
        except ValueError:
            self.qr_label.configure(image="", text="IP شبکه محلی یافت نشد.\nوای‌فای یا هات‌اسپات را فعال کنید.")
            self.url_var.set("")
            return
        url = f"http://{ip}:{self.server.port}/?token={self.manager.token}"
        qr = qrcode.make(url).convert("RGB").resize((270, 270))
        self.qr_image = ImageTk.PhotoImage(qr)
        self.qr_label.configure(image=self.qr_image, text="", width=270, height=270)
        self.url_var.set(f"http://{ip}:{self.server.port}/  |  نوبت {self.manager.session_code}")

    def create_hotspot(self):
        self.hotspot_text.set("در حال درخواست از ویندوز...")

        def work():
            try:
                details = start_hotspot()
                self.after(0, lambda: self.hotspot_ok(details))
            except Exception as exc:
                error = str(exc)
                self.after(0, lambda: self.hotspot_fail(error))

        threading.Thread(target=work, daemon=True).start()

    def hotspot_ok(self, details):
        self.hotspot_credentials = details
        self.hotspot_started = True
        ssid, password = details
        self.hotspot_text.set(f"نام: {ssid}\nرمز: {password}\nابتدا QR شبکه، سپس QR انتقال را اسکن کنید.")
        qr = qrcode.make(wifi_qr(ssid, password)).convert("RGB").resize((125, 125))
        self.wifi_qr_image = ImageTk.PhotoImage(qr)
        self.wifi_label.configure(image=self.wifi_qr_image)
        self.refresh_ips()

    def hotspot_fail(self, error: str):
        self.hotspot_text.set("ایجاد خودکار ممکن نشد. از تنظیمات ویندوز Mobile Hotspot را روشن کنید و سپس بازخوانی شبکه را بزنید.")
        messagebox.showwarning("هات‌اسپات پشتیبانی نشد", error)

    def open_hotspot_settings(self):
        if sys.platform == "win32":
            os.startfile("ms-settings:network-mobilehotspot")
        else:
            messagebox.showinfo("فقط ویندوز", "این قابلیت مخصوص ویندوز است.")

    def open_folder(self):
        try:
            if sys.platform == "win32":
                os.startfile(str(self.manager.folder))
            else:
                import subprocess
                subprocess.Popen(["xdg-open", str(self.manager.folder)])
        except OSError as exc:
            messagebox.showerror("خطا", str(exc))

    def tick(self):
        if not self.winfo_exists():
            return
        data = self.manager.snapshot()
        rows = data["files"]
        for item in self.table.get_children():
            self.table.delete(item)
        for f in rows[-250:]:
            pct = 100 if f["status"] == "complete" else (round(f["receivedBytes"] / f["size"] * 100) if f["size"] else 0)
            status = {"receiving": "در حال دریافت", "complete": "کامل", "cancelled": "لغو شد"}.get(f["status"], f["status"])
            self.table.insert("", "end", values=(status, f"{pct}%", f"{f['size']/1024**2:.1f} MB", f["name"]))
        self.status_var.set(f"نوبت {data['code']}  |  فایل کامل: {data['completed']} از {len(rows)}  |  داده دریافت‌شده: {data['totalBytes']/1024**2:.1f} MB  |  درگاه: {self.server.port}")
        self.after(900, self.tick)

    def on_close(self):
        if not messagebox.askyesno("خروج", "برنامه بسته شود؟ انتقال‌های ناقص حذف می‌شوند."):
            return
        self.server.stop()
        self.manager.close()
        if self.hotspot_started:
            try:
                stop_hotspot()
            except Exception:
                pass
        self.destroy()


def main():
    setup_logs()
    app = Desktop()
    if app.winfo_exists():
        app.mainloop()


if __name__ == "__main__":
    main()
