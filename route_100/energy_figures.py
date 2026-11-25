#!/usr/bin/env python3
"""Compute Route 100 trip energy and write Part 1 figures + summary JSON.

Elevation comes from ``route_100/data/route_100_main_routes_elevation.gpx``
(GPS Visualizer lookup of the two main-path shapes). Climb, descent, distance,
trip energy, and the example-block state-of-charge path are derived from that
file. Results are written to ``route_100/data/energy_summary.json`` so the
report and dashboard quote the same numbers.

Does not modify the GTFS feed, the GPX, or the extracted CSVs.
"""

from __future__ import annotations

import csv
import json
import math
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent
ROUTE_SHORT_NAME = "100"
TRIPS = ROOT / "data" / f"route_{ROUTE_SHORT_NAME}_trips.csv"
STOPS = ROOT / "data" / f"route_{ROUTE_SHORT_NAME}_stop_times.csv"
GPX = ROOT / "data" / f"route_{ROUTE_SHORT_NAME}_main_routes_elevation.gpx"
SHAPES = ROOT / "data" / f"route_{ROUTE_SHORT_NAME}_main_routes.geojson"
OUT = ROOT / "figures"
SUMMARY = ROOT / "data" / "energy_summary.json"

# ---------------------------------------------------------------------------
# Model assumptions (stated once; used everywhere below)
# Match the assumptions table in energy_model.qmd.
# ---------------------------------------------------------------------------
BUS_EMPTY_KG = 14_700
PASSENGERS = 55
PASSENGER_KG = 70
MASS_KG = BUS_EMPTY_KG + PASSENGERS * PASSENGER_KG

G = 9.81  # m/s^2
EARTH_RADIUS_M = 6_371_000.0
JOULES_PER_KWH = 3_600_000

# Level-road consumption already includes ordinary stop-and-go regeneration.
RATE_KWH_PER_KM = 1.5
DRIVE_EFFICIENCY = 0.85
REGEN_FRACTION = 0.50

PACK_KWH = 374
USEABLE_KWH = 350
START_SOC = 0.95
FLOOR_KWH = PACK_KWH - USEABLE_KWH
BUDGET_KWH = USEABLE_KWH  # schedulable day energy

# Approximate one-way depot link Hamilton <-> 22nd Street (not in GTFS).
DEADHEAD_KM = 4

# Cold-morning planning factors (placeholders for a later weather join).
COLD_WEATHER_FACTOR = 1.10
COLD_REGEN_FACTOR = 0.50

# DEM heights jitter between neighbouring shape points; smooth before climb.
SMOOTH_WINDOW_M = 400

# GTFS service_id for Monday–Friday in this feed.
WEEKDAY_SERVICE_ID = "1"

# Map colours (aligned with the route map).
COLOR_EAST = "#c2410c"  # toward 22nd Street Station (direction_id 0)
COLOR_WEST = "#1d4ed8"  # toward Marpole Loop (direction_id 1)

KWH_PER_M = MASS_KG * G / JOULES_PER_KWH
GPX_NS = {"g": "http://www.topografix.com/GPX/1/1"}


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------
def read_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def minutes(hms: str) -> float:
    hour, minute, second = (int(part) for part in hms.split(":"))
    return hour * 60 + minute + second / 60


def clock(minute: float) -> str:
    minute = int(round(minute))
    return f"{minute // 60:02d}:{minute % 60:02d}"


def haversine_m(a, b) -> float:
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    half = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(half))


def is_main_path(trip: dict) -> bool:
    return "via trapp" not in trip["trip_headsign"].lower()


# ---------------------------------------------------------------------------
# Elevation / energy physics
# ---------------------------------------------------------------------------
def load_gpx(path: Path) -> list:
    root = ET.parse(path).getroot()
    tracks = []
    for track in root.findall("g:trk", GPX_NS):
        points = []
        for node in track.findall(".//g:trkpt", GPX_NS):
            points.append(
                (
                    float(node.get("lat")),
                    float(node.get("lon")),
                    float(node.findtext("g:ele", namespaces=GPX_NS)),
                )
            )
        tracks.append(points)
    return tracks


