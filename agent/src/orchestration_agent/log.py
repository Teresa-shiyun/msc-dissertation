from __future__ import annotations

import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Optional, TextIO


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class EventLog:
    def __init__(self, file_path: Optional[str] = None) -> None:
        self._file: Optional[TextIO] = open(file_path, "a", encoding="utf-8") if file_path else None

    def emit(self, **fields: Any) -> None:
        record = {"ts": _utc_now_iso(), **fields}
        for k, v in list(record.items()):
            if hasattr(v, "__dataclass_fields__"):
                record[k] = asdict(v)
        line = json.dumps(record, default=str, sort_keys=False)
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
        if self._file is not None:
            self._file.write(line + "\n")
            self._file.flush()

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
