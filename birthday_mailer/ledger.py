"""Persist a reservation BEFORE sending; uncertain deliveries need review."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from .contacts import DataError


@contextmanager
def ledger_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + ".lock").open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        locked = False
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
            yield
        except OSError as exc:
            if not locked:
                raise DataError("发送记录被另一个进程占用，请稍后重试") from exc
            raise
        finally:
            if locked:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class Ledger:
    def __init__(self, path: Path, secret: str, sync: Callable[[], None] | None = None):
        if len(secret) < 32:
            raise DataError("STATE_SECRET 至少需要 32 个字符；可用 secrets.token_hex(32) 生成")
        self.path = path
        self.secret = secret.encode("utf-8")
        self.sync = sync
        self.key_id = hashlib.sha256(self.secret).hexdigest()[:16]
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError) as exc:
                raise DataError("发送记录损坏，已停止；请恢复记录后再运行") from exc
            if not isinstance(self.data, dict) or self.data.get("version") != 1 or self.data.get("key_id") != self.key_id:
                raise DataError("发送记录版本或 STATE_SECRET 不匹配，已停止以避免重发")
            entries = self.data.get("deliveries")
            if not isinstance(entries, dict) or any(
                not isinstance(v, dict) or v.get("status") not in {"pending", "sent", "uncertain"}
                for v in entries.values()
            ):
                raise DataError("发送记录结构无效，已停止")
        else:
            self.data = {"version": 1, "key_id": self.key_id, "deliveries": {}}

    def token(self, email: str, occurrence: str) -> str:
        return hmac.new(self.secret, f"{email.casefold()}\0{occurrence}".encode(), hashlib.sha256).hexdigest()

    def contains(self, email: str, occurrence: str) -> bool:
        return self.token(email, occurrence) in self.data["deliveries"]

    def status(self, email: str, occurrence: str) -> str | None:
        item = self.data["deliveries"].get(self.token(email, occurrence))
        return item["status"] if item else None

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".ledger-", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(self.data, handle, ensure_ascii=False, sort_keys=True, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        if self.sync:
            self.sync()  # A failed reservation sync prevents SMTP submission.

    def reserve(self, email: str, occurrence: str) -> bool:
        token = self.token(email, occurrence)
        if token in self.data["deliveries"]:
            return False
        self.data["deliveries"][token] = {"status": "pending", "updated_at": datetime.now(timezone.utc).isoformat()}
        self.save()
        return True

    def finish(self, email: str, occurrence: str, status: str) -> None:
        if status not in {"sent", "uncertain"}:
            raise ValueError("Invalid delivery status")
        self.data["deliveries"][self.token(email, occurrence)] = {
            "status": status, "updated_at": datetime.now(timezone.utc).isoformat()
        }
        self.save()

    def resolve(self, email: str, occurrence: str, action: str) -> None:
        token = self.token(email, occurrence)
        if token not in self.data["deliveries"]:
            raise DataError("没有找到该地址及生日周期的发送记录")
        if action == "allow-retry":
            del self.data["deliveries"][token]
            self.save()
        elif action == "mark-sent":
            self.finish(email, occurrence, "sent")
        else:
            raise ValueError("Invalid resolution")
