"""A tiny JSON document store with atomic writes (temp file + rename) and a process lock."""
from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

EMPTY: dict[str, Any] = {
    "version": 1,
    "household": {"name": "My household", "timezone": "UTC", "recipient": None, "members": []},
    "settings": {"monthly_spend_cap_cents": 15000, "quote_ttl_minutes": 10},
    "meds": [], "doses": [], "appointments": [], "notes": [], "seen": {},
    "checkins": [], "alerts": [], "orders": [], "quotes": {}, "audit": [], "counters": {},
}


class Store:
    def __init__(self, path: str | os.PathLike | None):
        self.path = Path(path) if path else None
        self.lock = threading.RLock()
        self.data: dict[str, Any] = copy.deepcopy(EMPTY)
        if self.path and self.path.exists():
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            self.data.update(loaded)

    def next_id(self, prefix: str) -> str:
        c = self.data["counters"]
        c[prefix] = c.get(prefix, 0) + 1
        return f"{prefix}_{c[prefix]}"

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".careloop-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
