"""Football matches crossed with the speed panel.

The question is whether a high-profile match moves traffic, and the whole
difficulty is in the word *moves*: moved compared to what? Montevideo has no
second, football-free copy of itself to compare against, so the counterfactual
has to be built, and building it badly is the failure mode this module exists to
avoid.

Two mechanisms are in play and they point opposite ways.

  Broadcast.  Almost everyone watches on television. In the hours before
  kick-off people travel to wherever they are going to watch, and during the
  match the streets empty. The first shows up as *slower* traffic, the second as
  faster. It is city-wide and it has no stadium.

  Stadium.  A match played in Montevideo pulls tens of thousands of people to
  one point and then releases them at once. It is local, it is sharper on the
  way out than on the way in, and it is invisible more than a kilometre or two
  away.

On a Centenario night both fire at the same time, in opposite directions, which
is why `ring_study` exists.

The estimator is a difference-in-differences in matched-pairs form. For every
(sensor, half-hour) touched by a match, the counterfactual is *that same sensor,
that same half-hour, that same weekday*, on nearby dates with no match and no
holiday. Then the differences are aggregated, weighted by how much data the
treated side has.

Why not a pooled "typical day" baseline, which would be far less code: because
it does not work. Measured against a norm pooled over the whole Jan-Aug panel,
every June weekday afternoon in this panel reads 0.54 km/h slow, because January
-- when Montevideo empties for the summer -- is in the same average and sits
2.07 km/h above it. That artefact has the same sign and roughly the same size as
the pre-kick-off congestion this page was built to look for. A pooled baseline
does not merely add noise here; it manufactures the finding.

Why not a regression: the repo has no statsmodels and no scipy, and a two-way
fixed-effects fit would give the same number with a dependency and a layer of
opacity added. The matched form is the same estimator written out.

Why permutation rather than a t-test: 4.4 million rows make every p-value zero,
and the observations are anything but independent -- one slow Tuesday moves
hundreds of them together. `placebo_band` re-runs the identical estimator on
fixtures moved to dates when nothing happened, which prices in exactly that
correlation. It also self-tests: a band not centred on zero means the baseline
is wrong.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from mvdspeed.config import (
    BUCKET_MINUTES,
    CONTROL_WEEKS,
    EVENT_WINDOW_BUCKETS,
    FAR_RING_KM,
    HOLIDAYS_CSV,
    KM_PER_DEG_LAT,
    MATCH_MINUTES,
    MATCHES_CSV,
    MIN_SAMPLES,
    NEAR_RING_KM,
    PLACEBO_DRAWS,
    PLACEBO_SEED,
    km_per_deg_lon,
)
from mvdspeed.data import BUCKETS_PER_DAY, Dataset, bucket_label

# The windows the headline numbers are read off, in minutes from kick-off.
#
# The pre-kick-off window is one bucket, not the ninety minutes it started as.
# Whatever happens before a match happens late, and a wider window averages it
# against quiet time until it disappears: around the Gran Parque Central the
# ingress effect is -1.57 km/h over the last half hour (p = 0.004) and -0.45
# over the last ninety (p = 0.16), and city-wide before a Uruguay match it is
# +1.31 km/h (p = 0.032) against +0.56 (p = 0.29). Same data, same estimator;
# the only difference is how much nothing is averaged in with it.
#
# The two windows after kick-off stay wide because the effects they measure
# really do last that long -- a match, and then everybody leaving at once.
PRE_WINDOW = (-BUCKET_MINUTES, 0)
DURING_WINDOW = (0, MATCH_MINUTES)
POST_WINDOW = (MATCH_MINUTES, MATCH_MINUTES + 90)

# Sensors on the road out to the Campeon del Siglo. There is not one sensor
# within five kilometres of that stadium, so a ring around it is empty and the
# only measurable trace of a Penarol home match is on the way there. These are
# the street labels of the continuous axis from Tres Cruces east.
CORRIDOR_STREETS = ("8 de Octubre", "Camino Carrasco", "Cno Carrasco")

# Rings for the distance-decay view, in kilometres from the ground. Tight where
# the effect changes fastest and wide out where it has flattened; at the
# Centenario and the Gran Parque Central these hold roughly 26-32 sensors in the
# innermost band and over a hundred in the next, which is enough for each point
# on the curve to mean something.
DISTANCE_EDGES_KM = (0.0, 1.0, 2.0, 3.0, 5.0, 8.0, 12.0)

# Distance bands along that axis, in kilometres from the stadium. They start at
# 8 because that is where the sensors start: the 24 locatable sites on this
# corridor span 8.1 km to 14.3 km out, so bands any nearer would be drawn empty
# and would suggest a coverage this network does not have. Four to eight sensors
# land in each band.
CORRIDOR_EDGES_KM = (8.0, 9.0, 10.5, 12.0, 15.0)

# Sentinel column for an event with fewer control dates than its neighbours.
# Any slot far enough outside the panel reads back as NaN and drops out.
_MISSING_SLOT = -1_000_000


def load_events(path=MATCHES_CSV) -> pd.DataFrame:
    """The hand-typed fixture list, with kick-off resolved to a bucket.

    Rows whose kick-off time is unknown are kept, not dropped: the file is the
    record of what has been checked, and silently losing a fixture would make
    the panel look more complete than it is. They carry `kickoff_bucket` as NA
    and every estimator here skips them, so the count of usable events is always
    smaller than or equal to the count of known fixtures and the page says so.
    """
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found -- the fixture list is hand-curated and committed; "
            "it is not produced by any of the mvdspeed-* commands"
        )
    events = pd.read_csv(path, dtype={"kickoff_local": "string"})
    events["date"] = pd.to_datetime(events["date"])
    events["is_national_team"] = (
        events["is_national_team"].astype("string").str.lower() == "true"
    )

    parsed = pd.to_datetime(events["kickoff_local"], format="%H:%M", errors="coerce")
    minutes = parsed.dt.hour * 60 + parsed.dt.minute
    events["kickoff_bucket"] = (minutes // BUCKET_MINUTES).astype("Int16")
    events["kickoff"] = events["kickoff_bucket"].map(
        lambda b: bucket_label(int(b)) if pd.notna(b) else ""
    )

    events["label"] = (
        events["home"] + " v " + events["away"] + " · " + events["date"].dt.strftime("%d %b")
    )
    events["in_montevideo"] = events["venue_lat"].notna() & events["venue_lon"].notna()
    return events.sort_values("date").reset_index(drop=True)


def load_holidays(path=HOLIDAYS_CSV) -> pd.DataFrame:
    """Public holidays, working holidays and the bridge days around them.

    All three kinds are treated the same way -- as dates that can never serve as
    a control -- even though only some are legally non-working. What matters
    here is whether traffic looked like an ordinary weekday, and Carnival
    Tuesday does not, whatever the labour code says about it.

    Saturday 18 July 2026, Jura de la Constitucion, is why this file exists: in
    the raw panel it lights up across 21 consecutive half hours and is otherwise
    indistinguishable from a match day.
    """
    if not path.exists():
        raise FileNotFoundError(f"{path} not found -- see load_events() for why")
    holidays = pd.read_csv(path)
    holidays["date"] = pd.to_datetime(holidays["date"])
    return holidays.sort_values("date").reset_index(drop=True)


@dataclass(frozen=True)
class EventPanel:
    """The measurements as a dense sensor-by-half-hour grid.

    Every estimator below is a gather of a few hundred columns out of this, so
    the panel is built once and indexed thereafter. The alternative -- filtering
    a 4.4-million-row frame once per event per placebo draw -- is the same
    arithmetic several thousand times slower, and 500 placebo draws is what
    makes the difference matter.

    Stored as float32, which is exact for this data rather than an
    approximation: published speeds are whole km/h, so `speed_sum` is an integer
    below about 4,300 and the counts are smaller still. Summing eight control
    days keeps every value inside the range float32 represents exactly. The
    divisions are done in float64.

    Column index is a half-hour slot counted from `origin`, so a 21:30 kick-off
    plus four hours crosses midnight without special-casing -- which the naive
    `bucket + rel` arithmetic gets wrong, silently, by wrapping back to the same
    morning.
    """

    speed_sum: np.ndarray
    n_moving: np.ndarray
    n_zero: np.ndarray
    site_ids: np.ndarray
    origin: pd.Timestamp
    dates: pd.DatetimeIndex
    dry_slots: np.ndarray | None

    @property
    def n_sites(self) -> int:
        return int(self.speed_sum.shape[0])

    @property
    def n_slots(self) -> int:
        return int(self.speed_sum.shape[1])

    def slot(self, date: pd.Timestamp, bucket: int) -> int:
        return int((date - self.origin).days) * BUCKETS_PER_DAY + int(bucket)

    def rows_for(self, site_ids=None) -> np.ndarray:
        """Positional index of the requested sensors, or of all of them."""
        if site_ids is None:
            return np.arange(self.n_sites)
        wanted = np.asarray(list(site_ids))
        return np.flatnonzero(np.isin(self.site_ids, wanted))


def build_panel(data: Dataset, *, only_usable: bool = True) -> EventPanel:
    """Lay the measurements out as a dense grid, once."""
    frame = data._usable(data.measurements) if only_usable else data.measurements
    site_ids = np.sort(frame["site_id"].unique())
    positions = pd.Series(np.arange(len(site_ids)), index=site_ids)

    origin = pd.Timestamp(frame["date"].min()).normalize()
    dates = pd.DatetimeIndex(sorted(frame["date"].unique()))
    span = int((dates.max() - origin).days) + 1
    shape = (len(site_ids), span * BUCKETS_PER_DAY)

    row = positions.loc[frame["site_id"]].to_numpy()
    col = (
        (frame["date"] - origin).dt.days.to_numpy() * BUCKETS_PER_DAY
        + frame["bucket"].to_numpy()
    )

    grids = {}
    for name in ("speed_sum", "n_moving", "n_zero"):
        grid = np.full(shape, np.nan, dtype=np.float32)
        grid[row, col] = frame[name].to_numpy(dtype=np.float32)
        grids[name] = grid

    return EventPanel(
        speed_sum=grids["speed_sum"],
        n_moving=grids["n_moving"],
        n_zero=grids["n_zero"],
        site_ids=site_ids,
        origin=origin,
        dates=dates,
        dry_slots=_dry_slots(data, origin, shape[1]),
    )


def _dry_slots(data: Dataset, origin: pd.Timestamp, n_slots: int) -> np.ndarray | None:
    """Which half hours had dry roads, as a flag per slot rather than per row.

    Rain here comes from a single station, so it is a property of the hour and
    not of the sensor -- which makes weather matching a mask over columns, and
    costs one boolean array instead of a join. `recently_wet` rather than
    `is_wet` is the test, matching the app's dry baseline: a road is still wet
    for a while after the rain stops. Hours the station did not report are not
    dry, because a missing reading is not evidence of anything.
    """
    if data.weather is None:
        return None
    dry = np.zeros(n_slots, dtype=bool)
    weather = data.weather
    day = (weather["date"] - origin).dt.days.to_numpy()
    hour = weather["hour"].to_numpy()
    known_dry = (weather["recently_wet"] == False).fillna(False).to_numpy()  # noqa: E712
    for half in (0, 1):
        slot = day * BUCKETS_PER_DAY + hour * 2 + half
        inside = (slot >= 0) & (slot < n_slots)
        dry[slot[inside]] = known_dry[inside]
    return dry


# --- control matching ---------------------------------------------------------


def blocked_dates(events: pd.DataFrame, holidays: pd.DataFrame) -> set:
    """Dates that may never stand in for an ordinary day.

    Every fixture in the file blocks its own date, not just the ones currently
    selected in the UI. A Libertadores Tuesday is a poor control for a World Cup
    Monday even when the page is only asking about the World Cup, and letting
    the filtered-out matches back in as controls would bias every effect toward
    zero by putting some of the treatment in the comparison.
    """
    return set(events["date"]) | set(holidays["date"])


def control_dates(
    target: pd.Timestamp,
    *,
    available: pd.DatetimeIndex,
    blocked: set,
    weeks: int = CONTROL_WEEKS,
) -> list[pd.Timestamp]:
    """Same weekday, nearby, nothing happening."""
    span = pd.Timedelta(days=weeks * 7)
    same_weekday = available[available.dayofweek == target.dayofweek]
    near = same_weekday[
        (same_weekday >= target - span)
        & (same_weekday <= target + span)
        & (same_weekday != target)
    ]
    return [d for d in near if d not in blocked]


def _slot_matrix(
    panel: EventPanel,
    targets: list[tuple[pd.Timestamp, int]],
    *,
    blocked: set,
    weeks: int,
) -> tuple[np.ndarray, np.ndarray, list[int]]:
    """Kick-off slots and their control slots, padded to a rectangle."""
    kickoffs, controls = [], []
    for date, bucket in targets:
        dates = control_dates(date, available=panel.dates, blocked=blocked, weeks=weeks)
        kickoffs.append(panel.slot(date, bucket))
        controls.append([panel.slot(d, bucket) for d in dates])

    width = max((len(c) for c in controls), default=0)
    matrix = np.full((len(targets), max(width, 1)), _MISSING_SLOT, dtype=np.int64)
    for i, slots in enumerate(controls):
        matrix[i, : len(slots)] = slots
    return np.asarray(kickoffs, dtype=np.int64), matrix, [len(c) for c in controls]


def _take(
    grid: np.ndarray, columns: np.ndarray, allowed: np.ndarray | None = None
) -> np.ndarray:
    """Gather columns, reading NaN for anything off the end of the panel.

    `allowed` is an optional per-slot filter -- used for dry-roads-only matching,
    where a wet half hour has to drop out of both the match night and its
    controls rather than out of one side of the comparison.
    """
    clipped = np.clip(columns, 0, grid.shape[1] - 1)
    inside = (columns >= 0) & (columns < grid.shape[1])
    if allowed is not None:
        inside = inside & allowed[clipped]
    gathered = grid[:, clipped]
    return np.where(inside[None, ...], gathered, np.nan)


def _measure(
    panel: EventPanel,
    kickoffs: np.ndarray,
    controls: np.ndarray,
    rels: np.ndarray,
    *,
    rows: np.ndarray,
    include_zeros: bool,
    min_samples: int,
    with_standstill: bool = True,
    allowed: np.ndarray | None = None,
) -> dict[str, np.ndarray]:
    """The estimator itself: matched differences, aggregated per relative bucket.

    Returns arrays indexed by `rels`. The aggregation is a weighted mean of
    *within-sensor* differences, never a difference of two pooled averages: a
    sensor that reported on the match night but not on half its control nights
    would otherwise shift the comparison simply by being present on one side.
    This is the same stratify-then-combine shape as `data.rain_penalty`, and for
    the same reason.
    """
    speed = panel.speed_sum[rows]
    moving = panel.n_moving[rows]
    zero = panel.n_zero[rows]
    total = moving + zero
    denominator = total if include_zeros else moving

    treated_cols = kickoffs[:, None] + rels[None, :]
    control_cols = controls[:, :, None] + rels[None, None, :]

    t_sum = _take(speed, treated_cols, allowed)
    t_den = _take(denominator, treated_cols, allowed)
    c_sum = np.nansum(_take(speed, control_cols, allowed), axis=2)
    c_den = np.nansum(_take(denominator, control_cols, allowed), axis=2)

    usable = (
        np.isfinite(t_sum)
        & (t_den >= min_samples)
        & np.isfinite(c_sum)
        & (c_den >= min_samples)
    )
    weight = np.where(usable, t_den, 0.0).astype(np.float64)
    total_weight = weight.sum(axis=(0, 1))

    with np.errstate(invalid="ignore", divide="ignore"):
        treated_speed = t_sum.astype(np.float64) / t_den
        baseline_speed = c_sum.astype(np.float64) / c_den
    delta = np.where(usable, treated_speed - baseline_speed, 0.0)
    baseline = np.where(usable, baseline_speed, 0.0)

    safe = np.where(total_weight > 0, total_weight, np.nan)
    out = {
        "delta": (delta * weight).sum(axis=(0, 1)) / safe,
        "baseline_speed": (baseline * weight).sum(axis=(0, 1)) / safe,
        "samples": total_weight,
        "n_sites": (usable.any(axis=1)).sum(axis=0).astype(float),
        "n_events": (usable.any(axis=0)).sum(axis=0).astype(float),
    }
    out["treated_speed"] = out["baseline_speed"] + out["delta"]
    with np.errstate(invalid="ignore", divide="ignore"):
        out["pct"] = out["delta"] / out["baseline_speed"]

    if with_standstill:
        t_stop = _take(zero, treated_cols, allowed)
        t_all = _take(total, treated_cols, allowed)
        c_stop = np.nansum(_take(zero, control_cols, allowed), axis=2)
        c_all = np.nansum(_take(total, control_cols, allowed), axis=2)
        stop_ok = usable & (t_all > 0) & (c_all > 0)
        stop_weight = np.where(stop_ok, t_all, 0.0).astype(np.float64)
        with np.errstate(invalid="ignore", divide="ignore"):
            share = t_stop.astype(np.float64) / t_all - c_stop.astype(np.float64) / c_all
        share = np.where(stop_ok, share, 0.0)
        stop_total = stop_weight.sum(axis=(0, 1))
        out["standstill_delta"] = (share * stop_weight).sum(axis=(0, 1)) / np.where(
            stop_total > 0, stop_total, np.nan
        )
    return out


def _targets(events: pd.DataFrame) -> list[tuple[pd.Timestamp, int]]:
    dated = events[events["kickoff_bucket"].notna()]
    return [(row.date, int(row.kickoff_bucket)) for row in dated.itertuples()]


def _rels(window: int) -> np.ndarray:
    return np.arange(-window, window + 1, dtype=np.int64)


def event_study(
    panel: EventPanel,
    events: pd.DataFrame,
    holidays: pd.DataFrame,
    *,
    site_ids=None,
    window: int = EVENT_WINDOW_BUCKETS,
    weeks: int = CONTROL_WEEKS,
    include_zeros: bool = False,
    min_samples: int = MIN_SAMPLES,
    dry_only: bool = False,
    all_events: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Speed against its matched counterfactual, by distance from kick-off.

    `events` is the selection being measured; `all_events` is the full fixture
    list, used only to keep every other match out of the control days. They are
    separate arguments because filtering the page down to, say, the World Cup
    must not quietly turn the Libertadores nights back into ordinary Tuesdays.

    `dry_only` drops every half hour with wet or recently wet roads from both
    sides. Rain costs about 0.9 km/h city-wide, which is small next to a World
    Cup evening but not next to the pre-kick-off window, and a wet match night
    compared against dry controls would read as congestion.
    """
    targets = _targets(events)
    if not targets:
        return pd.DataFrame(
            columns=["rel", "minutes", "delta", "pct", "treated_speed",
                     "baseline_speed", "standstill_delta", "samples", "n_sites",
                     "n_events"]
        )
    blocked = blocked_dates(all_events if all_events is not None else events, holidays)
    kickoffs, controls, _ = _slot_matrix(panel, targets, blocked=blocked, weeks=weeks)
    rels = _rels(window)
    measured = _measure(
        panel,
        kickoffs,
        controls,
        rels,
        rows=panel.rows_for(site_ids),
        include_zeros=include_zeros,
        min_samples=min_samples,
        allowed=panel.dry_slots if dry_only else None,
    )
    return pd.DataFrame({"rel": rels, "minutes": rels * BUCKET_MINUTES, **measured})


