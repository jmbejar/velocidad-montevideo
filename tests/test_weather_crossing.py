"""The weather crossing, on data whose right answer is known by construction.

The rain penalty is the number in this app most able to be wrong and still look
right: it is a difference of two averages over unevenly distributed hours, and
any of the confounds would move it a plausible-looking amount. So the tests here
build panels where the true answer is fixed by construction and check that the
code recovers it -- particularly the stratification, which is tested on a panel
where the naive answer has the *opposite sign* to the correct one.
"""

from __future__ import annotations

import pandas as pd
import pytest

from mvdspeed import data as mvd
from mvdspeed.config import BUCKET_MINUTES

NIGHT_BUCKET = 4  # 02:00, empty roads
PEAK_BUCKET = 36  # 18:00, rush hour


def make_dataset(rows: list[dict], *, n_sites: int = 1) -> mvd.Dataset:
    """A Dataset from explicit bucket rows, with every sensor usable.

    `rows` carry date/bucket/speed/n plus the weather flags, which keeps each
    test's setup readable as a table of the situation it describes.
    """
    frame = pd.DataFrame(rows)
    frame["date"] = pd.to_datetime(frame["date"])
    frame["month"] = frame["date"].dt.strftime("%Y-%m")
    frame["dow"] = frame["date"].dt.dayofweek.map(lambda d: (d + 1) % 7)
    frame["n_zero"] = frame.get("n_zero", 0)
    frame["hour"] = frame["bucket"] * BUCKET_MINUTES // 60
    frame["speed_sum"] = frame["speed"] * frame["n_moving"]
    for flag in ("is_wet", "is_heavy", "recently_wet"):
        if flag not in frame:
            frame[flag] = False
        frame[flag] = frame[flag].astype("boolean")
    frame["precip_mm"] = frame["is_wet"].map({True: 1.0, False: 0.0}).astype(float)

    sites = pd.DataFrame(
        {
            "site_id": list(range(1, n_sites + 1)),
            "street": [f"Street {i}" for i in range(1, n_sites + 1)],
            "from_street": ["A"] * n_sites,
            "to_street": ["B"] * n_sites,
            "is_usable": [True] * n_sites,
            "n_months": [1] * n_sites,
        }
    )
    return mvd.Dataset(measurements=frame, sites=sites, weather=pd.DataFrame())


def rows_for(bucket: int, dates: list[str], speed: float, *, wet: bool,
             n: int = 100, site_id: int = 1) -> list[dict]:
    return [
        {
            "site_id": site_id, "date": d, "bucket": bucket, "speed": speed,
            "n_moving": n, "is_wet": wet, "recently_wet": wet, "is_heavy": False,
        }
        for d in dates
    ]


WEEKDAYS = mvd.DAY_SCOPES["Weekdays (Mon-Fri)"]


def test_stratifying_recovers_the_true_penalty_when_pooling_inverts_it() -> None:
    """Rain falls mostly at night, when the roads are fast anyway.

    Within each time of day wet is exactly 2 km/h slower than dry. But rain is
    concentrated in the fast night bucket, so pooling every wet hour against
    every dry hour makes the wet average look *faster* overall. The stratified
    figure has to come back as -2.
    """
    # Weekdays in January 2026.
    dry_days = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"]
    wet_days = ["2026-01-12", "2026-01-13"]

    rows = []
    # Night: fast. Mostly wet.
    rows += rows_for(NIGHT_BUCKET, dry_days[:1], 60.0, wet=False)
    rows += rows_for(NIGHT_BUCKET, wet_days, 58.0, wet=True, n=1000)
    # Peak: slow. Mostly dry.
    rows += rows_for(PEAK_BUCKET, dry_days, 20.0, wet=False, n=1000)
    rows += rows_for(PEAK_BUCKET, wet_days[:1], 18.0, wet=True, n=50)

    data = make_dataset(rows)
    head = mvd.rain_headline(data, dows=WEEKDAYS, months=None)

    assert head["delta"] == pytest.approx(-2.0, abs=1e-9), (
        "within each bucket wet is exactly 2 km/h slower"
    )
    # The naive figure is not merely bigger here -- it has the wrong sign, which
    # is the whole reason the stratification exists.
    assert head["naive_delta"] > 0
    assert head["pct"] < 0


