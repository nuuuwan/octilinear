"""
Two-step pipeline to produce octilinear TopoJSON maps.

Step 1 – Simplify
    Each arc is resampled so that every segment has approximately the same
    specified length (chord-length resampling).  Results are written to
    data/simplified/.

Step 2 – Octilinearize
    For every arc segment that is not already at a multiple of 45° it is
    replaced by two segments that each ARE at a multiple of 45°.  The
    intermediate "elbow" point is found by decomposing the original vector
    into the two nearest bracketing octilinear unit directions.  Results are
    written to data/octilinear/.

Images (3-panel: original | simplified | octilinear) are saved to
images/octilinear/.

Usage:
    python workflows/pipeline.py [--segment-length DEG] [--min-area AREA]
                                  [input ...]

Positional arguments:
    input              Path(s) to input TopoJSON file(s).  Defaults to all
                       *.topojson files under data/original/.

Optional arguments:
    --segment-length   Target arc-segment length in degrees.  Default: 0.02.
    --min-area AREA    Remove polygons whose area (km²) is below this value.
                       Default: 100 000.
"""

import argparse
import json
import math
import os
import random
import sys

import matplotlib.collections as mc
import matplotlib.patches as mpatches
import matplotlib.path as mpath
import matplotlib.pyplot as plt

# ---------------------------------------------------------------------------
# Decoding helpers
# ---------------------------------------------------------------------------


def decode_arc(arc, transform):
    """
    Convert a delta-encoded integer arc (with transform) to a list of absolute
    [lon, lat] float positions.
    """
    sx, sy = transform["scale"]
    tx, ty = transform["translate"]
    points = []
    x = y = 0
    for dx, dy in arc:
        x += dx
        y += dy
        points.append([x * sx + tx, y * sy + ty])
    return points


def decode_arcs(arcs, transform):
    if transform is None:
        # Already absolute float coordinates — nothing to decode.
        return [list(map(list, arc)) for arc in arcs]
    return [decode_arc(arc, transform) for arc in arcs]


# ---------------------------------------------------------------------------
# Simplification (chord-length resampling)
# ---------------------------------------------------------------------------


def simplify_arc(points, segment_length):
    """
    Resample *points* so that every consecutive pair of output points is
    approximately *segment_length* apart (Euclidean chord length).

    The algorithm walks along the original polyline, accumulates distance, and
    emits a new vertex whenever the accumulated distance reaches
    *segment_length*.  The first and last original points are always kept.

    Parameters
    ----------
    points        : list of [x, y]
    segment_length: float – target chord length (same units as coordinates)

    Returns
    -------
    list of [x, y] with approximately uniform spacing.
    """
    if len(points) < 2 or segment_length <= 0:
        return [list(p) for p in points]

    result = [list(points[0])]
    accumulated = 0.0

    for i in range(1, len(points)):
        x0, y0 = points[i - 1]
        x1, y1 = points[i]
        seg_len = math.hypot(x1 - x0, y1 - y0)
        if seg_len < 1e-15:
            continue

        remaining = seg_len
        start_frac = 0.0

        while accumulated + remaining >= segment_length:
            step = segment_length - accumulated
            frac = start_frac + step / seg_len
            nx = x0 + frac * (x1 - x0)
            ny = y0 + frac * (y1 - y0)
            result.append([nx, ny])
            start_frac = frac
            remaining -= step
            accumulated = 0.0

        accumulated += remaining

    # Always include the original last point (close the arc)
    last = list(points[-1])
    if result[-1] != last:
        result.append(last)

    return result


# ---------------------------------------------------------------------------
# Octilinear geometry
# ---------------------------------------------------------------------------


def _angle_deg(dx, dy):
    return math.degrees(math.atan2(dy, dx)) % 360.0


def _is_snapped(dx, dy, angle_step, tol=1e-9):
    """
    Return True when vector (dx, dy) already points at a multiple of
    *angle_step* degrees.
    """
    if abs(dx) < tol and abs(dy) < tol:
        return True  # degenerate / zero-length
    remainder = _angle_deg(dx, dy) % angle_step
    return remainder < tol or remainder > (angle_step - tol)