def ring_study(
    panel: EventPanel,
    events: pd.DataFrame,
    holidays: pd.DataFrame,
    sites: pd.DataFrame,
    *,
    venue: tuple[float, float],
    near_km: float = NEAR_RING_KM,
    far_km: float = FAR_RING_KM,
    **kwargs,
) -> pd.DataFrame:
    """Near the stadium minus far from it, per relative bucket.

    On a match night in Montevideo the broadcast effect and the stadium effect
    happen at once and pull opposite ways, so neither is readable on its own.
    The far ring is exposed to the broadcast and not to the stadium, which makes
    it the control the near ring needs: `did` is what is left after the
    city-wide swing is subtracted, and it is the only number here that is about
    stadium traffic rather than about television.
    """
    distance = site_distances(sites, *venue)
    near = sites.loc[distance <= near_km, "site_id"]
    far = sites.loc[distance >= far_km, "site_id"]

    inner = event_study(panel, events, holidays, site_ids=near, **kwargs)
    outer = event_study(panel, events, holidays, site_ids=far, **kwargs)
    if inner.empty or outer.empty:
        return pd.DataFrame(columns=["rel", "minutes", "near", "far", "did"])

    merged = inner[
        ["rel", "minutes", "delta", "n_sites", "samples", "baseline_speed"]
    ].merge(outer[["rel", "delta", "n_sites"]], on="rel", suffixes=("_near", "_far"))
    return merged.rename(
        columns={
            "delta_near": "near",
            "delta_far": "far",
            "n_sites_near": "n_near",
            "n_sites_far": "n_far",
            # The near ring's own normal speed, so a difference can be quoted
            # against a level. Deliberately not carried into the `did` column's
            # frame: a difference of two differences has no baseline, and
            # dividing it by this one would invent a percentage.
            "baseline_speed": "near_baseline",
        }
    ).assign(did=lambda f: f["near"] - f["far"])


