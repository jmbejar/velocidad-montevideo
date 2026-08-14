"""The football crossing, on panels whose right answer is known by construction.

This estimator is easier to fool than the rain one. Rain is at least sprinkled
across the calendar; a fixture list is a dozen dates, all in one season, several
of them within days of each other. Every confound that varies slowly -- the
summer holidays, the school year, the direction of the weather -- lines up with
the treatment almost perfectly, and a baseline that averages over the wrong span
will report that alignment as football.

So the panels below are built with the answer fixed in advance, and the most
important test in the file is the one where the *true* effect is zero:
`test_a_january_speed_bump_does_not_become_a_june_football_effect` reproduces
the artefact that a pooled baseline produced on the real data, where every June
weekday afternoon read 0.54 km/h slow because January sat in the same average.
That artefact had the same sign and roughly the same size as the effect this
page exists to measure. If that test ever passes for the wrong reason, every
number on the page is suspect.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from mvdspeed import data as mvd
from mvdspeed import events as ev

KICKOFF_BUCKET = 38  # 19:00, the commonest kick-off in the real fixture list
LATE_BUCKET = 43  # 21:30, late enough that the window crosses midnight


def make_panel(
    frame: pd.DataFrame,
    *,
    sites: pd.DataFrame | None = None,
    wet_dates: list[str] | None = None,
) -> ev.EventPanel:
    """An EventPanel from a frame of site/date/bucket/speed/n rows."""
    frame = frame.copy()
    frame["date"] = pd.to_datetime(frame["date"])
    frame["month"] = frame["date"].dt.strftime("%Y-%m")
    frame["dow"] = frame["date"].dt.dayofweek.map(lambda d: (d + 1) % 7)
    frame["speed_sum"] = frame["speed"] * frame["n_moving"]
    if "n_zero" not in frame:
        frame["n_zero"] = 0.0

    n_sites = frame["site_id"].nunique()
    if sites is None:
        sites = pd.DataFrame(
            {
                "site_id": sorted(frame["site_id"].unique()),
                "street": [f"Street {i}" for i in range(n_sites)],
                "is_usable": True,
                "has_location": True,
                "lat": -34.9,
                "lon": -56.16,
            }
        )
    weather = None
    if wet_dates is not None:
        wet = pd.to_datetime(wet_dates)
        grid = pd.MultiIndex.from_product(
            [pd.DatetimeIndex(sorted(frame["date"].unique())), range(24)],
            names=["date", "hour"],
        ).to_frame(index=False)
        grid["recently_wet"] = grid["date"].isin(wet)
        weather = grid.astype({"recently_wet": "boolean"})

    dataset = mvd.Dataset(measurements=frame, sites=sites, weather=weather)
    return ev.build_panel(dataset)


def flat_panel(
    dates: list[str],
    *,
    speed: float = 30.0,
    buckets: range = range(28, 48),
    n_sites: int = 4,
    n: int = 100,
    **kwargs,
) -> pd.DataFrame:
    """Every site, every listed date, every bucket, at one speed."""
    rows = [
        {"site_id": s, "date": d, "bucket": b, "speed": speed, "n_moving": float(n)}
        for d in dates
        for b in buckets
        for s in range(n_sites)
    ]
    return pd.DataFrame(rows, **kwargs)


def weekly(start: str, count: int) -> list[str]:
    """`count` consecutive same-weekday dates, as strings."""
    first = pd.Timestamp(start)
    return [(first + pd.Timedelta(weeks=i)).strftime("%Y-%m-%d") for i in range(count)]


def fixtures(rows: list[dict]) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    frame["date"] = pd.to_datetime(frame["date"])
    for column, default in (
        ("competition", "Test cup"),
        ("tier", "test"),
        ("home", "A"),
        ("away", "B"),
        ("is_national_team", False),
        ("venue_lat", np.nan),
        ("venue_lon", np.nan),
    ):
        if column not in frame:
            frame[column] = default
    frame["kickoff_bucket"] = frame["kickoff_bucket"].astype("Int16")
    frame["label"] = frame["home"] + " v " + frame["away"]
    frame["kickoff"] = ""
    return frame


NO_HOLIDAYS = pd.DataFrame({"date": pd.to_datetime([])})


# --- does it find an effect that is really there? -----------------------------


@pytest.mark.parametrize("size", [-4.0, 2.5, 6.0])
def test_an_injected_effect_is_recovered_at_the_bucket_it_was_injected_into(size):
    """A known bump on the match date, at kick-off, comes back at rel 0."""
    dates = weekly("2026-03-01", 9)
    match_date = dates[4]
    frame = flat_panel(dates)
    hit = (frame["date"] == match_date) & (frame["bucket"] == KICKOFF_BUCKET)
    frame.loc[hit, "speed"] += size

    panel = make_panel(frame)
    events = fixtures([{"date": match_date, "kickoff_bucket": KICKOFF_BUCKET}])
    study = ev.event_study(panel, events, NO_HOLIDAYS, window=4)

    at_kickoff = study.loc[study["rel"] == 0, "delta"].iloc[0]
    assert at_kickoff == pytest.approx(size, abs=1e-6), "the bump should be rel 0"
    others = study.loc[study["rel"] != 0, "delta"]
    assert others.abs().max() == pytest.approx(0.0, abs=1e-6), (
        "no other relative bucket should move"
    )


def test_the_effect_lands_on_the_right_bucket_when_the_window_crosses_midnight():
    """A 21:30 kick-off plus four hours is the next morning, not the same one.

    Buckets are per-day, so `bucket + rel` wraps: 43 + 8 reads as bucket 3 of
    the *same* date, which is 01:30 nineteen and a half hours earlier. The panel
    indexes by an absolute half-hour slot precisely so this cannot happen, and
    this test injects the effect after midnight where the wrapping version would
    look in the wrong place and find nothing.
    """
    dates = weekly("2026-05-07", 9)
    match_date = dates[4]
    after_midnight = (pd.Timestamp(match_date) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")

    # Every control day needs its own small hours present too, or the cell the
    # match night is compared against does not exist and the answer is NaN
    # whether the arithmetic wraps or not.
    nights = [(pd.Timestamp(d) + pd.Timedelta(days=1)).strftime("%Y-%m-%d") for d in dates]
    frame = flat_panel(sorted(set(dates + nights)), buckets=range(0, 48))
    hit = (frame["date"] == after_midnight) & (frame["bucket"] == 1)  # 00:30
    frame.loc[hit, "speed"] += 5.0

    panel = make_panel(frame)
    events = fixtures([{"date": match_date, "kickoff_bucket": LATE_BUCKET}])
    study = ev.event_study(panel, events, NO_HOLIDAYS, window=8)

    # 21:30 -> 00:30 is six half-hours forward.
    assert study.loc[study["rel"] == 6, "delta"].iloc[0] == pytest.approx(5.0, abs=1e-6)
    assert study.loc[study["rel"] == 3, "delta"].iloc[0] == pytest.approx(0.0, abs=1e-6)


# --- does it refuse to find one that is not? ----------------------------------


def test_a_january_speed_bump_does_not_become_a_june_football_effect():
    """The artefact that a pooled baseline produced on the real panel.

    January in Montevideo is empty -- the city goes to the coast -- and reads
    2.07 km/h faster than the rest of the year. Averaged into one "typical
    Sunday at 19:00", that lifts the baseline for every other month, and a June
    match then shows a deficit it did not cause. Here the true football effect
    is exactly zero and January is 6 km/h fast; a correct estimator returns
    zero, and the pooled one returns something close to -6 * (january share).
    """
    dates = weekly("2026-01-04", 26)  # every Sunday, January into June
    frame = flat_panel(dates)
    january = pd.to_datetime(frame["date"]).dt.month == 1
    frame.loc[january, "speed"] += 6.0

    match_date = dates[22]  # a June Sunday, far from January
    panel = make_panel(frame)
    events = fixtures([{"date": match_date, "kickoff_bucket": KICKOFF_BUCKET}])
    study = ev.event_study(panel, events, NO_HOLIDAYS, window=4, weeks=4)

    assert study["delta"].abs().max() == pytest.approx(0.0, abs=1e-6), (
        "a seasonal level shift months away must not show up as a match effect"
    )

    # And the baseline the matched estimator used is the June level, not the
    # panel-wide average that January contaminates.
    assert study["baseline_speed"].iloc[0] == pytest.approx(30.0, abs=1e-6)


def test_a_placebo_band_on_a_quiet_panel_is_centred_on_zero():
    """The estimator's own self-test: no effect anywhere means no band anywhere."""
    dates = weekly("2026-02-01", 20)
    panel = make_panel(flat_panel(dates))
    events = fixtures([{"date": dates[10], "kickoff_bucket": KICKOFF_BUCKET}])

    null = ev.placebo(panel, events, NO_HOLIDAYS, window=2, draws=40)
    band = null.band()
    assert band["p50"].abs().max() == pytest.approx(0.0, abs=1e-9)
    assert band["p95"].abs().max() == pytest.approx(0.0, abs=1e-9)


