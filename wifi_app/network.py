"""Local IPv4 selection and optional Windows legacy hosted-network support."""
from __future__ import annotations

import ipaddress
import secrets
import string
import subprocess
import sys
import psutil


def local_ips() -> list[str]:
    result = set()
    for addresses in psutil.net_if_addrs().values():
        for entry in addresses:
            try:
                ip = ipaddress.ip_address(entry.address)
                if ip.version == 4 and ip.is_private and not ip.is_loopback and not ip.is_link_local:
                    result.add(str(ip))
            except ValueError:
                continue
    return sorted(result, key=lambda ip: (not ip.startswith("192.168.137."), ip))


def _netsh(*args: str) -> str:
    if sys.platform != "win32":
        raise RuntimeError("ساخت هات‌اسپات فقط در ویندوز پشتیبانی می‌شود.")
    p = subprocess.run(["netsh", "wlan", *args], capture_output=True, timeout=22, shell=False)
    output = p.stdout.decode("utf-8", errors="replace") or p.stdout.decode("cp1252", errors="replace")
    if p.returncode:
        raise RuntimeError("دستور ویندوز ناموفق بود. ممکن است کارت وای‌فای از Hosted Network پشتیبانی نکند یا دسترسی مدیر لازم باشد. " + output[:250])
    return output


def start_hotspot() -> tuple[str, str]:
    """Try legacy Hosted Network API; not supported by many modern Wi-Fi adapters.

    Caller must explain Windows Mobile Hotspot manual fallback. Windows may need elevation.
    """
    ssid = "AniWiFi-" + secrets.token_hex(2).upper()
    password = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(14))
    _netsh("set", "hostednetwork", "mode=allow", f"ssid={ssid}", f"key={password}")
    _netsh("start", "hostednetwork")
    return ssid, password


def stop_hotspot():
    _netsh("stop", "hostednetwork")


def wifi_qr(ssid: str, password: str) -> str:
    # Wi-Fi QR format as recognized by iOS/Android camera apps.
    escape = lambda s: s.replace("\\", "\\\\").replace(";", "\\;").replace(":", "\\:").replace(",", "\\,")
    return f"WIFI:T:WPA;S:{escape(ssid)};P:{escape(password)};;"
