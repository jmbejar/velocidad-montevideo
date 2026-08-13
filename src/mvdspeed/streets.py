"""Sensors matched onto the avenues they actually measure.

The heat surface paints an *area* around each sensor, which overstates what a
point detector knows: it watches one stretch of one road, not the neighbourhood
around it. This module attaches every sensor to a real road corridor so the
value can be drawn along the road instead.

The join is **proximity first, name second**. Matching `Bv. Artgias` to
`Bulevar General Artigas` from the string alone is hard; deciding which of the
three roads meeting at a known coordinate a sensor sits on is easy, and that is
all the name has to do here. A site with no confident name match keeps no
corridor at all rather than being snapped to whatever road is nearest -- painting
an entire avenue from a sensor that was really watching the cross street is a
worse failure than leaving that sensor as a dot.

Distances are plain euclidean, but only ever taken *within one corridor*, which
is what lets them stand in for distance along the road without a routing graph.
The approximation costs something only where a corridor doubles back on itself
within the cutoff -- the Rambla rounding Punta Carretas is the real case.

Deliberately free of colour and Streamlit so it can be checked on its own.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

import numpy as np
import pandas as pd

from mvdspeed.config import (
    KM_PER_DEG_LAT,
    STREET_BANDWIDTH_KM,
    STREET_CHUNK_M,
    STREET_CUTOFF_KM,
    STREET_NAME_FLOOR,
    STREET_SNAP_M,
    STREET_STUB_KM,
    km_per_deg_lon,
)

# Words that identify the *kind* of road rather than which road it is. Dropping
# them is what lets "Bv Artigas" reach "Bulevar General Artigas".
_GENERIC = frozenset(
    """
    avenida av avda bulevar bulevard bv boulevard camino cno calle ruta pasaje
    continuacion acceso nacional brigadier general gral doctor dr ingeniero ing
    arquitecto arq teniente tte coronel cnel capitan presidente profesor prof
    maestro don de del la el los las y
    """.split()
)

# Reach models: how far along its corridor one sensor's reading is drawn.
BLEND = "blend"
NEAREST = "nearest"
STUB = "stub"
REACH_MODELS = (BLEND, NEAREST, STUB)


def _tokens(name: str) -> list[str]:
    stripped = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return [t for t in re.split(r"[^a-z0-9]+", stripped.lower()) if t]


def normalize(name: str) -> tuple[str, ...]:
    """The identifying tokens of a street name, generic road words removed.

    If a name is *entirely* generic the full token list is kept instead of an
    empty key: "Rambla" is a real street name in Montevideo even though `rambla`
    is a road type, and emptying it would leave 20 sensors unmatchable.
    """
    tokens = _tokens(name)
    identifying = [t for t in tokens if t not in _GENERIC]
    return tuple(identifying or tokens)


def _token_score(token: str, against: tuple[str, ...]) -> float:
    if token in against:
        return 1.0
    # Sensor names abbreviate given names to initials: `L A de Herrera` has to
    # reach `Avenida Luis Alberto de Herrera`.
    if len(token) == 1 and any(other.startswith(token) for other in against):
        return 0.7
    # Near-misses on a long token are typos, not different streets: `artgias`
    # against `artigas` scores 0.86 here, while `colonia` against `colorado`
    # only reaches 0.67 and is correctly rejected.
    if len(token) >= 5:
        best = max(
            (SequenceMatcher(None, token, other).ratio() for other in against),
            default=0.0,
        )
        if best >= 0.85:
            return 0.9
    return 0.0


def name_score(sensor_name: str, road_name: str) -> float:
    """How well a sensor's street name matches a road name, in 0-1.

    Asymmetric on purpose: every token the *sensor* offers should be accounted
    for, while the road is free to carry extra words the feed omits. "Rambla"
    therefore scores 1.0 against "Rambla República Argentina", and proximity is
    left to choose which stretch of rambla is meant.
    """
    sensor = normalize(sensor_name)
    road = normalize(road_name)
    if not sensor or not road:
        return 0.0
    return sum(_token_score(t, road) for t in sensor) / len(sensor)


# --- geometry -----------------------------------------------------------------

def to_km(lon: np.ndarray, lat: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Degrees to a local flat km frame, good enough across a single city."""
    return np.asarray(lon, dtype=float) * km_per_deg_lon(), np.asarray(
        lat, dtype=float
    ) * KM_PER_DEG_LAT