def test_a_seeded_placebo_run_is_reproducible():
    """The page's numbers must not move when someone touches an unrelated slider."""
    dates = weekly("2026-02-01", 20)
    frame = flat_panel(dates)
    rng = np.random.default_rng(0)
    frame["speed"] += rng.normal(0, 2.0, len(frame))
    panel = make_panel(frame)
    events = fixtures([{"date": dates[10], "kickoff_bucket": KICKOFF_BUCKET}])

    first = ev.placebo(panel, events, NO_HOLIDAYS, window=2, draws=25, seed=7)
    second = ev.placebo(panel, events, NO_HOLIDAYS, window=2, draws=25, seed=7)
    other = ev.placebo(panel, events, NO_HOLIDAYS, window=2, draws=25, seed=8)

    np.testing.assert_allclose(first.deltas, second.deltas)
    assert not np.allclose(first.deltas, other.deltas), (
        "a different seed should draw different placebo dates"
    )


# --- are the control days the right ones? -------------------------------------


def test_control_days_exclude_other_fixtures_and_every_kind_of_holiday():
    dates = pd.DatetimeIndex(pd.to_datetime(weekly("2026-03-01", 9)))
    events = fixtures(
        [
            {"date": "2026-03-29", "kickoff_bucket": KICKOFF_BUCKET},
            {"date": "2026-03-15", "kickoff_bucket": KICKOFF_BUCKET},
        ]
    )
    holidays = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-03-08", "2026-03-22", "2026-04-05"]),
            "kind": ["public", "working", "bridge"],
        }
    )

    chosen = ev.control_dates(
        pd.Timestamp("2026-03-29"),
        available=dates,
        blocked=ev.blocked_dates(events, holidays),
        weeks=4,
    )
    assert pd.Timestamp("2026-03-15") not in chosen, "another fixture is not a control"
    assert pd.Timestamp("2026-03-22") not in chosen, "a working holiday is not a control"
    assert pd.Timestamp("2026-03-08") not in chosen, "a public holiday is not a control"
    assert pd.Timestamp("2026-04-05") not in chosen, "a bridge day is not a control"
    assert pd.Timestamp("2026-03-29") not in chosen, "the match day is not its own control"
    assert set(chosen) == {pd.Timestamp("2026-03-01"), pd.Timestamp("2026-04-12"),
                           pd.Timestamp("2026-04-19"), pd.Timestamp("2026-04-26")}