def ring_placebo(
    panel: EventPanel,
    events: pd.DataFrame,
    holidays: pd.DataFrame,
    sites: pd.DataFrame,
    *,
    venue: tuple[float, float],
    near_km: float = NEAR_RING_KM,
    far_km: float = FAR_RING_KM,
    seed: int = PLACEBO_SEED,
    **kwargs,
) -> Placebo:
    """Null distribution for the near-minus-far difference.

    Runs the placebo twice with the same seed. The draws depend only on the
    fixtures and the seed and never on which sensors are being read, so both
    runs land on identical placebo dates and differencing them gives the null
    for the difference itself. Two different seeds would pair unrelated days and
    inflate the band -- which would be the conservative mistake, but still a
    mistake, and it would hide the one result on this page that is genuinely
    about stadium traffic rather than television.
    """
    distance = site_distances(sites, *venue)
    near = sites.loc[distance <= near_km, "site_id"]
    far = sites.loc[distance >= far_km, "site_id"]
    inner = placebo(panel, events, holidays, site_ids=near, seed=seed, **kwargs)
    outer = placebo(panel, events, holidays, site_ids=far, seed=seed, **kwargs)
    if inner.deltas.size == 0 or outer.deltas.size == 0:
        return inner
    return Placebo(rels=inner.rels, deltas=inner.deltas - outer.deltas)


