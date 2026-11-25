#!/usr/bin/env python3
"""Extract one TransLink bus route from the GTFS static feed.

Reads ``data/google_transit`` and writes trip / stop-time CSVs under
``route_100/data/``. Does not modify the original feed.

Requires a complete feed (including ``stop_times.txt``).
"""

from __future__ import annotations

import csv
import sys
from collections import defaultdict
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths and route selection
# ---------------------------------------------------------------------------
PROJECT = Path(__file__).resolve().parent.parent
GTFS = PROJECT / "data" / "google_transit"
OUT = Path(__file__).resolve().parent / "data"

# Public route short name in routes.txt (TransLink Route 100 = Marine Drive).
ROUTE_SHORT_NAME = "100"

REQUIRED_GTFS_FILES = (
    "routes.txt",
    "trips.txt",
    "stops.txt",
    "stop_times.txt",
    "direction_names_exceptions.txt",
)


def read_gtfs(name: str):
    """Yield rows from a GTFS table (UTF-8 with optional BOM)."""
    path = GTFS / name
    with path.open(newline="", encoding="utf-8-sig") as handle:
        yield from csv.DictReader(handle)


def require_gtfs() -> None:
    missing = [name for name in REQUIRED_GTFS_FILES if not (GTFS / name).exists()]
    if missing:
        raise SystemExit(
            f"Incomplete GTFS feed in {GTFS}. Missing: {', '.join(missing)}.\n"
            "Download TransLink's google_transit package and place it under data/google_transit/."
        )


def main() -> None:
    require_gtfs()

    route = next(
        (row for row in read_gtfs("routes.txt") if row["route_short_name"] == ROUTE_SHORT_NAME),
        None,
    )
    if route is None:
        raise SystemExit(f"No route with route_short_name={ROUTE_SHORT_NAME!r} in routes.txt")

    route_id = route["route_id"]

    # TransLink extension: human-readable direction labels by direction_id.
    direction_name = {
        row["direction_id"]: row["direction_name"]
        for row in read_gtfs("direction_names_exceptions.txt")
        if row["route_name"] == ROUTE_SHORT_NAME
    }

    trips = {
        row["trip_id"]: row
        for row in read_gtfs("trips.txt")
        if row["route_id"] == route_id
    }
    trip_ids = set(trips)

    stops = {row["stop_id"]: row for row in read_gtfs("stops.txt")}

    stop_times_by_trip: dict[str, list] = defaultdict(list)
    for row in read_gtfs("stop_times.txt"):
        if row["trip_id"] in trip_ids:
            stop_times_by_trip[row["trip_id"]].append(row)

    OUT.mkdir(parents=True, exist_ok=True)
    trip_path = OUT / f"route_{ROUTE_SHORT_NAME}_trips.csv"
    stop_path = OUT / f"route_{ROUTE_SHORT_NAME}_stop_times.csv"

    trip_fields = [
        "trip_id",
        "service_id",
        "direction_id",
        "direction_name",
        "trip_headsign",
        "block_id",
        "shape_id",
        "first_stop",
        "last_stop",
        "departure_time",
        "arrival_time",
        "n_stops",
    ]
    stop_fields = [
        "trip_id",
        "direction_name",
        "stop_sequence",
        "stop_id",
        "stop_name",
        "arrival_time",
        "departure_time",
        "stop_lat",
        "stop_lon",
    ]

    with trip_path.open("w", newline="", encoding="utf-8") as trip_file, stop_path.open(
        "w", newline="", encoding="utf-8"
    ) as stop_file:
        trips_writer = csv.DictWriter(trip_file, fieldnames=trip_fields)
        stops_writer = csv.DictWriter(stop_file, fieldnames=stop_fields)
        trips_writer.writeheader()
        stops_writer.writeheader()

        for trip_id, times in sorted(stop_times_by_trip.items()):
            times.sort(key=lambda row: int(row["stop_sequence"]))
            trip = trips[trip_id]
            dest = direction_name.get(trip["direction_id"], trip["trip_headsign"])
            first, last = times[0], times[-1]
            first_stop = stops[first["stop_id"]]
            last_stop = stops[last["stop_id"]]

            trips_writer.writerow(
                {
                    "trip_id": trip_id,
                    "service_id": trip["service_id"],
                    "direction_id": trip["direction_id"],
                    "direction_name": dest,
                    "trip_headsign": trip["trip_headsign"],
                    "block_id": trip["block_id"],
                    "shape_id": trip["shape_id"],
                    "first_stop": first_stop["stop_name"],
                    "last_stop": last_stop["stop_name"],
                    "departure_time": first["departure_time"].strip(),
                    "arrival_time": last["arrival_time"].strip(),
                    "n_stops": len(times),
                }
            )
            for row in times:
                stop = stops[row["stop_id"]]
                stops_writer.writerow(
                    {
                        "trip_id": trip_id,
                        "direction_name": dest,
                        "stop_sequence": row["stop_sequence"],
                        "stop_id": row["stop_id"],
                        "stop_name": stop["stop_name"],
                        "arrival_time": row["arrival_time"].strip(),
                        "departure_time": row["departure_time"].strip(),
                        "stop_lat": stop["stop_lat"],
                        "stop_lon": stop["stop_lon"],
                    }
                )

    by_direction: dict[str, int] = defaultdict(int)
    for trip in trips.values():
        label = direction_name.get(trip["direction_id"], trip["direction_id"])
        by_direction[label] += 1

    print(
        f"Route {route['route_short_name']} {route['route_long_name']} "
        f"(route_id {route_id})"
    )
    print(f"Trips: {len(trips)}")
    for name, count in sorted(by_direction.items()):
        print(f"  {name}: {count}")
    print(f"Wrote {trip_path}")
    print(f"Wrote {stop_path}")


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        sys.exit(0)
