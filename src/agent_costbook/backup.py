from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path


class BackupError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _same_path(source: Path, destination: Path) -> bool:
    return source.resolve() == destination.resolve()


def _sqlite_backup(source: Path, destination: Path) -> None:
    """Copy a consistent database, including committed WAL frames, into a new file."""
    origin = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)
    copied = sqlite3.connect(destination)
    try:
        origin.backup(copied)
    finally:
        copied.close()
        origin.close()


def copy_database(source: str | Path, destination: str | Path) -> None:
    """Back up or restore one SQLite file without overwriting either path."""
    source = Path(source)
    destination = Path(destination)
    if not source.is_file():
        raise BackupError("not_found")
    if _same_path(source, destination):
        raise BackupError("same_file")
    if destination.exists() or destination.is_symlink():
        raise BackupError("target_exists")
    if not destination.parent.is_dir():
        raise BackupError("not_found")
    partial = destination.with_name(f".{destination.name}.{os.getpid()}.partial")
    if partial.exists() or partial.is_symlink():
        raise BackupError("target_exists")
    placeholder = False
    try:
        descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.close(descriptor)
        placeholder = True
        _sqlite_backup(source, partial)
        os.replace(partial, destination)
        placeholder = False
    except BackupError:
        raise
    except FileExistsError as exc:
        raise BackupError("target_exists") from exc
    except Exception as exc:
        raise BackupError("failed") from exc
    finally:
        if partial.exists():
            partial.unlink()
        if placeholder and destination.exists():
            destination.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-costbook-backup")
    parser.add_argument("--source", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--restore", action="store_true")
    args = parser.parse_args(argv)
    try:
        copy_database(args.source, args.destination)
    except BackupError as exc:
        print(exc.code, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
