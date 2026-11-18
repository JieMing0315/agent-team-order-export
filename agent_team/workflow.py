"""Coordinate the demo roles and checkpoint each state transition in SQLite."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .verifier import Check, verify


FIXTURES = Path(__file__).with_name("fixtures")
SOURCE_FILES = ("export_app.py", "client.py")
BUG = "        except OSError:\n            return  # Demo defect: failed writes leave the job processing.\n"
FIX = '        except OSError:\n            self._set_status(job_id, "failed")\n            return\n'
TERMINAL = ("done", "blocked")

SCHEMA = """
CREATE TABLE run (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    phase TEXT NOT NULL,
    revision INTEGER NOT NULL,
    max_repairs INTEGER NOT NULL
);
CREATE TABLE event (
    seq INTEGER PRIMARY KEY,
    timestamp TEXT NOT NULL,
    role TEXT NOT NULL,
    phase TEXT NOT NULL,
    revision INTEGER NOT NULL,
    message TEXT NOT NULL
);
"""


def open_run(path: Path) -> sqlite3.Connection:
    database = path / "run.sqlite3"
    if not database.is_file():
        raise ValueError(f"找不到运行记录 {database}")
    connection = sqlite3.connect(database, timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def state(connection: sqlite3.Connection) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM run WHERE id = 1").fetchone()
    if row is None:
        raise ValueError("运行记录缺少初始状态")
    return row


def events_after(connection: sqlite3.Connection, seq: int) -> list[sqlite3.Row]:
    return connection.execute("SELECT * FROM event WHERE seq > ? ORDER BY seq", (seq,)).fetchall()


def record(connection, role: str, phase: str, revision: int, message: str) -> None:
    connection.execute(
        "INSERT INTO event(timestamp, role, phase, revision, message) VALUES (?, ?, ?, ?, ?)",
        (datetime.now(timezone.utc).isoformat(timespec="milliseconds"), role, phase, revision, message),
    )


def create_run(root: Path, max_repairs: int) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix="run-", dir=root)).resolve()
    with closing(sqlite3.connect(path / "run.sqlite3")) as connection:
        connection.executescript(SCHEMA)
        with connection:
            connection.execute("INSERT INTO run VALUES (1, 'producing', 1, ?)", (max_repairs,))
            record(connection, "Leader", "producing", 1, "派发后端导出和前端轮询。验收成功下载与写入失败两条路径。")
    return path


def snapshot(path: Path, revision: int) -> Path:
    return path / "artifacts" / f"v{revision}"


def repair_backend(source: Path, destination: Path) -> None:
    content = source.read_text(encoding="utf-8")
    if content.count(BUG) != 1:
        raise ValueError("后端候选版本中找不到唯一的待修复分支")
    destination.write_text(content.replace(BUG, FIX, 1), encoding="utf-8")


def produce(path: Path, revision: int) -> Path:
    destination = snapshot(path, revision)
    destination.parent.mkdir(exist_ok=True)
    if destination.is_dir():
        # A process may have stopped immediately after publishing the files.
        return destination
    with tempfile.TemporaryDirectory(dir=destination.parent) as temp:
        staged = Path(temp) / "candidate"
        staged.mkdir()
        if revision == 1:
            with ThreadPoolExecutor(max_workers=2) as pool:
                backend = pool.submit(shutil.copyfile, FIXTURES / "export_app.py", staged / "export_app.py")
                frontend = pool.submit(shutil.copyfile, FIXTURES / "client.py", staged / "client.py")
                backend.result()
                frontend.result()
        else:
            previous = snapshot(path, revision - 1)
            repair_backend(previous / "export_app.py", staged / "export_app.py")
            shutil.copyfile(previous / "client.py", staged / "client.py")
        staged.rename(destination)
    return destination


def hashes(directory: Path) -> dict[str, str]:
    return {name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in SOURCE_FILES}


def write_report(path: Path, revision: int, source_hashes: dict[str, str], checks: list[Check]) -> Path:
    destination = path / f"verification-v{revision}.json"
    with tempfile.TemporaryDirectory(dir=path) as temp:
        staged = Path(temp) / destination.name
        report = {"revision": revision, "source_sha256": source_hashes, "checks": [asdict(check) for check in checks]}
        staged.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        staged.replace(destination)
    return destination


def verify_revision(connection, path: Path, revision: int, max_repairs: int) -> bool:
    candidate = snapshot(path, revision)
    source_hashes = hashes(candidate)
    checks = {check.name: check for check in verify(candidate)}
    if hashes(candidate) != source_hashes:
        raise ValueError("验收期间候选文件发生变化，请重新检查候选产物")
    report = write_report(path, revision, source_hashes, list(checks.values()))
    for check in checks.values():
        outcome = "通过" if check.passed else "失败"
        record(connection, "Verifier", "verifying", revision, f"{outcome} {check.name} · {check.details}")

    ready = checks.keys() == {"download", "write_failure"}
    if ready and all(check.passed for check in checks.values()):
        connection.execute("UPDATE run SET phase = 'done' WHERE id = 1")
        record(connection, "Leader", "done", revision, f"交付 artifacts/v{revision}，验收证据 {report.name}。")
        return False

    known_defect = ready and checks["download"].passed and checks["write_failure"].code == "poll_timeout"
    if known_defect and revision == 1 and revision <= max_repairs:
        connection.execute("UPDATE run SET phase = 'producing', revision = revision + 1 WHERE id = 1")
        record(connection, "Team Engine", "producing", revision + 1, "退回后端 Worker，修复写入失败后的状态。复用前端产物。")
        return True

    connection.execute("UPDATE run SET phase = 'blocked' WHERE id = 1")
    reason = "已用完返修预算" if revision > max_repairs else "没有适用的预设修复"
    record(connection, "Leader", "blocked", revision, f"{reason}，保留产物和报告 {report.name} 供人工排查。")
    return False


def execute(connection: sqlite3.Connection, path: Path, emit, *, pause_on_rejection: bool = False) -> str:
    """Advance one phase per transaction so a restart repeats only the uncommitted phase."""
    last_seq = connection.execute("SELECT COALESCE(MAX(seq), 0) FROM event").fetchone()[0]
    while True:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            current = state(connection)
            phase, revision = current["phase"], current["revision"]
            if phase in TERMINAL:
                return phase
            rejected = False
            if phase == "producing":
                produce(path, revision)
                if revision == 1:
                    record(connection, "Backend Worker", phase, revision, "提交异步导出接口与后台写入。")
                    record(connection, "Frontend Worker", phase, revision, "提交轮询客户端与下�
