"""Turn the raw 262 MB / 3.1 M-row CSV into two small parquet files.

The raw file is one row per detector (lane) every 5 minutes. That is far more
resolution than a time-of-day map needs, so we pre-aggregate into 30-minute
buckets keeping *sums and counts* rather than averages -- means computed later
from those are exact, and the app can still choose how to treat zero readings.

Run once:  uv run mvdspeed-etl /path/to/Velocidad_promedio_Agosto_2026.csv
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb

from mvdspeed.config import (
    BUCKET_MINUTES,
    DATA_PROCESSED,
    DETECTORS_PARQUET,
    FREE_FLOW_PERCENTILE,
    MAX_PLAUSIBLE_SPEED,
    MAX_STREETS_PER_COORD,
    MEASUREMENTS_PARQUET,
    STALLED_DAY_HOURS,
    STALLED_NIGHT_HOURS,
)

# The published column docs say `id_detector` / `velocidad_promedio`, the actual
# file header says `cod_detector` / `velocidad`. Accept either.
COLUMN_ALIASES = {
    "detector": ("cod_detector", "id_detector"),
    "speed": ("velocidad", "velocidad_promedio"),
}


def _resolve(columns: set[str], key: str) -> str:
    for candidate in COLUMN_ALIASES[key]:
        if candidate in columns:
            return candidate
    raise SystemExit(
        f"CSV has none of the expected {key} columns "
        f"{COLUMN_ALIASES[key]}; found: {sorted(columns)}"
    )


def build(csv_path: Path, con: duckdb.DuckDBPyConnection | None = None) -> None:
    if not csv_path.exists():
        raise SystemExit(f"CSV not found: {csv_path}")

    DATA_PROCESSED.mkdir(parents=True, exist_ok=True)
    con = con or duckdb.connect()

    header = con.execute(
        "SELECT * FROM read_csv_auto(?, sample_size=1000) LIMIT 0", [str(csv_path)]
    )
    columns = {d[0] for d in header.description}
    detector_col = _resolve(columns, "detector")
    speed_col = _resolve(columns, "speed")

    print(f"Reading {csv_path} ...", flush=True)
    con.execute(
        f"""
        CREATE OR REPLACE TABLE clean AS
        SELECT
            {detector_col}                              AS detector_id,
            id_carril                                   AS lane,
            fecha                                       AS date,
            hora                                        AS time,
            trim(dsc_avenida)                           AS street,
            trim(dsc_int_anterior)                      AS from_street,
            trim(dsc_int_siguiente)                     AS to_street,
            latitud                                     AS lat,
            longitud                                    AS lon,
            {speed_col}                                 AS speed
        FROM read_csv_auto(?, header=true)
        WHERE latitud IS NOT NULL AND longitud IS NOT NULL
        """,
        [str(csv_path)],
    )

    # Three different things hide in this column and only the first is a
    # measurement:
    #   speed > 0            a real average speed
    #   speed = 0            no vehicle crossed the lane, or traffic is stopped
    #   speed IS NULL        the sensor reported nothing at all (15.6% of rows)
    #   speed > 120          physically implausible on a city street
    total, n_moving, n_zero, n_missing, n_error = con.execute(
        f"""
        SELECT count(*),
               count(*) FILTER (speed > 0 AND speed <= {MAX_PLAUSIBLE_SPEED}),
               count(*) FILTER (speed = 0),
               count(*) FILTER (speed IS NULL),
               count(*) FILTER (speed > {MAX_PLAUSIBLE_SPEED})
        FROM clean
        """
    ).fetchone()
    print(
        f"  {total:,} rows read\n"
        f"    {n_moving:,} ({n_moving / total:.1%}) real speed measurements\n"
        f"    {n_zero:,} ({n_zero / total:.1%}) zeros (no vehicle, or stopped -- kept separately)\n"
        f"    {n_missing:,} ({n_missing / total:.1%}) empty (sensor reported nothing -- dropped)\n"
        f"    {n_error:,} ({n_error / total:.1%}) above {MAX_PLAUSIBLE_SPEED} km/h (sensor error -- dropped)",
        flush=True,
    )

    # A site is a physical measuring point: several lane detectors share one
    # coordinate pair, and stacking them on a map would just overplot.
    con.execute(
        """
        CREATE OR REPLACE TABLE sites AS
        SELECT
            dense_rank() OVER (ORDER BY lat, lon, street, from_street, to_street)
                AS site_id,
            lat, lon, street, from_street, to_street
        FROM (SELECT DISTINCT lat, lon, street, from_street, to_street FROM clean)
        """
    )

    # How many distinct streets claim each coordinate -- the tell for the feed's
    # placeholder location. See MAX_STREETS_PER_COORD.
    con.execute(
        """
        CREATE OR REPLACE TABLE coord_streets AS
        SELECT lat, lon, count(DISTINCT street) AS streets_at_coord
        FROM sites
        GROUP BY lat, lon
        """
    )

    con.execute(
        f"""
        CREATE OR REPLACE TABLE valid AS
        SELECT c.*, s.site_id,
               -- bucket index within the day: 0 .. (1440/BUCKET_MINUTES - 1)
               (datepart('hour', c.time) * 60 + datepart('minute', c.time))
                   // {BUCKET_MINUTES} AS bucket
        FROM clean c
        JOIN sites s USING (lat, lon, street, from_street, to_street)
        WHERE c.speed IS NOT NULL AND c.speed <= {MAX_PLAUSIBLE_SPEED}
        """
    )

    # Daytime vs overnight speed per sensor, weekdays only: the contrast that
    # exposes a detector which is not watching through traffic at all.
    # See STALLED_* in config.
    day_lo, day_hi = (h * 60 // BUCKET_MINUTES for h in STALLED_DAY_HOURS)
    night_lo, night_hi = (h * 60 // BUCKET_MINUTES for h in STALLED_NIGHT_HOURS)
    con.execute(
        f"""
        CREATE OR REPLACE TABLE day_night AS
        SELECT
            site_id,
            avg(speed) FILTER (
                speed > 0 AND bucket >= {day_lo} AND bucket < {day_hi}
            ) AS day_speed,
            avg(speed) FILTER (
                speed > 0 AND bucket >= {night_lo} AND bucket < {night_hi}
            ) AS night_speed
        FROM valid
        WHERE dayofweek(date) BETWEEN 1 AND 5
        GROUP BY site_id
        """
    )

    # Sums + counts, not averages: any later regrouping stays exact.
    con.execute(
        """
        CREATE OR REPLACE TABLE measurements AS
        SELECT
            site_id,
            detector_id,
            lane,
            date,
            bucket,
            dayofweek(date)                                   AS dow,
            sum(speed) FILTER (speed > 0)                     AS speed_sum,
            count(*)   FILTER (speed > 0)                     AS n_moving,
            count(*)   FILTER (speed = 0)                      AS n_zero
        FROM valid
        GROUP BY ALL
        """
    )

    con.execute(
        f"""
        CREATE OR REPLACE TABLE detectors AS
        SELECT
            s.site_id,
            any_value(s.lat)                                  AS lat,
            any_value(s.lon)                                  AS lon,
            any_value(s.street)                                AS street,
            any_value(s.from_street)                           AS from_street,
            any_value(s.to_street)                             AS to_street,
            count(DISTINCT v.detector_id)                      AS n_detectors,
            count(DISTINCT v.lane)                             AS n_lanes,
            count(*)                                           AS n_readings,
            count(*) FILTER (v.speed = 0)                      AS n_zero,
            avg(v.speed) FILTER (v.speed > 0)                  AS mean_speed,
            quantile_cont(v.speed, {FREE_FLOW_PERCENTILE})
                FILTER (v.speed > 0)                           AS free_flow_speed,
            any_value(m.n_missing)                             AS n_missing,
            any_value(cs.streets_at_coord)                     AS streets_at_coord,
            any_value(dn.day_speed)                            AS day_speed,
            any_value(dn.night_speed)                          AS night_speed
        FROM sites s
        JOIN valid v USING (site_id)
        JOIN coord_streets cs ON cs.lat = s.lat AND cs.lon = s.lon
        LEFT JOIN day_night dn USING (site_id)
        JOIN (
            SELECT s2.site_id, count(*) FILTER (c.speed IS NULL) AS n_missing
            FROM clean c
            JOIN sites s2 USING (lat, lon, street, from_street, to_street)
            GROUP BY s2.site_id
        ) m USING (site_id)
        GROUP BY s.site_id
        """
    )

    con.execute(
        f"COPY measurements TO '{MEASUREMENTS_PARQUET}' (FORMAT parquet, COMPRESSION zstd)"
    )
    con.execute(
        f"COPY detectors TO '{DETECTORS_PARQUET}' (FORMAT parquet, COMPRESSION zstd)"
    )

    n_meas, n_sites, n_placeholder = con.execute(
        f"""
        SELECT (SELECT count(*) FROM measurements),
               (SELECT count(*) FROM detectors),
               (SELECT count(*) FROM detectors
                WHERE streets_at_coord > {MAX_STREETS_PER_COORD})
        """
    ).fetchone()
    if n_placeholder:
        print(
            f"  {n_placeholder} site(s) share a placeholder coordinate "
            f"(>{MAX_STREETS_PER_COORD} streets at one point) -- kept, but flagged "
            "as having no usable location",
            flush=True,
        )
    print(
        f"  wrote {n_meas:,} bucket rows over {n_sites:,} sites\n"
        f"    {MEASUREMENTS_PARQUET.relative_to(MEASUREMENTS_PARQUET.parents[2])}"
        f"  ({MEASUREMENTS_PARQUET.stat().st_size / 1e6:.1f} MB)\n"
        f"    {DETECTORS_PARQUET.relative_to(DETECTORS_PARQUET.parents[2])}"
        f"  ({DETECTORS_PARQUET.stat().st_size / 1e6:.1f} MB)",
        flush=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", type=Path, help="path to the raw open-data CSV")
    args = parser.parse_args(argv)
    build(args.csv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