def _elbow_candidates(ax, ay, bx, by, angle_step):
    """
    Return both possible elbow points for segment A→B, or (None, None) if the
    segment is already snapped to *angle_step*.

    Two valid elbows always exist on opposite sides of the original line:
      M1 = A + t·d1  — first leg along the lower bracketing direction θ₁
      M2 = A + s·d2  — first leg along the upper bracketing direction θ₂
    """
    dx = bx - ax
    dy = by - ay

    if _is_snapped(dx, dy, angle_step):
        return None, None

    angle = _angle_deg(dx, dy)
    lower_mult = math.floor(angle / angle_step)
    theta1 = math.radians(lower_mult * angle_step)
    theta2 = math.radians((lower_mult + 1) * angle_step)

    d1x, d1y = math.cos(theta1), math.sin(theta1)
    d2x, d2y = math.cos(theta2), math.sin(theta2)

    # 2×2 linear system  [d1 | d2] * [t, s]ᵀ = [dx, dy]ᵀ  (Cramer's rule)
    det = d1x * d2y - d2x * d1y
    if abs(det) < 1e-12:
        return None, None

    t = (dx * d2y - dy * d2x) / det
    s = (d1x * dy - d1y * dx) / det

    m1 = [ax + t * d1x, ay + t * d1y]  # right of A→B
    m2 = [ax + s * d2x, ay + s * d2y]  # left  of A→B
    return m1, m2


def _cross2d(ox, oy, ax, ay, bx, by):
    return (ax - ox) * (by - oy) - (ay - oy) * (bx - ox)


def _segments_intersect(p1, p2, p3, p4, tol=1e-9):
    """
    Return True if segment p1-p2 properly intersects p3-p4.
    Shared endpoints are NOT counted as intersections.
    """
    d1 = _cross2d(p3[0], p3[1], p4[0], p4[1], p1[0], p1[1])
    d2 = _cross2d(p3[0], p3[1], p4[0], p4[1], p2[0], p2[1])
    d3 = _cross2d(p1[0], p1[1], p2[0], p2[1], p3[0], p3[1])
    d4 = _cross2d(p1[0], p1[1], p2[0], p2[1], p4[0], p4[1])
    return ((d1 > tol and d2 < -tol) or (d1 < -tol and d2 > tol)) and (
        (d3 > tol and d4 < -tol) or (d3 < -tol and d4 > tol)
    )


def octilinearize_arc(points, interior_pt, angle_step=45.0):
    """
    Snap every segment of *points* to the nearest multiple of *angle_step*
    degrees by inserting an elbow point.

    For each non-snapped segment A→B:
      1. Compute both candidate elbows M1 and M2.
      2. Prefer the one closer to interior_pt (tends toward polygon interior).
      3. If the preferred elbow causes segment A→M or M→B to intersect any
         already-placed segment, switch to the other candidate.

    The intersection check guarantees no crossings with previously placed
    segments, eliminating the self-intersection problem.
    """
    result = [points[0]]

    for i in range(1, len(points)):
        ax, ay = points[i - 1]
        bx, by = points[i]
        m1, m2 = _elbow_candidates(ax, ay, bx, by, angle_step)

        if m1 is None:
            result.append([bx, by])
            continue

        # Build list of already-placed segments as flat (x,y) tuples
        # (exclude the last segment which ends at the current start point A)
        placed = [
            (
                (result[j][0], result[j][1]),
                (result[j + 1][0], result[j + 1][1]),
            )
            for j in range(len(result) - 2)
        ]

        def _has_crossing(m):
            for p, q in placed:
                if _segments_intersect(
                    (ax, ay),
                    (m[0], m[1]),
                    (p[0], p[1]),
                    (q[0], q[1]),
                ):
                    return True
                if _segments_intersect(
                    (m[0], m[1]),
                    (bx, by),
                    (p[0], p[1]),
                    (q[0], q[1]),
                ):
                    return True
            return False

        # Primary preference: elbow closer to interior point
        ix, iy = interior_pt
        d1sq = (m1[0] - ix) ** 2 + (m1[1] - iy) ** 2
        d2sq = (m2[0] - ix) ** 2 + (m2[1] - iy) ** 2
        preferred, fallback = (m1, m2) if d1sq < d2sq else (m2, m1)

        if not _has_crossing(preferred):
            result.append(preferred)
        elif not _has_crossing(fallback):
            result.append(fallback)
        else:
            result.append(preferred)  # both cross; keep preferred

        result.append([bx, by])

    return result


