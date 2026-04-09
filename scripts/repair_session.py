#!/usr/bin/env python3
"""
scripts/repair_session.py

Safely back up and remove corrupt Pyrogram `.session` files and optionally
re-create a fresh session (interactive) and export a `SESSION_STRING`.

Usage:
  python scripts/repair_session.py [--session-name NAME] [--all] [--backup-dir DIR] [--export] [--yes]

Examples:
  # Inspect and interactively confirm backup/removal of all .session files
  python scripts/repair_session.py --all

  # Target a specific session file and export a new session string interactively
  python scripts/repair_session.py --session-name user_session --export

Notes:
  - This script moves matched .session files into a timestamped backup directory
    under the project by default (./session_backups).
  - If `--export` is given this will run `scripts/create_user_session.py --export`
    (interactive) to re-create and print a session string.
"""

from __future__ import annotations

import argparse
import datetime
import pathlib
import shutil
import sys
import subprocess
import os


def iso_ts() -> str:
    return datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")


def find_session_files(root: pathlib.Path) -> list[pathlib.Path]:
    return sorted(list(root.glob("*.session")))


def backup_and_remove(file: pathlib.Path, backup_dir: pathlib.Path) -> pathlib.Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = iso_ts()
    dest = backup_dir / f"{file.name}.bak-{ts}"
    shutil.move(str(file), str(dest))
    return dest


def run_create_export(session_name: str) -> int:
    script = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "create_user_session.py"
    cmd = [sys.executable, str(script), "--export", session_name]
    print("Launching interactive session creation to export SESSION_STRING:")
    print(" ", " ".join(cmd))
    # Run interactively so user can complete phone/code prompts
    return subprocess.call(cmd)


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    p = argparse.ArgumentParser(description="Repair Pyrogram .session files and optionally export SESSION_STRING")
    p.add_argument("--session-name", "-s", help="Target session name (without .session)")
    p.add_argument("--all", action="store_true", help="Operate on all found .session files")
    p.add_argument("--backup-dir", help="Directory to store backups (default: ./session_backups)")
    p.add_argument("--export", action="store_true", help="After repair, run create_user_session.py --export to recreate + print SESSION_STRING")
    p.add_argument("--yes", "-y", action="store_true", help="Assume yes for confirmations")

    args = p.parse_args(argv)

    project_root = pathlib.Path(__file__).resolve().parents[1]
    backup_dir = pathlib.Path(args.backup_dir) if args.backup_dir else project_root / "session_backups"

    if args.session_name:
        target = project_root / f"{args.session_name}.session"
        if not target.exists():
            print(f"Session file not found: {target}")
            # still allow export/create if requested
            if not args.export:
                return 1
            else:
                files = []
        else:
            files = [target]
    else:
        files = find_session_files(project_root)

    if not files:
        print("No .session files found in project root.")
        if args.export:
            session_name = args.session_name or os.getenv("SESSION_NAME") or "user_session"
            return run_create_export(session_name)
        return 0

    print("Found .session files:")
    for i, f in enumerate(files, start=1):
        try:
            size = f.stat().st_size
        except Exception:
            size = 0
        print(f"  {i}) {f}  ({size} bytes)")

    if not args.yes:
        if args.all:
            prompt = f"Backup and remove ALL {len(files)} session files? [y/N]: "
        else:
            prompt = f"Backup and remove these {len(files)} session files? [y/N]: "
        resp = input(prompt).strip().lower()
        if resp not in ("y", "yes"):
            print("Aborted by user.")
            return 0

    moved = []
    for f in files:
        try:
            dest = backup_and_remove(f, backup_dir)
            print(f"Backed up {f} -> {dest}")
            moved.append(dest)
        except Exception as e:
            print(f"Failed to move {f}: {e}")

    if args.export:
        # Choose a session name for recreation
        session_name = args.session_name or os.getenv("SESSION_NAME") or (files[0].stem if files else "user_session")
        rc = run_create_export(session_name)
        return rc

    print("Done. Backups are stored in:", str(backup_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())