def test_dry_baseline_excludes_the_hours_after_rain() -> None:
    """An hour that is not raining but follows rain is not a dry-road hour."""
    rows = [
        # Genuinely dry.
        {"site_id": 1, "date": "2026-01-05", "bucket": PEAK_BUCKET, "speed": 30.0,
         "n_moving": 100, "is_wet": False, "recently_wet": False, "is_heavy": False},
        # Not raining, but the road is still wet -- must not count as dry.
        {"site_id": 1, "date": "2026-01-06", "bucket": PEAK_BUCKET, "speed": 20.0,
         "n_moving": 100, "is_wet": False, "recently_wet": True, "is_heavy": False},
        # Raining.
        {"site_id": 1, "date": "2026-01-07", "bucket": PEAK_BUCKET, "speed": 24.0,
         "n_moving": 100, "is_wet": True, "recently_wet": True, "is_heavy": False},
    ]
    data = make_dataset(rows)
    head = mvd.rain_headline(data, dows=WEEKDAYS, months=None)

    # Dry is the 30 km/h day alone. Including the damp 20 km/h day would drag the
    # baseline to 25 and shrink the measured penalty from -6 to -1.
    assert head["dry_speed"] == pytest.approx(30.0)
    assert head["delta"] == pytest.approx(-6.0)


def test_unknown_weather_is_never_counted_as_dry() -> None:
    """A station outage is not evidence that the roads were dry."""
    rows = [
        {"site_id": 1, "date": "2026-01-05", "bucket": PEAK_BUCKET, "speed": 30.0,
         "n_moving": 100, "is_wet": False, "recently_wet": False, "is_heavy": False},
        {"site_id": 1, "date": "2026-01-06", "bucket": PEAK_BUCKET, "speed": 5.0,
         "n_moving": 100, "is_wet": None, "recently_wet": None, "is_heavy": None},
        {"site_id": 1, "date": "2026-01-07", "bucket": PEAK_BUCKET, "speed": 25.0,
         "n_moving": 100, "is_wet": True, "recently_wet": True, "is_heavy": False},
    ]
    data = make_dataset(rows)
    head = mvd.rain_headline(data, dows=WEEKDAYS, months=None)

    # The 5 km/h hour has no weather reading and must land on neither side.
    assert head["dry_speed"] == pytest.approx(30.0)
    assert head["wet_speed"] == pytest.approx(25.0)


def test_a_bucket_with_only_one_condition_is_dropped_not_guessed() -> None:
    """A time of day that never saw rain contributes no penalty either way."""
    rows = []
    rows += rows_for(NIGHT_BUCKET, ["2026-01-05"], 60.0, wet=False)   # dry only
    rows += rows_for(PEAK_BUCKET, ["2026-01-06"], 30.0, wet=False)
    rows += rows_for(PEAK_BUCKET, ["2026-01-07"], 27.0, wet=True)
    data = make_dataset(rows)
    head = mvd.rain_headline(data, dows=WEEKDAYS, months=None)

    assert head["n_buckets"] == 1, "only the peak bucket has both conditions"
    assert head["delta"] == pytest.approx(-3.0)


def test_rain_penalty_weights_a_street_by_where_its_rain_actually_fell() -> None:
    """A street is not credited with a penalty from a time it had no rain in."""
    rows = []
    # Site 1 has rain only at the peak, where it loses 4 km/h.
    rows += rows_for(PEAK_BUCKET, ["2026-01-05"], 30.0, wet=False, site_id=1)
    rows += rows_for(PEAK_BUCKET, ["2026-01-06"], 26.0, wet=True, site_id=1)
    rows += rows_for(NIGHT_BUCKET, ["2026-01-05"], 60.0, wet=False, site_id=1)
    # Site 2 has rain only at night, where it loses 1 km/h.
    rows += rows_for(NIGHT_BUCKET, ["2026-01-05"], 60.0, wet=False, site_id=2)
    rows += rows_for(NIGHT_BUCKET, ["2026-01-06"], 59.0, wet=True, site_id=2)
    rows += rows_for(PEAK_BUCKET, ["2026-01-05"], 30.0, wet=False, site_id=2)

    data = make_dataset(rows, n_sites=2)
    penalty = mvd.rain_penalty(data, dows=WEEKDAYS, months=None, by="street")
    by_street = penalty.set_index("street")["delta"]

    assert by_street["Street 1"] == pytest.approx(-4.0)
    assert by_street["Street 2"] == pytest.approx(-1.0)