def site_distances(sites: pd.DataFrame, lat: float, lon: float) -> pd.Series:
    """Straight-line kilometres from a point to every locatable sensor.

    Sensors carrying the feed's placeholder coordinate are returned as infinity
    rather than as a distance, so they fall out of every ring and every corridor
    instead of landing wherever that placeholder happens to sit. Four of them
    claim to be on Camino Carrasco, which is exactly the corridor this page
    measures the Penarol effect on.
    """
    dx = (sites["lon"] - lon) * km_per_deg_lon(lat)
    dy = (sites["lat"] - lat) * KM_PER_DEG_LAT
    distance = np.hypot(dx, dy)
    return distance.where(sites["has_location"], np.inf)


def distance_study(
    panel: EventPanel,
    events: pd.DataFrame,
    holidays: pd.DataFrame,
    sites: pd.DataFrame,
    *,
    venue: tuple[float, float],
    edges: tuple[float, ...] = DISTANCE_EDGES_KM,
    streets: tuple[str, ...] | None = None,
    **kwargs,
) -> pd.DataFrame:
    """The effect as a function of distance from the ground, one study per band.

    This is the shape that shows why a city-wide average is the wrong
    instrument here. Around a ground the effect changes *sign* with distance --
    congested within a kilometre or two, emptier further out as everybody
    watches -- so the mean over all sensors is not a small effect but two large
    ones cancelling. On Libertadores home nights the city-wide figure is +0.75
    km/h, which reads as "nothing happened", while the same nights are moving
    the near bands and the far bands by more than a km/h each in opposite
    directions.

    `streets` narrows the bands to named roads, which is what `corridor_study`
    uses it for. Left None, every locatable sensor is binned.
    """
    chooseable = sites["street"].isin(streets) if streets is not None else True
    distance = site_distances(sites, *venue)
    out = []
    for lo, hi in zip(edges, edges[1:]):
        chosen = sites.loc[chooseable & (distance >= lo) & (distance < hi), "site_id"]
        if chosen.empty:
            continue
        study = event_study(panel, events, holidays, site_ids=chosen, **kwargs)
        if study.empty:
            continue
        study["band"] = f"{lo:g}–{hi:g} km"
        study["band_from"] = lo
        study["band_mid"] = (lo + hi) / 2
        out.append(study)
    if not out:
        return pd.DataFrame(
            columns=["rel", "minutes", "delta", "band", "band_from", "band_mid"]
        )
    return pd.concat(out, ignore_index=True)


