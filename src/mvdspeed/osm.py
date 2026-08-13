"""OSM road geometry for the street layer, fetched once and committed.

Run by hand, not on app start:

    uv run mvdspeed-osm            # uses data/osm/*.json if already fetched
    uv run mvdspeed-osm --refresh  # re-queries Overpass

Two reasons this is an offline step rather than a runtime call. The dashboard's
only network dependency is the basemap CDN and it should stay that way; and the
public Overpass endpoints are genuinely unreliable -- building this took several
rounds of timeouts across four mirrors, which is not something to put between a
user and a map that otherwise loads in a second.

The raw responses are cached under data/osm/ (gitignored, ~11 MB) and the
derived chunk table is committed (data/streets.parquet, small), so a clone can
draw the layer without ever touching Overpass.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import pandas as pd

from mvdspeed import data as mvd
from mvdspeed import streets
from mvdspeed.config import (
    DATA_OSM,
    OSM_BBOX,
    OSM_HIGHWAY_CLASSES,
    STREETS_PARQUET,
)

# Tried in order, then round-robin again: on any given day some of these are
# refusing work while others answer immediately.
ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.osm.jp/api/interpreter",
)
TIMEOUT_S = 300
ATTEMPTS = 3


def _query(highway_classes: str) -> str:
    south, west, north, east = OSM_BBOX
    return (
        f"[out:json][timeout:{TIMEOUT_S}];"
        f'way["highway"~"^({highway_classes})$"]["name"]'
        f"({south},{west},{north},{east});"
        f"out geom;"
    )


def fetch(name: str, highway_classes: str, *, refresh: bool = False) -> dict:
    """Fetch one highway class group, caching the raw response.

    The classes are queried in groups rather than all at once because the
    combined query times out on every mirror while each group returns fine.
    """
    cache = DATA_OSM / f"overpass-{name}.json"
    if cache.exists() and not refresh:
        return json.loads(cache.read_text())

    DATA_OSM.mkdir(parents=True, exist_ok=True)
    body = urllib.parse.urlencode({"data": _query(highway_classes)}).encode()
    for attempt in range(ATTEMPTS):
        for endpoint in ENDPOINTS:
            print(f"  {name}: {endpoint} (attempt {attempt + 1})", file=sys.stderr)
            try:
                request = urllib.request.Request(
                    endpoint, data=body, headers={"User-Agent": "mvdspeed/0.1"}
                )
                with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
                    payload = json.loads(response.read())
            except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
                print(f"    {type(exc).__name__}: {exc}", file=sys.stderr)
                continue
            if payload.get("elements"):
                cache.write_text(json.dumps(payload))
                print(f"    {len(payload['elements'])} ways", file=sys.stderr)
                return payload
            print("    empty response", file=sys.stderr)
            time.sleep(20)
    raise RuntimeError(
        f"Overpass returned nothing for {name!r} after {ATTEMPTS} rounds over "
        f"{len(ENDPOINTS)} endpoints. Try again later; the cache under "
        f"{DATA_OSM} means this only has to succeed once."
    )


def load_ways(*, refresh: bool = False) -> list[dict]:
    """Every named road in the bbox, as {"name", "coords"} dicts."""
    ways = []
    for name, classes in OSM_HIGHWAY_CLASSES.items():
        payload = fetch(name, classes, refresh=refresh)
        for element in payload["elements"]:
            geometry = element.get("geometry")
            label = element.get("tags", {}).get("name")
            if not geometry or not label:
                continue
            ways.append(
                {
                    "name": label,
                    "coords": [(point["lon"], point["lat"]) for point in geometry],
                }
            )
    return ways


def build(*, refresh: bool = False) -> pd.DataFrame:
    """Chunk the road network and keep only the corridors sensors sit on."""
    ways = load_ways(refresh=refresh)
    print(f"{len(ways)} named ways", file=sys.stderr)

    chunks = streets.build_chunks(ways)
    print(
        f"{len(chunks)} chunks in {chunks['corridor_id'].nunique()} corridors",
        file=sys.stderr,
    )

    sites = mvd.load().sites
    mappable = sites[sites["is_usable"] & sites["has_location"]]
    assigned = streets.assign_corridors(mappable, chunks)
    claimed = set(assigned.dropna().astype(int))
    print(
        f"{assigned.notna().sum()}/{len(mappable)} sites matched to "
        f"{len(claimed)} corridors",
        file=sys.stderr,
    )

    missed = mappable.loc[assigned.isna(), "street"].value_counts()
    if not missed.empty:
        print("unmatched street names:", file=sys.stderr)
        for street, count in missed.items():
            print(f"  {count:3d}  {street}", file=sys.stderr)

    kept = chunks[chunks["corridor_id"].isin(claimed)].copy()
    # Renumber so the ids stay dense after dropping the unmeasured network.
    kept["corridor_id"] = kept["corridor_id"].map(
        {old: new for new, old in enumerate(sorted(claimed))}
    )
    return kept.reset_index(drop=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--refresh", action="store_true", help="re-query Overpass instead of the cache"
    )
    args = parser.parse_args()

    kept = build(refresh=args.refresh)
    kept.to_parquet(STREETS_PARQUET, index=False)
    size_kb = STREETS_PARQUET.stat().st_size / 1024
    print(
        f"wrote {STREETS_PARQUET} — {len(kept)} chunks, "
        f"{kept['corridor_id'].nunique()} corridors, {size_kb:.0f} KB",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
