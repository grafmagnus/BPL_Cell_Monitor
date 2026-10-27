"""Run: streamlit run dashboard.py -- --config config.json"""
import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

import pandas as pd
import plotly.express as px
import streamlit as st

from analytics import add_changes, device_stats, load_coordinates
from monitor import load_config


st.set_page_config(page_title="BPL Monitor", page_icon="📡", layout="wide")
parser = argparse.ArgumentParser()
parser.add_argument("--config", default="config.json")
args, _ = parser.parse_known_args()
st.title("BPL Headend Monitor")
try:
    cfg = load_config(args.config)
except (OSError, ValueError, KeyError, TypeError) as exc:
    st.error(f"Configuration: {exc}")
    st.stop()

days = st.sidebar.number_input("History window (days)", 1, 3650, min(2, cfg["retention_days"]))
st.sidebar.caption("UTC timestamps. Window ends now; use the snapshot selector to inspect earlier rounds.")
st.sidebar.button("Refresh now")
st.sidebar.caption("Auto-refresh every 60 seconds while this page is open. Collector runs independently.")


def draw_map(inventory, snapshot=None):
    located = inventory.dropna(subset=["latitude", "longitude"]).copy()
    if located.empty:
        st.info("No devices with coordinates in the inventory.")
        return
    if snapshot is None:
        geo = located.assign(status="never_observed", assignment="never_observed", coverage_complete=0)
    else:
        fields = ["mac", "status", "assignment", "coverage_complete"]
        geo = located.merge(snapshot[fields], on="mac", how="left")
        geo["status"] = geo.status.fillna("never_observed")
        geo["assignment"] = geo.assignment.fillna("never_observed")
        geo["coverage_complete"] = geo.coverage_complete.fillna(0)
    geo["device"] = geo.label.where(geo.label.ne(""), geo.mac)
    color = st.radio("Colour by", ["assignment", "status"], horizontal=True)
    fig = px.scatter_map(geo, lat="latitude", lon="longitude", color=color,
                         hover_name="device", hover_data=["mac", "status", "assignment", "coverage_complete"],
                         center={"lat": float(located.latitude.median()), "lon": float(located.longitude.median())},
                         zoom=10, height=540, map_style="open-street-map")
    st.plotly_chart(fig, width="stretch")
    st.caption("OpenStreetMap background requires internet. Positions come from the inventory; overlapping positions may contain multiple devices.")


