#!/usr/bin/env python3
"""Turn a SUMO trace + network into the Trips-tab route picture.

Run this on YOUR computer (the files are too big to ship around). It streams
both XML files, so memory use stays small even for very large inputs, and it
writes two small files that the website picks up automatically:

    website/static/data/trip_route.svg    the road map + the vehicle's path
    website/static/data/trip_route.json   distance / duration / average speed

Usage (from the repo root):

    python tools/sumo_route.py \\
        --net  path/to/uct_cbd.net.xml \\
        --trace path/to/fcd_trace.xml \\
        --vehicle 0 \\
        --from-name UCT --to-name "Cape Town CBD" \\
        --start-time "2026-10-01 09:00"        # optional label only

Then rebuild the website:  docker compose up -d --build website

Input notes
  * --trace must be SUMO "fcd-output" XML (<timestep time=..><vehicle id= x= y= speed=/>),
    written WITHOUT --fcd-output.geo, so x/y are in the same metres as the net.
    (If you only have a geo trace, re-run SUMO with  --fcd-output trace.xml
    and no --fcd-output.geo.)  .gz files are read directly.
  * Only vehicle --vehicle (default "0") is read; the rest of the trace is skipped.
  * Only roads near the route are drawn, which keeps the SVG small.
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def open_any(path):
    p = str(path)
    return gzip.open(p, "rb") if p.endswith(".gz") else open(p, "rb")


def read_vehicle_trace(trace_path, vehicle_id):
    """Return [(time_s, x, y, speed_m_s_or_None), ...] for one vehicle."""
    pts = []
    t = None
    root = None
    with open_any(trace_path) as fh:
        for event, el in ET.iterparse(fh, events=("start", "end")):
            tag = el.tag
            if root is None:
                root = el          # first start event is the document root
            if event == "start":
                if tag == "timestep":
                    t = float(el.get("time", "nan"))
                continue
            if tag == "vehicle":
                if el.get("id") == vehicle_id and el.get("x") is not None:
                    sp = el.get("speed")
                    pts.append((t if t is not None else float(len(pts)),
                                float(el.get("x")), float(el.get("y")),
                                float(sp) if sp is not None else None))
                el.clear()
            elif tag == "timestep":
                el.clear()
                root.clear()       # drop finished timesteps so huge traces stay small in memory
    return pts


def parse_shape(text):
    out = []
    for pair in text.split():
        x, y = pair.split(",")[:2]
        out.append((float(x), float(y)))
    return out


def read_roads(net_path, bbox):
    """Stream lane shapes (not internal junction lanes) that touch bbox."""
    x0, y0, x1, y1 = bbox
    roads = []
    root = None
    with open_any(net_path) as fh:
        for event, el in ET.iterparse(fh, events=("start", "end")):
            if root is None:
                root = el
            if event == "start":
                continue
            if el.tag != "lane":
                if el.tag in ("edge", "junction", "connection"):
                    el.clear()
                    root.clear()   # keep memory flat on very large networks
                continue
            lane_id = el.get("id", "")
            shape = el.get("shape")
            if shape and not lane_id.startswith(":"):
                pl = parse_shape(shape)
                xs = [p[0] for p in pl]
                ys = [p[1] for p in pl]
                if max(xs) >= x0 and min(xs) <= x1 and max(ys) >= y0 and min(ys) <= y1:
                    roads.append((float(el.get("speed", "13.9")), pl))
            el.clear()
    return roads


def rdp(points, eps):
    """Ramer–Douglas–Peucker (iterative) to thin out polylines."""
    if len(points) < 3:
        return points
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        a, b = stack.pop()
        ax, ay = points[a]
        bx, by = points[b]
        dx, dy = bx - ax, by - ay
        norm = math.hypot(dx, dy)
        best, idx = -1.0, None
        for i in range(a + 1, b):
            px, py = points[i]
            d = (math.hypot(px - ax, py - ay) if norm == 0
                 else abs(dy * (px - ax) - dx * (py - ay)) / norm)
            if d > best:
                best, idx = d, i
        if idx is not None and best > eps:
            keep[idx] = True
            stack.append((a, idx))
            stack.append((idx, b))
    return [p for p, k in zip(points, keep) if k]


def fmt_duration(seconds):
    s = int(round(seconds))
    return f"{s // 60} min {s % 60:02d} sec"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--net", required=True, help="SUMO .net.xml (or .gz)")
    ap.add_argument("--trace", required=True, help="SUMO fcd-output XML (or .gz)")
    ap.add_argument("--vehicle", default="0", help="vehicle id to draw (default 0)")
    ap.add_argument("--out-dir", default="website/static/data")
    ap.add_argument("--from-name", default="Start")
    ap.add_argument("--to-name", default="End")
    ap.add_argument("--start-time", default=None, help="label only, e.g. '2026-10-01 09:00' (traces have no calendar time)")
    ap.add_argument("--margin", type=float, default=0.12, help="extra map around the route, as a fraction of its size")
    ap.add_argument("--width", type=int, default=900, help="SVG width in px")
    a = ap.parse_args()

    trace = read_vehicle_trace(a.trace, a.vehicle)
    if len(trace) < 2:
        sys.exit(f"Vehicle '{a.vehicle}' has {len(trace)} point(s) in {a.trace}. "
                 "Check the id (try --vehicle with another id) and that the file is fcd-output XML.")
    xs = [p[1] for p in trace]
    ys = [p[2] for p in trace]
    w_m = max(max(xs) - min(xs), 50.0)
    h_m = max(max(ys) - min(ys), 50.0)
    mx, my = w_m * a.margin, h_m * a.margin
    bbox = (min(xs) - mx, min(ys) - my, max(xs) + mx, max(ys) + my)
    bw, bh = bbox[2] - bbox[0], bbox[3] - bbox[1]

    scale = a.width / bw
    height = max(120, min(int(bh * scale), int(a.width * 1.1)))
    scale = min(scale, height / bh)           # keep aspect if height was capped
    ox = (a.width - bw * scale) / 2
    oy = (height - bh * scale) / 2

    def px(p):
        return (ox + (p[0] - bbox[0]) * scale, height - (oy + (p[1] - bbox[1]) * scale))

    def path(points, eps_m):
        pts = rdp(points, eps_m)
        return "M" + "L".join(f"{x:.1f} {y:.1f}" for x, y in map(px, pts))

    roads = read_roads(a.net, bbox)
    major, minor = [], []
    for speed, pl in roads:
        (major if speed >= 16.0 else minor).append(path(pl, 2.0))

    route_pts = [(p[1], p[2]) for p in trace]
    route_d = path(route_pts, 1.0)
    sx, sy = px(route_pts[0])
    ex, ey = px(route_pts[-1])

    # Statistics straight from the trace.
    dist_m = sum(math.hypot(route_pts[i + 1][0] - route_pts[i][0], route_pts[i + 1][1] - route_pts[i][1])
                 for i in range(len(route_pts) - 1))
    duration = trace[-1][0] - trace[0][0]
    avg_kmh = (dist_m / duration * 3.6) if duration > 0 else 0.0

    esc = lambda t: t.replace("&", "&amp;").replace("<", "&lt;")
    lab_dx = 16
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {a.width} {height}" role="img" aria-label="Simulated route from {esc(a.from_name)} to {esc(a.to_name)}">
<rect width="{a.width}" height="{height}" rx="18" fill="#0c261f"/>
<g fill="none" stroke-linecap="round" stroke-linejoin="round">
<path d="{' '.join(minor)}" stroke="#244236" stroke-width="1.6"/>
<path d="{' '.join(major)}" stroke="#3b6552" stroke-width="3"/>
<path d="{route_d}" stroke="#07130f" stroke-width="9"/>
<path d="{route_d}" stroke="#52ffc8" stroke-width="5"/>
</g>
<g fill="#52ffc8" stroke="#07130f" stroke-width="4"><circle cx="{sx:.1f}" cy="{sy:.1f}" r="9"/><circle cx="{ex:.1f}" cy="{ey:.1f}" r="9"/></g>
<g fill="#edfff6" font-family="system-ui,sans-serif" font-size="16" font-weight="600" paint-order="stroke" stroke="#07130f" stroke-width="4">
<text x="{min(max(sx + lab_dx, 8), a.width - 120):.1f}" y="{min(max(sy - 14, 20), height - 8):.1f}">{esc(a.from_name)}</text>
<text x="{min(max(ex + lab_dx, 8), a.width - 140):.1f}" y="{min(max(ey - 14, 20), height - 8):.1f}">{esc(a.to_name)}</text></g>
</svg>
'''
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "trip_route.svg").write_text(svg, encoding="utf-8")
    info = {
        "source": "SUMO simulation",
        "vehicle": a.vehicle,
        "from": a.from_name,
        "to": a.to_name,
        "start_time_label": a.start_time,
        "distance_km": round(dist_m / 1000, 2),
        "duration_s": round(duration, 1),
        "duration_text": fmt_duration(duration),
        "avg_speed_kmh": round(avg_kmh, 1),
        "trace_points": len(trace),
        "roads_drawn": len(roads),
        "net_file": Path(a.net).name,
        "trace_file": Path(a.trace).name,
    }
    (out / "trip_route.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    print(f"Wrote {out/'trip_route.svg'} ({len(svg)//1024} KB) and trip_route.json")
    print(f"  {len(roads)} road lanes drawn, {len(trace)} trace points")
    print(f"  distance {info['distance_km']} km, duration {info['duration_text']}, average {info['avg_speed_kmh']} km/h")


if __name__ == "__main__":
    main()
