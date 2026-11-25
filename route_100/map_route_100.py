#!/usr/bin/env python3
"""Build GeoJSON and an interactive Leaflet map for Route 100.

Reads extracted trip/stop CSVs plus ``data/google_transit/shapes.txt``.
Writes GeoJSON under ``route_100/data/`` and HTML under ``route_100/maps/``
and ``demos/``. Does not modify the GTFS feed.
"""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT = Path(__file__).resolve().parent.parent
GTFS = PROJECT / "data" / "google_transit"
DATA = Path(__file__).resolve().parent / "data"
MAPS = Path(__file__).resolve().parent / "maps"
DEMOS = PROJECT / "demos"

ROUTE_SHORT_NAME = "100"

# Direction colours match the report map (0 = toward 22nd Street, 1 = Marpole).
DIRECTION_COLOR = {
    "0": "#c2410c",
    "1": "#1d4ed8",
}

# Terminal stop-name prefixes used for map labels.
TERMINALS = ("Marpole Loop", "22nd Street Station")


def read_csv(path: Path):
    with path.open(newline="", encoding="utf-8-sig") as handle:
        yield from csv.DictReader(handle)


def shape_catalog() -> dict:
    """One record per shape_id used by Route 100 trips."""
    catalog = {}
    trips_path = DATA / f"route_{ROUTE_SHORT_NAME}_trips.csv"
    for row in read_csv(trips_path):
        shape_id = row["shape_id"]
        if shape_id not in catalog:
            catalog[shape_id] = {
                "shape_id": shape_id,
                "direction_id": row["direction_id"],
                "direction_name": row["direction_name"],
                "trip_headsign": row["trip_headsign"],
                "trip_count": 1,
            }
        else:
            catalog[shape_id]["trip_count"] += 1
    return catalog


def load_shapes(wanted: set) -> dict:
    """Load lon/lat polylines for the requested shape_ids from shapes.txt."""
    points = defaultdict(list)
    with (GTFS / "shapes.txt").open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            shape_id = row["shape_id"]
            if shape_id in wanted:
                points[shape_id].append(
                    (
                        int(row["shape_pt_sequence"]),
                        float(row["shape_pt_lon"]),
                        float(row["shape_pt_lat"]),
                    )
                )

    lines = {}
    for shape_id, pts in points.items():
        pts.sort(key=lambda item: item[0])
        lines[shape_id] = [[lon, lat] for _, lon, lat in pts]

    missing = wanted - lines.keys()
    if missing:
        raise SystemExit(f"shapes.txt is missing shape_id(s): {sorted(missing)}")
    return lines


def unique_stops() -> list:
    stops = {}
    stops_path = DATA / f"route_{ROUTE_SHORT_NAME}_stop_times.csv"
    for row in read_csv(stops_path):
        stops[row["stop_id"]] = {
            "stop_id": row["stop_id"],
            "stop_name": row["stop_name"],
            "lon": float(row["stop_lon"]),
            "lat": float(row["stop_lat"]),
        }
    return list(stops.values())


def shapes_geojson(catalog: dict, lines: dict) -> dict:
    features = []
    for shape_id, meta in sorted(catalog.items(), key=lambda kv: -kv[1]["trip_count"]):
        via = "via" in meta["trip_headsign"].lower()
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "shape_id": shape_id,
                    "direction_id": meta["direction_id"],
                    "direction_name": meta["direction_name"],
                    "trip_headsign": meta["trip_headsign"],
                    "trip_count": meta["trip_count"],
                    "via_trapp": via,
                    "color": DIRECTION_COLOR[meta["direction_id"]],
                },
                "geometry": {"type": "LineString", "coordinates": lines[shape_id]},
            }
        )
    return {"type": "FeatureCollection", "features": features}


def stops_geojson(stops: list) -> dict:
    features = []
    for stop in sorted(stops, key=lambda item: item["stop_name"]):
        terminal = next(
            (name for name in TERMINALS if stop["stop_name"].startswith(name)),
            "",
        )
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "stop_id": stop["stop_id"],
                    "stop_name": stop["stop_name"],
                    "terminal": terminal,
                },
                "geometry": {
                    "type": "Point",
                    "coordinates": [stop["lon"], stop["lat"]],
                },
            }
        )
    return {"type": "FeatureCollection", "features": features}