def _ring_centroid(points):
    """Simple centroid of a polygon ring."""
    n = len(points)
    if n == 0:
        return [0.0, 0.0]
    return [sum(p[0] for p in points) / n, sum(p[1] for p in points) / n]


def _assign_arc_interior_pts(objects, decoded_arcs):
    """
    For every arc, accumulate the centroid of each ring that references it and
    return the average.  Shared boundary arcs get the average of the two
    neighbouring polygons' centroids; exterior arcs get just their polygon's
    centroid.

    Returns {arc_index: [cx, cy]}.
    """
    from collections import defaultdict

    arc_centroid_sums = defaultdict(lambda: [0.0, 0.0, 0])
    for obj in objects.values():
        for geom in obj.get("geometries", []):
            geo_type = geom.get("type")
            if geo_type == "Polygon":
                polygon_list = [geom.get("arcs", [])]
            elif geo_type == "MultiPolygon":
                polygon_list = geom.get("arcs", [])
            else:
                continue
            for rings in polygon_list:
                for ring_indices in rings:
                    ring_pts = _ring_points(ring_indices, decoded_arcs)
                    cx, cy = _ring_centroid(ring_pts)
                    for idx in ring_indices:
                        arc_idx = idx if idx >= 0 else ~idx
                        entry = arc_centroid_sums[arc_idx]
                        entry[0] += cx
                        entry[1] += cy
                        entry[2] += 1
    return {
        arc_idx: [s[0] / s[2], s[1] / s[2]]
        for arc_idx, s in arc_centroid_sums.items()
    }


# ---------------------------------------------------------------------------
# Polygon area helpers
# ---------------------------------------------------------------------------


def _ring_points(arc_indices, arcs):
    """Reconstruct the coordinate list of a ring from TopoJSON arc indices."""
    points = []
    for idx in arc_indices:
        if idx >= 0:
            arc = arcs[idx]
            seg = arc[:-1]  # last point is shared with start of next arc
        else:
            arc = arcs[~idx]
            seg = arc[:0:-1]  # reversed, same exclusion
        points.extend(seg)
    return points


# 1° of latitude ≈ 111.32 km; used for rough sq-degree → km² conversion.
_KM_PER_DEG = 111.32
_SQ_DEG_TO_KM2 = _KM_PER_DEG**2  # ~12,392 km² per square degree


def _shoelace_area(points):
    """Absolute area of a polygon ring via the shoelace formula."""
    n = len(points)
    if n < 3:
        return 0.0
    area = 0.0
    for i in range(n):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % n]
        area += x1 * y2 - x2 * y1
    return abs(area) / 2.0


def geometry_area(geometry, arcs):
    """Approximate area of a Polygon or MultiPolygon geometry in km²."""
    geo_type = geometry.get("type")
    if geo_type == "Polygon":
        rings = geometry.get("arcs", [])
        if not rings:
            return 0.0
        return _shoelace_area(_ring_points(rings[0], arcs)) * _SQ_DEG_TO_KM2
    elif geo_type == "MultiPolygon":
        total = 0.0
        for polygon in geometry.get("arcs", []):
            if polygon:
                total += _shoelace_area(_ring_points(polygon[0], arcs))
        return total * _SQ_DEG_TO_KM2
    return 0.0


def _polygon_area_km2(polygon_arcs, arcs):
    """Area of a single polygon (list of rings) in km²."""
    if not polygon_arcs:
        return 0.0
    return _shoelace_area(_ring_points(polygon_arcs[0], arcs)) * _SQ_DEG_TO_KM2


def _filter_geometry(geom, arcs, min_area):
    """
    Return a (possibly modified) copy of geom with sub-polygons below min_area
    removed, or None if nothing remains.

    - Polygon:       kept as-is if its area >= min_area, otherwise dropped.
    - MultiPolygon:  individual polygon members below min_area are stripped;
                     the geometry is kept (as Polygon if only one remains) as
                     long as at least one member survives.
    """
    geo_type = geom.get("type")

    if geo_type == "Polygon":
        area = (
            _shoelace_area(_ring_points(geom["arcs"][0], arcs))
            * _SQ_DEG_TO_KM2
            if geom.get("arcs")
            else 0.0
        )
        return geom if area >= min_area else None

    if geo_type == "MultiPolygon":
        kept = [
            p
            for p in geom.get("arcs", [])
            if _polygon_area_km2(p, arcs) >= min_area
        ]
        if not kept:
            return None
        if len(kept) == 1:
            return {**geom, "type": "Polygon", "arcs": kept[0]}
        return {**geom, "arcs": kept}

    return geom  # Point, LineString, etc. — leave untouched


