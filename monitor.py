"""BPL polling, DNS discovery and SQLite storage. Python 3.11+."""
from __future__ import annotations

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import csv
from dataclasses import dataclass, field
import json
import ipaddress
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
                         "retries": 1, "retry_delay_seconds": 2, "workers": 4,
                         "dns_timeout_seconds": 5, "dns_port": 53}.items():
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
    cfg.setdefault("jumphost", "NMS3_Infra")
    jump = cfg["jumphost"]
    if jump is not None and (not isinstance(jump, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@:-]*", jump)):
        raise ValueError("jumphost must be an SSH alias or user@host[:port], or null")
    cfg.setdefault("dns_server", "10.40.8.117")
    cfg["dns_server"] = str(ipaddress.ip_address(cfg["dns_server"]))
    if cfg["dns_port"] > 65535:
        raise ValueError("dns_port out of range")
    cfg.setdefault("ssh_user", "root")
    if not isinstance(cfg["ssh_user"], str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", cfg["ssh_user"]):
        raise ValueError("Invalid ssh_user")
    if cfg.get("headends_csv"):
        if cfg.get("headends"):
            raise ValueError("Choose headends_csv or a manual headends list, not both")
        cfg["headends_csv"] = str((path.parent / cfg["headends_csv"]).resolve())
        cfg["headends"] = load_headends(cfg["headends_csv"], cfg["ssh_user"])
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
        if headend.get("ssh_user") is not None and not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", headend["ssh_user"]):
            raise ValueError("Invalid headend ssh_user")
        if headend.get("dns_name") and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", headend["dns_name"]):
            raise ValueError("Invalid headend dns_name")
        names.add(name)
        destinations.add(destination)
    return cfg


def load_headends(path, ssh_user="root"):
    """HE rows only; validate hostname against MAC, never use CSV IP for routing."""
    result, seen = [], set()
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=";")
        if not {"identification", "deviceUniqueIdAnchor", "unitType"}.issubset(reader.fieldnames or []):
            raise ValueError("Headend CSV requires identification;deviceUniqueIdAnchor;unitType")
        for line, row in enumerate(reader, 2):
            if (row.get("unitType") or "").strip().upper() != "HE":
                continue
            mac = normalize_mac(row["deviceUniqueIdAnchor"])
            hostname = row["identification"].strip().lower()
            expected = mac.replace(":", "") + ".eui"
            if hostname != expected:
                raise ValueError(f"Headend CSV line {line}: hostname {hostname!r} does not match MAC {mac}")
            if hostname in seen:
                raise ValueError(f"Duplicate headend in CSV: {hostname}")
            seen.add(hostname)
            result.append({"name": hostname, "ssh_host": hostname, "dns_name": hostname,
                           "ssh_user": ssh_user, "mac": mac,
                           "label": row.get("deviceName", ""),
                           "csv_reference_ip": row.get("deviceWanIpAddress", "")})
    if not result:
        raise ValueError("No unitType=HE rows found in headend CSV")
    return result


def resolve_headend(hostname, cfg):
    """Explicit resolver; no system search domain or CSV-IP fallback."""
    try:
        import dns.exception
        import dns.resolver
    except ImportError as exc:
        raise RuntimeError("DNS discovery requires: python -m pip install -r requirements-collector.txt") from exc
    resolver = dns.resolver.Resolver(configure=False)
    resolver.nameservers = [cfg["dns_server"]]
    resolver.port = cfg["dns_port"]
    resolver.timeout = cfg["dns_timeout_seconds"]
    resolver.lifetime = cfg["dns_timeout_seconds"]
    try:
        answer = resolver.resolve(hostname.rstrip(".") + ".", "A", search=False,
                                  lifetime=cfg["dns_timeout_seconds"])
        addresses = sorted({str(ipaddress.IPv4Address(record.address)) for record in answer}, key=ipaddress.IPv4Address)
    except (dns.exception.DNSException, OSError, ValueError) as exc:
        raise RuntimeError(f"DNS {hostname} via {cfg['dns_server']}: {type(exc).__name__}: {exc}") from exc
    if len(addresses) != 1:
        raise RuntimeError(f"DNS {hostname}: expected one distinct A record, got {addresses}")
    return addresses[0]


SCHEMA = """
CREATE TABLE IF NOT EXISTS cycles (
 id INTEGER PRIMARY KEY, started_at REAL NOT NULL, finished_at REAL NOT NULL,
 interval_seconds INTEGER NOT NULL, expected_headends INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS polls (
 cycle_id INTEGER NOT NULL REFERENCES cycles(id) ON DELETE CASCADE,
 headend TEXT NOT NULL, status TEXT NOT NULL, observed_at REAL NOT NULL,
 attempts INTEGER NOT NULL, error TEXT, row_count INTEGER NOT NULL,
 duplicate_count INTEGER NOT NULL, dns_name TEXT, resolved_ip TEXT,
 PRIMARY KEY(cycle_id,headend));
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
    if version not in (0, 1, 2):
        db.close()
        raise ValueError(f"Unsupported database schema {version}")
    db.executescript(SCHEMA)
    with db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(polls)")}
        for name in ("dns_name", "resolved_ip"):
            if name not in columns:
                db.execute(f"ALTER TABLE polls ADD COLUMN {name} TEXT")
        db.execute("PRAGMA user_version=2")
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
    dns_name: str | None = None
    resolved_ip: str | None = None


def ssh_command(destination, cfg, resolved_ip=None, ssh_user=None):
    # OpenSSH itself reads ~/.ssh/config, including ProxyJump, keys and aliases.
    command = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
               "-o", f"ConnectTimeout={cfg['connect_timeout_seconds']}",
               "-o", "ConnectionAttempts=1", "-o", "ServerAliveInterval=10",
               "-o", "ServerAliveCountMax=2"]
    if cfg.get("jumphost"):
        command += ["-J", cfg["jumphost"]]
    if ssh_user:
        command += ["-l", ssh_user]
    if resolved_ip:
        # Keep the .eui name for SSH config and known_hosts; route via DNS IP.
        command += ["-o", f"HostName={ipaddress.IPv4Address(resolved_ip)}",
                    "-o", f"HostKeyAlias={destination}", "-o", "CanonicalizeHostname=no"]
    return command + [destination, "ppcdinfo unittable"]


def ssh_output(destination, cfg, resolved_ip=None, ssh_user=None):
    command = ssh_command(destination, cfg, resolved_ip, ssh_user)
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
    hostname = headend.get("dns_name")
    resolved_ip = None
    for attempt in range(1, cfg["retries"] + 2):
        try:
            resolved_ip = None
            if hostname:
                resolved_ip = resolve_headend(hostname, cfg)
            units = parse_unittable(ssh_output(headend["ssh_host"], cfg, resolved_ip, headend.get("ssh_user")))
            return Poll(headend["name"], "ok", time.time(), units, attempt,
                        dns_name=hostname, resolved_ip=resolved_ip)
        except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
            error = f"{type(exc).__name__}: {exc}"[:2400]
            LOG.warning("%s attempt %d: %s", headend["name"], attempt, error)
            if attempt <= cfg["retries"]:
                time.sleep(cfg["retry_delay_seconds"])
    return Poll(headend["name"], "error", time.time(), attempts=attempt, error=error,
                dns_name=hostname, resolved_ip=resolved_ip)


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
            db.execute("""INSERT INTO polls(cycle_id,headend,status,observed_at,attempts,error,
                       row_count,duplicate_count,dns_name,resolved_ip) VALUES(?,?,?,?,?,?,?,?,?,?)""", (
                cycle, poll.headend, poll.status, poll.observed_at, poll.attempts,
                poll.error, sum(map(len, groups.values())), duplicates, poll.dns_name, poll.resolved_ip))
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
    parser.add_argument("--list-headends", action="store_true", help="List selected targets without DNS or SSH")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(args.config)
    if args.list_headends:
        print(f"{len(cfg['headends'])} headends; jumphost={cfg['jumphost']}; DNS={cfg['dns_server']}")
        for headend in cfg["headends"]:
            print(f"{headend['name']}\t{headend.get('mac', '')}\t{headend.get('label', '')}")
        return 0
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
