#!/usr/bin/env python3
"""Interactive Route 100 dashboard: pick a service day and block, see its trips.

Writes ``demos/route_100_bus_day.html``. Pack / energy parameters are read from
``energy_summary.json`` (produced by ``energy_figures.py``). The CCS charging
curve is loaded from ``data/charging/ccs_charging_curve.csv``.

Hamilton Transit Centre is marked from its published address (not a GTFS stop).
Does not modify the feed.
"""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent
DATA = ROOT / "data"
DEMOS = PROJECT / "demos"
CCS_CURVE_CSV = PROJECT / "data" / "charging" / "ccs_charging_curve.csv"

ROUTE_SHORT_NAME = "100"

# Published depot location (4111 Boundary Road, Richmond) — not in GTFS.
HTC = {
    "name": "Hamilton Transit Centre",
    "address": "4111 Boundary Road, Richmond",
    "lat": 49.1841,
    "lon": -122.9500,
}

# service_id labels in this TransLink feed.
SERVICES = [
    {"id": "1", "name": "Weekday"},
    {"id": "2", "name": "Saturday"},
    {"id": "3", "name": "Sunday"},
]

# Dashboard advice threshold (% SoC) for mid-day charge messaging.
ADVICE_SOC_PCT = 30


def minutes(hms: str) -> int:
    hour, minute, _second = (int(part) for part in hms.split(":"))
    return hour * 60 + minute


def load_ccs_curve(path: Path) -> list:
    """Load [time_s, soc_pct] pairs from the CCS charging-curve CSV."""
    if not path.exists():
        raise SystemExit(f"Missing charging curve: {path}")

    points = []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle)
        for row in reader:
            # File has a leading empty column: "", time_s, power_kw, soc_pct
            cells = [cell.strip() for cell in row if cell.strip() != ""]
            if len(cells) < 3:
                continue
            try:
                time_s = float(cells[0])
                soc_pct = float(cells[2])
            except ValueError:
                continue  # header / non-numeric row
            points.append([time_s, soc_pct])
    if not points:
        raise SystemExit(f"No numeric CCS points found in {path}")
    return points


def main() -> None:
    shapes = json.loads(
        (DATA / f"route_{ROUTE_SHORT_NAME}_shapes.geojson").read_text(encoding="utf-8")
    )
    stops = json.loads(
        (DATA / f"route_{ROUTE_SHORT_NAME}_stops.geojson").read_text(encoding="utf-8")
    )
    summary_path = DATA / "energy_summary.json"
    if not summary_path.exists():
        raise SystemExit(f"Missing {summary_path}. Run energy_figures.py first.")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))

    trips_path = DATA / f"route_{ROUTE_SHORT_NAME}_trips.csv"
    with trips_path.open(newline="", encoding="utf-8") as handle:
        trips = list(csv.DictReader(handle))

    shape_payload = []
    for feature in shapes["features"]:
        props = feature["properties"]
        coords = [
            [round(lat, 5), round(lon, 5)]
            for lon, lat in feature["geometry"]["coordinates"]
        ]
        shape_payload.append(
            {
                "id": props["shape_id"],
                "direction": props["direction_id"],
                "via": props["via_trapp"],
                "color": props["color"],
                "coords": coords,
            }
        )

    stop_payload = [
        {
            "name": feature["properties"]["stop_name"],
            "lat": round(feature["geometry"]["coordinates"][1], 5),
            "lon": round(feature["geometry"]["coordinates"][0], 5),
        }
        for feature in stops["features"]
    ]

    by_block = defaultdict(list)
    for trip in trips:
        by_block[(trip["service_id"], trip["block_id"])].append(
            {
                "id": trip["trip_id"],
                "direction": trip["direction_id"],
                "headsign": trip["trip_headsign"],
                "shape": trip["shape_id"],
                "depart": trip["departure_time"],
                "arrive": trip["arrival_time"],
                "from": trip["first_stop"],
                "to": trip["last_stop"],
            }
        )

    blocks = []
    for (service, block_id), block_trips in by_block.items():
        block_trips.sort(key=lambda item: minutes(item["depart"]))
        blocks.append({"service": service, "id": block_id, "trips": block_trips})

    # Pack parameters come from the energy model summary — not re-stated here.
    pack = {
        "kwh": summary["pack_kwh"],
        "useable_kwh": summary["useable_kwh"],
        "start_soc": summary.get("start_soc", summary["start_kwh"] / summary["pack_kwh"]),
        "start_kwh": summary["start_kwh"],
        "floor_kwh": summary["floor_kwh"],
        "budget_kwh": summary["budget_kwh"],
    }

    data = {
        "services": SERVICES,
        "shapes": shape_payload,
        "stops": stop_payload,
        "blocks": blocks,
        "htc": HTC,
        "defaultBlock": summary["block_id"],
        "pack": pack,
        "advice_soc_pct": ADVICE_SOC_PCT,
        "ccs_curve": load_ccs_curve(CCS_CURVE_CSV),
        "energy": {
            "0": {
                "km": summary["east"]["distance_km"],
                "kwh": summary["east"]["kwh"],
                "label": "To 22nd Street Station",
            },
            "1": {
                "km": summary["west"]["distance_km"],
                "kwh": summary["west"]["kwh"],
                "label": "To Marpole Loop",
            },
        },
    }

    html = TEMPLATE.replace("__DATA__", json.dumps(data, separators=(",", ":")))
    DEMOS.mkdir(parents=True, exist_ok=True)
    path = DEMOS / f"route_{ROUTE_SHORT_NAME}_bus_day.html"
    path.write_text(html, encoding="utf-8")
    print(f"Blocks: {len(blocks)}")
    print(f"Default block: {data['defaultBlock']}")
    print(f"Wrote {path}")


TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Route 100 buses by day</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>
  html, body { height: 100%; margin: 0; }
  body { display: flex; }
  #map { flex: 1; height: 100%; }
  .panel {
    background: #fff;
    padding: 12px 14px;
    border-right: 1px solid #ccc;
    font: 13px system-ui, sans-serif;
    width: 380px;
    flex: 0 0 380px;
    height: 100%;
    box-sizing: border-box;
    overflow: auto;
    line-height: 1.35;
  }
  .panel h1 { font-size: 15px; margin: 0 0 8px; }
  .panel label { display: block; margin-top: 8px; font-size: 12px; color: #444; }
  .panel select { width: 100%; margin-top: 2px; font: 13px system-ui, sans-serif; }
  .stats { margin-top: 10px; }
  .stats p { margin: 4px 0; }
  .trip { border-top: 1px solid #e5e7eb; padding: 4px 0; }
  .warn { color: #9a3412; }
  .ok { color: #166534; }
  .note { color: #52606d; font-size: 12px; }
  .callout { background: #f8fafc; border-left: 3px solid #9a3412; padding: 6px 8px; margin: 8px 0; font-size: 12px; }
  canvas#charge { width: 100%; height: 200px; display: block; margin-top: 4px; cursor: crosshair; }
  .advice { background: #fff7ed; border: 1px solid #fdba74; border-radius: 4px; padding: 6px 8px; margin: 8px 0; }
  .advice.tight { background: #fef2f2; border-color: #fca5a5; }
  .ticker { display: flex; align-items: baseline; gap: 6px; margin-top: 2px; font-weight: 650; font-variant-numeric: tabular-nums; }
  .ticker .arrow { font-size: 16px; line-height: 1; }
  .ticker.ok { color: #15803d; }
  .ticker.low { color: #b45309; }
  .ticker.bad { color: #b91c1c; }
  .budget { margin: 6px 0 4px; }
  .budget svg { width: 100%; height: auto; display: block; }
  .budget figcaption { margin: 4px 0 0; }
</style>
</head>
<body>
<aside class="panel">
  <h1>Route 100 Marine Drive</h1>
  <label for="day">Day</label>
  <select id="day"></select>
  <label for="bus">Bus (block)</label>
  <select id="bus"></select>
  <div class="stats" id="stats"></div>
</aside>
<div id="map"></div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const DATA = __DATA__;

// Pack parameters injected from energy_summary.json
const PACK = DATA.pack.kwh;
const USEABLE = DATA.pack.useable_kwh;
const START = DATA.pack.start_kwh;
const FLOOR = DATA.pack.floor_kwh;
const BUDGET = DATA.pack.budget_kwh;
const START_SOC_PCT = DATA.pack.start_soc * 100;
const FLOOR_PCT = FLOOR / PACK * 100;
const ADVICE_SOC = DATA.advice_soc_pct;
const CCS = DATA.ccs_curve;

function clockMinutes(hms) {
  const parts = hms.split(":").map(Number);
  return parts[0] * 60 + parts[1];
}
function hm(hms) {
  const parts = hms.split(":");
  return String(parts[0]).padStart(2, "0") + ":" + parts[1];
}
function tripModel(direction) {
  return DATA.energy[direction];
}

const map = L.map("map");
map.createPane("route");
map.getPane("route").style.zIndex = 450;
map.createPane("stops");
map.getPane("stops").style.zIndex = 480;
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
  maxZoom: 19
}).addTo(map);

const shapes = Object.fromEntries(DATA.shapes.map((shape) => [shape.id, shape]));
const routeLayer = L.layerGroup().addTo(map);
const stopLayer = L.layerGroup().addTo(map);
DATA.stops.forEach((stop) => {
  L.circleMarker([stop.lat, stop.lon], {
    pane: "stops",
    radius: 3,
    color: "#111",
    weight: 1,
    fillColor: "#fff",
    fillOpacity: 1
  }).bindPopup(stop.name).addTo(stopLayer);
});

const htcIcon = L.divIcon({
  className: "",
  html: '<div style="width:0;height:0;border-left:7px solid transparent;border-right:7px solid transparent;border-bottom:16px solid #dc2626;position:relative;left:2px;top:8px;filter:drop-shadow(0 1px 1px rgba(0,0,0,.35))"></div><div style="width:3px;height:18px;background:#7f1d1d;margin-left:6px"></div>',
  iconSize: [16, 28],
  iconAnchor: [8, 26]
});
L.marker([DATA.htc.lat, DATA.htc.lon], { icon: htcIcon, zIndexOffset: 800 })
  .bindPopup("<b>" + DATA.htc.name + "</b><br>" + DATA.htc.address + "<br>Depot. Not a stop in the Route 100 timetable.")
  .addTo(map);

const daySelect = document.getElementById("day");
const busSelect = document.getElementById("bus");
DATA.services.forEach((service) => {
  const option = document.createElement("option");
  option.value = service.id;
  option.textContent = service.name;
  daySelect.appendChild(option);
});

function blocksFor(service) {
  return DATA.blocks
    .filter((block) => block.service === service)
    .sort((a, b) => clockMinutes(a.trips[0].depart) - clockMinutes(b.trips[0].depart));
}

function fillBuses() {
  const blocks = blocksFor(daySelect.value);
  busSelect.innerHTML = "";
  blocks.forEach((block) => {
    const option = document.createElement("option");
    option.value = block.id;
    const first = hm(block.trips[0].depart);
    const last = hm(block.trips[block.trips.length - 1].arrive);
    option.textContent = block.id + "  ·  " + first + "–" + last + "  ·  " + block.trips.length + " trips";
    busSelect.appendChild(option);
  });
  const preferred = blocks.find((block) => block.id === DATA.defaultBlock);
  busSelect.value = preferred ? preferred.id : blocks[0].id;
  show();
}

function budgetFigure(kwh, finish) {
  const floorPct = FLOOR_PCT;
  const useablePct = USEABLE / PACK * 100;
  const startPct = START_SOC_PCT;
  const left = 8;
  const span = 324;
  const xOf = (pct) => left + span * Math.max(0, Math.min(100, pct)) / 100;
  const finishDraw = Math.max(0, Math.min(100, finish));
  const over = kwh > BUDGET;
  const x0 = xOf(0);
  const xFloor = xOf(floorPct);
  const xFinish = xOf(finishDraw);
  const xUseable = xOf(useablePct);
  const xStart = xOf(startPct);
  const xFull = left + span;
  const barY = 18;
  const barH = 22;
  const budgetLabel = Math.round(BUDGET);
  const usedLabel = Math.round(kwh);
  const finishKwh = finish / 100 * PACK;
  const leftLabel = Math.max(0, Math.round(finishKwh - FLOOR));
  function rect(a, b, fill) {
    const width = b - a;
    if (width < 0.4) return "";
    return "<rect x='" + a.toFixed(1) + "' y='" + barY + "' width='" + width.toFixed(1) + "' height='" + barH + "' fill='" + fill + "'/>";
  }
  function segText(a, b, text, fill) {
    if (b - a < 46) return "";
    return "<text x='" + ((a + b) / 2).toFixed(1) + "' y='" + (barY + 15) + "' text-anchor='middle' font-size='11' font-family='system-ui,sans-serif' fill='" + fill + "'>" + text + "</text>";
  }
  const inside = finish >= floorPct;
  const bars = inside
    ? rect(x0, xFloor, "#fecaca") + rect(xFloor, xFinish, "#86efac") + rect(xFinish, xUseable, "#60a5fa") + rect(xUseable, xFull, "#e5e7eb")
    : rect(x0, xFinish, "#fecaca") + rect(xFinish, xFloor, "#f87171") + rect(xFloor, xUseable, "#60a5fa") + rect(xUseable, xFull, "#e5e7eb");
  const labels = inside
    ? segText(xFloor, xFinish, leftLabel + " left", "#14532d") + segText(xFinish, xUseable, usedLabel + " used", "#1e3a8a")
    : "";
  const tone = over ? "#9a3412" : "#166534";
  let labelX = xFinish;
  let anchor = "middle";
  if (xFinish < 92) {
    labelX = xFinish + 5;
    anchor = "start";
  } else if (xFinish > 246) {
    labelX = xFinish - 5;
    anchor = "end";
  }
  let ends = "";
  if (finishDraw > 24) {
    ends += "<text x='" + x0.toFixed(1) + "' y='12' font-size='10' font-family='system-ui,sans-serif' fill='#64748b'>0%</text>";
  }
  if (finishDraw < 76) {
    ends += "<text x='" + xFull.toFixed(1) + "' y='12' text-anchor='end' font-size='10' font-family='system-ui,sans-serif' fill='#64748b'>100%</text>";
  }
  const bracketY = barY + barH + 3;
  return "<figure class='budget'><svg viewBox='0 0 340 62' role='img' aria-label='Pack from 0 to 100 percent. The usable budget is the schedulable day energy.'>"
    + ends
    + "<text x='" + labelX.toFixed(1) + "' y='12' text-anchor='" + anchor + "' font-size='11' font-weight='650' font-family='system-ui,sans-serif' fill='" + tone + "'>finish " + finish.toFixed(1) + "%</text>"
    + bars
    + labels
    + "<rect x='" + x0.toFixed(1) + "' y='" + barY + "' width='" + span + "' height='" + barH + "' fill='none' stroke='#94a3b8'/>"
    + "<line x1='" + xFloor.toFixed(1) + "' y1='" + barY + "' x2='" + xFloor.toFixed(1) + "' y2='" + (barY + barH) + "' stroke='#9f1239'/>"
    + "<line x1='" + xUseable.toFixed(1) + "' y1='" + barY + "' x2='" + xUseable.toFixed(1) + "' y2='" + (barY + barH) + "' stroke='#334155'/>"
    + "<line x1='" + xStart.toFixed(1) + "' y1='" + barY + "' x2='" + xStart.toFixed(1) + "' y2='" + (barY + barH) + "' stroke='#94a3b8' stroke-dasharray='2 2'/>"
    + "<line x1='" + xFinish.toFixed(1) + "' y1='" + (barY - 1) + "' x2='" + xFinish.toFixed(1) + "' y2='" + (barY + barH) + "' stroke='" + tone + "' stroke-width='1.5'/>"
    + "<path d='M" + xFloor.toFixed(1) + " " + bracketY + " v4 H" + xUseable.toFixed(1) + " v-4' fill='none' stroke='" + tone + "'/>"
    + "<text x='" + xFloor.toFixed(1) + "' y='" + (bracketY + 15) + "' font-size='10' font-family='system-ui,sans-serif' fill='#9f1239'>floor</text>"
    + "<text x='" + ((xFloor + xUseable) / 2).toFixed(1) + "' y='" + (bracketY + 15) + "' text-anchor='middle' font-size='11' font-family='system-ui,sans-serif' fill='" + tone + "'>" + budgetLabel + " kWh</text>"
    + "<text x='" + xUseable.toFixed(1) + "' y='" + (bracketY + 15) + "' text-anchor='end' font-size='10' font-family='system-ui,sans-serif' fill='#334155'>usable</text>"
    + "</svg></figure>";
}

function show() {
  const block = DATA.blocks.find((item) => item.service === daySelect.value && item.id === busSelect.value);
  routeLayer.clearLayers();
  const bounds = [[DATA.htc.lat, DATA.htc.lon]];
  const seen = new Set();
  let km = 0;
  let kwh = 0;
  let stored = START;
  let advice = "";
  const rows = block.trips.map((trip, index) => {
    const model = tripModel(trip.direction);
    km += model.km;
    kwh += model.kwh;
    const via = /via/i.test(trip.headsign) ? " via Trapp Road" : "";
    if (!seen.has(trip.shape)) {
      seen.add(trip.shape);
      const shape = shapes[trip.shape];
      shape.coords.forEach((pair) => bounds.push(pair));
      L.polyline(shape.coords, {
        pane: "route",
        color: shape.color,
        weight: 4,
        opacity: 0.9,
        dashArray: shape.via ? "8 8" : null
      }).bindPopup("<b>" + model.label + via + "</b>").addTo(routeLayer);
    }
    const tripMinutes = Math.max(1, clockMinutes(trip.arrive) - clockMinutes(trip.depart));
    const kw = model.kwh / (tripMinutes / 60);
    stored -= model.kwh;
    const socNow = stored / PACK * 100;
    const later = block.trips.slice(index + 1);
    if (!advice && later.length && socNow < ADVICE_SOC) {
      const remain = later.reduce((sum, item) => sum + tripModel(item.direction).kwh, 0);
      const need = Math.max(0, remain + FLOOR - stored);
      const mins = chargeMinutes(socNow, need);
      const gap = clockMinutes(later[0].depart) - clockMinutes(trip.arrive);
      const charge = "Charge is " + socNow.toFixed(0) + "% and " + later.length + " trips are still scheduled. ";
      if (need <= 0) {
        advice = "<p class='advice'><b>After " + hm(trip.arrive) + " the pack is still inside the day.</b> "
          + charge + "Those trips finish above the floor, so nothing is added.</p>";
      } else if (mins > gap) {
        advice = "<p class='advice tight'><b>The timetable does not move.</b> "
          + "After " + hm(trip.arrive) + " the next trip leaves at " + hm(later[0].depart)
          + " (" + gap + " min). " + charge
          + "The CCS curve needs about " + formatMinutes(mins) + " to add " + need.toFixed(0)
          + " kWh, and that is before the ride to Hamilton, which is not in the timetable. "
          + "The gap is shorter than the charge, so the remaining trips stay on the clock and the line is drawn through the floor.</p>";
      } else {
        advice = "<p class='advice'><b>After " + hm(trip.arrive) + " there are " + gap + " min before "
          + hm(later[0].depart) + ".</b> " + charge
          + "The CCS curve needs about " + formatMinutes(mins) + " to add " + need.toFixed(0)
          + " kWh. The ride to Hamilton is still not in the timetable, so that gap is station time only, and this chart does not insert a charge.</p>";
      }
    }
    const tone = socNow < FLOOR_PCT ? "bad" : socNow < ADVICE_SOC ? "low" : "ok";
    return "<div class='trip'><b>" + hm(trip.depart) + "–" + hm(trip.arrive) + "</b> "
      + model.label + via + "<br>" + model.kwh.toFixed(1) + " kWh · " + kw.toFixed(0) + " kW average · " + model.km.toFixed(1) + " km"
      + "<div class='ticker " + tone + "'><span class='arrow'>↓</span><span>" + stored.toFixed(0) + " kWh left</span></div></div>";
  });
  const finish = (START - kwh) / PACK * 100;
  const over = kwh > BUDGET;
  document.getElementById("stats").innerHTML =
    "<p><b>" + block.trips.length + " scheduled trips</b><br>"
    + hm(block.trips[0].depart) + " to " + hm(block.trips[block.trips.length - 1].arrive) + "</p>"
    + "<p>" + km.toFixed(1) + " km in service<br>" + kwh.toFixed(1) + " kWh, mild day, full bus</p>"
    + "<p class='" + (over ? "warn" : "ok") + "'>If the first stop starts at " + START_SOC_PCT.toFixed(0) + "%: finish "
    + finish.toFixed(1) + "%.<br>"
    + (over
        ? ("Over the " + BUDGET + " kWh usable budget.")
        : ("Inside the " + BUDGET + " kWh usable budget.")) + "</p>"
    + budgetFigure(kwh, finish)
    + advice
    + "<canvas id='charge' width='700' height='340'></canvas>"
    + "<p id='charttip' class='note'>Move across the line to read a moment.</p>"
    + rows.join("")
    + "<p class='note'>Scheduled trips only. Each direction uses the main-path elevation total, "
    + DATA.energy["0"].kwh.toFixed(1) + " kWh to 22nd Street and "
    + DATA.energy["1"].kwh.toFixed(1) + " kWh to Marpole. Hamilton is the red flag. The timetable does not include the ride between the depot and 22nd Street Station. Layovers add no energy.</p>"
    + "<p class='callout'><b>! Note.</b> A finish below the floor is left there on purpose. The timetable is not moved to fit a charge. That shortfall is why more chargers and overhead charging are needed.</p>"
    + "<p class='note'>Pink is the " + FLOOR + " kWh under the floor. Grey is above the " + USEABLE + " kWh usable cap. The bracket is the " + BUDGET + " kWh schedulable budget. Charge over the scheduled day. No energy is added, and no trip is moved.</p>";
  drawCharge(block);
  map.invalidateSize();
  map.fitBounds(bounds, { padding: [28, 28] });
}

function chargePoints(block) {
  const points = [];
  let stored = START;
  block.trips.forEach((trip, index) => {
    const model = tripModel(trip.direction);
    const depart = clockMinutes(trip.depart);
    const arrive = clockMinutes(trip.arrive);
    if (index === 0) points.push({ t: depart, stored, label: "Start" });
    else points.push({ t: depart, stored, label: "Layover" });
    stored -= model.kwh;
    const tripMinutes = Math.max(1, arrive - depart);
    points.push({
      t: arrive,
      stored,
      label: model.label,
      kwh: model.kwh,
      km: model.km,
      kw: model.kwh / (tripMinutes / 60)
    });
  });
  return points;
}

function secondsAtSoc(soc) {
  const lowKw = 140; // CCS power below the first curve knot (matches curve file)
  if (soc <= CCS[0][1]) return -((CCS[0][1] - soc) / 100 * PACK / lowKw) * 3600;
  const last = CCS[CCS.length - 1];
  if (soc >= last[1]) return last[0] + ((soc - last[1]) / 100 * PACK / 5) * 3600;
  for (let i = 1; i < CCS.length; i++) {
    const [t0, s0] = CCS[i - 1];
    const [t1, s1] = CCS[i];
    if (soc <= s1) return t0 + (soc - s0) / (s1 - s0) * (t1 - t0);
  }
  return last[0];
}

function chargeMinutes(fromSoc, addKwh) {
  const toSoc = fromSoc + addKwh / PACK * 100;
  return Math.max(0, (secondsAtSoc(toSoc) - secondsAtSoc(fromSoc)) / 60);
}

function formatMinutes(mins) {
  const rounded = Math.round(mins);
  const hour = Math.floor(rounded / 60);
  const minute = rounded % 60;
  if (hour === 0) return minute + " min";
  return hour + " h " + String(minute).padStart(2, "0") + " min";
}

function drawCharge(block) {
  const canvas = document.getElementById("charge");
  const tip = document.getElementById("charttip");
  const points = chargePoints(block);
  const dpr = window.devicePixelRatio || 1;
  const width = canvas.clientWidth || 340;
  const height = 200;
  canvas.width = Math.round(width * dpr);
  canvas.height = Math.round(height * dpr);
  canvas.style.height = height + "px";
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const pad = { l: 36, r: 10, t: 12, b: 28 };
  const t0 = points[0].t;
  const t1 = points[points.length - 1].t;
  const lowPct = Math.min(0, ...points.map((point) => point.stored / PACK * 100));
  const y0 = lowPct - 8;
  const y1 = 100;
  const xOf = (t) => pad.l + (t - t0) / Math.max(1, t1 - t0) * (width - pad.l - pad.r);
  const yOfPct = (pct) => pad.t + (y1 - pct) / (y1 - y0) * (height - pad.t - pad.b);
  const yOf = (stored) => yOfPct(stored / PACK * 100);
  ctx.clearRect(0, 0, width, height);
  ctx.font = "10px system-ui, sans-serif";
  ctx.strokeStyle = "#e5e7eb";
  ctx.fillStyle = "#52606d";
  ctx.beginPath();
  ctx.moveTo(pad.l, pad.t);
  ctx.lineTo(pad.l, height - pad.b);
  ctx.lineTo(width - pad.r, height - pad.b);
  ctx.stroke();
  const yTicks = [0, 30, 60, Math.round(START_SOC_PCT)].filter((tick) => tick >= y0 && tick <= 100);
  if (lowPct < 0) yTicks.unshift(Math.round(lowPct));
  yTicks.forEach((tick) => {
    const y = yOfPct(tick);
    ctx.strokeStyle = "#e5e7eb";
    ctx.beginPath();
    ctx.moveTo(pad.l, y);
    ctx.lineTo(width - pad.r, y);
    ctx.stroke();
    ctx.fillStyle = "#52606d";
    ctx.fillText(tick + "%", 2, y + 3);
  });
  const xStep = Math.max(30, Math.round((t1 - t0) / 4 / 30) * 30);
  for (let t = Math.ceil(t0 / xStep) * xStep; t < t1; t += xStep) {
    const x = xOf(t);
    ctx.strokeStyle = "#e5e7eb";
    ctx.beginPath();
    ctx.moveTo(x, pad.t);
    ctx.lineTo(x, height - pad.b);
    ctx.stroke();
    ctx.fillStyle = "#52606d";
    ctx.fillText(hmFromMinutes(t), x - 12, height - 8);
  }
  const floorY = yOf(FLOOR);
  ctx.fillStyle = "#fde8e8";
  ctx.fillRect(pad.l, floorY, width - pad.l - pad.r, height - pad.b - floorY);
  ctx.strokeStyle = "#dc2626";
  ctx.setLineDash([4, 3]);
  ctx.beginPath();
  ctx.moveTo(pad.l, floorY);
  ctx.lineTo(width - pad.r, floorY);
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle = "#9a3412";
  ctx.fillText("floor", pad.l + 4, Math.max(14, floorY - 3));
  ctx.lineWidth = 2;
  ctx.lineJoin = "round";
  ctx.strokeStyle = "#1d4ed8";
  ctx.beginPath();
  points.forEach((point, index) => {
    const x = xOf(point.t);
    const y = yOf(point.stored);
    if (index === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();
  ctx.strokeStyle = "#b91c1c";
  ctx.beginPath();
  let drawingLow = false;
  for (let i = 1; i < points.length; i++) {
    const a = points[i - 1];
    const b = points[i];
    const aLow = a.stored <= FLOOR;
    const bLow = b.stored <= FLOOR;
    if (!aLow && !bLow) {
      drawingLow = false;
      continue;
    }
    let x1 = xOf(a.t);
    let y1 = yOf(a.stored);
    let x2 = xOf(b.t);
    let y2 = yOf(b.stored);
    if (!aLow && bLow) {
      const fraction = (a.stored - FLOOR) / (a.stored - b.stored);
      x1 += (x2 - x1) * fraction;
      y1 = yOf(FLOOR);
    } else if (aLow && !bLow) {
      const fraction = (FLOOR - a.stored) / (b.stored - a.stored);
      x2 = x1 + (x2 - x1) * fraction;
      y2 = yOf(FLOOR);
    }
    if (!drawingLow) ctx.moveTo(x1, y1);
    else ctx.lineTo(x1, y1);
    ctx.lineTo(x2, y2);
    drawingLow = bLow;
  }
  ctx.stroke();
  points.forEach((point) => {
    ctx.fillStyle = point.stored <= FLOOR ? "#b91c1c" : "#1d4ed8";
    ctx.beginPath();
    ctx.arc(xOf(point.t), yOf(point.stored), 2.5, 0, Math.PI * 2);
    ctx.fill();
  });
  canvas.onmousemove = (event) => {
    const rect = canvas.getBoundingClientRect();
    const x = event.clientX - rect.left;
    let nearest = 0;
    let best = Infinity;
    points.forEach((point, index) => {
      const distance = Math.abs(xOf(point.t) - x);
      if (distance < best) {
        best = distance;
        nearest = index;
      }
    });
    const point = points[nearest];
    const soc = point.stored / PACK * 100;
    let text = hmFromMinutes(point.t) + " · " + soc.toFixed(1) + "% · " + point.label;
    if (point.kwh) text += " · " + point.kwh.toFixed(1) + " kWh · " + point.kw.toFixed(0) + " kW average · " + point.km.toFixed(1) + " km";
    tip.textContent = text;
  };
}

function hmFromMinutes(total) {
  const hour = Math.floor(total / 60);
  const minute = total % 60;
  return String(hour).padStart(2, "0") + ":" + String(minute).padStart(2, "0");
}

daySelect.addEventListener("change", fillBuses);
busSelect.addEventListener("change", show);
fillBuses();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
