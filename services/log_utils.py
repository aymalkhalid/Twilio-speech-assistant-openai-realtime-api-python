from __future__ import annotations
import json
from datetime import datetime
from typing import Any, Optional

DASH = "-" * 70

class Log:
    @staticmethod
    def _ts() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def line() -> None:
        print(DASH)

    @staticmethod
    def header(title: str) -> None:
        print(f"\n{DASH}\n[{Log._ts()}] {title}\n{DASH}")

    @staticmethod
    def subheader(title: str) -> None:
        print(f"\n[{Log._ts()}] {title}\n{DASH}")

    @staticmethod
    def info(msg: str) -> None:
        print(f"[{Log._ts()}] {msg}")

    @staticmethod
    def error(msg: str) -> None:
        print(f"[{Log._ts()}] ERROR: {msg}")

    @staticmethod
    def event(title: str, details: Optional[dict[str, Any]] = None) -> None:
        Log.header(title)
        if details is not None:
            try:
                print(json.dumps(details, indent=2, ensure_ascii=False))
            except Exception:
                print(str(details))
        Log.line()

    @staticmethod
    def json(label: str, data: Any) -> None:
        Log.subheader(label)
        try:
            print(json.dumps(data, indent=2, ensure_ascii=False))
        except Exception:
            print(str(data))
        Log.line()
