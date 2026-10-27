"""Generate isolated synthetic history; never SSH and never overwrite a database."""
import argparse
import json
from pathlib import Path
import time

from monitor import Poll, Unit, collector_lock, open_database, store_cycle


def generate(directory, days=3):
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    database = directory / "demo.sqlite3"
    if database.exists() or (directory / "config.json").exists():
        raise ValueError("Demo destination already contains a database/config; choose a new directory")
    cfg = {"database": "demo.sqlite3", "coordinates": "coordinates.csv",
           "interval_seconds": 300, "retention_days": 56, "max_age": 20,
           "headends": [{"name": "HE-A", "ssh_host": "demo-a"}, {"name": "HE-B", "ssh_host": "demo-b"}]}
    (directory / "config.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    (directory / "coordinates.csv").write_text(
        "mac,latitude,longitude,label\n"
        "00:25:18:04:7a:02,47.3769,8.5417,DEMO device A\n"
        "00:25:18:05:b6:a4,47.3780,8.5450,DEMO device B\n"
        "00:25:18:05:a7:8e,47.3740,8.5380,DEMO device C\n", encoding="utf-8")
    a, b, c = "00:25:18:04:7a:02", "00:25:18:05:b6:a4", "00:25:18:05:a7:8e"
    end = int(time.time() // 300) * 300
    with collector_lock(database):
        db = open_database(database)
        try:
            for i in range(days * 288):
                ts = end - (days * 288 - 1 - i) * 300
                phase = i % 48
                left = [Unit(b, 4, 15, 1), Unit(c, 5, 15, 80 if phase == 36 else 2)]
                right = []
                if phase < 16:
                    left.append(Unit(a, 1, 15, 2))
                elif phase < 32:
                    left.append(Unit(a, 1, 16, 1))
                elif phase < 44:
                    right.append(Unit(a, 1, 17, 1))
                if phase == 33:
                    left.append(Unit(a, 1, 16, 1))  # conflicting headend assignment
                if phase == 5:
                    left.append(Unit(b, 4, 15, 2))  # duplicate
                polls = [Poll("HE-A", "ok", ts+1, left), Poll("HE-B", "ok", ts+2, right)]
                if phase == 45:
                    polls[1] = Poll("HE-B", "error", ts+2, error="DEMO SSH timeout")
                store_cycle(db, polls, cfg, ts, ts+3)
        finally:
            db.close()
    return directory / "config.json"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", default="demo-data")
    parser.add_argument("--days", type=int, choices=range(1, 15), default=3)
    args = parser.parse_args()
    print(generate(args.directory, args.days))