def terminal_labels(stops: list) -> list:
    """Average coordinates of stops that share a terminal name prefix."""
    grouped = defaultdict(list)
    for stop in stops:
        for name in TERMINALS:
            if stop["stop_name"].startswith(name):
                grouped[name].append(stop)

    labels = []
    for name, group in grouped.items():
        labels.append(
            {
                "name": name,
                "lon": sum(item["lon"] for item in group) / len(group),
                "lat": sum(item["lat"] for item in group) / len(group),
            }
        )
    return labels


def write_html(shapes: dict, stops: dict, labels: list) -> Path:
    """Write the Leaflet map; keep a copy under demos/ for the README."""
    payload = json.dumps({"shapes": shapes, "stops": stops, "labels": labels})
    # Relative path from route_100/maps/ to figures/; demos/ uses a fallback.
    html = HTML_TEMPLATE.replace("__DATA__", payload)

    MAPS.mkdir(parents=True, exist_ok=True)
    DEMOS.mkdir(parents=True, exist_ok=True)
    map_path = MAPS / f"route_{ROUTE_SHORT_NAME}_map.html"
    demo_path = DEMOS / f"route_{ROUTE_SHORT_NAME}_map.html"
    map_path.write_text(html, encoding="utf-8")
    demo_path.write_text(html, encoding="utf-8")
    return map_path


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Route 100 — Marine Drive</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>
  html, body { margin: 0; }
  #map { height: calc(100vh - 132px); min-height: 420px; }
  .energy {
    background: #fff;
    padding: 6px 12px 8px;
    font: 12px/1.35 system-ui, sans-serif;
    color: #1f2933;
  }
  .energy h2 { margin: 0 0 2px; font-size: 13px; font-weight: 650; }
  .energy p { margin: 0 0 4px; max-width: 52em; color: #52606d; font-size: 11px; }
  .energy img { display: block; width: auto; max-width: 100%; max-height: 78px; height: auto; }
  .legend {
    background: #fff;
    padding: 6px 7px;
    line-height: 1.25;
    font: 11px/1.25 system-ui, sans-serif;
    box-shadow: 0 1px 4px rgba(0,0,0,.25);
  }
  .legend b { display: block; margin-bottom: 2px; font-size: 11px; }
  .swatch {
    display: inline-block;
    width: 18px;
    height: 3px;
    margin-right: 5px;
    vertical-align: middle;
    background: currentColor;
  }
  .swatch.dashed {
    background: repeating-linear-gradient(90deg, currentColor 0 4px, transparent 4px 7px);
  }
  .chooser button {
    display: block;
    width: 100%;
    margin-top: 3px;
    padding: 2px 5px;
    border: 1px solid #ccc;
    background: #fff;
    font: 11px system-ui, sans-serif;
    text-align: left;
    cursor: pointer;
  }
  .chooser button.active { border-color: #111; font-weight: 600; }
</style>
</head>
<body>
<div id="map"></div>
<section class="energy">
  <h2>Elevation along the round trip</h2>
  <p>Main roads only. This profile does not include trips that run via alternative roads (via Trapp Rd). Orange is toward 22nd Street Station; blue is back toward Marpole Loop.</p>
  <img src="../figures/elevation_round_trip.png" alt="Elevation along the Route 100 round trip" onerror="this.onerror=null;this.src='../route_100/figures/elevation_round_trip.png'">
</section>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const DATA = __DATA__;

const map = L.map("map");
map.createPane("route");
map.getPane("route").style.zIndex = 450;
map.createPane("stops");
map.getPane("stops").style.zIndex = 480;
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
  maxZoom: 19
}).addTo(map);

function best(directionId, via) {
  return DATA.shapes.features
    .filter((f) => f.properties.direction_id === directionId && f.properties.via_trapp === via)
    .sort((a, b) => b.properties.trip_count - a.properties.trip_count)[0];
}

