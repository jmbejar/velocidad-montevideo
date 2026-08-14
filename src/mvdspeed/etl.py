"""Turn the monthly open-data files into two small parquet files.

The raw files are one row per detector (lane) every 5 minutes -- 8.4 M rows a
month, 63 M across 2026 so far. That is far more resolution than a time-of-day
map needs, so they are pre-aggregated into 30-minute buckets keeping *sums and
counts* rather than averages: means computed later from those are exact, and the
app can still choose how to treat zero readings.

Run over everything sitting in data/raw (fetch it with `mvdspeed-fetch`):

    uv run mvdspeed-etl                        # every month on disk
    uv run mvdspeed-etl --only 2026-07,2026-08
    uv run mvdspeed-etl --from 2026-01

Months are ingested one at a time and each `.zip` is expanded to a scratch file
that is deleted straight after, so peak disk is the archives plus one expanded
month (~800 MB) rather than the ~6 GB a year occupies uncompressed.

Two things about this ETL are panel-wide rather than per-file, and both are the
reason it cannot simply be run once per month and the results concatenated:

  - **Site identity.** A sensor is keyed on its rounded coordinate and its street
    labels, because the publisher rewrote every coordinate to fewer decimal places
    between March and April 2026. See COORD_DECIMALS in config.py.
  - **Every "typical" reference.** Free-flow speed, a sensor's own mean and the
    day/night contrast are computed across all ingested months, so congestion
    means the same thing in January and in August. Free-flow is an exact
    percentile taken from a per-lane speed histogram -- the readings are whole
    km/h, so 121 counts per lane carry the full distribution and the quantile
    needs no sample of the 53 M individual readings.
"""

from __future__ import annotations

import argparse
import re
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

import duckdb

from mvdspeed.config import (
    BUCKET_MINUTES,
    COORD_DECIMALS,
    DATA_PROCESSED,
    DATA_RAW,
    DETECTORS_PARQUET,
    FLATLINE_SPEED,
    FREE_FLOW_PERCENTILE,
    MAX_PLAUSIBLE_SPEED,
    MAX_STREETS_PER_COORD,
    MEASUREMENTS_PARQUET,
    STALLED_DAY_HOURS,
    STALLED_NIGHT_HOURS,
)

# The published column docs say `id_detector` / `velocidad_promedio`, the actual
# file headers say `cod_detector` / `velocidad`. Accept either.
COLUMN_ALIASES = {
    "detector": ("cod_detector", "id_detector"),
    "speed": ("velocidad", "velocidad_promedio"),
}

# `velocidad_2026-07.zip`, as written by mvdspeed-fetch.
_MONTH_FILE = re.compile(r"velocidad_(\d{4}-\d{2})\.(csv|zip)$", re.IGNORECASE)


@dataclass(frozen=True)
class MonthFile:
    ym: str
    path: Path

    @property
    def is_zip(self) -> bool:
        return self.path.suffix.lower() == ".zip"


def month_files(raw_dir: Path = DATA_RAW) -> list[MonthFile]:
    """Every month sitting in data/raw, oldest first."""
    found = []
    for path in sorted(raw_dir.iterdir()):
        match = _MONTH_FILE.search(path.name)
        if match:
            found.append(MonthFile(ym=match.group(1), path=path))
    return sorted(found, key=lambda f: f.ym)


def _resolve(columns: set[str], key: str) -> str:
    for candidate in COLUMN_ALIASES[key]:
        if candidate in columns:
            return candidate
    raise SystemExit(
        f"CSV has none of the expected {key} columns "
        f"{COLUMN_ALIASES[key]}; found: {sorted(columns)}"
    )


def _expand(month: MonthFile, scratch: Path) -> Path:
    """The CSV for a month, expanding the archive if needed.

    The name of the member inside the archive is not predictable -- January 2026
    ships `Autoscope_01_2026_velocidad.csv` and April ships
    `Velocidad_promedio_Abril_2026.csv` -- so the single CSV member is taken
    whatever it is called, and an archive holding none or several is an error
    rather than a guess.
    """
    if not month.is_zip:
        return month.path

    with zipfile.ZipFile(month.path) as archive:
        members = [
            info
            for info in archive.infolist()
            if info.filename.lower().endswith(".csv") and not info.is_dir()
        ]
        if len(members) != 1:
            raise SystemExit(
                f"{month.path.name} holds {len(members)} CSV members "
                f"({[m.filename for m in members]}); expected exactly one"
            )
        target = scratch / f"{month.ym}.csv"
        with archive.open(members[0]) as src, target.open("wb") as dst:
            while chunk := src.read(1 << 22):
                dst.write(chunk)
    return target


def _create_accumulators(con: duckdb.DuckDBPyConnection) -> None:
    """Tables that grow one month at a time.

    Everything here is keyed on `geom_id`, a small integer standing for the
    geometry tuple, rather than on the tuple itself: `lane_bucket` reaches ten
    million rows and carrying three street names down every one of them would
    cost most of a gigabyte for no gain. The deterministic `site_id` is assigned
    at the end, once the full set of geometries is known.
    """
    con.execute("CREATE SEQUENCE geom_seq START 1")
    con.execute(
        """
        CREATE TABLE geom (
            geom_id     BIGINT,
            lat         DOUBLE,
            lon         DOUBLE,
            street      VARCHAR,
            from_street VARCHAR,
            to_street   VARCHAR
        )
        """
    )
    # Bucket-level but still per lane: the stuck-lane test below has to run per
    # lane, so the collapse to site level happens only after it.
    con.execute(
        """
        CREATE TABLE lane_bucket (
            geom_id     BIGINT,
            detector_id BIGINT,
            lane        BIGINT,
            month       VARCHAR,
            date        DATE,
            bucket      SMALLINT,
            dow         SMALLINT,
            speed_sum   BIGINT,
            n_moving    INTEGER,
            n_zero      INTEGER
        )
        """
    )
    # The full speed distribution per lane, as counts. Readings are whole km/h
    # and capped at MAX_PLAUSIBLE_SPEED, so this is at most 120 rows per lane and
    # carries an exact percentile.
    con.execute(
        """
        CREATE TABLE lane_hist (
            geom_id     BIGINT,
            detector_id BIGINT,
            lane        BIGINT,
            speed       SMALLINT,
            n           BIGINT
        )
        """
    )
    con.execute(
        "CREATE TABLE geom_missing (geom_id BIGINT, month VARCHAR, n_missing BIGINT)"
    )
    con.execute(
        """
        CREATE TABLE month_log (
            month     VARCHAR,
            n_rows    BIGINT,
            n_moving  BIGINT,
            n_zero    BIGINT,
            n_missing BIGINT,
            n_error   BIGINT,
            n_dates   BIGINT,
            n_sites   BIGINT
        )
        """
    )