def filter_small_geometries(objects, arcs, min_area):
    """Return a copy of objects with sub-polygons smaller than min_area removed."""
    filtered = {}
    sub_before = sub_after = geoms_removed = 0
    for name, obj in objects.items():
        new_geoms = []
        for geom in obj.get("geometries", []):
            # count sub-polygons before
            if geom.get("type") == "MultiPolygon":
                sub_before += len(geom.get("arcs", []))
            else:
                sub_before += 1
            result = _filter_geometry(geom, arcs, min_area)
            if result is None:
                geoms_removed += 1
            else:
                new_geoms.append(result)
                if result.get("type") == "MultiPolygon":
                    sub_after += len(result.get("arcs", []))
                else:
                    sub_after += 1
        filtered[name] = {**obj, "geometries": new_geoms}
    removed_subs = sub_before - sub_after
    if removed_subs:
        print(
            f"Filtered: removed {removed_subs} sub-polygon(s) (area < {min_area} km²); "
            f"{geoms_removed} geometry feature(s) dropped entirely"
        )
    return filtered


# ---------------------------------------------------------------------------
# Plotting
# ---------------------------------------------------------------------------


# Sri Lankan flag palette
_LK_PALETTE = [
    "#8D153A",  # maroon
    "#FF7900",  # saffron
    "#00534E",  # green
    "#FFD100",  # gold
]


def _build_color_map(objects):
    """
    Greedy 4-colouring of features using the Sri Lankan flag palette so that
    no two adjacent features (sharing a TopoJSON arc) get the same colour.
    """
    # Collect the set of arc indices each feature references.
    feature_arcs = {}
    for obj in objects.values():
        for idx, geom in enumerate(obj.get("geometries", [])):
            key = geom.get("id", idx)
            used = set()
            geo_type = geom.get("type")
            if geo_type == "Polygon":
                ring_groups = [geom.get("arcs", [])]
            elif geo_type == "MultiPolygon":
                ring_groups = geom.get("arcs", [])
            else:
                ring_groups = []
            for poly in ring_groups:
                for ring in poly:
                    for a in ring:
                        used.add(~a if a < 0 else a)
            feature_arcs[key] = used

    # arc index → list of feature keys that reference it
    arc_features = {}
    for key, arcs in feature_arcs.items():
        for a in arcs:
            arc_features.setdefault(a, []).append(key)

    # Adjacency: two features sharing an arc are neighbours.
    adj = {key: set() for key in feature_arcs}
    for features in arc_features.values():
        for i in range(len(features)):
            for j in range(i + 1, len(features)):
                adj[features[i]].add(features[j])
                adj[features[j]].add(features[i])

    # Greedy colouring — process most-constrained features first.
    sorted_keys = sorted(feature_arcs, key=lambda k: -len(adj[k]))
    color_map = {}
    for key in sorted_keys:
        neighbour_colors = {
            color_map[nb] for nb in adj[key] if nb in color_map
        }
        for color in _LK_PALETTE:
            if color not in neighbour_colors:
                color_map[key] = color
                break
        else:
            color_map[key] = _LK_PALETTE[
                0
            ]  # fallback (shouldn't happen for planar graphs)
    return color_map


def _render_to_axes(ax, objects, arcs, color_map, xlim, ylim, title):
    """Render one map panel onto *ax*."""
    for patch in _geom_patches(objects, arcs, color_map):
        ax.add_patch(patch)
    ax.set_xlim(xlim)
    ax.set_ylim(ylim)
    ax.set_aspect("equal")
    ax.set_title(title, fontsize=11)
    ax.axis("off")