def test_control_days_are_the_same_weekday_and_inside_the_window():
    every_day = pd.DatetimeIndex(pd.date_range("2026-01-01", "2026-12-31"))
    target = pd.Timestamp("2026-06-21")  # a Sunday
    chosen = ev.control_dates(target, available=every_day, blocked=set(), weeks=2)

    assert all(d.dayofweek == target.dayofweek for d in chosen), "same weekday only"
    assert all(abs((d - target).days) <= 14 for d in chosen), "inside +/- 2 weeks"
    assert len(chosen) == 4


def test_unfiltered_fixtures_still_block_control_days():
    """Selecting one competition must not turn the others into ordinary days.

    Measuring the World Cup while quietly using Libertadores nights as controls
    would put part of the treatment into the comparison and bias every effect
    toward zero. `all_events` exists to prevent that, and this test injects an
    effect on a fixture that is *not* selected: if it leaked into the baseline,
    the measured effect would shrink.
    """
    dates = weekly("2026-04-05", 11)
    selected_date, other_date = dates[5], dates[6]
    frame = flat_panel(dates)
    frame.loc[
        (frame["date"] == selected_date) & (frame["bucket"] == KICKOFF_BUCKET), "speed"
    ] += 4.0
    frame.loc[
        (frame["date"] == other_date) & (frame["bucket"] == KICKOFF_BUCKET), "speed"
    ] += 10.0

    panel = make_panel(frame)
    all_fixtures = fixtures(
        [
            {"date": selected_date, "kickoff_bucket": KICKOFF_BUCKET, "tier": "wanted"},
            {"date": other_date, "kickoff_bucket": KICKOFF_BUCKET, "tier": "other"},
        ]
    )
    selected = all_fixtures[all_fixtures["tier"] == "wanted"]

    guarded = ev.event_study(
        panel, selected, NO_HOLIDAYS, window=2, all_events=all_fixtures
    )
    leaky = ev.event_study(panel, selected, NO_HOLIDAYS, window=2)

    assert guarded.loc[guarded["rel"] == 0, "delta"].iloc[0] == pytest.approx(4.0)
    assert leaky.loc[leaky["rel"] == 0, "delta"].iloc[0] < 4.0, (
        "without all_events the unselected fixture contaminates the baseline"
    )