def shape_starts() -> dict:
    """First lat/lon of each main-path shape (for pairing GPX tracks)."""
    collection = json.loads(SHAPES.read_text(encoding="utf-8"))
    starts = {}
    for feature in collection["features"]:
        lon, lat = feature["geometry"]["coordinates"][0]
        starts[feature["properties"]["shape_id"]] = (lat, lon)
    return starts


def main_path_shape_ids() -> tuple[str, str]:
    """Return (east/dir0, west/dir1) shape_ids from the main-routes GeoJSON."""
    collection = json.loads(SHAPES.read_text(encoding="utf-8"))
    by_direction = {}
    for feature in collection["features"]:
        props = feature["properties"]
        by_direction[props["direction_id"]] = props["shape_id"]
    if "0" not in by_direction or "1" not in by_direction:
        raise SystemExit(f"Expected direction_id 0 and 1 in {SHAPES}")
    return by_direction["0"], by_direction["1"]


def assign_tracks(tracks: list) -> tuple:
    """Pair each GPX track with a main-path shape by its first coordinate."""
    east_shape, west_shape = main_path_shape_ids()
    starts = shape_starts()
    assigned = {}
    for points in tracks:
        lat, lon, _ele = points[0]
        shape_id = min(starts, key=lambda key: haversine_m((lat, lon), starts[key]))
        assigned[shape_id] = points

    missing = {east_shape, west_shape} - assigned.keys()
    if missing:
        raise SystemExit(f"GPX is missing a track for shape(s): {sorted(missing)}")
    return assigned[east_shape], assigned[west_shape]


def track_distance(points: list) -> list[float]:
    distance = [0.0]
    for index in range(1, len(points)):
        step = haversine_m(points[index - 1][:2], points[index][:2])
        distance.append(distance[-1] + step)
    return distance


def smooth_elevation(distance_m: list, elevation: list, window_m: float) -> list:
    """Mean elevation of points within half a window along the track."""
    smoothed = []
    left = 0
    right = 0
    half = window_m / 2
    count = len(elevation)
    for centre in distance_m:
        while distance_m[left] < centre - half:
            left += 1
        while right < count and distance_m[right] <= centre + half:
            right += 1
        window = elevation[left:right]
        smoothed.append(sum(window) / len(window))
    return smoothed


def integrate(distance_m: list, elevation: list, cold: float = 1.0, cold_regen: float = 1.0) -> dict:
    """Level-road energy on each step, plus climb cost or descent credit."""
    climb = descent = flat = grade = 0.0
    kilometres = [0.0]
    energy = [0.0]
    heights = [elevation[0]]
    for index in range(len(elevation) - 1):
        step_m = distance_m[index + 1] - distance_m[index]
        if step_m <= 0:
            continue
        rise = elevation[index + 1] - elevation[index]
        if rise >= 0:
            climb += rise
            grade_step = KWH_PER_M * rise / DRIVE_EFFICIENCY
        else:
            descent += -rise
            grade_step = KWH_PER_M * rise * REGEN_FRACTION * cold_regen
        flat_step = RATE_KWH_PER_KM * cold * step_m / 1000
        flat += flat_step
        grade += grade_step
        kilometres.append(distance_m[index + 1] / 1000)
        energy.append(energy[-1] + flat_step + grade_step)
        heights.append(elevation[index + 1])
    return {
        "km": kilometres,
        "kwh": energy,
        "elevation_m": heights,
        "distance_km": kilometres[-1],
        "climb_m": climb,
        "descent_m": descent,
        "net_m": elevation[-1] - elevation[0],
        "flat_kwh": flat,
        "grade_kwh": grade,
        "kwh_total": energy[-1],
    }


