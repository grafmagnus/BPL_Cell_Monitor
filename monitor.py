"""BPL polling, parsing and SQLite storage. Python 3.11+, standard library only."""
from __future__ import annotations

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
import json
import logging
import math
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import time

LOG = logging.getLogger("bpl")
MAC = r"[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}"
ROW = re.compile(rf"^\s*({MAC})\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|?\s*$")
HEADER = re.compile(r"ethmac\s*\|\s*id\s*\|\s*cell\s*\|\s*age\s*\|?", re.I)


def normalize_mac(value):
    value = value.strip().lower().replace("-", ":")
    if re.fullmatch(r"[0-9a-f]{12}", value):
        value = ":".join(value[i:i+2] for i in range(0, 12, 2))
    if not re.fullmatch(MAC, value):
        raise ValueError(f"Invalid MAC: {value!r}")
    return value


@dataclass(frozen=True)
class Unit:
    mac: str
    unit_id: int
    cell: int
    age: int


def parse_unittable(output: str) -> list[Unit]:
    """Fail closed on changed format or malformed body; never silently drop rows."""
    lines = output.replace("\r", "").splitlines()
    headers = [i for i, line in enumerate(lines) if HEADER.fullmatch(line.strip())]
    if len(headers) != 1:
        raise ValueError("Expected exactly one ethmac | id | cell | age header")
    if not any(re.fullmatch(r"[-+\s]+", line.strip()) and "-" in line
               for line in lines[headers[0]+1:]):
        raise ValueError("Table separator missing (possibly truncated output)")
    units = []
    for line in lines[headers[0]+1:]:
        line = line.strip()
        if not line or re.fullmatch(r"[-+=|\s]+", line):
            continue
        match = ROW.fullmatch(line)
        if not match:
            raise ValueError(f"Unrecognized table body: {line[:160]!r}")
        mac, unit_id, cell, age = match.groups()
        units.append(Unit(normalize_mac(mac), int(unit_id), int(cell), int(age)))
    return units


def load_config(path):
    path = Path(path).resolve()
    cfg = json.loads(path.read_text(encoding="utf-8-sig"))
    for key, default in {"interval_seconds": 300, "retention_days": 56,
                         "timeout_seconds": 35, "connect_timeout_seconds": 10,
                         "retries": 1, "retry_delay_seconds": 2, "workers": 4}.items():
        cfg.setdefault(key, default)
        if type(cfg[key]) is not int or cfg[key] < (0 if key in {"retries", "retry_delay_seconds"} else 1):
            raise ValueError(f"Invalid {key}")
    if cfg["retries"] > 5 or cfg["workers"] > 64:
        raise ValueError("retries <= 5 and workers <= 64 required")
    cfg.setdefault("max_age", None)
    if cfg["max_age"] is not None and (type(cfg["max_age"]) is not int or cfg["max_age"] < 0):
        raise ValueError("max_age must be null or a nonnegative integer")
    cfg["database"] = str((path.parent / cfg.get("database", "data/bpl.sqlite3")).resolve())
    cfg["coordinates"] = str((path.parent / cfg.get("coordinates", "coordinates.csv")).resolve())
    names, destinations = set(), set()
    if not cfg.get("headends"):
        raise ValueError("At least one headend is required")
    for headend in cfg["headends"]:
        name, destination = headend["name"], headend["ssh_host"]
        if not isinstance(name, str) or not name or name in names:
            raise ValueError("Headend names must be nonempty and unique")
        if not isinstance(destination, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@:%\[\]-]*", destination):
            raise ValueError("ssh_host must be a host alias, address, or user@host")
        if destination in destinations:
            raise ValueError("Duplicate ssh_host; do not poll the same alias twice")
        names.add(name)
        destinations.add(destination)
    return cfg