def ingest_month(
    con: duckdb.DuckDBPyConnection, month: MonthFile, csv_path: Path
) -> None:
    """Fold one month's CSV into the accumulator tables."""
    header = con.execute(
        "SELECT * FROM read_csv_auto(?, sample_size=1000) LIMIT 0", [str(csv_path)]
    )
    columns = {d[0] for d in header.description}
    detector_col = _resolve(columns, "detector")
    speed_col = _resolve(columns, "speed")

    # The coordinate is rounded here, at the door, so nothing downstream can see
    # the raw precision and accidentally key on it. Labels are coalesced to '' so
    # the geometry key is total: one test detector ships null street names, and on
    # a null-dropping join its rows would vanish without a word.
    con.execute(
        f"""
        CREATE OR REPLACE TABLE month_raw AS
        SELECT
            {detector_col}                                  AS detector_id,
            id_carril                                       AS lane,
            fecha                                           AS date,
            hora                                            AS time,
            round(latitud,  {COORD_DECIMALS})               AS lat,
            round(longitud, {COORD_DECIMALS})               AS lon,
            coalesce(trim(dsc_avenida), '')                 AS street,
            coalesce(trim(dsc_int_anterior), '')            AS from_street,
            coalesce(trim(dsc_int_siguiente), '')           AS to_street,
            CAST({speed_col} AS DOUBLE)                     AS speed
        FROM read_csv_auto(?, header=true)
        WHERE latitud IS NOT NULL AND longitud IS NOT NULL
        """,
        [str(csv_path)],
    )

    # New geometries get an id; ones already seen in an earlier month keep theirs.
    con.execute(
        """
        INSERT INTO geom
        SELECT nextval('geom_seq'), d.lat, d.lon, d.street, d.from_street, d.to_street
        FROM (
            SELECT DISTINCT lat, lon, street, from_street, to_street FROM month_raw
        ) d
        LEFT JOIN geom g USING (lat, lon, street, from_street, to_street)
        WHERE g.geom_id IS NULL
        """
    )

    con.execute(
        f"""
        CREATE OR REPLACE TABLE month_keyed AS
        SELECT g.geom_id, m.detector_id, m.lane, m.date, m.speed,
               (datepart('hour', m.time) * 60 + datepart('minute', m.time))
                   // {BUCKET_MINUTES} AS bucket
        FROM month_raw m
        JOIN geom g USING (lat, lon, street, from_street, to_street)
        """
    )

    con.execute(
        f"""
        INSERT INTO lane_bucket
        SELECT geom_id, detector_id, lane, '{month.ym}', date, bucket,
               dayofweek(date),
               sum(speed) FILTER (speed > 0),
               count(*)   FILTER (speed > 0),
               count(*)   FILTER (speed = 0)
        FROM month_keyed
        WHERE speed IS NOT NULL AND speed <= {MAX_PLAUSIBLE_SPEED}
        GROUP BY geom_id, detector_id, lane, date, bucket
        """
    )

    con.execute(
        f"""
        INSERT INTO lane_hist
        SELECT geom_id, detector_id, lane, CAST(speed AS SMALLINT), count(*)
        FROM month_keyed
        WHERE speed > 0 AND speed <= {MAX_PLAUSIBLE_SPEED}
        GROUP BY geom_id, detector_id, lane, speed
        """
    )

    con.execute(
        f"""
        INSERT INTO geom_missing
        SELECT geom_id, '{month.ym}', count(*) FILTER (speed IS NULL)
        FROM month_keyed
        GROUP BY geom_id
        """
    )

    con.execute(
        f"""
        INSERT INTO month_log
        SELECT
            '{month.ym}',
            count(*),
            count(*) FILTER (speed > 0 AND speed <= {MAX_PLAUSIBLE_SPEED}),
            count(*) FILTER (speed = 0),
            count(*) FILTER (speed IS NULL),
            count(*) FILTER (speed > {MAX_PLAUSIBLE_SPEED}),
            count(DISTINCT date),
            count(DISTINCT geom_id)
        FROM month_keyed
        """
    )

    row = con.execute(
        "SELECT n_rows, n_moving, n_missing, n_error, n_dates, n_sites "
        "FROM month_log WHERE month = ?",
        [month.ym],
    ).fetchone()
    print(
        f"  {month.ym}: {row[0]:>10,} rows  {row[4]:>2} days  {row[5]:>3} sites  "
        f"{row[1] / row[0]:5.1%} usable  {row[2] / row[0]:5.1%} empty  "
        f"{row[3] / row[0]:5.2%} implausible",
        flush=True,
    )


def _weighted_quantile_sql(percentile: float) -> str:
    """Exact `quantile_cont` over a (site_id, speed, n) histogram.

    DuckDB has no weighted quantile, and expanding the histogram back to the 53 M
    readings it summarises would defeat the point of keeping one. This reproduces
    `quantile_cont` semantics on the order statistics directly: for n values the
    result sits at position h = (n-1)p, interpolating linearly between the values
    either side of it. Each of those two values is located by finding the first
    speed whose cumulative count reaches its rank.
    """
    return f"""
        WITH hist AS (
            SELECT site_id, speed, sum(n) AS n
            FROM site_hist GROUP BY site_id, speed
        ),
        total AS (SELECT site_id, sum(n) AS n_all FROM hist GROUP BY site_id),
        cum AS (
            SELECT h.site_id, h.speed,
                   sum(h.n) OVER (PARTITION BY h.site_id ORDER BY h.speed
                                  ROWS UNBOUNDED PRECEDING) AS cum_n
            FROM hist h
        ),
        target AS (
            -- CAST, not a bare literal: DuckDB reads `0.85` as DECIMAL, which
            -- propagates all the way to the output column and lands in the
            -- parquet as a decimal that pandas then hands back as
            -- decimal.Decimal objects rather than floats.
            SELECT site_id, n_all,
                   (n_all - 1) * CAST({percentile} AS DOUBLE) AS h
            FROM total
        ),
        picked AS (
            SELECT t.site_id, t.h,
                   min(c.speed) FILTER (c.cum_n >= floor(t.h) + 1) AS x_lo,
                   min(c.speed) FILTER (c.cum_n >= floor(t.h) + 2) AS x_hi
            FROM target t JOIN cum c USING (site_id)
            GROUP BY t.site_id, t.h
        )
        SELECT site_id,
               CAST(x_lo + (h - floor(h)) * (coalesce(x_hi, x_lo) - x_lo) AS DOUBLE)
                   AS free_flow_speed
        FROM picked
    """


