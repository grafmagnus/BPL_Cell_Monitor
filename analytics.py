"""Pure data transformations shared by dashboard and tests."""
import csv
import json
import math
from pathlib import Path
from monitor import normalize_mac


def load_coordinates(path):
    """Read canonical CSV or semicolon export with deviceUniqueIdAnchor.

    Missing coordinate pairs are allowed so names remain available for all MACs.
    Present but invalid/partial coordinates and duplicate MACs fail closed.
    """
    rows, seen = [], set()
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        header = handle.readline()
        handle.seek(0)
        exported = "deviceUniqueIdAnchor" in header
        reader = csv.DictReader(handle, delimiter=";" if exported else ",")
        required = {"deviceUniqueIdAnchor", "coordinates", "deviceName"} if exported else {"mac", "latitude", "longitude"}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError("Expected mac,latitude,longitude,label or deviceUniqueIdAnchor;coordinates;deviceName export")
        for line, row in enumerate(reader, 2):
            try:
                mac = normalize_mac(row["deviceUniqueIdAnchor"] if exported else row["mac"])
                label = (row["deviceName"] if exported else row.get("label", "") or "").strip()
                if exported:
                    raw = row["coordinates"].strip()
                    pair = json.loads(raw) if raw else [None, None]
                    if not isinstance(pair, list) or len(pair) != 2:
                        raise ValueError("coordinates must be [latitude, longitude]")
                    lat, lon = pair
                else:
                    lat, lon = row["latitude"].strip(), row["longitude"].strip()
                if lat in (None, "") and lon in (None, ""):
                    lat = lon = None
                else:
                    if isinstance(lat, bool) or isinstance(lon, bool):
                        raise ValueError("Boolean coordinates")
                    lat, lon = float(lat), float(lon)
                    if not math.isfinite(lat) or not math.isfinite(lon) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
                        raise ValueError("Coordinates out of range")
            except (ValueError, TypeError, AttributeError) as exc:
                raise ValueError(f"Inventory line {line}: {exc}") from exc
            if mac in seen:
                raise ValueError(f"Duplicate coordinate MAC: {mac}")
            seen.add(mac)
            rows.append({"mac": mac, "latitude": lat, "longitude": lon,
                         "label": label})
    return rows


def add_changes(frame):
    """Only count changes between consecutive, complete, unambiguous samples.

    Gaps > 1.5 polling intervals, unknown/conflict/missing/stale states and partial
    coverage break continuity. ID-only reassignment is counted separately.
    """
    frame = frame.sort_values(["mac", "started_at", "cycle_id"]).copy()
    previous = frame.groupby("mac").shift()
    valid = (frame.status.eq("observed") & frame.coverage_complete.eq(1)
             & previous.status.eq("observed") & previous.coverage_complete.eq(1)
             & ((frame.started_at - previous.started_at) > 0)
             & ((frame.started_at - previous.started_at) <= 1.5 * previous.interval_seconds))
    frame["cell_change"] = valid & frame.cell.ne(previous.cell)
    frame["headend_change"] = valid & frame.headend.ne(previous.headend)
    frame["assignment_change"] = frame.cell_change | frame.headend_change
    frame["id_change"] = valid & frame.unit_id.ne(previous.unit_id)
    frame["assignment"] = frame.apply(
        lambda r: f"{r.headend} / cell {int(r.cell)}" if r.status == "observed" else r.status, axis=1)
    return frame


def device_stats(frame):
    result = frame.groupby("mac").agg(
        first_sample=("started_at", "min"), last_sample=("started_at", "max"),
        samples=("status", "size"), assignment_changes=("assignment_change", "sum"),
        cell_changes=("cell_change", "sum"), headend_changes=("headend_change", "sum"),
        id_changes=("id_change", "sum"))
    for status in ["observed", "missing", "unknown", "conflict", "stale"]:
        result[status] = frame.status.eq(status).groupby(frame.mac).sum()
    result["complete_coverage_samples"] = frame.coverage_complete.groupby(frame.mac).sum()
    seen = frame[frame.status.eq("observed")].groupby("mac")
    result["first_observed"] = seen.started_at.min()
    result["last_observed"] = seen.started_at.max()
    result["distinct_headends"] = seen.headend.nunique()
    result["distinct_assignments"] = seen.assignment.nunique()
    result["observed_sample_percent"] = (100 * result.observed / result.samples).round(2)
    return result.reset_index()