@st.fragment(run_every=60)
def render():
    inventory = pd.DataFrame(columns=["mac", "latitude", "longitude", "label"])
    try:
        inventory = pd.DataFrame(load_coordinates(cfg["coordinates"]), columns=inventory.columns)
    except (OSError, ValueError, KeyError) as exc:
        st.warning(f"Inventory: {exc}")
    with st.expander(f"Device inventory ({len(inventory):,} MACs)"):
        st.caption(f"{inventory.latitude.notna().sum():,} with coordinates; {inventory.latitude.isna().sum():,} without coordinates. Names are joined by MAC and reflect the current inventory, including for historical samples.")
        st.dataframe(inventory, hide_index=True)
    path = Path(cfg["database"])
    if not path.exists():
        st.info("No database yet. Start the collector or generate the demo database.")
        draw_map(inventory)
        return
    since = (datetime.now(timezone.utc) - timedelta(days=int(days))).timestamp()
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")  # All dashboard queries see one consistent snapshot.
            cycles = pd.read_sql_query("SELECT * FROM cycles WHERE started_at>=? ORDER BY id", db, params=(since,))
            frame = pd.read_sql_query("""SELECT s.*,c.started_at,c.interval_seconds FROM states s
                JOIN cycles c ON c.id=s.cycle_id WHERE c.started_at>=?""", db, params=(since,))
            polls = pd.read_sql_query("""SELECT p.*,c.started_at FROM polls p JOIN cycles c ON c.id=p.cycle_id
                WHERE c.started_at>=? ORDER BY p.cycle_id,p.headend""", db, params=(since,))
    except (sqlite3.Error, pd.errors.DatabaseError) as exc:
        st.error(f"Database read failed: {exc}")
        return
    if cycles.empty:
        st.info("No rounds in this time window.")
        draw_map(inventory)
        return
    latest = cycles.iloc[-1]
    lag = datetime.now(timezone.utc).timestamp() - latest.finished_at
    if lag > 2 * latest.interval_seconds:
        st.warning(f"Collector data is stale: last completed round {lag/60:.1f} minutes ago.")
    cycles["label"] = pd.to_datetime(cycles.started_at, unit="s", utc=True).dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    labels = dict(zip(cycles.id, cycles.label))
    follow = st.checkbox("Follow latest round", value=True)
    cycle = int(latest.id) if follow else st.select_slider(
        "Map snapshot / round", options=cycles.id.tolist(), value=int(latest.id), format_func=lambda x: labels[x])
    st.caption(f"Snapshot: {labels[cycle]} · completed rounds in window: {len(cycles):,}")
    health = polls[polls.cycle_id.eq(cycle)]
    with st.expander("Headend health and duplicate counts", expanded=not health.status.eq("ok").all()):
        st.dataframe(health, hide_index=True)
        st.caption("A healthy, empty table is a successful poll. A parse error is a failed poll.")
    if frame.empty:
        st.info("Rounds recorded; no device has been observed yet.")
        draw_map(inventory)
        return
    frame = add_changes(frame)
    frame = frame.merge(inventory, on="mac", how="left", validate="many_to_one")
    frame["label"] = frame.label.fillna("")
    device_labels = {mac: f"{label} [{mac}]" if label else mac
                     for mac, label in frame[["mac", "label"]].drop_duplicates().itertuples(index=False, name=None)}
    frame["time"] = pd.to_datetime(frame.started_at, unit="s", utc=True)
    snapshot = frame[frame.cycle_id.eq(cycle)].copy()
    counts = snapshot.status.value_counts()
    cols = st.columns(5)
    for col, status in zip(cols, ["observed", "conflict", "stale", "missing", "unknown"]):
        col.metric(status.title(), int(counts.get(status, 0)))
    st.caption("Observed = one fresh candidate; partial coverage remains flagged. Missing is absence from tables, not proof that a device is offline.")

    map_tab, timeline_tab, heat_tab, stats_tab = st.tabs(["Map", "Device timeline", "Heatmaps", "Statistics & export"])
    with map_tab:
        draw_map(inventory, snapshot)
        located_macs = inventory.loc[inventory.latitude.notna(), "mac"]
        st.caption(f"{snapshot.loc[~snapshot.mac.isin(located_macs), 'mac'].nunique()} monitored MACs have no inventory coordinates.")
        st.dataframe(snapshot[["mac", "label", "status", "headend", "cell", "unit_id", "age", "coverage_complete", "reason"]], hide_index=True)

    with timeline_tab:
        mac = st.selectbox("Device name / MAC", sorted(frame.mac.unique()), format_func=lambda m: device_labels[m])
        selected = frame[frame.mac.eq(mac)].copy()
        fig = px.scatter(selected, x="time", y="assignment", color="status",
                         symbol="coverage_complete", hover_data=["unit_id", "age", "reason"], height=420)
        st.plotly_chart(fig, width="stretch")
        st.caption("Each point is one polling round. Gaps are not interpolated. Changes are counted only between adjacent complete, unambiguous samples within 1.5 polling intervals.")
        events = selected[selected.assignment_change | selected.id_change]
        st.write("Observed assignment / ID changes")
        st.dataframe(events[["time", "headend", "cell", "unit_id", "cell_change", "headend_change", "id_change"]], hide_index=True)
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            raw = pd.read_sql_query("""SELECT o.*,p.observed_at FROM observations o JOIN polls p
                ON p.cycle_id=o.cycle_id AND p.headend=o.headend
                WHERE o.mac=? AND o.cycle_id IN (SELECT id FROM cycles WHERE started_at>=?)
                ORDER BY o.cycle_id,o.headend,o.cell,o.unit_id""", db, params=(mac, since))
        st.write("All candidate observations (including conflicts and duplicate counters)")
        st.dataframe(raw, hide_index=True)

    with heat_tab:
        st.write("Device samples per headend / cell and UTC hour")
        observed = frame[frame.status.eq("observed") & frame.coverage_complete.eq(1)].copy()
        observed["hour"] = observed.time.dt.floor("h")
        if not observed.empty:
            pivot = observed.groupby(["assignment", "hour"]).size().unstack(fill_value=0)
            st.plotly_chart(px.imshow(pivot, aspect="auto", labels={"color": "device samples"}), width="stretch")
        else:
            st.info("No complete, unambiguous samples in this window.")
        st.write("Per-device state over time")
        selected_macs = st.multiselect("Devices in state heatmap (maximum 100)", sorted(frame.mac.unique()), default=sorted(frame.mac.unique())[:10], max_selections=100, format_func=lambda m: device_labels[m])
        codes = {"observed": 0, "missing": 1, "unknown": 2, "conflict": 3, "stale": 4}
        h = frame[frame.mac.isin(selected_macs)].copy()
        if not h.empty:
            h["code"] = h.status.map(codes)
            pivot = h.pivot(index="mac", columns="time", values="code")
            pivot.index = pivot.index.map(device_labels)
            fig = px.imshow(pivot, aspect="auto", zmin=0, zmax=4,
                            color_continuous_scale=["#23a07a", "#64748b", "#eab308", "#ef4444", "#a855f7"])
            fig.update_coloraxes(colorbar={"tickvals": list(codes.values()), "ticktext": list(codes)})
            st.plotly_chart(fig, width="stretch")
        st.caption("Sample counts, not dwell time or uptime. No sample means no measurement; partial coverage is excluded from the occupancy heatmap.")

    with stats_tab:
        stats = device_stats(frame).merge(inventory, on="mac", how="left", validate="one_to_one")
        stats["label"] = stats.label.fillna("")
        stats = stats[["mac", "label", "latitude", "longitude"] + [c for c in stats.columns if c not in {"mac", "label", "latitude", "longitude"}]]
        for col in ["first_sample", "last_sample", "first_observed", "last_observed"]:
            stats[col] = pd.to_datetime(stats[col], unit="s", utc=True)
        st.dataframe(stats, hide_index=True)
        st.caption("Statistics cover this window only. First/last sample includes missing states; counts do not imply continuous availability. ID-only changes are separate from cell/headend changes.")
        st.download_button("Download device statistics CSV", stats.to_csv(index=False), "bpl-statistics.csv", "text/csv")
        st.download_button("Download sampled states CSV", frame.to_csv(index=False), "bpl-states.csv", "text/csv")
        st.write("Headend failures in the selected window")
        st.dataframe(polls[polls.status.ne("ok")], hide_index=True)


render()