def build_profile(points: list, cold: float = 1.0, cold_regen: float = 1.0) -> dict:
    distance_m = track_distance(points)
    raw = [point[2] for point in points]
    elevation = smooth_elevation(distance_m, raw, SMOOTH_WINDOW_M)
    profile = integrate(distance_m, elevation, cold=cold, cold_regen=cold_regen)
    profile["points"] = [(point[0], point[1]) for point in points]
    profile["distance_m"] = distance_m
    return profile


def leg_kwh(direction_id: str, east: dict, west: dict) -> float:
    return east["kwh_total"] if direction_id == "0" else west["kwh_total"]


def energy_at(profile: dict, distance_km: float) -> float:
    kilometres = profile["km"]
    energy = profile["kwh"]
    if distance_km <= kilometres[0]:
        return energy[0]
    if distance_km >= kilometres[-1]:
        return energy[-1]
    for index in range(1, len(kilometres)):
        if kilometres[index] >= distance_km:
            span = kilometres[index] - kilometres[index - 1]
            if span <= 0:
                return energy[index]
            share = (distance_km - kilometres[index - 1]) / span
            return energy[index - 1] + share * (energy[index] - energy[index - 1])
    return energy[-1]


def stop_marks(stops: list, trip_id: str, profile: dict) -> list:
    rows = [row for row in stops if row["trip_id"] == trip_id]
    rows.sort(key=lambda row: int(row["stop_sequence"]))
    marks = []
    for row in rows:
        lat, lon = float(row["stop_lat"]), float(row["stop_lon"])
        nearest = min(
            range(len(profile["points"])),
            key=lambda index: haversine_m((lat, lon), profile["points"][index]),
        )
        distance_km = profile["distance_m"][nearest] / 1000
        marks.append((distance_km, energy_at(profile, distance_km)))
    return marks


# ---------------------------------------------------------------------------
# Example block: median weekday duty by in-service distance
# ---------------------------------------------------------------------------
def block_service_km(rows: list, east: dict, west: dict) -> float:
    total = 0.0
    for trip in rows:
        total += east["distance_km"] if trip["direction_id"] == "0" else west["distance_km"]
    return total


