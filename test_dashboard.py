"""Streamlit execution smoke tests; no SSH or map-tile downloads."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest
from demo import generate
from monitor import Poll, Unit, open_database, store_cycle


class DashboardTests(unittest.TestCase):
    def run_app(self, config):
        with patch("sys.argv", ["dashboard.py", "--config", str(config)]):
            app = AppTest.from_file(str(Path(__file__).with_name("dashboard.py")), default_timeout=45).run()
        self.assertEqual(len(app.exception), 0, str(app.exception))
        return app

    def test_demo_and_historical_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            config = generate(directory, days=1)
            app = self.run_app(config)
            self.assertEqual(len(app.tabs), 4)
            self.assertEqual(len(app.metric), 5)
            self.assertTrue(any("DEMO device" in name for name in app.selectbox[0].options))
            with patch("sys.argv", ["dashboard.py", "--config", str(config)]):
                app.checkbox[0].uncheck().run()
                app.select_slider[0].set_value(app.select_slider[0].value - 10).run()
            self.assertEqual(len(app.exception), 0, str(app.exception))

    def test_all_conflict_then_missing_and_no_coordinates(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            cfg = {"headends": [{"name": "a", "ssh_host": "a"}, {"name": "b", "ssh_host": "b"}],
                   "database": "test.sqlite3", "interval_seconds": 300, "retention_days": 56}
            config.write_text(json.dumps(cfg))
            db = open_database(Path(directory) / "test.sqlite3")
            ts = time.time() - 600
            try:
                for offset, units in [(0, [Unit("00:25:18:04:7a:02", 1, 15, 1)]), (300, [])]:
                    store_cycle(db, [Poll("a", "ok", ts+offset+1, units), Poll("b", "ok", ts+offset+2, units)], cfg, ts+offset, ts+offset+3)
            finally:
                db.close()
            self.run_app(config)

    def test_empty_tables(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            cfg = {"headends": [{"name": "a", "ssh_host": "a"}], "database": "test.sqlite3", "interval_seconds": 300, "retention_days": 56}
            config.write_text(json.dumps(cfg))
            db = open_database(Path(directory) / "test.sqlite3")
            try:
                store_cycle(db, [Poll("a", "ok", time.time(), [])], cfg)
            finally:
                db.close()
            self.run_app(config)


    def test_inventory_map_before_first_poll(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            config.write_text(json.dumps({"headends": [{"name": "a", "ssh_host": "a"}]}))
            (Path(directory) / "coordinates.csv").write_text(
                "mac,latitude,longitude,label\n00:25:18:04:7a:02,47.3,8.5,Gerät Eins\n00:25:18:05:b6:a4,,,Ohne Koordinaten\n", encoding="utf-8")
            app = self.run_app(config)
            self.assertEqual(len(app.radio), 1)
            self.assertEqual(len(app.dataframe[0].value), 2)


if __name__ == "__main__":
    unittest.main()