def corridor_study(
    panel: EventPanel,
    events: pd.DataFrame,
    holidays: pd.DataFrame,
    sites: pd.DataFrame,
    *,
    venue: tuple[float, float],
    streets: tuple[str, ...] = CORRIDOR_STREETS,
    edges: tuple[float, ...] = CORRIDOR_EDGES_KM,
    **kwargs,
) -> pd.DataFrame:
    """The effect along an approach road, binned by distance from the ground.

    For the Campeon del Siglo this is all there is. The nearest sensor to that
    stadium is 8.1 km away, so there is no ring to draw and no near-side
    comparison to make; what a Penarol home match can leave behind is a gradient
    on the road out, strongest at the eastern end. A gradient that is flat, or
    that points the wrong way, is evidence against the effect -- which is the
    point of binning rather than reporting one corridor-wide number.
    """
    return distance_study(
        panel, events, holidays, sites,
        venue=venue, edges=edges, streets=streets, **kwargs,
    )


@dataclass(frozen=True)
class Placebo:
    """The null distribution: the same estimator run on days nothing happened.

    Keeps every draw rather than just the quantiles, because the band on the
    chart and the percentile beside a headline number have to come from the same
    distribution. Collapsing to quantiles first and then trying to place a
    window effect inside them compares two different things and quietly flatters
    whichever one is noisier.
    """

    rels: np.ndarray
    deltas: np.ndarray  # (draws, len(rels))

    def band(self) -> pd.DataFrame:
        quantiles = np.nanpercentile(self.deltas, [1, 5, 50, 95, 99], axis=0)
        return pd.DataFrame(
            {
                "rel": self.rels,
                "minutes": self.rels * BUCKET_MINUTES,
                "p01": quantiles[0],
                "p05": quantiles[1],
                "p50": quantiles[2],
                "p95": quantiles[3],
                "p99": quantiles[4],
                "draws": len(self.deltas),
            }
        )

    def _collapse(self, study: pd.DataFrame, window: tuple[int, int]):
        minutes = self.rels * BUCKET_MINUTES
        inside = (minutes >= window[0]) & (minutes < window[1])
        if not inside.any() or study.empty:
            return float("nan"), np.array([])
        aligned = study.set_index("rel").reindex(self.rels[inside])
        weight = np.nan_to_num(aligned["samples"].to_numpy(dtype=float))
        if weight.sum() == 0:
            return float("nan"), np.array([])
        measured = float(
            np.nansum(aligned["delta"].to_numpy(dtype=float) * weight) / weight.sum()
        )
        null = np.nansum(self.deltas[:, inside] * weight, axis=1) / weight.sum()
        return measured, null

    def percentile_of(self, study: pd.DataFrame, window: tuple[int, int]) -> float:
        """Share of placebo draws that came out below the measured effect."""
        measured, null = self._collapse(study, window)
        if not np.isfinite(measured) or null.size == 0:
            return float("nan")
        return float((null < measured).mean())

    def p_value(self, study: pd.DataFrame, window: tuple[int, int]) -> float:
        """Two-sided: how often a placebo swing was at least this big either way."""
        percentile = self.percentile_of(study, window)
        if not np.isfinite(percentile):
            return float("nan")
        return float(2 * min(percentile, 1 - percentile))


