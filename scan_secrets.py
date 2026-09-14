"""Fail CI when private runtime files or obvious credential literals are tracked."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent
FORBIDDEN_NAMES = {
    ".env", "config.json", "steam_guard.maFile", "steam_cookies.json",
    "steam_web_session.json", ".cstrade_session", ".skinstable_session",
    "trade_log.xlsx",
}
PATTERNS = {
    "DMarket private key": re.compile(r"(?<![0-9a-f])[0-9a-f]{128}(?![0-9a-f])", re.I),
    "Telegram bot token": re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"),
    "assigned credential": re.compile(
        r"(?i)(?:api[_-]?key|token|password|private[_-]?key|shared[_-]?secret|"
        r"identity[_-]?secret)\s*=\s*['\"][^'\"]{8,}['\"]"
    ),
}
ALLOWED = {".env.example", "scan_secrets.py"}


def tracked_files() -> list[Path]:
    try:
        output = subprocess.check_output(
            ["git", "ls-files"], cwd=ROOT, text=True, encoding="utf-8"
        )
        names = [line for line in output.splitlines() if line]
        if not names:
            names = [str(path.relative_to(ROOT)) for path in ROOT.rglob("*")
                     if path.is_file() and ".git" not in path.parts]
    except (OSError, subprocess.CalledProcessError):
        names = [str(path.relative_to(ROOT)) for path in ROOT.rglob("*") if path.is_file()]
    return [ROOT / name for name in names]


def main() -> int:
    failures: list[str] = []
    for path in tracked_files():
        rel = path.relative_to(ROOT)
        if path.name in FORBIDDEN_NAMES or path.suffix in {".maFile", ".session"}:
            failures.append(f"private file: {rel}")
            continue
        if path.name in ALLOWED or path.suffix not in {".py", ".service", ".md", ".txt", ".yml", ".yaml"}:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for label, pattern in PATTERNS.items():
            if pattern.search(text):
                failures.append(f"{label}: {rel}")
    if failures:
        print("Secret scan failed:")
        print("\n".join(f"- {item}" for item in failures))
        return 1
    print("Secret scan passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