def test_weather_join_maps_both_half_hours_onto_their_hour() -> None:
    """Buckets 2 and 3 are 01:00 and 01:30, and share the 01:00 weather row."""
    measurements = pd.DataFrame(
        {
            "site_id": [1, 1, 1],
            "month": ["2026-01"] * 3,
            "date": pd.to_datetime(["2026-01-05"] * 3),
            "bucket": [2, 3, 4],
            "dow": [1, 1, 1],
            "speed_sum": [100.0, 100.0, 100.0],
            "n_moving": [10, 10, 10],
            "n_zero": [0, 0, 0],
        }
    )
    weather = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-01-05", "2026-01-05"]),
            "hour": [1, 2],
            "precip_mm": [3.0, 0.0],
            "temp_c": [15.0, 16.0],
            "humidity_pct": [90.0, 80.0],
            "is_wet": pd.array([True, False], dtype="boolean"),
            "is_heavy": pd.array([True, False], dtype="boolean"),
            "recently_wet": pd.array([True, True], dtype="boolean"),
        }
    )
    joined = mvd._join_weather(measurements, weather)

    assert list(joined["hour"]) == [1, 1, 2]
    assert list(joined["precip_mm"]) == [3.0, 3.0, 0.0]
    assert list(joined["is_wet"]) == [True, True, False]


def test_weather_join_never_multiplies_rows() -> None:
    """A duplicated weather hour would silently double every reading."""
    measurements = pd.DataFrame(
        {
            "site_id": [1], "month": ["2026-01"],
            "date": pd.to_datetime(["2026-01-05"]), "bucket": [2], "dow": [1],
            "speed_sum": [100.0], "n_moving": [10], "n_zero": [0],
        }
    )
    weather = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-01-05", "2026-01-05"]),
            "hour": [1, 1],  # the same hour twice
            "precip_mm": [3.0, 0.0], "temp_c": [15.0, 15.0],
            "humidity_pct": [90.0, 90.0],
            "is_wet": pd.array([True, False], dtype="boolean"),
            "is_heavy": pd.array([False, False], dtype="boolean"),
            "recently_wet": pd.array([True, False], dtype="boolean"),
        }
    )
    with pytest.raises(pd.errors.MergeError):
        mvd._join_weather(measurements, weather)


def test_rain_views_refuse_to_run_without_weather() -> None:
    frame = pd.DataFrame(
        {
            "site_id": [1], "month": ["2026-01"],
            "date": pd.to_datetime(["2026-01-05"]), "bucket": [2], "dow": [1],
            "speed_sum": [100.0], "n_moving": [10], "n_zero": [0],
        }
    )
    with pytest.raises(ValueError, match="no weather"):
        mvd._rain_mask(frame, "dry")
    # …but an unfiltered view still works on a panel with no weather at all.
    assert mvd._rain_mask(frame, None).all()


@pytest.mark.parametrize(
    "month,expected",
    [("2026-01", "Jan 2026"), ("2026-09", "Sep 2026"), ("2025-12", "Dec 2025")],
)
def test_month_label(month: str, expected: str) -> None:
    assert mvd.month_label(month) == expected


def test_month_filter_selects_only_the_named_months() -> None:
    rows = []
    for date in ["2026-01-05", "2026-02-05", "2026-03-05"]:
        rows += rows_for(PEAK_BUCKET, [date], 30.0, wet=False)
    data = make_dataset(rows)

    assert data.months == ["2026-01", "2026-02", "2026-03"]
    profile = mvd.city_profile(data, dows=WEEKDAYS, months=["2026-02"])
    assert profile["samples"].sum() == 100
    both = mvd.city_profile(data, dows=WEEKDAYS, months=["2026-01", "2026-03"])
    assert both["samples"].sum() == 200