def finalize(con: duckdb.DuckDBPyConnection) -> None:
    """Assign site ids, judge the sensors panel-wide, write the parquet pair."""
    # Deterministic ids: ordered by position and name, so they do not depend on
    # which month happened to be ingested first.
    con.execute(
        """
        CREATE OR REPLACE TABLE sites AS
        SELECT geom_id,
               dense_rank() OVER (
                   ORDER BY lat, lon, street, from_street, to_street
               ) AS site_id,
               lat, lon, street, from_street, to_street
        FROM geom
        """
    )

    # How many distinct streets claim each coordinate -- the tell for the feed's
    # placeholder location. See MAX_STREETS_PER_COORD.
    con.execute(
        """
        CREATE OR REPLACE TABLE coord_streets AS
        SELECT lat, lon, count(DISTINCT street) AS streets_at_coord
        FROM sites GROUP BY lat, lon
        """
    )

    # The stuck-detector test runs per *lane*, not per site: the raw file is one
    # row per lane and a healthy lane hides a dead one when they are averaged
    # together. Judged over every ingested month, so a lane that worked in
    # January and died in June is not condemned for the whole panel -- and a lane
    # that never worked at all is caught with eight months of evidence rather
    # than one.
    con.execute(
        f"""
        CREATE OR REPLACE TABLE lane_health AS
        SELECT
            b.geom_id, b.detector_id, b.lane,
            coalesce(h.n_moving, 0)  AS n_moving,
            h.mean_speed,
            (coalesce(h.n_moving, 0) = 0
             OR h.mean_speed < {FLATLINE_SPEED}) AS is_dead
        FROM (SELECT DISTINCT geom_id, detector_id, lane FROM lane_bucket) b
        LEFT JOIN (
            SELECT geom_id, detector_id, lane,
                   sum(n)                      AS n_moving,
                   sum(speed * n) / sum(n)     AS mean_speed
            FROM lane_hist GROUP BY geom_id, detector_id, lane
        ) h USING (geom_id, detector_id, lane)
        """
    )

    n_dead, n_lanes, dead_moving, dead_zero = con.execute(
        """
        SELECT count(*) FILTER (is_dead), count(*),
               (SELECT coalesce(sum(b.n_moving), 0) FROM lane_bucket b
                  JOIN lane_health l USING (geom_id, detector_id, lane)
                WHERE l.is_dead),
               (SELECT coalesce(sum(b.n_zero), 0) FROM lane_bucket b
                  JOIN lane_health l USING (geom_id, detector_id, lane)
                WHERE l.is_dead)
        FROM lane_health
        """
    ).fetchone()
    print(
        f"  {n_dead} of {n_lanes} lane detectors never measured moving traffic "
        f"(or never averaged {FLATLINE_SPEED:g} km/h) across the whole panel -- "
        f"dropping their {dead_moving:,} moving and {dead_zero:,} zero readings",
        flush=True,
    )

    # Sums + counts, not averages: any later regrouping stays exact. Lanes are
    # collapsed away here -- the app works per site, and the lane counts it
    # reports come from the sensor table.
    con.execute(
        """
        CREATE OR REPLACE TABLE measurements AS
        SELECT
            s.site_id,
            b.month,
            b.date,
            b.bucket,
            any_value(b.dow)          AS dow,
            sum(b.speed_sum)          AS speed_sum,
            sum(b.n_moving)           AS n_moving,
            sum(b.n_zero)             AS n_zero
        FROM lane_bucket b
        JOIN lane_health l USING (geom_id, detector_id, lane)
        JOIN sites s USING (geom_id)
        WHERE NOT l.is_dead
        GROUP BY s.site_id, b.month, b.date, b.bucket
        ORDER BY b.month, s.site_id, b.date, b.bucket
        """
    )

    # Panel-wide speed distribution per site, dead lanes excluded.
    con.execute(
        """
        CREATE OR REPLACE TABLE site_hist AS
        SELECT s.site_id, h.speed, sum(h.n) AS n
        FROM lane_hist h
        JOIN lane_health l USING (geom_id, detector_id, lane)
        JOIN sites s USING (geom_id)
        WHERE NOT l.is_dead
        GROUP BY s.site_id, h.speed
        """
    )
    con.execute(
        f"CREATE OR REPLACE TABLE free_flow AS {_weighted_quantile_sql(FREE_FLOW_PERCENTILE)}"
    )

    # Daytime vs overnight speed per sensor, weekdays only: the contrast that
    # exposes a detector not watching through traffic at all. See STALLED_* in
    # config. Taken from the bucket sums, which makes it exact.
    day_lo, day_hi = (h * 60 // BUCKET_MINUTES for h in STALLED_DAY_HOURS)
    night_lo, night_hi = (h * 60 // BUCKET_MINUTES for h in STALLED_NIGHT_HOURS)
    con.execute(
        f"""
        CREATE OR REPLACE TABLE day_night AS
        SELECT
            site_id,
            sum(speed_sum) FILTER (bucket >= {day_lo} AND bucket < {day_hi})
              / nullif(sum(n_moving) FILTER (bucket >= {day_lo} AND bucket < {day_hi}), 0)
                AS day_speed,
            sum(speed_sum) FILTER (bucket >= {night_lo} AND bucket < {night_hi})
              / nullif(sum(n_moving) FILTER (bucket >= {night_lo} AND bucket < {night_hi}), 0)
                AS night_speed
        FROM measurements
        WHERE dow BETWEEN 1 AND 5
        GROUP BY site_id
        """
    )

    # Which months each site actually reported in. A third of the sites are not
    # present for the whole panel -- sensors installed, removed, or given a real
    # coordinate part-way through the year -- and a reference built from three
    # months is worth flagging rather than presenting as a yearly figure.
    con.execute(
        """
        CREATE OR REPLACE TABLE site_months AS
        SELECT s.site_id,
               count(DISTINCT b.month) AS n_months,
               min(b.month)            AS first_month,
               max(b.month)            AS last_month
        FROM lane_bucket b JOIN sites s USING (geom_id)
        GROUP BY s.site_id
        """
    )

    con.execute(
        """
        CREATE OR REPLACE TABLE detectors AS
        SELECT
            s.site_id,
            any_value(s.lat)                        AS lat,
            any_value(s.lon)                        AS lon,
            any_value(s.street)                     AS street,
            any_value(s.from_street)                AS from_street,
            any_value(s.to_street)                  AS to_street,
            count(DISTINCT lh.detector_id)          AS n_detectors,
            count(DISTINCT lh.lane) FILTER (NOT lh.is_dead)      AS n_lanes,
            any_value(sm.n_months)                  AS n_months,
            any_value(sm.first_month)               AS first_month,
            any_value(sm.last_month)                AS last_month,
            any_value(st.n_readings)                AS n_readings,
            any_value(st.n_zero)                    AS n_zero,
            any_value(st.mean_speed)                AS mean_speed,
            any_value(ff.free_flow_speed)           AS free_flow_speed,
            any_value(ms.n_missing)                 AS n_missing,
            any_value(cs.streets_at_coord)          AS streets_at_coord,
            any_value(dn.day_speed)                 AS day_speed,
            any_value(dn.night_speed)               AS night_speed,
            any_value(dl.n_dead_lanes)              AS n_dead_lanes
        -- The inventory is every site that produced at least one usable reading,
        -- before the dead-lane filter. A site whose every lane is dead still
        -- appears, with a null mean_speed, so data.py can flag it via is_live and
        -- keep the "excluded as stuck" count reportable instead of the site
        -- silently vanishing from the totals.
        FROM sites s
        JOIN lane_health lh USING (geom_id)
        JOIN site_months sm USING (site_id)
        LEFT JOIN (
            SELECT site_id,
                   sum(n_moving) + sum(n_zero)                  AS n_readings,
                   sum(n_zero)                                  AS n_zero,
                   sum(speed_sum) / nullif(sum(n_moving), 0)    AS mean_speed
            FROM measurements GROUP BY site_id
        ) st USING (site_id)
        LEFT JOIN free_flow ff USING (site_id)
        LEFT JOIN day_night dn USING (site_id)
        LEFT JOIN (
            SELECT s2.site_id, sum(gm.n_missing) AS n_missing
            FROM geom_missing gm JOIN sites s2 USING (geom_id)
            GROUP BY s2.site_id
        ) ms USING (site_id)
        LEFT JOIN (
            SELECT s3.site_id, count(*) FILTER (lh2.is_dead) AS n_dead_lanes
            FROM lane_health lh2 JOIN sites s3 USING (geom_id)
            GROUP BY s3.site_id
        ) dl USING (site_id)
        JOIN coord_streets cs ON cs.lat = s.lat AND cs.lon = s.lon
        GROUP BY s.site_id
        ORDER BY s.site_id
        """
    )

    con.execute(
        f"COPY measurements TO '{MEASUREMENTS_PARQUET}' "
        "(FORMAT parquet, COMPRESSION zstd)"
    )
    con.execute(
        f"COPY detectors TO '{DETECTORS_PARQUET}' (FORMAT parquet, COMPRESSION zstd)"
    )


def _report(con: duckdb.DuckDBPyConnection) -> None:
    total_rows, total_missing, total_error = con.execute(
        "SELECT sum(n_rows), sum(n_missing), sum(n_error) FROM month_log"
    ).fetchone()
    n_meas, n_sites, n_months, d_lo, d_hi = con.execute(
        """
        SELECT (SELECT count(*) FROM measurements),
               (SELECT count(*) FROM detectors),
               (SELECT count(DISTINCT month) FROM measurements),
               (SELECT min(date) FROM measurements),
               (SELECT max(date) FROM measurements)
        """
    ).fetchone()
    n_placeholder, n_partial = con.execute(
        f"""
        SELECT (SELECT count(*) FROM detectors
                WHERE streets_at_coord > {MAX_STREETS_PER_COORD}),
               (SELECT count(*) FROM detectors
                WHERE n_months < (SELECT max(n_months) FROM detectors))
        """
    ).fetchone()

    print(
        f"\n  {total_rows:,} raw readings over {n_months} month(s), "
        f"{d_lo} to {d_hi}\n"
        f"    {total_missing:,} ({total_missing / total_rows:.1%}) empty and "
        f"{total_error:,} ({total_error / total_rows:.2%}) implausible, both dropped",
        flush=True,
    )
    if n_placeholder:
        print(
            f"  {n_placeholder} site(s) share a placeholder coordinate "
            f"(>{MAX_STREETS_PER_COORD} streets at one point) -- kept, but flagged "
            "as having no usable location",
            flush=True,
        )
    if n_partial:
        print(
            f"  {n_partial} of {n_sites} sites did not report in every month -- "
            "their references come from the months they have, and data.py flags them",
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


def build(
    months: list[MonthFile],
    con: duckdb.DuckDBPyConnection | None = None,
) -> None:
    if not months:
        raise SystemExit(
            f"no monthly files in {DATA_RAW} -- fetch them first:\n"
            "  uv run mvdspeed-fetch --from 2026-01"
        )

    DATA_PROCESSED.mkdir(parents=True, exist_ok=True)
    con = con or duckdb.connect()
    _create_accumulators(con)

    print(f"Ingesting {len(months)} month(s) from {DATA_RAW} ...", flush=True)
    with TemporaryDirectory(prefix="mvdspeed-etl-") as tmp:
        scratch = Path(tmp)
        for month in months:
            csv_path = _expand(month, scratch)
            try:
                ingest_month(con, month, csv_path)
            finally:
                # Only the expanded copy is removed; a month published as a plain
                # CSV is the download itself and must survive.
                if month.is_zip and csv_path.exists():
                    csv_path.unlink()

    print("Building panel-wide references ...", flush=True)
    finalize(con)
    _report(con)


def select(
    months: list[MonthFile], *, only: str | None, since: str | None
) -> list[MonthFile]:
    if only:
        wanted = {m.strip() for m in only.split(",")}
        available = {m.ym for m in months}
        missing = wanted - available
        if missing:
            raise SystemExit(
                f"not in {DATA_RAW}: {', '.join(sorted(missing))}. "
                f"Present: {', '.join(sorted(available)) or 'nothing'}"
            )
        months = [m for m in months if m.ym in wanted]
    if since:
        months = [m for m in months if m.ym >= since]
    return months


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--only", help="comma-separated months to ingest, e.g. 2026-07,2026-08"
    )
    parser.add_argument("--from", dest="since", help="earliest month, e.g. 2026-01")
    parser.add_argument(
        "--list", action="store_true", help="print the months on disk and exit"
    )
    args = parser.parse_args(argv)

    available = month_files()
    if args.list:
        for month in available:
            size = month.path.stat().st_size / 1e6
            print(f"{month.ym}  {size:6.0f} MB  {month.path.name}")
        return 0

    build(select(available, only=args.only, since=args.since))
    return 0


if __name__ == "__main__":
    sys.exit(main())