def _geom_patches(objects, arcs, color_map):
    """
    Build a list of matplotlib PathPatch objects for all polygon geometries.
    Each feature gets a consistent color from color_map (keyed by feature id/index).
    """
    patches = []
    for obj in objects.values():
        for idx, geom in enumerate(obj.get("geometries", [])):
            geo_type = geom.get("type")
            feature_key = geom.get("id", idx)
            color = color_map[feature_key]

            if geo_type == "Polygon":
                ring_groups = [geom.get("arcs", [])]
            elif geo_type == "MultiPolygon":
                ring_groups = geom.get("arcs", [])
            else:
                continue

            for rings in ring_groups:
                verts, codes = [], []
                for ring in rings:
                    pts = _ring_points(ring, arcs)
                    if len(pts) < 3:
                        continue
                    closed = pts + [pts[0]]
                    verts.extend(closed)
                    codes += (
                        [mpath.Path.MOVETO]
                        + [mpath.Path.LINETO] * (len(closed) - 2)
                        + [mpath.Path.CLOSEPOLY]
                    )
                if verts:
                    path = mpath.Path(verts, codes)
                    patch = mpatches.PathPatch(
                        path,
                        facecolor=color,
                        edgecolor="white",
                        linewidth=0.3,
                        alpha=0.85,
                    )
                    patches.append(patch)
    return patches


def plot_comparison(
    orig_objects, orig_arcs, filt_objects, oct_arcs, plot_path
):
    """Save a side-by-side plot of the original and octilinearized maps."""
    # Assign each feature a random color, shared across both panels.
    all_keys = set()
    for obj in orig_objects.values():
        for idx, geom in enumerate(obj.get("geometries", [])):
            all_keys.add(geom.get("id", idx))

    rng = random.Random(42)
    color_map = {
        k: (rng.random(), rng.random(), rng.random()) for k in all_keys
    }

    # Compute bounds from original arcs for consistent, correct view limits.
    all_xs = [pt[0] for arc in orig_arcs for pt in arc]
    all_ys = [pt[1] for arc in orig_arcs for pt in arc]
    xlim = (min(all_xs), max(all_xs))
    ylim = (min(all_ys), max(all_ys))

    fig, (ax_orig, ax_octi) = plt.subplots(1, 2, figsize=(14, 7))

    for objects, arcs, ax, title in [
        (orig_objects, orig_arcs, ax_orig, "Original"),
        (filt_objects, oct_arcs, ax_octi, "Octilinear"),
    ]:
        for patch in _geom_patches(objects, arcs, color_map):
            ax.add_patch(patch)
        ax.set_xlim(xlim)
        ax.set_ylim(ylim)
        ax.set_aspect("equal")
        ax.set_title(title)
        ax.axis("off")

    fig.tight_layout()
    plt.savefig(plot_path, dpi=150)
    plt.close(fig)


def plot_four_panel(
    orig_objects,
    orig_arcs,
    filt_objects,
    filt_arcs,
    simp_objects,
    simp_arcs,
    oct_objects,
    oct_arcs,
    plot_path,
    angle_step=45.0,
    min_area=0.0,
    segment_length=0.0,
):
    """
    Save a 2×2 combined panel plot and four individual panel images.

    2×2 layout:
      Original        | Filtered   (min_area)
      Simplified (seg)| Snapped    (angle_step°)

    Individual images are saved alongside the combined one, named
    <stem>.original.png, .filtered.png, .simplified.png, .snapped.png.
    """
    color_map = _build_color_map(orig_objects)

    all_xs = [pt[0] for arc in orig_arcs for pt in arc]
    all_ys = [pt[1] for arc in orig_arcs for pt in arc]
    xlim = (min(all_xs), max(all_xs))
    ylim = (min(all_ys), max(all_ys))

    def _fmt(v):
        return format(v, "g")

    panels = [
        (orig_objects, orig_arcs, "Original", "original"),
        (
            filt_objects,
            filt_arcs,
            f"Filtered  (min-area={_fmt(min_area)} km²)",
            "filtered",
        ),
        (
            simp_objects,
            simp_arcs,
            f"Simplified  (seg={_fmt(segment_length * _KM_PER_DEG)} km)",
            "simplified",
        ),
        (
            oct_objects,
            oct_arcs,
            f"Snapped  (angle={_fmt(angle_step)}°)",
            "octilinear",
        ),
    ]

    # Each pipeline run gets its own subfolder; files have short fixed names.
    os.makedirs(plot_path, exist_ok=True)

    # --- 2×2 combined image ---
    fig, axes = plt.subplots(2, 2, figsize=(14, 14))
    ax_grid = [axes[0, 0], axes[0, 1], axes[1, 0], axes[1, 1]]
    for (objects, arcs, title, _), ax in zip(panels, ax_grid):
        _render_to_axes(ax, objects, arcs, color_map, xlim, ylim, title)
    fig.tight_layout()
    combined_path = os.path.join(plot_path, "all.png")
    plt.savefig(combined_path, dpi=150)
    plt.close(fig)
    print(f"Plot  : {combined_path}")

    # --- individual panel images ---
    for objects, arcs, title, label in panels:
        fig, ax = plt.subplots(1, 1, figsize=(7, 7))
        _render_to_axes(ax, objects, arcs, color_map, xlim, ylim, title)
        fig.tight_layout()
        panel_path = os.path.join(plot_path, f"{label}.png")
        plt.savefig(panel_path, dpi=150)
        plt.close(fig)
        print(f"Plot  : {panel_path}")