def test_dry_matching_keeps_a_wet_control_day_out_of_the_baseline():
    """A wet control day makes a dry match night look like a football effect.

    Rain costs about 0.9 km/h city-wide. That is small next to a World Cup
    evening and not at all small next to the pre-kick-off window, so a baseline
    built partly from wet days quietly credits the weather to the match. Here
    two of the eight control days are wet and 6 km/h slow, the match night is
    dry, and the true effect is zero.
    """
    dates = weekly("2026-03-01", 9)
    match_date = dates[4]
    wet = [dates[2], dates[6]]

    frame = flat_panel(dates)
    frame.loc[frame["date"].isin(wet), "speed"] -= 6.0

    panel = make_panel(frame, wet_dates=wet)
    events = fixtures([{"date": match_date, "kickoff_bucket": KICKOFF_BUCKET}])

    soaked = ev.event_study(panel, events, NO_HOLIDAYS, window=2)
    matched = ev.event_study(panel, events, NO_HOLIDAYS, window=2, dry_only=True)

    assert soaked["delta"].max() > 1.0, (
        "with wet days in the baseline the dry match night looks fast"
    )
    assert matched["delta"].abs().max() == pytest.approx(0.0, abs=1e-6), (
        "matching dry to dry leaves nothing behind"
    )


# --- does the ring separate the two mechanisms? -------------------------------


def test_the_ring_difference_cancels_a_city_wide_shock():
    """A broadcast effect hits every sensor, so the near-minus-far reads zero.

    This is the whole reason `ring_study` exists: on a Centenario night the city
    empties and the neighbourhood clogs at the same moment, and the near ring on
    its own cannot tell the two apart.
    """
    dates = weekly("2026-02-08", 9)
    match_date = dates[4]
    frame = flat_panel(dates, n_sites=6)
    sites = pd.DataFrame(
        {
            "site_id": range(6),
            "street": [f"Street {i}" for i in range(6)],
            "is_usable": True,
            "has_location": True,
            # Three sensors on top of the venue, three about 9 km east of it.
            "lat": [-34.894] * 3 + [-34.894] * 3,
            "lon": [-56.153] * 3 + [-56.053] * 3,
        }
    )
    at_kickoff = (frame["date"] == match_date) & (frame["bucket"] == KICKOFF_BUCKET)
    frame.loc[at_kickoff, "speed"] += 5.0  # city-wide: every sensor, same size

    panel = make_panel(frame, sites=sites)
    events = fixtures([{"date": match_date, "kickoff_bucket": KICKOFF_BUCKET}])
    ring = ev.ring_study(
        panel, events, NO_HOLIDAYS, sites, venue=(-34.894, -56.153), window=2
    )

    row = ring.loc[ring["rel"] == 0].iloc[0]
    assert row["near"] == pytest.approx(5.0, abs=1e-6)
    assert row["far"] == pytest.approx(5.0, abs=1e-6)
    assert row["did"] == pytest.approx(0.0, abs=1e-6), (
        "a shock with no spatial gradient must leave nothing behind"
    )