def placebo(
    panel: EventPanel,
    events: pd.DataFrame,
    holidays: pd.DataFrame,
    *,
    site_ids=None,
    window: int = EVENT_WINDOW_BUCKETS,
    weeks: int = CONTROL_WEEKS,
    include_zeros: bool = False,
    min_samples: int = MIN_SAMPLES,
    dry_only: bool = False,
    draws: int = PLACEBO_DRAWS,
    seed: int = PLACEBO_SEED,
    all_events: pd.DataFrame | None = None,
) -> Placebo:
    """How big a swing this estimator produces when nothing has happened.

    Each draw keeps the real fixtures' weekdays and kick-off times and moves
    them to dates with no match and no holiday, then runs the identical
    estimator. The spread of the results is the null: it prices in the fact that
    one unusual Tuesday moves hundreds of rows together, which is the assumption
    a t-test over this panel would get catastrophically wrong.

    Read it two ways. A real curve outside the 5th-95th band at kick-off is an
    effect; a *placebo* band not centred on zero is a broken baseline, and that
    self-test has caught more than it has confirmed.
    """
    rels = _rels(window)
    targets = _targets(events)
    if not targets:
        return Placebo(rels=rels, deltas=np.empty((0, len(rels))))

    blocked = blocked_dates(all_events if all_events is not None else events, holidays)
    eligible = pd.DatetimeIndex([d for d in panel.dates if d not in blocked])
    rows = panel.rows_for(site_ids)
    rng = np.random.default_rng(seed)

    # Group the eligible dates by weekday once; each draw then only has to pick
    # indices. Placebo fixtures keep the real weekday *and* the real kick-off
    # time, so the band is measured at the same times of day as the effect and
    # not against some average hour of the week.
    by_weekday = {
        day: eligible[eligible.dayofweek == day].to_numpy() for day in range(7)
    }

    results = np.empty((draws, len(rels)), dtype=np.float64)
    for draw in range(draws):
        moved = []
        for date, bucket in targets:
            pool = by_weekday[date.dayofweek]
            if len(pool) == 0:
                continue
            moved.append((pd.Timestamp(rng.choice(pool)), bucket))
        if not moved:
            results[draw] = np.nan
            continue
        kickoffs, controls, _ = _slot_matrix(
            panel, moved, blocked=blocked, weeks=weeks
        )
        results[draw] = _measure(
            panel,
            kickoffs,
            controls,
            rels,
            rows=rows,
            include_zeros=include_zeros,
            min_samples=min_samples,
            with_standstill=False,
            allowed=panel.dry_slots if dry_only else None,
        )["delta"]

    return Placebo(rels=rels, deltas=results)


