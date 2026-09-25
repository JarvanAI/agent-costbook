import sqlite3
from pathlib import Path

import pytest

from agent_costbook.backup import BackupError, copy_database, main


def _database(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE kept (id INTEGER PRIMARY KEY)")
    connection.execute("INSERT INTO kept VALUES (1)")
    connection.commit()
    connection.close()


def _ids(path: Path) -> list[int]:
    connection = sqlite3.connect(path)
    rows = [row[0] for row in connection.execute("SELECT id FROM kept ORDER BY id")]
    connection.close()
    return rows


def test_backup_refuses_existing_target_and_same_file(tmp_path):
    source = tmp_path / "source.sqlite3"
    _database(source)
    before = source.read_bytes()
    with pytest.raises(BackupError) as same:
        copy_database(source, source)
    assert same.value.code == "same_file"
    occupied = tmp_path / "occupied.sqlite3"
    occupied.write_bytes(b"keep-me")
    with pytest.raises(BackupError) as exists:
        copy_database(source, occupied)
    assert exists.value.code == "target_exists"
    assert occupied.read_bytes() == b"keep-me"
    assert source.read_bytes() == before
    assert main(["--source", str(source), "--destination", str(source)]) == 2
    assert main(["--restore", "--source", str(source), "--destination", str(occupied)]) == 2
    assert occupied.read_bytes() == b"keep-me"


def test_backup_reads_committed_wal_without_copying_the_main_file(tmp_path):
    source = tmp_path / "live.sqlite3"
    connection = sqlite3.connect(source)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE kept (id INTEGER PRIMARY KEY)")
    connection.execute("INSERT INTO kept VALUES (1)")
    connection.commit()
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.execute("INSERT INTO kept VALUES (2)")
    connection.commit()
    main_before = source.read_bytes()
    wal = Path(str(source) + "-wal")
    wal_before = wal.read_bytes()
    raw = tmp_path / "main-only.sqlite3"
    raw.write_bytes(main_before)
    assert _ids(raw) == [1]
    destination = tmp_path / "backup.sqlite3"
    copy_database(source, destination)
    assert _ids(destination) == [1, 2]
    assert source.read_bytes() == main_before
    assert wal.read_bytes() == wal_before
    connection.close()


def test_restore_round_trip_preserves_rows_and_source(tmp_path):
    source = tmp_path / "source.sqlite3"
    _database(source)
    saved = tmp_path / "saved.sqlite3"
    assert main(["--source", str(source), "--destination", str(saved)]) == 0
    assert _ids(saved) == [1]
    connection = sqlite3.connect(source)
    connection.execute("INSERT INTO kept VALUES (9)")
    connection.commit()
    connection.close()
    source_after = source.read_bytes()
    restored = tmp_path / "restored.sqlite3"
    assert main(["--restore", "--source", str(saved), "--destination", str(restored)]) == 0
    assert _ids(restored) == [1]
    assert _ids(source) == [1, 9]
    assert source.read_bytes() == source_after


def test_backup_failure_does_not_leave_a_destination(tmp_path, monkeypatch):
    source = tmp_path / "source.sqlite3"
    _database(source)
    before = source.read_bytes()
    destination = tmp_path / "destination.sqlite3"

    def boom(_source, _destination):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr("agent_costbook.backup._sqlite_backup", boom)
    with pytest.raises(BackupError) as failure:
        copy_database(source, destination)
    assert failure.value.code == "failed"
    assert not destination.exists()
    assert source.read_bytes() == before
    assert list(tmp_path.glob(".*partial")) == []