def test_the_ring_difference_keeps_an_effect_that_is_only_near_the_ground():
    dates = weekly("2026-02-08", 9)
    match_date = dates[4]
    frame = flat_panel(dates, n_sites=6)
    sites = pd.DataFrame(
        {
            "site_id": range(6),
            "street": [f"Street {i}" for i in range(6)],
            "is_usable": True,
            "has_location": True,
            "lat": [-34.894] * 6,
            "lon": [-56.153] * 3 + [-56.053] * 3,
        }
    )
    near_only = (
        (frame["date"] == match_date)
        & (frame["bucket"] == KICKOFF_BUCKET)
        & (frame["site_id"] < 3)
    )
    frame.loc[near_only, "speed"] -= 8.0  # ingress congestion, local

    panel = make_panel(frame, sites=sites)
    events = fixtures([{"date": match_date, "kickoff_bucket": KICKOFF_BUCKET}])
    ring = ev.ring_study(
        panel, events, NO_HOLIDAYS, sites, venue=(-34.894, -56.153), window=2
    )

    row = ring.loc[ring["rel"] == 0].iloc[0]
    assert row["did"] == pytest.approx(-8.0, abs=1e-6)


def test_the_ring_placebo_pairs_both_rings_on_the_same_placebo_dates():
    """Otherwise the null for the difference is the sum of two unrelated nulls.

    The near and far placebo runs have to land on identical dates so that
    subtracting them cancels the city-wide swing exactly as the real estimator
    does. Pairing unrelated days would roughly double the band — conservative,
    but it would bury the egress signal, which is the one number on this page
    that is genuinely about stadium traffic.
    """
    dates = weekly("2026-02-08", 14)
    frame = flat_panel(dates, n_sites=6)
    rng = np.random.default_rng(1)
    # A big day-level shock shared by every sensor: exactly what the difference
    # is supposed to cancel.
    for date in dates:
        frame.loc[frame["date"] == date, "speed"] += rng.normal(0, 4.0)

    sites = pd.DataFrame(
        {
            "site_id": range(6),
            "street": [f"Street {i}" for i in range(6)],
            "is_usable": True,
            "has_location": True,
            "lat": [-34.894] * 6,
            "lon": [-56.153] * 3 + [-56.053] * 3,
        }
    )
    panel = make_panel(frame, sites=sites)
    events = fixtures([{"date": dates[7], "kickoff_bucket": KICKOFF_BUCKET}])

    paired = ev.ring_placebo(
        panel, events, NO_HOLIDAYS, sites, venue=(-34.894, -56.153),
        window=2, draws=30, seed=3,
    )
    assert paired.deltas.shape[0] == 30
    # A shock with no spatial gradient cancels exactly, every draw.
    assert np.nanmax(np.abs(paired.deltas)) == pytest.approx(0.0, abs=1e-6)


def test_a_window_effect_works_on_a_frame_with_no_baseline_speed():
    """A ring difference has no single speed underneath it.

    `ring_study` returns near, far and their difference but no baseline, and the
    page reads window numbers off the difference. Requiring the column crashed
    the ring view; the delta is still well defined, only the percentage is not.
    """
    did = pd.DataFrame(
        {
            "rel": [-1, 0, 1],
            "minutes": [-30, 0, 30],
            "delta": [-2.0, -1.0, 0.0],
            "samples": [100.0, 100.0, 100.0],
        }
    )
    effect = ev.window_effect(did, (-30, 0))
    assert effect["delta"] == pytest.approx(-2.0)
    assert np.isnan(effect["pct"]), "no baseline means no percentage, not a fake one"
    assert effect["samples"] == pytest.approx(100.0)


def test_sensors_without_a_location_are_never_placed_in_a_ring():
    """The feed parks 41 sensors on one placeholder coordinate out in the bay.

    Four of them claim to be on Camino Carrasco, which is the corridor the
    Penarol effect is measured on, so treating that coordinate as a position
    would put real readings from unknown places into a distance band.
    """
    sites = pd.DataFrame(
        {
            "site_id": [1, 2],
            "lat": [-34.894, -34.890561],
            "lon": [-56.153, -56.220631],
            "has_location": [True, False],
        }
    )
    distance = ev.site_distances(sites, -34.894, -56.153)
    assert distance.iloc[0] == pytest.approx(0.0, abs=1e-6)
    assert np.isinf(distance.iloc[1]), "a placeholder coordinate has no distance"