def window_effect(study: pd.DataFrame, window: tuple[int, int]) -> dict[str, float]:
    """Collapse an event study to one number over a span of minutes.

    Weighted by each bucket's own sample count, so a half hour the sensors
    barely covered does not count as much as one they covered fully.

    `baseline_speed` is optional. A ring difference is a difference of two
    differences and has no single speed underneath it, so it gets a delta and no
    percentage rather than a percentage of some arbitrarily chosen denominator.
    """
    lo, hi = window
    part = study[(study["minutes"] >= lo) & (study["minutes"] < hi)]
    part = part[part["samples"] > 0]
    if part.empty:
        return {
            "delta": float("nan"), "pct": float("nan"),
            "baseline_speed": float("nan"), "samples": 0.0,
        }
    weight = part["samples"]
    delta = float((part["delta"] * weight).sum() / weight.sum())
    baseline = (
        float((part["baseline_speed"] * weight).sum() / weight.sum())
        if "baseline_speed" in part.columns
        else float("nan")
    )
    return {
        "delta": delta,
        "pct": delta / baseline if baseline else float("nan"),
        "baseline_speed": baseline,
        "samples": float(weight.sum()),
    }


def event_table(
    panel: EventPanel,
    events: pd.DataFrame,
    holidays: pd.DataFrame,
    *,
    site_ids=None,
    window: int = EVENT_WINDOW_BUCKETS,
    all_events: pd.DataFrame | None = None,
    **kwargs,
) -> pd.DataFrame:
    """One row per fixture, so heterogeneity is visible rather than averaged away.

    A dozen matches is far too few to trust any single row, and reporting only
    the pooled curve would hide that. The sample column is here to be looked at.

    `events` is what gets a row; `all_events` is what blocks control days. They
    are separate for the same reason they are separate in `event_study`: a
    fixture can be too minor to list and still be a day nothing else should be
    compared against.
    """
    blocking = all_events if all_events is not None else events
    rows = []
    for event in events.itertuples():
        if pd.isna(event.kickoff_bucket):
            rows.append(
                {
                    "date": event.date,
                    "label": event.label,
                    "competition": event.competition,
                    "tier": event.tier,
                    "kickoff": "",
                    "pre": float("nan"),
                    "during": float("nan"),
                    "post": float("nan"),
                    "samples": 0.0,
                    "note": "no kick-off time on file",
                }
            )
            continue
        one = events[events["date"] == event.date]
        study = event_study(
            panel, one, holidays, site_ids=site_ids, window=window,
            all_events=blocking, **kwargs,
        )
        rows.append(
            {
                "date": event.date,
                "label": event.label,
                "competition": event.competition,
                "tier": event.tier,
                "kickoff": event.kickoff,
                "pre": window_effect(study, PRE_WINDOW)["delta"],
                "during": window_effect(study, DURING_WINDOW)["delta"],
                "post": window_effect(study, POST_WINDOW)["delta"],
                "samples": float(study["samples"].sum()),
                "note": "",
            }
        )
    return pd.DataFrame(rows).sort_values("date").reset_index(drop=True)