# ---------------------------------------------------------------------------
# Step 1 – Filter small polygons
# ---------------------------------------------------------------------------


def filter_topojson(input_path, output_path, min_area):
    """
    Load *input_path*, remove every sub-polygon (including MultiPolygon
    members) whose area in the **original** arcs is below *min_area* km²,
    and write the result to *output_path*.

    The output retains the original transform and arc data unchanged;
    only the objects/geometries list is filtered.
    """
    with open(input_path) as fh:
        topo = json.load(fh)

    decoded = decode_arcs(topo["arcs"], topo.get("transform"))
    objects = filter_small_geometries(topo["objects"], decoded, min_area)

    new_topo = {**topo, "objects": objects}

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w") as fh:
        json.dump(new_topo, fh)

    print(
        f"[filter]   {os.path.basename(input_path)}: "
        f"min_area={min_area} km²"
    )
    print(f"  Output: {output_path}")

    return topo, decoded, objects


# ---------------------------------------------------------------------------
# Step 2 – Simplification
# ---------------------------------------------------------------------------


def simplify_topojson(input_path, output_path, segment_length):
    """
    Load *input_path*, resample every arc to ~*segment_length* chord length,
    and write the result to *output_path*.

    Sub-polygons that become degenerate (area ≈ 0) after resampling are
    dropped so that tiny features that survived the area filter but shrink
    to nothing at the chosen segment length are still removed.

    The output uses absolute float coordinates (no transform).
    """
    with open(input_path) as fh:
        topo = json.load(fh)

    decoded = decode_arcs(topo["arcs"], topo.get("transform"))
    simplified_arcs = [simplify_arc(arc, segment_length) for arc in decoded]

    # Drop any polygon that collapsed to zero (or near-zero) area after
    # resampling.  Using a small positive epsilon ensures that rings that
    # simplify down to fewer than 3 points (area == 0 by the shoelace
    # formula) are removed rather than carried as invisible ghost polygons.
    objects = filter_small_geometries(
        topo["objects"], simplified_arcs, min_area=1e-6
    )

    new_topo = {
        "type": "Topology",
        "objects": objects,
        "arcs": simplified_arcs,
    }
    if "bbox" in topo:
        new_topo["bbox"] = topo["bbox"]

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w") as fh:
        json.dump(new_topo, fh)

    orig_segs = sum(len(a) - 1 for a in decoded)
    simp_segs = sum(len(a) - 1 for a in simplified_arcs)
    print(
        f"[simplify] {os.path.basename(input_path)}: "
        f"{orig_segs} → {simp_segs} segments  "
        f"(segment_length={segment_length})"
    )
    print(f"  Output: {output_path}")

    return decoded, objects, simplified_arcs


# ---------------------------------------------------------------------------
# Step 2 – Octilinearization
# ---------------------------------------------------------------------------


def octilinearize_topojson(input_path, output_path, angle_step=45.0):
    """
    Load *input_path* (a simplified TopoJSON with absolute float coordinates),
    snap every arc segment to a multiple of *angle_step* degrees, and write
    the result to *output_path*.
    """
    with open(input_path) as fh:
        topo = json.load(fh)

    # Simplified files have no transform – decode_arcs handles that gracefully.
    decoded = decode_arcs(topo["arcs"], topo.get("transform"))

    arc_interior_pts = _assign_arc_interior_pts(topo["objects"], decoded)
    oct_arcs = [
        octilinearize_arc(
            arc,
            interior_pt=arc_interior_pts.get(i, [arc[0][0], arc[0][1]]),
            angle_step=angle_step,
        )
        for i, arc in enumerate(decoded)
    ]

    new_topo = {
        "type": "Topology",
        "objects": topo["objects"],
        "arcs": oct_arcs,
    }
    if "bbox" in topo:
        new_topo["bbox"] = topo["bbox"]

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w") as fh:
        json.dump(new_topo, fh)

    simp_segs = sum(len(a) - 1 for a in decoded)
    oct_segs = sum(len(a) - 1 for a in oct_arcs)
    print(
        f"[snapped/{angle_step}°] {os.path.basename(input_path)}: "
        f"{simp_segs} → {oct_segs} segments"
    )
    print(f"  Output: {output_path}")

    return decoded, topo["objects"], oct_arcs