def select_example_block(trips: list, east: dict, west: dict) -> tuple[str, list]:
    """Median weekday block by scheduled service distance (report worked example)."""
    grouped: dict[str, list] = defaultdict(list)
    for trip in trips:
        if trip["service_id"] == WEEKDAY_SERVICE_ID:
            grouped[trip["block_id"]].append(trip)

    ranked = []
    for block_id, rows in grouped.items():
        rows = sorted(rows, key=lambda row: minutes(row["departure_time"]))
        ranked.append((block_service_km(rows, east, west), block_id, rows))
    ranked.sort(key=lambda item: item[0])

    if not ranked:
        raise SystemExit("No weekday blocks found in the trip extract.")
    _km, block_id, rows = ranked[len(ranked) // 2]
    return block_id, rows


def example_trips_for_marks(rows: list) -> tuple[str, str]:
    """First main-path trip each way on the example block (for stop dots)."""
    east_trip = next(
        trip["trip_id"]
        for trip in rows
        if trip["direction_id"] == "0" and is_main_path(trip)
    )
    west_trip = next(
        trip["trip_id"]
        for trip in rows
        if trip["direction_id"] == "1" and is_main_path(trip)
    )
    return east_trip, west_trip


# ---------------------------------------------------------------------------
# Figures
# ---------------------------------------------------------------------------
def style(ax) -> None:
    ax.spines["top"].set_visible(False)
    ax.grid(True, color="#d9e6ef", linewidth=0.6)
    ax.set_axisbelow(True)


def plot_elevation(east: dict, west: dict) -> Path:
    """Round-trip elevation: east leg, then west leg continuing in distance."""
    fig, ax = plt.subplots(figsize=(10.2, 3.6))
    east_km = east["km"]
    west_km = [east["distance_km"] + km for km in west["km"]]
    ax.plot(east_km, east["elevation_m"], color=COLOR_EAST, linewidth=1.8, label="To 22nd Street Station")
    ax.plot(west_km, west["elevation_m"], color=COLOR_WEST, linewidth=1.8, label="To Marpole Loop")
    ax.set_xlabel("Distance along the round trip (km)")
    ax.set_ylabel("Smoothed elevation (m)")
    ax.set_title("Elevation profile — main-path round trip", loc="left", fontsize=12)
    ax.legend(frameon=False, fontsize=9)
    style(ax)
    ax.spines["right"].set_visible(False)
    fig.tight_layout()
    path = OUT / "elevation_round_trip.png"
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return path


def plot_energy(stops: list, east: dict, west: dict, east_trip: str, west_trip: str) -> Path:
    fig, axes = plt.subplots(1, 2, figsize=(10.2, 4.6), sharey=True)
    panels = (
        (axes[0], east, stop_marks(stops, east_trip, east), "To 22nd Street Station", COLOR_EAST),
        (axes[1], west, stop_marks(stops, west_trip, west), "To Marpole Loop", COLOR_WEST),
    )
    for ax, profile, marks, title, color in panels:
        ax.plot(profile["km"], profile["kwh"], color=color, linewidth=2.0, zorder=3)
        ax.scatter(
            [mark[0] for mark in marks],
            [mark[1] for mark in marks],
            s=12,
            color=color,
            zorder=4,
        )
        height = ax.twinx()
        height.plot(profile["km"], profile["elevation_m"], color="#9aa5b1", linewidth=1.0, zorder=1)
        height.set_ylabel("Smoothed elevation (m)", color="#7b8794", fontsize=8)
        height.tick_params(axis="y", labelsize=8, colors="#7b8794")
        height.spines["top"].set_visible(False)
        average = profile["kwh_total"] / profile["distance_km"]
        ax.set_title(title, loc="left", fontsize=11)
        ax.set_xlabel("Distance along the trip (km)")
        ax.text(
            0.04,
            0.92,
            f"{profile['kwh_total']:.1f} kWh\n{average:.2f} kWh/km",
            transform=ax.transAxes,
            va="top",
            fontsize=9,
            color="#1f2933",
        )
        style(ax)
    axes[0].set_ylabel("Cumulative energy from the battery (kWh)")
    fig.suptitle(
        "Energy along the main path, mild day, full bus",
        x=0.02,
        ha="left",
        fontsize=12,
    )
    fig.text(
        0.02,
        0.01,
        f"Grey line: GPS Visualizer elevation, averaged over {SMOOTH_WINDOW_M:.0f} m. "
        "Coloured line: battery energy. Dots are scheduled stops.",
        fontsize=8,
        color="#52606d",
    )
    fig.tight_layout(rect=(0, 0.05, 1, 0.92))
    path = OUT / "energy_along_route.png"
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return path


def soc_series(rows: list, east: dict, west: dict, include_deadhead: bool = False):
    """SoC path for the example block (in-service by default)."""
    pace = (
        (minutes(rows[0]["arrival_time"]) - minutes(rows[0]["departure_time"]))
        / west["distance_km"]
    )
    deadhead_min = DEADHEAD_KM * pace
    deadhead_kwh = RATE_KWH_PER_KM * DEADHEAD_KM

    stored = START_SOC * PACK_KWH
    points = []
    cursor = minutes(rows[0]["departure_time"])
    if include_deadhead:
        cursor = minutes(rows[0]["departure_time"]) - deadhead_min
        points.append((cursor, stored))
        stored -= deadhead_kwh
        cursor = minutes(rows[0]["departure_time"])
        points.append((cursor, stored))
    else:
        points.append((cursor, stored))

    for trip in rows:
        depart = minutes(trip["departure_time"])
        arrive = minutes(trip["arrival_time"])
        if depart > cursor + 0.2:
            points.append((depart, stored))
        stored -= leg_kwh(trip["direction_id"], east, west)
        points.append((arrive, stored))
        cursor = arrive

    if include_deadhead:
        pull_in_end = cursor + deadhead_min
        stored -= deadhead_kwh
        points.append((pull_in_end, stored))
    return points, deadhead_kwh, deadhead_min


def plot_soc(points: list, block_id: str):
    xs = [point[0] for point in points]
    ys = [100 * point[1] / PACK_KWH for point in points]
    floor_pct = 100 * FLOOR_KWH / PACK_KWH

    fig, ax = plt.subplots(figsize=(10.2, 4.6))
    ax.plot(xs, ys, color=COLOR_WEST, linewidth=2.0)
    ax.axhline(floor_pct, color="#9aa5b1", linestyle="--", linewidth=1.0)
    ax.text(xs[0], floor_pct + 1.2, f"Floor {floor_pct:.1f}%", color="#52606d", fontsize=8)
    ax.scatter([xs[0], xs[-1]], [ys[0], ys[-1]], color=COLOR_WEST, zorder=3)
    ax.annotate(
        f"Start {ys[0]:.1f}%",
        (xs[0], ys[0]),
        textcoords="offset points",
        xytext=(8, -14),
        fontsize=9,
    )
    ax.annotate(
        f"Finish {ys[-1]:.1f}%",
        (xs[-1], ys[-1]),
        textcoords="offset points",
        xytext=(-78, 8),
        fontsize=9,
    )
    ax.set_xlim(xs[0] - 5, xs[-1] + 8)
    ax.set_ylim(0, 100)
    ticks = list(range(int(xs[0] // 30) * 30, int(xs[-1]) + 30, 30))
    ax.set_xticks(ticks)
    ax.set_xticklabels([clock(tick) for tick in ticks])
    ax.set_xlabel("Time of day")
    ax.set_ylabel(f"State of charge (% of {PACK_KWH:g} kWh)")
    ax.set_title(
        f"Weekday block {block_id}, depot charging, mild day (in-service trips)",
        loc="left",
        fontsize=12,
    )
    style(ax)
    ax.spines["right"].set_visible(False)
    fig.tight_layout()
    path = OUT / "soc_weekday_block.png"
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return path, ys[0], ys[-1], clock(xs[0]), clock(xs[-1])


def blocks_over_budget(trips: list, east: dict, west: dict) -> dict:
    grouped = defaultdict(list)
    for trip in trips:
        grouped[(trip["service_id"], trip["block_id"])].append(trip)

    service_labels = (
        (WEEKDAY_SERVICE_ID, "weekday"),
        ("2", "saturday"),
        ("3", "sunday"),
    )
    counts = {}
    for service, label in service_labels:
        over = total = 0
        for (svc, _), block in grouped.items():
            if svc != service:
                continue
            total += 1
            energy = RATE_KWH_PER_KM * 2 * DEADHEAD_KM
            for trip in block:
                energy += leg_kwh(trip["direction_id"], east, west)
            over += energy > BUDGET_KWH
        counts[label] = {"over": over, "total": total}
    return counts


def direction_record(profile: dict, cold_profile: dict) -> dict:
    distance = profile["distance_km"]
    return {
        "distance_km": round(distance, 3),
        "climb_m": round(profile["climb_m"], 1),
        "descent_m": round(profile["descent_m"], 1),
        "net_m": round(profile["net_m"], 1),
        "flat_kwh": round(profile["flat_kwh"], 3),
        "grade_kwh": round(profile["grade_kwh"], 3),
        "kwh": round(profile["kwh_total"], 3),
        "kwh_per_km": round(profile["kwh_total"] / distance, 3),
        "cold_kwh": round(cold_profile["kwh_total"], 3),
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    stops = read_csv(STOPS)
    trips = read_csv(TRIPS)
    east_points, west_points = assign_tracks(load_gpx(GPX))
    east = build_profile(east_points)
    west = build_profile(west_points)
    east_cold = build_profile(east_points, cold=COLD_WEATHER_FACTOR, cold_regen=COLD_REGEN_FACTOR)
    west_cold = build_profile(west_points, cold=COLD_WEATHER_FACTOR, cold_regen=COLD_REGEN_FACTOR)

    block_id, rows = select_example_block(trips, east, west)
    east_trip, west_trip = example_trips_for_marks(rows)

    series, deadhead_kwh, _deadhead_min = soc_series(rows, east, west)
    elev_path = plot_elevation(east, west)
    energy_path = plot_energy(stops, east, west, east_trip, west_trip)
    soc_path, start_soc, finish_soc, start_clock, finish_clock = plot_soc(series, block_id)

    used = START_SOC * PACK_KWH - series[-1][1]
    service = sum(leg_kwh(trip["direction_id"], east, west) for trip in rows)
    counts = blocks_over_budget(trips, east, west)
    loop = east["kwh_total"] + west["kwh_total"]
    flat_loop = east["flat_kwh"] + west["flat_kwh"]
    cold_loop = east_cold["kwh_total"] + west_cold["kwh_total"]

    summary = {
        "source": str(GPX.relative_to(ROOT.parent)),
        "tool": "https://www.gpsvisualizer.com/elevation",
        "smooth_m": SMOOTH_WINDOW_M,
        "kwh_per_m": round(KWH_PER_M, 6),
        "joules_per_m": round(MASS_KG * G, 1),
        "mass_kg": MASS_KG,
        "rate_kwh_per_km": RATE_KWH_PER_KM,
        "drive": DRIVE_EFFICIENCY,
        "regen": REGEN_FRACTION,
        "cold": COLD_WEATHER_FACTOR,
        "cold_regen": COLD_REGEN_FACTOR,
        "deadhead_km": DEADHEAD_KM,
        "deadhead_kwh": round(deadhead_kwh, 3),
        "pack_kwh": PACK_KWH,
        "useable_kwh": USEABLE_KWH,
        "start_soc": START_SOC,
        "start_kwh": round(START_SOC * PACK_KWH, 1),
        "floor_kwh": FLOOR_KWH,
        "budget_kwh": BUDGET_KWH,
        "east": direction_record(east, east_cold),
        "west": direction_record(west, west_cold),
        "loop_kwh": round(loop, 3),
        "flat_loop_kwh": round(flat_loop, 3),
        "cold_loop_kwh": round(cold_loop, 3),
        "loop_kwh_per_km": round(loop / (east["distance_km"] + west["distance_km"]), 3),
        "block_id": block_id,
        "block_trips": len(rows),
        "block_service_kwh": round(service, 3),
        "block_used_kwh": round(used, 3),
        "block_left_kwh": round(series[-1][1], 3),
        "block_start_soc": round(start_soc, 1),
        "block_finish_soc": round(finish_soc, 1),
        "block_start_clock": start_clock,
        "block_finish_clock": finish_clock,
        "block_service_km": round(block_service_km(rows, east, west), 1),
        "over_budget": counts,
    }
    SUMMARY.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    print(f"Example block (median weekday by distance): {block_id}")
    print(f"kWh per metre {KWH_PER_M:.6f}")
    print(
        f"east {east['distance_km']:.3f} km  climb {east['climb_m']:.1f} m  "
        f"descent {east['descent_m']:.1f} m  {east['kwh_total']:.3f} kWh  "
        f"{east['kwh_total'] / east['distance_km']:.3f} kWh/km"
    )
    print(
        f"west {west['distance_km']:.3f} km  climb {west['climb_m']:.1f} m  "
        f"descent {west['descent_m']:.1f} m  {west['kwh_total']:.3f} kWh  "
        f"{west['kwh_total'] / west['distance_km']:.3f} kWh/km"
    )
    print(f"loop {loop:.3f} kWh  flat {flat_loop:.3f}  cold {cold_loop:.3f}")
    print(f"soc {start_soc:.2f}% at {start_clock} -> {finish_soc:.2f}% at {finish_clock}")
    print(f"energy used on the example block {used:.2f} kWh")
    print("over budget", counts)
    print(elev_path)
    print(energy_path)
    print(soc_path)
    print(SUMMARY)


if __name__ == "__main__":
    main()
