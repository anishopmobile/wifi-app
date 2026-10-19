"""Thread-safe local-only resumable transfer store.

A session is a short-lived capability: its random token is required for every action.
Only complete files survive ending a session. No client path is ever trusted.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
import hashlib
import hmac
import math
import os
import re
import secrets
import shutil
import threading
import uuid

CHUNK_SIZE = 4 * 1024 * 1024
_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


class TransferError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def safe_name(value: str) -> str:
    """Return a safe *basename*, preserving Persian characters and extensions."""
    if not isinstance(value, str):
        raise TransferError("نام فایل نامعتبر است.")
    name = value.replace("\\", "/").split("/")[-1]
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", name).strip(" .")
    if not name or name in (".", ".."):
        raise TransferError("نام فایل نامعتبر است.")
    name = name[:180].rstrip(" .")
    if name.split(".")[0].upper() in _WINDOWS_RESERVED:
        name = "_" + name
    return name


@dataclass
class Limits:
    max_file_gb: int = 20
    max_session_gb: int = 100
    max_files: int = 1000
    reserve_disk_gb: int = 1

    def validate(self):
        if not 1 <= self.max_file_gb <= 500 or not 1 <= self.max_session_gb <= 2000:
            raise TransferError("محدودیت حجم باید در بازه مجاز باشد.")
        if not 1 <= self.max_files <= 10000 or not 0 <= self.reserve_disk_gb <= 100:
            raise TransferError("محدودیت تعداد یا فضای دیسک نامعتبر است.")


@dataclass
class Upload:
    id: str
    name: str
    key: str
    size: int
    part: Path
    target: Path
    received: set[int] = field(default_factory=set)
    state: str = "receiving"
    lock: threading.RLock = field(default_factory=threading.RLock)

    @property
    def count(self) -> int:
        return max(1, math.ceil(self.size / CHUNK_SIZE))

    @property
    def received_bytes(self) -> int:
        return sum(min(CHUNK_SIZE, self.size - n * CHUNK_SIZE) for n in self.received)

    def info(self, chunks: bool = False) -> dict:
        result = {"id": self.id, "name": self.name, "size": self.size,
                  "receivedBytes": self.received_bytes, "status": self.state,
                  "chunkCount": self.count}
        if chunks:
            result["received"] = sorted(self.received)
        return result


class TransferManager:
    def __init__(self, destination: Path, limits: Limits | None = None):
        self.destination = Path(destination).expanduser().resolve()
        self.limits = limits or Limits()
        self.limits.validate()
        self.lock = threading.RLock()
        self._token = ""
        self.folder: Path | None = None
        self.uploads: dict[str, Upload] = {}
        self.keys: dict[str, str] = {}
        self.session_code = ""
        self.new_session()

    @property
    def token(self) -> str:
        with self.lock:
            return self._token

    def require(self, token: str | None):
        if not token or not hmac.compare_digest(self._token, token):
            raise TransferError("این نوبت پایان یافته یا لینک معتبر نیست.", 403)

    def new_session(self, destination: Path | None = None, limits: Limits | None = None):
        with self.lock:
            if self.folder:
                self._discard_incomplete()
            if destination is not None:
                self.destination = Path(destination).expanduser().resolve()
            if limits is not None:
                limits.validate()
                self.limits = limits
            self.destination.mkdir(parents=True, exist_ok=True)
            self._token = secrets.token_urlsafe(32)
            self.session_code = secrets.token_hex(3).upper()
            label = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            self.folder = self.destination / f"customer_{label}_{self.session_code}"
            self.folder.mkdir(mode=0o700, parents=False, exist_ok=False)
            self.uploads.clear()
            self.keys.clear()
            return self._token

    def _discard_incomplete(self):
        for upload in self.uploads.values():
            if upload.state != "complete":
                upload.part.unlink(missing_ok=True)
        # Retain finished files only; empty customer folders are harmless.
        if self.folder and not any(self.folder.iterdir()):
            self.folder.rmdir()

    def close(self):
        with self.lock:
            self._discard_incomplete()
            self._token = ""
            self.uploads.clear()
            self.keys.clear()

    def _unique_target(self, name: str) -> Path:
        assert self.folder
        path = self.folder / name
        used = {u.target for u in self.uploads.values()}
        base, ext = os.path.splitext(name)
        i = 2
        while path.exists() or path in used:
            path = self.folder / f"{base} ({i}){ext}"
            i += 1
        return path

    def init_file(self, token: str, name: str, size: int, key: str) -> dict:
        with self.lock:
            self.require(token)
            name = safe_name(name)
            if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                raise TransferError("حجم فایل نامعتبر است.")
            if not isinstance(key, str) or not 1 <= len(key) <= 250:
                raise TransferError("شناسه فایل نامعتبر است.")
            if key in self.keys:
                old = self.uploads[self.keys[key]]
                if old.name != name or old.size != size:
                    raise TransferError("شناسه فایل با فایل دیگری تداخل دارد.", 409)
                return old.info(chunks=True)
            if len(self.uploads) >= self.limits.max_files:
                raise TransferError("حداکثر تعداد فایل این نوبت پر شده است.", 413)
            if size > self.limits.max_file_gb * 1024 ** 3:
                raise TransferError("حجم این فایل بیش از محدودیت است.", 413)
            total = sum(u.size for u in self.uploads.values() if u.state != "cancelled")
            if total + size > self.limits.max_session_gb * 1024 ** 3:
                raise TransferError("ظرفیت این نوبت تکمیل شده است.", 413)
            if size and shutil.disk_usage(self.destination).free < size + self.limits.reserve_disk_gb * 1024 ** 3:
                raise TransferError("فضای خالی دیسک کافی نیست.", 507)
            upload_id = uuid.uuid4().hex
            target = self._unique_target(name)
            part = self.folder / f".part-{upload_id}"
            part.touch(exist_ok=False)
            upload = Upload(upload_id, name, key, size, part, target)
            self.uploads[upload_id] = upload
            self.keys[key] = upload_id
            return upload.info(chunks=True)

    def get_file(self, token: str, upload_id: str) -> dict:
        with self.lock:
            return self._get(token, upload_id).info(chunks=True)

    def _get(self, token: str, upload_id: str) -> Upload:
        self.require(token)
        upload = self.uploads.get(upload_id)
        if upload is None:
            raise TransferError("فایل یافت نشد.", 404)
        return upload

    def write_chunk(self, token: str, upload_id: str, index: int, data: bytes, checksum: str | None = None) -> dict:
        with self.lock:
            upload = self._get(token, upload_id)
            with upload.lock:
                if upload.state != "receiving":
                    raise TransferError("انتقال این فایل دیگر فعال نیست.", 409)
                if not isinstance(index, int) or not 0 <= index < upload.count:
                    raise TransferError("شماره بخش نامعتبر است.")
                expected = min(CHUNK_SIZE, upload.size - index * CHUNK_SIZE)
                if len(data) != expected:
                    raise TransferError("اندازه بخش دریافت‌شده صحیح نیست.")
                if checksum is not None and not hmac.compare_digest(hashlib.sha256(data).hexdigest(), checksum.lower()):
                    raise TransferError("کنترل صحت بخش ناموفق بود.", 422)
                if index not in upload.received:
                    with upload.part.open("r+b", buffering=0) as output:
                        output.seek(index * CHUNK_SIZE)
                        output.write(data)
                    upload.received.add(index)
                return upload.info()

    def finish(self, token: str, upload_id: str) -> dict:
        with self.lock:
            upload = self._get(token, upload_id)
            with upload.lock:
                if upload.state == "complete":
                    return upload.info()
                if upload.state != "receiving" or len(upload.received) != upload.count:
                    raise TransferError("همه بخش‌های فایل دریافت نشده‌اند.", 409)
                if upload.part.stat().st_size != upload.size:
                    raise TransferError("حجم فایل با مقدار اعلام‌شده مطابقت ندارد.", 409)
                os.replace(upload.part, upload.target)
                upload.state = "complete"
                return upload.info()

    def cancel(self, token: str, upload_id: str) -> dict:
        with self.lock:
            upload = self._get(token, upload_id)
            with upload.lock:
                if upload.state == "complete":
                    raise TransferError("فایل تکمیل شده و قابل لغو نیست.", 409)
                upload.state = "cancelled"
                upload.part.unlink(missing_ok=True)
                self.keys.pop(upload.key, None)
                return upload.info()

    def snapshot(self) -> dict:
        with self.lock:
            files = [u.info() for u in self.uploads.values()]
            return {"code": self.session_code, "folder": str(self.folder), "files": files,
                    "completed": sum(f["status"] == "complete" for f in files),
                    "totalBytes": sum(f["receivedBytes"] for f in files),
                    "maxFileBytes": self.limits.max_file_gb * 1024**3,
                    "maxSessionBytes": self.limits.max_session_gb * 1024**3,
                    "maxFiles": self.limits.max_files}
