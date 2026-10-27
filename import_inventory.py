"""Convert device export to normalized monitoring inventory, keeping missing positions."""
import argparse
import csv
from pathlib import Path
from analytics import load_coordinates


def convert(source, destination):
    rows = load_coordinates(source)
    with Path(destination).open("x", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["mac", "latitude", "longitude", "label"])
        writer.writeheader()
        writer.writerows(rows)
    return len(rows), sum(r["latitude"] is not None for r in rows)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source")
    parser.add_argument("destination", help="New CSV file; existing files are never overwritten")
    args = parser.parse_args()
    total, located = convert(args.source, args.destination)
    print(f"{total} devices, {located} with coordinates, {total-located} without coordinates")
