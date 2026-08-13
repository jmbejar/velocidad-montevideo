"""Hourly Montevideo weather from INUMET, aligned to the speed panel's buckets.

Run after the speed ETL, which is what defines the date range to cover:

    uv run mvdspeed-weather

Four INUMET datasets are published on the same catalogue as the speed data, one
per variable, each a single national CSV covering 2020 onward. They are fetched
whole (~45 MB together), filtered to the one Montevideo station, and pivoted into
one row per hour. See INUMET_* in config.py for the station and its limitations.

The output is a complete hourly grid over the panel's dates: an hour the station
did not report is present with nulls rather than absent, so a gap can never be
read as dry weather.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

import pandas as pd

from mvdspeed.config import (
    DATA_PROCESSED,
    DATA_RAW,
    INUMET_DATASETS,
    INUMET_STATION,
    MEASUREMENTS_PARQUET,
    RAIN_HEAVY_MM,
    RAIN_LAG_HOURS,
    RAIN_WET_MM,
    WEATHER_PARQUET,
)

CKAN_API = "https://catalogodatos.gub.uy/api/3/action/package_show"
TIMEOUT_S = 180
USER_AGENT = "mvdspeed/0.1"

# The national CSVs are cached here so a re-run costs no network. Gitignored with
# the rest of data/raw.
WEATHER_RAW = DATA_RAW / "weather"


def _resource_url(slug: str) -> str:
    """The CSV resource of an INUMET dataset.

    Each of these datasets publishes the same table as CSV, XML and XLSX. The
    `format` field is trustworthy here -- unlike the speed dataset's, where it
    reports "csv zip" -- but the URL is checked anyway.
    """
    request = urllib.request.Request(
        f"{CKAN_API}?id={slug}", headers={"User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            payload = json.loads(response.read())
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
        raise RuntimeError(
            f"could not read the INUMET catalogue entry {slug} "
            f"({type(exc).__name__}: {exc})"
        ) from exc
    if not payload.get("success"):
        raise RuntimeError(f"CKAN reported failure for {slug!r}")

    candidates = [
        r
        for r in payload["result"]["resources"]
        if (r.get("format") or "").upper() == "CSV"
        and r["url"].lower().endswith(".csv")
    ]
    if not candidates:
        raise RuntimeError(f"no CSV resource in {slug!r}")
    if len(candidates) > 1:
        print(
            f"  warning: {slug} offers {len(candidates)} CSV resources, using the first",
            file=sys.stderr,
        )
    return candidates[0]["url"]


def fetch_variable(name: str, *, force: bool = False) -> pd.DataFrame:
    """One INUMET variable at the Montevideo station, as (timestamp, value).

    The national CSV is cached under data/raw/weather/. These files carry blank
    `;;` rows at the top and bottom, and a station column that must be filtered
    before anything else -- there are seven stations in the file and six of them
    are hundreds of kilometres away.
    """
    slug, column = INUMET_DATASETS[name]
    cached = WEATHER_RAW / f"inumet_{name}.csv"
    if force or not cached.exists():
        WEATHER_RAW.mkdir(parents=True, exist_ok=True)
        url = _resource_url(slug)
        print(f"  {name}: downloading ...", end="", flush=True, file=sys.stderr)
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            payload = response.read()
        partial = cached.with_suffix(".csv.part")
        partial.write_bytes(payload)
        partial.rename(cached)
        print(f" {len(payload) / 1e6:.0f} MB", file=sys.stderr)
    else:
        print(f"  {name}: cached", file=sys.stderr)

    frame = pd.read_csv(cached, sep=";", usecols=["fecha", "estacion_id", column])
    frame = frame[frame["estacion_id"] == INUMET_STATION]
    if frame.empty:
        raise SystemExit(
            f"{name}: no rows for station {INUMET_STATION!r}. The station list in "
            f"this file is {sorted(pd.read_csv(cached, sep=';')['estacion_id'].dropna().unique())}"
        )
    frame["timestamp"] = pd.to_datetime(frame["fecha"], errors="coerce")
    frame = frame.dropna(subset=["timestamp"])
    out = (
        frame[["timestamp", column]]
        .rename(columns={column: name})
        .sort_values("timestamp")
    )
    # A station occasionally logs the same hour twice; keep the first so the
    # pivot below cannot fan out.
    return out.drop_duplicates("timestamp", keep="first")


def panel_dates() -> tuple[pd.Timestamp, pd.Timestamp]:
    """The date range the speed panel covers, which is what weather must span."""
    if not MEASUREMENTS_PARQUET.exists():
        raise SystemExit(
            f"{MEASUREMENTS_PARQUET} not found -- run the speed ETL first:\n"
            "  uv run mvdspeed-etl"
        )
    dates = pd.read_parquet(MEASUREMENTS_PARQUET, columns=["date"])["date"]
    return pd.Timestamp(dates.min()), pd.Timestamp(dates.max())


def build(*, force: bool = False) -> pd.DataFrame:
    start, end = panel_dates()
    print(
        f"Weather for {start.date()} to {end.date()}, station {INUMET_STATION}",
        flush=True,
    )

    series = [fetch_variable(name, force=force) for name in INUMET_DATASETS]
    merged = series[0]
    for other in series[1:]:
        merged = merged.merge(other, on="timestamp", how="outer")

    # A complete hourly grid, so a missing hour is explicitly null rather than a
    # row that simply is not there. Reindexing before the lag flag also stops a
    # gap from making two hours look adjacent.
    grid = pd.DataFrame(
        {"timestamp": pd.date_range(start, end + pd.Timedelta(hours=23), freq="h")}
    )
    weather = grid.merge(merged, on="timestamp", how="left")

    # Midnight-normalised datetime rather than a date object or a string: this is
    # half of the join key against the speed panel, and a string/date mismatch
    # there fails by producing an empty merge rather than an error.
    weather["date"] = weather["timestamp"].dt.normalize()
    weather["hour"] = weather["timestamp"].dt.hour.astype("int16")

    rain = weather["precip_mm"]
    weather["is_wet"] = rain >= RAIN_WET_MM
    weather["is_heavy"] = rain >= RAIN_HEAVY_MM
    # Wet road rather than falling rain: an hour that follows rain is not a dry
    # baseline. Includes the hour itself, so `recently_wet` is a superset of
    # `is_wet`. min_periods=1 so the first hours of the panel are usable.
    weather["recently_wet"] = (
        weather["is_wet"]
        .rolling(RAIN_LAG_HOURS + 1, min_periods=1)
        .max()
        .astype(bool)
    )
    # An hour the station did not report is not evidence of anything. Both flags
    # go null so downstream code has to decide explicitly, rather than inheriting
    # a False that reads as "dry".
    unknown = rain.isna()
    for flag in ("is_wet", "is_heavy", "recently_wet"):
        weather[flag] = weather[flag].astype("boolean").mask(unknown)

    columns = [
        "date", "hour", "precip_mm", "temp_c", "humidity_pct", "wind_kmh",
        "is_wet", "is_heavy", "recently_wet",
    ]
    weather = weather[columns]

    DATA_PROCESSED.mkdir(parents=True, exist_ok=True)
    weather.to_parquet(WEATHER_PARQUET, index=False, compression="zstd")

    n = len(weather)
    print(f"  {n:,} hours on the grid", flush=True)
    for name in INUMET_DATASETS:
        have = int(weather[name].notna().sum())
        print(f"    {name:14s} {have:5,} hours ({have / n:.1%})", flush=True)
    wet = int(weather["is_wet"].fillna(False).sum())
    heavy = int(weather["is_heavy"].fillna(False).sum())
    damp = int(weather["recently_wet"].fillna(False).sum())
    print(
        f"  {wet:,} wet hours ({wet / n:.1%}), of which {heavy:,} at or above "
        f"{RAIN_HEAVY_MM:g} mm/h\n"
        f"  {damp:,} hours ({damp / n:.1%}) wet or within {RAIN_LAG_HOURS} h of rain "
        f"-- these are excluded from the dry baseline\n"
        f"  total rainfall {weather['precip_mm'].sum():,.0f} mm\n"
        f"    {WEATHER_PARQUET.relative_to(WEATHER_PARQUET.parents[2])} "
        f"({WEATHER_PARQUET.stat().st_size / 1e3:.0f} kB)",
        flush=True,
    )
    return weather


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force", action="store_true", help="refetch the national CSVs"
    )
    args = parser.parse_args(argv)
    build(force=args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
