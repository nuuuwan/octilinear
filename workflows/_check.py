import json, sys

sys.path.insert(0, "workflows")
import pipeline as P

with open("data/original/districts.topojson") as f:
    topo = json.load(f)
decoded = P.decode_arcs(topo["arcs"], topo.get("transform"))

# Trace what happens to every geometry
for obj in topo["objects"].values():
    for geom in obj["geometries"]:
        gid = geom.get("id", "?")
        gt = geom.get("type")
        result = P._filter_geometry(geom, decoded, 10)
        if result is None:
            print(f"DROPPED: {gid} ({gt})")
        elif result.get("type") != gt:
            print(
                f'TYPE CHANGE: {gid}  {gt} -> {result["type"]}  arcs={len(result["arcs"])}'
            )
        elif gt == "MultiPolygon":
            orig = len(geom["arcs"])
            kept = len(result["arcs"])
            print(f"FILTERED: {gid} MultiPolygon {orig} -> {kept} sub-polys")
        else:
            print(f"OK: {gid} ({gt})")