# ---------------------------------------------------------------------------
# Main processing – orchestrates both steps + plotting
# ---------------------------------------------------------------------------


def process_file(input_path, repo_root, segment_length, min_area, angle_step):
    """Run all pipeline steps for a single input TopoJSON file."""
    base = os.path.splitext(os.path.basename(input_path))[0]

    def _fmt(v):
        """Format a float param value without trailing zeros."""
        return format(v, "g")

    filt_stem = f"{base}.min-area-{_fmt(min_area)}"
    simp_stem = f"{filt_stem}.seg-{_fmt(segment_length)}"
    oct_stem = f"{simp_stem}.angle-{_fmt(angle_step)}"

    filt_path = os.path.join(
        repo_root, "data", "generate", "filtered", filt_stem + ".topojson"
    )
    simp_path = os.path.join(
        repo_root, "data", "generate", "simplified", simp_stem + ".topojson"
    )
    oct_path = os.path.join(
        repo_root, "data", "generate", "octilinear", oct_stem + ".topojson"
    )
    # plot_path is a directory; images/octilinear/<oct_stem>/
    plot_path = os.path.join(repo_root, "images", "octilinear", oct_stem)

    print(f"\n=== {base} ===")

    # Step 1 – filter
    orig_topo, orig_decoded, filt_objects = filter_topojson(
        input_path, filt_path, min_area
    )

    # Step 2 – simplify (reads filtered file)
    filt_decoded, simp_objects, simp_arcs = simplify_topojson(
        filt_path, simp_path, segment_length
    )

    # Step 3 – snap to angle grid
    _, oct_objects, oct_arcs = octilinearize_topojson(
        simp_path, oct_path, angle_step
    )

    # 4-panel image
    plot_four_panel(
        orig_topo["objects"],
        orig_decoded,
        filt_objects,
        filt_decoded,
        simp_objects,
        simp_arcs,
        oct_objects,
        oct_arcs,
        plot_path,
        angle_step=angle_step,
        min_area=min_area,
        segment_length=segment_length,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Two-step pipeline: simplify then octilinearize TopoJSON files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "input",
        nargs="*",
        help=(
            "Path(s) to input TopoJSON file(s).  "
            "Defaults to all *.topojson files under data/original/."
        ),
    )
    parser.add_argument(
        "--segment-length",
        type=float,
        default=0.02,
        metavar="DEG",
        help=(
            "Target arc-segment length in degrees for the simplification step. "
            "Default: 0.02 (≈ 2 km)."
        ),
    )
    parser.add_argument(
        "--min-area",
        type=float,
        default=1.0,
        metavar="AREA",
        help="Remove polygons with area (km²) below this value. Default: 1.",
    )
    parser.add_argument(
        "--angle-step",
        type=float,
        default=45.0,
        metavar="DEG",
        help=(
            "Angular resolution for snapping in degrees. "
            "Must divide 360 evenly (e.g. 45, 60, 30, 90). Default: 45."
        ),
    )
    args = parser.parse_args()

    if 360.0 % args.angle_step != 0:
        parser.error(
            f"--angle-step {args.angle_step} does not divide 360 evenly."
        )

    _repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    if args.input:
        input_files = args.input
    else:
        orig_dir = os.path.join(_repo_root, "data", "original")
        input_files = sorted(
            os.path.join(orig_dir, f)
            for f in os.listdir(orig_dir)
            if f.endswith(".topojson")
        )
        if not input_files:
            print(f"No *.topojson files found in {orig_dir}", file=sys.stderr)
            sys.exit(1)

    for path in input_files:
        process_file(
            path,
            _repo_root,
            args.segment_length,
            args.min_area,
            args.angle_step,
        )
