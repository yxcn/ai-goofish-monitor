"""bootstrap_sqlite_storage() 每个库路径只跑一次 DDL；reset_bootstrap_state() / 库文件消失时重跑。"""
import os

from src.infrastructure.persistence import sqlite_bootstrap as bootstrap_module
from src.infrastructure.persistence.sqlite_bootstrap import (
    bootstrap_sqlite_storage,
    reset_bootstrap_state,
)
from src.infrastructure.persistence.sqlite_connection import sqlite_connection


def _count_init_schema(monkeypatch) -> list:
    calls = []
    real_init = bootstrap_module.init_schema

    def counting_init(conn):
        calls.append(1)
        real_init(conn)

    monkeypatch.setattr(bootstrap_module, "init_schema", counting_init)
    return calls


def test_bootstrap_runs_ddl_once_per_db_path(tmp_path, monkeypatch):
    calls = _count_init_schema(monkeypatch)
    db_a = str(tmp_path / "a.sqlite3")
    db_b = str(tmp_path / "b.sqlite3")

    bootstrap_sqlite_storage(db_a, legacy_config_file=None)
    bootstrap_sqlite_storage(db_a, legacy_config_file=None)
    bootstrap_sqlite_storage(os.path.join(str(tmp_path), ".", "a.sqlite3"), legacy_config_file=None)
    assert len(calls) == 1

    bootstrap_sqlite_storage(db_b, legacy_config_file=None)
    assert len(calls) == 2

    reset_bootstrap_state()
    bootstrap_sqlite_storage(db_a, legacy_config_file=None)
    assert len(calls) == 3


def test_bootstrap_reruns_when_db_file_disappears(tmp_path, monkeypatch):
    calls = _count_init_schema(monkeypatch)
    db_path = tmp_path / "a.sqlite3"

    bootstrap_sqlite_storage(str(db_path), legacy_config_file=None)
    assert len(calls) == 1
    db_path.unlink()
    for suffix in ("-wal", "-shm"):
        sidecar = tmp_path / f"a.sqlite3{suffix}"
        if sidecar.exists():
            sidecar.unlink()

    bootstrap_sqlite_storage(str(db_path), legacy_config_file=None)
    assert len(calls) == 2
    with sqlite_connection(str(db_path)) as conn:
        names = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"tasks", "result_items", "price_snapshots"} <= names


def test_default_relative_path_is_keyed_by_absolute_location(tmp_path, monkeypatch):
    calls = _count_init_schema(monkeypatch)
    monkeypatch.delenv("APP_DATABASE_FILE", raising=False)
    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()

    monkeypatch.chdir(tmp_path / "one")
    bootstrap_sqlite_storage()
    bootstrap_sqlite_storage()
    assert len(calls) == 1

    monkeypatch.chdir(tmp_path / "two")
    bootstrap_sqlite_storage()
    assert len(calls) == 2
    assert (tmp_path / "one" / "data" / "app.sqlite3").exists()
    assert (tmp_path / "two" / "data" / "app.sqlite3").exists()