SCHEMA = """
CREATE TABLE IF NOT EXISTS cycles (
 id INTEGER PRIMARY KEY, started_at REAL NOT NULL, finished_at REAL NOT NULL,
 interval_seconds INTEGER NOT NULL, expected_headends INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS polls (
 cycle_id INTEGER NOT NULL REFERENCES cycles(id) ON DELETE CASCADE,
 headend TEXT NOT NULL, status TEXT NOT NULL, observed_at REAL NOT NULL,
 attempts INTEGER NOT NULL, error TEXT, row_count INTEGER NOT NULL,
 duplicate_count INTEGER NOT NULL, PRIMARY KEY(cycle_id,headend));
CREATE TABLE IF NOT EXISTS devices (mac TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS observations (
 cycle_id INTEGER NOT NULL, headend TEXT NOT NULL, mac TEXT NOT NULL,
 cell INTEGER NOT NULL, unit_id INTEGER NOT NULL, age INTEGER NOT NULL,
 age_max INTEGER NOT NULL, duplicates INTEGER NOT NULL,
 FOREIGN KEY(cycle_id,headend) REFERENCES polls(cycle_id,headend) ON DELETE CASCADE,
 PRIMARY KEY(cycle_id,headend,mac,cell,unit_id));
CREATE TABLE IF NOT EXISTS states (
 cycle_id INTEGER NOT NULL REFERENCES cycles(id) ON DELETE CASCADE,
 mac TEXT NOT NULL REFERENCES devices(mac), status TEXT NOT NULL,
 headend TEXT, cell INTEGER, unit_id INTEGER, age INTEGER,
 coverage_complete INTEGER NOT NULL, reason TEXT NOT NULL,
 PRIMARY KEY(cycle_id,mac));
CREATE INDEX IF NOT EXISTS states_mac_cycle ON states(mac,cycle_id);
CREATE INDEX IF NOT EXISTS observations_mac_cycle ON observations(mac,cycle_id);
CREATE INDEX IF NOT EXISTS cycles_time ON cycles(started_at);
"""


def open_database(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30)
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA journal_mode=WAL")
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version not in (0, 1):
        db.close()
        raise ValueError(f"Unsupported database schema {version}")
    db.executescript(SCHEMA)
    db.execute("PRAGMA user_version=1")
    return db


@contextmanager
def collector_lock(database):
    """OS-held lock releases after a crash; the lock file itself may remain."""
    lock_path = Path(str(database) + ".collector.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    acquired = False
    try:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError as exc:
            raise RuntimeError("Another collector is using this database") from exc
        yield
    finally:
        if acquired:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


@dataclass
class Poll:
    headend: str
    status: str
    observed_at: float
    units: list[Unit] = field(default_factory=list)
    attempts: int = 1
    error: str | None = None


def ssh_output(destination, cfg):
    # OpenSSH itself reads ~/.ssh/config, including ProxyJump, keys and aliases.
    command = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
               "-o", f"ConnectTimeout={cfg['connect_timeout_seconds']}",
               "-o", "ConnectionAttempts=1", "-o", "ServerAliveInterval=10",
               "-o", "ServerAliveCountMax=2", destination, "ppcdinfo unittable"]
    options = {"start_new_session": True} if os.name != "nt" else {
        "creationflags": subprocess.CREATE_NO_WINDOW}
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          stdin=subprocess.DEVNULL, **options) as proc:
        try:
            stdout, stderr = proc.communicate(timeout=cfg["timeout_seconds"])
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                os.killpg(proc.pid, signal.SIGKILL)
            proc.kill()
            proc.communicate()
            raise TimeoutError(f"SSH exceeded {cfg['timeout_seconds']} seconds")
        if proc.returncode:
            raise RuntimeError(f"SSH exit {proc.returncode}: {stderr.decode('utf-8', 'replace')[-2000:]}")
        return stdout.decode("utf-8", "strict")


def poll_headend(headend, cfg):
    error = ""
    for attempt in range(1, cfg["retries"] + 2):
        try:
            units = parse_unittable(ssh_output(headend["ssh_host"], cfg))
            return Poll(headend["name"], "ok", time.time(), units, attempt)
        except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
            error = f"{type(exc).__name__}: {exc}"[:2400]
            LOG.warning("%s attempt %d: %s", headend["name"], attempt, error)
            if attempt <= cfg["retries"]:
                time.sleep(cfg["retry_delay_seconds"])
    return Poll(headend["name"], "error", time.time(), attempts=attempt, error=error)


