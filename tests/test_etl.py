"""The parts of the ETL that a look at the map cannot check.

The histogram quantile is the one that matters most: it replaces a call to
DuckDB's own `quantile_cont` with hand-written SQL over counts, and a percentile
that is quietly a percent or two off would still look entirely plausible on the
map. So it is tested against `quantile_cont` itself, on random data, including
the shapes that break naive implementations: single readings, ties, and
distributions concentrated on one value.
"""

from __future__ import annotations

import random

import duckdb
import pytest

from mvdspeed.etl import (
    MonthFile,
    _MONTH_FILE,
    _weighted_quantile_sql,
    month_files,
    select,
)


def _quantile_from_hist(
    con: duckdb.DuckDBPyConnection, samples: dict[int, list[int]], percentile: float
) -> dict[int, float]:
    rows = [
        (site, speed, values.count(speed))
        for site, values in samples.items()
        for speed in sorted(set(values))
    ]
    con.execute("CREATE OR REPLACE TABLE site_hist (site_id BIGINT, speed SMALLINT, n BIGINT)")
    con.executemany("INSERT INTO site_hist VALUES (?, ?, ?)", rows)
    result = con.execute(_weighted_quantile_sql(percentile)).fetchall()
    return {site: value for site, value in result}


def _quantile_direct(
    con: duckdb.DuckDBPyConnection, samples: dict[int, list[int]], percentile: float
) -> dict[int, float]:
    rows = [(site, v) for site, values in samples.items() for v in values]
    con.execute("CREATE OR REPLACE TABLE raw (site_id BIGINT, speed DOUBLE)")
    con.executemany("INSERT INTO raw VALUES (?, ?)", rows)
    result = con.execute(
        f"SELECT site_id, quantile_cont(speed, {percentile}) FROM raw GROUP BY site_id"
    ).fetchall()
    return {site: value for site, value in result}


@pytest.mark.parametrize("percentile", [0.0, 0.25, 0.5, 0.85, 1.0])
def test_histogram_quantile_matches_duckdb(percentile: float) -> None:
    rng = random.Random(20260813)
    samples = {
        1: [rng.randint(0, 120) for _ in range(500)],   # a full spread
        2: [rng.randint(30, 40) for _ in range(200)],   # heavy ties
        3: [42],                                        # a single reading
        4: [7, 7, 7, 7],                                # one value only
        5: [1, 120],                                    # two, far apart
        6: sorted(rng.randint(0, 5) for _ in range(97)),  # odd count, low spread
    }
    con = duckdb.connect()
    from_hist = _quantile_from_hist(con, samples, percentile)
    direct = _quantile_direct(con, samples, percentile)

    assert set(from_hist) == set(direct)
    for site in direct:
        assert from_hist[site] == pytest.approx(direct[site], abs=1e-9), (
            f"site {site} at p{percentile}"
        )


def test_histogram_quantile_is_exact_for_known_case() -> None:
    """A worked example, so the test does not only compare two implementations."""
    con = duckdb.connect()
    # Ten readings: 10,10,20,20,20,30,30,40,40,50. p85 sits at h = 9*0.85 = 7.65,
    # between the 8th value (40, 0-indexed 7) and the 9th (40), so it is 40.
    samples = {1: [10, 10, 20, 20, 20, 30, 30, 40, 40, 50]}
    assert _quantile_from_hist(con, samples, 0.85)[1] == pytest.approx(40.0)
    # p95: h = 9*0.95 = 8.55, between the 9th value (40) and the 10th (50) ->
    # 40 + 0.55*10 = 45.5.
    assert _quantile_from_hist(con, samples, 0.95)[1] == pytest.approx(45.5)


@pytest.mark.parametrize(
    "name,expected",
    [
        ("velocidad_2026-01.zip", "2026-01"),
        ("velocidad_2026-12.csv", "2026-12"),
        ("velocidad_2021-03.csv", "2021-03"),
        ("VELOCIDAD_2026-05.ZIP", "2026-05"),
    ],
)
def test_month_file_names_are_recognised(name: str, expected: str) -> None:
    match = _MONTH_FILE.search(name)
    assert match is not None and match.group(1) == expected


@pytest.mark.parametrize(
    "name",
    [
        "velocidad_2026-1.zip",      # month not zero-padded
        "velocidad_2026.zip",        # no month
        "metadata.txt",
        "velocidad_2026-01.csv.part",  # a download still in flight
    ],
)
def test_non_month_files_are_ignored(name: str) -> None:
    assert _MONTH_FILE.search(name) is None


def test_month_files_are_discovered_in_order(tmp_path) -> None:
    for name in [
        "velocidad_2026-03.zip",
        "velocidad_2026-01.csv",
        "velocidad_2026-02.zip",
        "velocidad_2026-04.csv.part",
        "notes.txt",
    ]:
        (tmp_path / name).write_bytes(b"")
    found = month_files(tmp_path)
    assert [m.ym for m in found] == ["2026-01", "2026-02", "2026-03"]
    assert [m.is_zip for m in found] == [False, True, True]


def test_select_filters_by_month() -> None:
    months = [MonthFile(ym=f"2026-{m:02d}", path=None) for m in range(1, 9)]
    assert [m.ym for m in select(months, only="2026-03,2026-07", since=None)] == [
        "2026-03",
        "2026-07",
    ]
    assert [m.ym for m in select(months, only=None, since="2026-06")] == [
        "2026-06",
        "2026-07",
        "2026-08",
    ]


def test_select_rejects_a_month_that_is_not_on_disk() -> None:
    months = [MonthFile(ym="2026-01", path=None)]
    with pytest.raises(SystemExit, match="2025-12"):
        select(months, only="2025-12", since=None)