def chunk_way(coords: list[tuple[float, float]]) -> list[tuple[float, float, float, float]]:
    """Split a way's polyline into drawable pieces of at most STREET_CHUNK_M.

    A way's own nodes would do, except that OSM digitizes a straight kilometre
    with two of them and a chunk is drawn as one flat colour.
    """
    limit_km = STREET_CHUNK_M / 1000.0
    pieces: list[tuple[float, float, float, float]] = []
    for (lon0, lat0), (lon1, lat1) in zip(coords, coords[1:]):
        (x0, x1), (y0, y1) = to_km(np.array([lon0, lon1]), np.array([lat0, lat1]))
        length = float(np.hypot(x1 - x0, y1 - y0))
        steps = max(1, int(np.ceil(length / limit_km)))
        for i in range(steps):
            a, b = i / steps, (i + 1) / steps
            pieces.append(
                (
                    lon0 + (lon1 - lon0) * a,
                    lat0 + (lat1 - lat0) * a,
                    lon0 + (lon1 - lon0) * b,
                    lat0 + (lat1 - lat0) * b,
                )
            )
    return pieces


def _components(ways: list[dict], cell_km: float = 0.1) -> list[int]:
    """Group same-named ways into connected corridors by spatial adjacency.

    Two unrelated streets can share a name at opposite ends of the city, and
    merging them would let one sensor's colour teleport across town. Endpoint
    matching alone is not enough either: a dual carriageway like Bv Artigas is
    two parallel ways that never touch. So ways are joined when their vertices
    land in the same (or an adjoining) ~100 m cell.
    """
    parent = list(range(len(ways)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[max(ri, rj)] = min(ri, rj)

    occupants: dict[tuple[int, int], list[int]] = {}
    cells_of: list[set[tuple[int, int]]] = []
    for index, way in enumerate(ways):
        lon = np.array([c[0] for c in way["coords"]])
        lat = np.array([c[1] for c in way["coords"]])
        x, y = to_km(lon, lat)
        cells = {
            (int(np.floor(cx / cell_km)), int(np.floor(cy / cell_km)))
            for cx, cy in zip(x, y)
        }
        cells_of.append(cells)
        for cell in cells:
            occupants.setdefault(cell, []).append(index)

    for index, cells in enumerate(cells_of):
        for cx, cy in cells:
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for other in occupants.get((cx + dx, cy + dy), ()):
                        union(index, other)

    return [find(i) for i in range(len(ways))]


def build_chunks(ways: list[dict]) -> pd.DataFrame:
    """Turn raw OSM ways into one row per drawable chunk, grouped into corridors.

    `ways` are dicts of `{"name": str, "coords": [(lon, lat), ...]}`. Corridors
    are formed per normalized name, then split into spatially connected pieces.
    """
    rows = []
    by_key: dict[tuple[str, ...], list[dict]] = {}
    for way in ways:
        if len(way["coords"]) < 2:
            continue
        by_key.setdefault(normalize(way["name"]), []).append(way)

    corridor_id = 0
    for key, group in sorted(by_key.items()):
        labels = _components(group)
        for label in sorted(set(labels)):
            members = [w for w, lab in zip(group, labels) if lab == label]
            # The most frequent spelling stands for the corridor in tooltips.
            name = pd.Series([w["name"] for w in members]).mode().iloc[0]
            for way in members:
                for lon0, lat0, lon1, lat1 in chunk_way(way["coords"]):
                    rows.append(
                        (corridor_id, name, " ".join(key), lon0, lat0, lon1, lat1)
                    )
            corridor_id += 1

    chunks = pd.DataFrame(
        rows,
        columns=["corridor_id", "name", "key", "lon0", "lat0", "lon1", "lat1"],
    )
    chunks["mid_lon"] = (chunks["lon0"] + chunks["lon1"]) / 2
    chunks["mid_lat"] = (chunks["lat0"] + chunks["lat1"]) / 2
    return chunks


def _segment_distance_km(
    px: np.ndarray, py: np.ndarray, chunks: pd.DataFrame
) -> np.ndarray:
    """Distance from each point to each chunk, as an (n_points, n_chunks) array.

    To the segment rather than to its midpoint: at a 60 m chunk the midpoint is
    up to 30 m out, which is a quarter of the snapping radius.
    """
    ax, ay = to_km(chunks["lon0"].to_numpy(), chunks["lat0"].to_numpy())
    bx, by = to_km(chunks["lon1"].to_numpy(), chunks["lat1"].to_numpy())

    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    length2 = np.where(length2 > 0, length2, 1e-12)

    rel_x = px[:, None] - ax[None, :]
    rel_y = py[:, None] - ay[None, :]
    t = np.clip((rel_x * dx[None, :] + rel_y * dy[None, :]) / length2[None, :], 0.0, 1.0)
    return np.hypot(rel_x - t * dx[None, :], rel_y - t * dy[None, :])


def assign_corridors(sites: pd.DataFrame, chunks: pd.DataFrame) -> pd.Series:
    """Pick the corridor each site sits on, or <NA> where none is confident.

    Every corridor passing within STREET_SNAP_M is a candidate; the name decides
    between them, and distance only breaks ties. That ordering matters: a sensor
    at an intersection is near both streets, and the nearer one is as often the
    cross street as the one it is named for.
    """
    if sites.empty or chunks.empty:
        return pd.Series(pd.NA, index=sites.index, dtype="Int64")

    px, py = to_km(sites["lon"].to_numpy(), sites["lat"].to_numpy())
    distance = _segment_distance_km(px, py, chunks)

    corridor = chunks["corridor_id"].to_numpy()
    names = chunks.groupby("corridor_id")["name"].first()
    snap_km = STREET_SNAP_M / 1000.0

    # Nearest approach of every corridor to every site.
    n_corridors = int(corridor.max()) + 1
    nearest = np.full((len(sites), n_corridors), np.inf)
    np.minimum.at(nearest.T, corridor, distance.T)

    scores: dict[tuple[str, int], float] = {}
    chosen: list[object] = []
    for row, street in enumerate(sites["street"].to_numpy()):
        candidates = np.flatnonzero(nearest[row] <= snap_km)
        best, best_score, best_distance = pd.NA, 0.0, np.inf
        for cid in candidates:
            cache_key = (street, int(cid))
            if cache_key not in scores:
                scores[cache_key] = name_score(street, names.loc[int(cid)])
            score = scores[cache_key]
            gap = nearest[row, cid]
            if score > best_score or (score == best_score and gap < best_distance):
                best, best_score, best_distance = int(cid), score, gap
        chosen.append(best if best_score >= STREET_NAME_FLOOR else pd.NA)

    return pd.Series(chosen, index=sites.index, dtype="Int64")


# --- the three reach models ---------------------------------------------------

def street_field(
    chunks: pd.DataFrame,
    sites: pd.DataFrame,
    column: str,
    *,
    mode: str = BLEND,
    bandwidth_km: float = STREET_BANDWIDTH_KM,
    cutoff_km: float = STREET_CUTOFF_KM,
    stub_km: float = STREET_STUB_KM,
) -> tuple[np.ndarray, np.ndarray]:
    """Value and support for every chunk, from the sites on its own corridor.

    `sites` must carry a `corridor_id`. Returns two arrays aligned with `chunks`:
    the value to colour with (NaN where nothing supports the chunk) and the
    support to fade with. The three modes differ only in how far one reading
    reaches:

    - BLEND   a Gaussian-weighted mean of the corridor's sensors, the surface's
              expression restricted to one road. Smooth, and it interpolates
              between sensors.
    - NEAREST the closest sensor takes the stretch outright, handing over at the
              midpoint. Every painted metre is then one real measurement.
    - STUB    the same, but only within `stub_km` of the sensor, so the road is
              painted only where it was actually measured.

    Support is the kernel weight either way -- summed for BLEND, the single
    nearest sensor's for the other two -- so opacity keeps meaning the same thing
    across the three.
    """
    if mode not in REACH_MODELS:
        raise ValueError(f"unknown reach model {mode!r}, expected one of {REACH_MODELS}")

    values = np.full(len(chunks), np.nan)
    support = np.zeros(len(chunks))
    if chunks.empty or sites.empty or "corridor_id" not in sites:
        return values, support

    numeric = pd.to_numeric(sites[column], errors="coerce")
    usable = sites.loc[numeric.notna() & sites["corridor_id"].notna()]
    if usable.empty:
        return values, support
    readings = numeric.loc[usable.index].to_numpy(dtype=float)

    reach = stub_km if mode == STUB else cutoff_km
    positions = {cid: rows for cid, rows in chunks.groupby("corridor_id").indices.items()}

    for cid, site_rows in usable.groupby("corridor_id").indices.items():
        rows = positions.get(int(cid))
        if rows is None:
            continue
        sx, sy = to_km(
            usable["lon"].to_numpy()[site_rows], usable["lat"].to_numpy()[site_rows]
        )
        cx, cy = to_km(
            chunks["mid_lon"].to_numpy()[rows], chunks["mid_lat"].to_numpy()[rows]
        )
        distance = np.hypot(cx[:, None] - sx[None, :], cy[:, None] - sy[None, :])
        weights = np.exp(-((distance / bandwidth_km) ** 2))
        weights[distance > reach] = 0.0

        local = readings[site_rows]
        if mode == BLEND:
            total = weights.sum(axis=1)
            backed = total > 0
            estimate = np.full(total.shape, np.nan)
            np.divide(
                (weights * local[None, :]).sum(axis=1), total,
                out=estimate, where=backed,
            )
            values[rows], support[rows] = estimate, total
        else:
            closest = np.argmin(distance, axis=1)
            picked = weights[np.arange(len(rows)), closest]
            backed = picked > 0
            values[rows] = np.where(backed, local[closest], np.nan)
            support[rows] = picked

    return values, support