def store_cycle(db, polls, cfg, started_at=None, finished_at=None):
    """Commit an entire round atomically. All candidates survive reconciliation."""
    started_at = time.time() if started_at is None else started_at
    finished_at = time.time() if finished_at is None else finished_at
    if sorted(p.headend for p in polls) != sorted(h["name"] for h in cfg["headends"]):
        raise ValueError("Poll set does not match configured headends")
    complete = all(p.status == "ok" for p in polls)
    candidates, collisions = defaultdict(list), set()
    with db:
        cycle = db.execute("INSERT INTO cycles(started_at,finished_at,interval_seconds,expected_headends) VALUES(?,?,?,?)",
                           (started_at, finished_at, cfg["interval_seconds"], len(polls))).lastrowid
        for poll in polls:
            groups, addresses = defaultdict(list), defaultdict(set)
            for unit in poll.units if poll.status == "ok" else []:
                groups[(unit.mac, unit.cell, unit.unit_id)].append(unit.age)
                addresses[(unit.cell, unit.unit_id)].add(unit.mac)
            collisions.update(mac for macs in addresses.values() if len(macs) > 1 for mac in macs)
            duplicates = sum(len(ages)-1 for ages in groups.values())
            db.execute("INSERT INTO polls VALUES(?,?,?,?,?,?,?,?)", (
                cycle, poll.headend, poll.status, poll.observed_at, poll.attempts,
                poll.error, sum(map(len, groups.values())), duplicates))
            for (mac, cell, unit_id), ages in groups.items():
                age = min(ages)
                db.execute("INSERT OR IGNORE INTO devices VALUES(?)", (mac,))
                db.execute("INSERT INTO observations VALUES(?,?,?,?,?,?,?,?)",
                           (cycle, poll.headend, mac, cell, unit_id, age, max(ages), len(ages)-1))
                if cfg.get("max_age") is None or age <= cfg["max_age"]:
                    candidates[mac].append((poll.headend, cell, unit_id, age))
        seen = {u.mac for p in polls if p.status == "ok" for u in p.units}
        for (mac,) in db.execute("SELECT mac FROM devices").fetchall():
            options = candidates[mac]
            headend = cell = unit_id = age = None
            if mac in collisions or len(options) > 1:
                status, reason = "conflict", "ID collision or multiple fresh assignments; inspect observations"
            elif len(options) == 1:
                headend, cell, unit_id, age = options[0]
                status, reason = "observed", "Unique fresh assignment" if complete else "Unique observation; other headends failed"
            elif mac in seen:
                status, reason = "stale", "All observations exceed max_age"
            elif complete:
                status, reason = "missing", "Absent from every successfully queried table"
            else:
                status, reason = "unknown", "Absent from available tables; incomplete headend coverage"
            db.execute("INSERT INTO states VALUES(?,?,?,?,?,?,?,?,?)", (
                cycle, mac, status, headend, cell, unit_id, age, int(complete), reason))
        db.execute("DELETE FROM cycles WHERE started_at < ?", (started_at - cfg["retention_days"] * 86400,))
    return cycle


def collect_once(db, cfg):
    started = time.time()
    with ThreadPoolExecutor(max_workers=cfg["workers"]) as executor:
        polls = list(executor.map(lambda h: poll_headend(h, cfg), cfg["headends"]))
    cycle = store_cycle(db, polls, cfg, started)
    LOG.info("Round %d: %d/%d headends successful", cycle, sum(p.status == "ok" for p in polls), len(polls))
    return all(p.status == "ok" for p in polls)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--once", action="store_true", help="Single round; exit 2 on partial/failing round")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(args.config)
    with collector_lock(cfg["database"]):
        db = open_database(cfg["database"])
        try:
            if args.once:
                return 0 if collect_once(db, cfg) else 2
            deadline = time.monotonic()
            while True:
                collect_once(db, cfg)
                deadline += cfg["interval_seconds"]
                now = time.monotonic()
                if deadline < now:
                    missed = math.floor((now-deadline)/cfg["interval_seconds"]) + 1
                    deadline += missed * cfg["interval_seconds"]
                    LOG.warning("Round overrun: skipping %d scheduled slots", missed)
                time.sleep(max(0, deadline-time.monotonic()))
        finally:
            db.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        LOG.info("Stopped")