const routes = ["0", "1"].flatMap((id) => [best(id, false), best(id, true)]).filter(Boolean);
const layers = {};
const bounds = [];
routes.forEach((feature) => {
  const p = feature.properties;
  const label = p.via_trapp ? `To ${p.direction_name} via Trapp Rd` : `To ${p.direction_name}`;
  const latlngs = feature.geometry.coordinates.map(([lon, lat]) => [lat, lon]);
  latlngs.forEach((ll) => bounds.push(ll));
  const group = L.layerGroup();
  L.polyline(latlngs, {
    pane: "route",
    color: p.color,
    weight: 3,
    opacity: 0.9,
    dashArray: p.via_trapp ? "8 8" : null
  }).bindPopup(`<b>${label}</b><br>${p.trip_count} trips`).addTo(group);
  layers[p.shape_id] = { group, label, color: p.color, via: p.via_trapp };
});

const stopLayer = L.layerGroup();
DATA.stops.features.forEach((feature) => {
  const [lon, lat] = feature.geometry.coordinates;
  const p = feature.properties;
  L.circleMarker([lat, lon], {
    pane: "stops",
    radius: 3,
    color: "#111",
    weight: 1,
    fillColor: "#fff",
    fillOpacity: 1
  }).bindPopup(p.stop_name).addTo(stopLayer);
});
stopLayer.addTo(map);

DATA.labels.forEach((label) => {
  L.marker([label.lat, label.lon], {
    icon: L.divIcon({
      className: "",
      html: `<div style="background:#fff;border:1px solid #111;padding:2px 6px;font:12px system-ui,sans-serif;white-space:nowrap">${label.name}</div>`,
      iconSize: [140, 22],
      iconAnchor: [70, 28]
    })
  }).addTo(map);
});

map.fitBounds(bounds, { padding: [24, 24] });

const chooser = L.control({ position: "topright" });
chooser.onAdd = function () {
  const div = L.DomUtil.create("div", "legend chooser");
  div.innerHTML = `<b>Route 100 Marine Drive</b>`;
  L.DomEvent.disableClickPropagation(div);
  routes.forEach((feature) => {
    const id = feature.properties.shape_id;
    const layer = layers[id];
    const button = L.DomUtil.create("button", "", div);
    button.type = "button";
    const mark = layer.via ? "swatch dashed" : "swatch";
    button.innerHTML = `<span class="${mark}" style="color:${layer.color}"></span>${layer.label}`;
    button.addEventListener("click", () => show(id));
    layer.button = button;
  });
  return div;
};
chooser.addTo(map);

function show(id) {
  Object.entries(layers).forEach(([key, layer]) => {
    if (key === id) layer.group.addTo(map);
    else map.removeLayer(layer.group);
    layer.button.classList.toggle("active", key === id);
  });
}
show(routes[0].properties.shape_id);
</script>
</body>
</html>
"""


def main() -> None:
    catalog = shape_catalog()
    lines = load_shapes(set(catalog))
    stops = unique_stops()
    shapes = shapes_geojson(catalog, lines)
    stop_fc = stops_geojson(stops)
    labels = terminal_labels(stops)

    DATA.mkdir(parents=True, exist_ok=True)
    shapes_path = DATA / f"route_{ROUTE_SHORT_NAME}_shapes.geojson"
    stops_path = DATA / f"route_{ROUTE_SHORT_NAME}_stops.geojson"
    shapes_path.write_text(json.dumps(shapes), encoding="utf-8")
    stops_path.write_text(json.dumps(stop_fc), encoding="utf-8")
    map_path = write_html(shapes, stop_fc, labels)

    print(f"Shapes: {len(shapes['features'])}")
    for feature in shapes["features"]:
        props = feature["properties"]
        n_points = len(feature["geometry"]["coordinates"])
        kind = "via Trapp" if props["via_trapp"] else "main path"
        print(
            f"  {props['shape_id']} {props['direction_name']} ({kind}): "
            f"{props['trip_count']} trips, {n_points} points"
        )
    print(f"Stops: {len(stop_fc['features'])}")
    print(f"Wrote {shapes_path}")
    print(f"Wrote {stops_path}")
    print(f"Wrote {map_path}")
    print(f"Wrote {DEMOS / f'route_{ROUTE_SHORT_NAME}_map.html'}")


if __name__ == "__main__":
    main()