def peak_shift(
    panel: EventPanel,
    events: pd.DataFrame,
    holidays: pd.DataFrame,
    *,
    site_ids=None,
    weeks: int = CONTROL_WEEKS,
    include_zeros: bool = False,
    search: tuple[int, int] = (26, 45),
    all_events: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """When the evening low arrived, on match days and on their control days.

    If people leave work early to get home for kick-off, the evening peak moves
    rather than deepens, and a km/h delta averaged over a fixed clock window can
    miss that entirely -- the traffic that left 17:30 has arrived at 16:30, and
    the two cancel. The clock time of the slowest half hour is the statistic
    that sees it, and it survives a mis-specified baseline better than a level
    does, because it is a position rather than a magnitude.
    """
    targets = _targets(events)
    if not targets:
        return pd.DataFrame(columns=["date", "label", "event_low", "control_low", "shift"])

    blocked = blocked_dates(all_events if all_events is not None else events, holidays)
    rows_idx = panel.rows_for(site_ids)
    speed = panel.speed_sum[rows_idx]
    moving = panel.n_moving[rows_idx]
    zero = panel.n_zero[rows_idx]
    denominator = moving + zero if include_zeros else moving
    lo, hi = search
    buckets = np.arange(lo, hi + 1)

    def low_bucket(dates: list[pd.Timestamp]) -> float:
        if not dates:
            return float("nan")
        cols = np.array([[panel.slot(d, int(b)) for b in buckets] for d in dates])
        sums = np.nansum(_take(speed, cols), axis=(0, 1))
        dens = np.nansum(_take(denominator, cols), axis=(0, 1))
        with np.errstate(invalid="ignore", divide="ignore"):
            curve = np.where(dens > 0, sums / dens, np.nan)
        if not np.isfinite(curve).any():
            return float("nan")
        return float(buckets[int(np.nanargmin(curve))])

    labels = dict(zip(events["date"], events["label"]))
    rows = []
    for date, _ in targets:
        controls = control_dates(
            date, available=panel.dates, blocked=blocked, weeks=weeks
        )
        event_low = low_bucket([date])
        control_low = low_bucket(controls)
        rows.append(
            {
                "date": date,
                "label": labels.get(date, ""),
                "event_low": bucket_label(int(event_low)) if np.isfinite(event_low) else "",
                "control_low": (
                    bucket_label(int(control_low)) if np.isfinite(control_low) else ""
                ),
                "shift": (event_low - control_low) * BUCKET_MINUTES,
                "n_controls": len(controls),
            }
        )
    return pd.DataFrame(rows)


def site_deltas(
    panel: EventPanel,
    events: pd.DataFrame,
    holidays: pd.DataFrame,
    sites: pd.DataFrame,
    *,
    window: tuple[int, int] = DURING_WINDOW,
    weeks: int = CONTROL_WEEKS,
    include_zeros: bool = False,
    min_samples: int = MIN_SAMPLES,
    dry_only: bool = False,
    all_events: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Per-sensor effect over one window, for the map.

    Same matched difference as everywhere else, only aggregated over sensors
    rather than across them.
    """
    targets = _targets(events)
    if not targets:
        return pd.DataFrame(columns=["site_id", "delta", "pct", "samples"])

    blocked = blocked_dates(all_events if all_events is not None else events, holidays)
    kickoffs, controls, _ = _slot_matrix(panel, targets, blocked=blocked, weeks=weeks)
    lo, hi = window
    rels = np.arange(
        int(np.ceil(lo / BUCKET_MINUTES)), int(np.ceil(hi / BUCKET_MINUTES))
    )
    if len(rels) == 0:
        rels = np.array([0])

    speed = panel.speed_sum
    denominator = (
        panel.n_moving + panel.n_zero if include_zeros else panel.n_moving
    )
    treated_cols = kickoffs[:, None] + rels[None, :]
    control_cols = controls[:, :, None] + rels[None, None, :]

    allowed = panel.dry_slots if dry_only else None
    t_sum = _take(speed, treated_cols, allowed)
    t_den = _take(denominator, treated_cols, allowed)
    c_sum = np.nansum(_take(speed, control_cols, allowed), axis=2)
    c_den = np.nansum(_take(denominator, control_cols, allowed), axis=2)

    usable = (
        np.isfinite(t_sum) & (t_den >= min_samples)
        & np.isfinite(c_sum) & (c_den >= min_samples)
    )
    weight = np.where(usable, t_den, 0.0).astype(np.float64)
    with np.errstate(invalid="ignore", divide="ignore"):
        delta = t_sum.astype(np.float64) / t_den - c_sum.astype(np.float64) / c_den
        baseline = c_sum.astype(np.float64) / c_den
    delta = np.where(usable, delta, 0.0)
    baseline = np.where(usable, baseline, 0.0)

    per_site = weight.sum(axis=(1, 2))
    safe = np.where(per_site > 0, per_site, np.nan)
    out = pd.DataFrame(
        {
            "site_id": panel.site_ids,
            "delta": (delta * weight).sum(axis=(1, 2)) / safe,
            "baseline_speed": (baseline * weight).sum(axis=(1, 2)) / safe,
            "samples": per_site,
        }
    )
    out["pct"] = out["delta"] / out["baseline_speed"]
    out = out[out["samples"] > 0]
    return out.merge(sites, on="site_id", how="inner", validate="one_to_one")


def event_caveats(n_events: int, n_usable: int) -> list[str]:
    """The things a reader has to know before trusting these numbers."""
    return [
        f"{n_usable} of {n_events} fixtures on file carry a kick-off time and are "
        f"measurable. That is a small number of treated days, and it is why the "
        f"per-match table is shown alongside the pooled curve rather than instead "
        f"of it -- no single row here is worth much on its own.",
        "The counterfactual is the same sensor, the same half hour and the same "
        f"weekday, on dates within {CONTROL_WEEKS} weeks that carry no fixture and "
        f"no holiday. A baseline pooled over the whole panel instead reports every "
        f"June weekday afternoon as 0.54 km/h slow, because January's empty summer "
        f"city is in the same average -- an artefact the same size and sign as the "
        f"effect being looked for.",
        "The shaded band is 500 placebo runs of this same estimator on fixtures "
        "moved to days when nothing happened. A curve inside it is not "
        "distinguishable from an ordinary week.",
        "Uruguay's three World Cup matches were played in North America, so they "
        "have no Montevideo venue and can only show a broadcast effect. Kick-off "
        "times are Uruguayan local time.",
        "Away Libertadores ties are the club-football equivalent: same clubs, same "
        "competition, no ground in Montevideo. They are not quite a clean control "
        "for the home ties, because three of the six kick off at 23:00 against a "
        "latest of 21:30 at home, so the roads they are measured on are a little "
        "emptier already -- a baseline of 34.5 km/h during the match against 33.5. "
        "That gap is small next to the effect it is being used to rule out, but it "
        "is not zero, and this panel cannot separate the stadium from the clock.",
        "There is no sensor within 8 km of the Campeon del Siglo -- the nearest is "
        "8.1 km away, on Camino Carrasco -- so a Penarol home match has no ring to "
        "measure. The corridor bins along 8 de Octubre and Camino Carrasco are "
        "indirect evidence about the road there, not a measurement at the ground. "
        "The Centenario and the Gran Parque Central are both well covered, with "
        "26 and 32 sensors inside a kilometre.",
        "Weather is not matched between a match day and its controls by default. "
        "Rain costs about 0.9 km/h city-wide, which is smaller than the effects "
        "here but not negligible against the pre-kick-off window; the sidebar "
        "toggle restricts both sides to dry hours.",
        "This is an association measured against a constructed comparison, not a "
        "randomised experiment. Fixture dates are chosen by broadcasters and "
        "federations, and nothing here makes them random.",
    ]
