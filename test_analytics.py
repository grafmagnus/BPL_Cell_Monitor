from pathlib import Path
import tempfile
import unittest

import pandas as pd
from analytics import add_changes, device_stats, load_coordinates
from import_inventory import convert


class AnalyticsTests(unittest.TestCase):
    def frame(self, specs):
        return pd.DataFrame([dict(cycle_id=i+1, mac="a", started_at=ts,
            interval_seconds=300, status=status, headend=he, cell=cell,
            unit_id=unit_id, coverage_complete=complete)
            for i, (ts, status, he, cell, unit_id, complete) in enumerate(specs)])

    def test_changes_and_gap_boundaries(self):
        frame = self.frame([
            (0, "observed", "a", 15, 1, 1),
            (300, "observed", "a", 16, 1, 1),
            (600, "observed", "b", 16, 2, 1),
            (900, "conflict", None, None, None, 1),
            (1200, "observed", "a", 15, 1, 1),
            (2400, "observed", "a", 17, 1, 1),
            (2700, "observed", "b", 17, 1, 0),
            (3000, "observed", "a", 18, 1, 1)])
        changed = add_changes(frame)
        self.assertEqual(changed.cell_change.tolist(), [False, True, False, False, False, False, False, False])
        stats = device_stats(changed).iloc[0]
        self.assertEqual((stats.assignment_changes, stats.cell_changes, stats.headend_changes, stats.id_changes), (2, 1, 1, 1))
        self.assertEqual((stats.observed, stats.conflict, stats.samples), (7, 1, 8))

    def test_different_devices_no_cross_comparison(self):
        frame = self.frame([(0, "observed", "a", 15, 1, 1), (300, "observed", "a", 16, 1, 1)])
        frame.loc[1, "mac"] = "b"
        self.assertFalse(add_changes(frame).assignment_change.any())

    def test_no_observed_samples_stats(self):
        frame = self.frame([(0, "conflict", None, None, None, 1)])
        stats = device_stats(add_changes(frame)).iloc[0]
        self.assertTrue(pd.isna(stats.last_observed))
        self.assertEqual(stats.observed, 0)

    def test_coordinate_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "coords.csv"
            path.write_text("mac,latitude,longitude\n002518047A02,47.3,8.5\n")
            self.assertEqual(load_coordinates(path)[0]["mac"], "00:25:18:04:7a:02")
            for value in ["nan", "91", "inf"]:
                path.write_text(f"mac,latitude,longitude\n002518047A02,{value},8.5\n")
                with self.assertRaises(ValueError):
                    load_coordinates(path)
            path.write_text("mac,latitude,longitude\n002518047A02,47,8\n002518047A02,48,8\n")
            with self.assertRaises(ValueError):
                load_coordinates(path)


class InventoryImportTests(unittest.TestCase):
    def test_device_export_and_import_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "export.csv"
            target = Path(directory) / "coordinates.csv"
            source.write_text('identification;deviceUniqueIdAnchor;coordinates;deviceName\n'
                '002518047a02.eui;00:25:18:04:7A:02;"[47.3, 8.5]";"ÖB, Gebäude; Ost"\n'
                '00251805b6a4.eui;00:25:18:05:B6:A4;;Ohne Position\n', encoding="utf-8-sig")
            self.assertEqual(convert(source, target), (2, 1))
            original, normalized = load_coordinates(source), load_coordinates(target)
            self.assertEqual(original, normalized)
            self.assertEqual(normalized[0]["label"], "ÖB, Gebäude; Ost")
            self.assertIsNone(normalized[1]["latitude"])
            with self.assertRaises(FileExistsError):
                convert(source, target)

    def test_partial_or_invalid_export_coordinates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "export.csv"
            for coords in ["[47, null]", "[true, 8]", "[47]", "null", "invalid"]:
                path.write_text(f'deviceUniqueIdAnchor;coordinates;deviceName\n00:25:18:04:7a:02;{coords};Name\n')
                with self.subTest(coords=coords), self.assertRaises(ValueError):
                    load_coordinates(path)


if __name__ == "__main__":
    unittest.main()