# --- does it refuse to guess? -------------------------------------------------


def test_a_fixture_with_no_kick_off_time_is_skipped_rather_than_assumed():
    dates = weekly("2026-03-01", 9)
    panel = make_panel(flat_panel(dates))
    events = fixtures(
        [
            {"date": dates[4], "kickoff_bucket": pd.NA},
            {"date": dates[5], "kickoff_bucket": KICKOFF_BUCKET},
        ]
    )
    study = ev.event_study(panel, events, NO_HOLIDAYS, window=2)
    assert study["n_events"].max() == 1, "only the dated fixture is measured"

    table = ev.event_table(panel, events, NO_HOLIDAYS, window=2)
    assert len(table) == 2, "the undated fixture is still listed"
    assert table.loc[table["date"] == pd.Timestamp(dates[4]), "note"].iloc[0] != ""
    assert pd.isna(table.loc[table["date"] == pd.Timestamp(dates[4]), "during"].iloc[0])


def test_an_empty_fixture_selection_returns_an_empty_frame_not_an_error():
    dates = weekly("2026-03-01", 5)
    panel = make_panel(flat_panel(dates))
    empty = fixtures([{"date": dates[0], "kickoff_bucket": pd.NA}]).iloc[0:0]
    assert ev.event_study(panel, empty, NO_HOLIDAYS).empty
    assert ev.placebo(panel, empty, NO_HOLIDAYS).deltas.size == 0


def test_buckets_below_the_sample_floor_do_not_reach_the_average():
    """A sensor with a handful of readings should not swing a half-hour."""
    dates = weekly("2026-03-01", 9)
    match_date = dates[4]
    frame = flat_panel(dates, n_sites=2)
    thin = (frame["site_id"] == 1) & (frame["bucket"] == KICKOFF_BUCKET)
    frame.loc[thin, "n_moving"] = 2.0
    frame.loc[thin & (frame["date"] == match_date), "speed"] += 50.0

    panel = make_panel(frame)
    events = fixtures([{"date": match_date, "kickoff_bucket": KICKOFF_BUCKET}])
    study = ev.event_study(panel, events, NO_HOLIDAYS, window=1, min_samples=10)

    assert study.loc[study["rel"] == 0, "delta"].iloc[0] == pytest.approx(0.0, abs=1e-6)


# --- the real calendars --------------------------------------------------------


def test_the_committed_fixture_list_parses_and_every_row_carries_a_source():
    events = ev.load_events()
    assert len(events) > 0
    assert events["date"].is_monotonic_increasing

    # Every row says where it came from. Most cite a URL; a row sourced directly
    # from the repository owner says so, which is a weaker provenance than a link
    # but a far better one than an unlabelled guess.
    cited = events["source"].str.startswith(("http", "curator:"))
    assert cited.all(), f"unsourced rows: {list(events.loc[~cited, 'date'])}"

    dated = events[events["kickoff_bucket"].notna()]
    assert dated["kickoff_bucket"].between(0, 47).all()
    # Rows the curator has not been able to verify a time for are labelled, not
    # quietly dropped and not filled in with a plausible-looking guess.
    undated = events[events["kickoff_bucket"].isna()]
    assert (undated["verified"] == "time-unknown").all()


def test_fixtures_held_out_of_the_study_still_block_their_own_dates():
    """`blocked` is not a competition, but it is still a day nothing is like.

    Removing a fixture from the file entirely would hand its date back as an
    ordinary control day. The Supercopa final is a clásico at the Centenario and
    the Intermedio final is a Peñarol final: neither belongs in the study, and
    neither is an ordinary evening to measure another match against.
    """
    events = ev.load_events()
    holidays = ev.load_holidays()
    held_out = events[events["tier"] == "blocked"]
    assert not held_out.empty
    assert set(held_out["date"]) <= ev.blocked_dates(events, holidays)


def test_the_holiday_list_contains_the_day_that_looks_most_like_a_match():
    """18 July 2026, Jura de la Constitucion: 21 anomalous half hours."""
    holidays = ev.load_holidays()
    assert pd.Timestamp("2026-07-18") in set(holidays["date"])
    assert pd.Timestamp("2026-07-18") not in set(ev.load_events()["date"])
