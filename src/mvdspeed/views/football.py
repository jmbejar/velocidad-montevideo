"""Streamlit page: what a big match does to Montevideo traffic.

Run with:  uv run streamlit run src/mvdspeed/app.py   (then pick this page)

The estimator lives in mvdspeed/events.py and is documented there. This file is
the presentation: which fixtures, which sensors, which window, and how to draw a
number next to the range of numbers the same method produces on days when
nothing happened.

Page config is set by app.py, which runs before this in the same pass; calling
st.set_page_config again here would raise.
"""

from __future__ import annotations

import altair as alt
import numpy as np
import pandas as pd
import pydeck as pdk
import streamlit as st

from mvdspeed import colors, data as mvd, events as ev, surface
from mvdspeed.charts import SERIES_HUES, make_axis
from mvdspeed.config import (
    CONTROL_WEEKS,
    HOLIDAYS_CSV,
    MATCHES_CSV,
    DE_EMPHASIS,
    MATCH_MINUTES,
    SURFACE_ALPHA_GAMMA,
    SURFACE_ALPHA_MAX,
    SURFACE_ALPHA_MIN,
    SURFACE_ALPHA_REF,
    SURFACE_BANDWIDTH_KM,
    SURFACE_CUTOFF_KM,
    SURFACE_LIGHT,
    SURFACE_STEP_KM,
    TEXT_MUTED,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
)

# The fixture list's `tier` column, in the order the page offers them: loudest
# first, because the World Cup is both the biggest effect in the data and the
# one with no stadium in it, which is the distinction the page is built around.
#
# A tier missing from this dict is never offered as something to study but is
# still read from the file, and still blocks its own dates from being used as
# controls. That is what `blocked` is for: fixtures judged too minor to measure
# are not thereby ordinary evenings to measure other matches against.
TIERS = {
    "national": "Uruguay at the World Cup",
    "clasico": "Clásicos",
    "libertadores": "Libertadores at home",
    # Away ties are the club-football control for a home tie: same two clubs,
    # same competition, same audience -- and no ground in Montevideo. Selecting
    # them alone leaves the broadcast effect with the stadium taken out, which
    # is how this page shows that club football does not empty the city at all.
    "libertadores_away": "Libertadores away",
    "broadcast": "World Cup final",
}

WINDOWS = {
    "Before kick-off": ev.PRE_WINDOW,
    "During the match": ev.DURING_WINDOW,
    "After the whistle": ev.POST_WINDOW,
}



# Recomputed on every rerun, so pressing R after editing a calendar is enough.
STAMP = (MATCHES_CSV.stat().st_mtime, HOLIDAYS_CSV.stat().st_mtime)


# --- loaders ------------------------------------------------------------------
@st.cache_resource(show_spinner="Loading sensor data…")
def load_data() -> mvd.Dataset:
    return mvd.load()


@st.cache_resource(show_spinner="Laying out the event panel…")
def load_panel() -> ev.EventPanel:
    return ev.build_panel(load_data())


@st.cache_resource(show_spinner="Reading the fixture list…")
def load_calendars(stamp: tuple[float, float] = ()) -> tuple[pd.DataFrame, pd.DataFrame]:
    """The two hand-edited calendars, re-read whenever either file changes.

    `stamp` is the pair of file modification times and is never used in the
    body. It is here to be part of the cache key, because these files get
    edited far more often than the code does and the alternative is worse than
    it looks: this is a cache_resource that reads once per process, and every
    slice below was keyed on the *row count*, which does not change when a
    kick-off time does. A corrected time sat on disk for a whole session while
    the page kept serving the old parse and reporting "no kick-off time".
    """
    return ev.load_events(), ev.load_holidays()


@st.cache_data(show_spinner=False)
def study_for(
    _key: tuple, tiers: tuple[str, ...], site_ids: tuple[int, ...] | None,
    weeks: int, include_zeros: bool, min_samples: int, dry_only: bool,
) -> pd.DataFrame:
    matches, holidays = load_calendars(STAMP)
    return ev.event_study(
        load_panel(), matches[matches["tier"].isin(tiers)], holidays,
        site_ids=list(site_ids) if site_ids is not None else None,
        weeks=weeks, include_zeros=include_zeros, min_samples=min_samples,
        dry_only=dry_only, all_events=matches,
    )


@st.cache_data(show_spinner="Running placebo fixtures…")
def placebo_for(
    _key: tuple, tiers: tuple[str, ...], site_ids: tuple[int, ...] | None,
    weeks: int, include_zeros: bool, min_samples: int, dry_only: bool, draws: int,
) -> ev.Placebo:
    matches, holidays = load_calendars(STAMP)
    return ev.placebo(
        load_panel(), matches[matches["tier"].isin(tiers)], holidays,
        site_ids=list(site_ids) if site_ids is not None else None,
        weeks=weeks, include_zeros=include_zeros, min_samples=min_samples,
        dry_only=dry_only, draws=draws, all_events=matches,
    )


def _played_at(matches: pd.DataFrame, tiers: tuple[str, ...], venue_name: str):
    """Selected fixtures actually played at this ground.

    A ring is only meaningful for matches held inside it. Pooling in the nights
    a club played across town adds days on which nothing happened here at all,
    and drags the estimate toward zero -- measurably: over Nacional's four home
    matches the egress difference is -1.71 km/h, and mixed with Peñarol's three
    home nights it reads -1.36.
    """
    return matches[matches["tier"].isin(tiers) & (matches["venue"] == venue_name)]


@st.cache_data(show_spinner="Measuring the effect ring by ring…")
def decay_for(
    _key: tuple, tiers: tuple[str, ...], venue_name: str,
    venue: tuple[float, float],
    weeks: int, include_zeros: bool, min_samples: int, dry_only: bool,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Distance bands and the city-wide curve, over the *same* fixtures.

    Returned together so they cannot drift apart: comparing bands measured on
    the four matches played at a ground against a city-wide figure measured on
    every fixture in the sidebar would be an apples-to-oranges cancellation, and
    the whole point of the section is that the two numbers are the same data
    sliced two ways.
    """
    matches, holidays = load_calendars(STAMP)
    data = load_data()
    here = _played_at(matches, tiers, venue_name)
    shared = dict(
        weeks=weeks, include_zeros=include_zeros, min_samples=min_samples,
        dry_only=dry_only, all_events=matches,
    )
    bands = ev.distance_study(
        load_panel(), here, holidays, data.sites, venue=venue, **shared
    )
    whole_city = ev.event_study(load_panel(), here, holidays, **shared)
    return bands, whole_city


@st.cache_data(show_spinner=False)
def sites_for(
    _key: tuple, tiers: tuple[str, ...], window: tuple[int, int], weeks: int,
    include_zeros: bool, min_samples: int, dry_only: bool,
) -> pd.DataFrame:
    matches, holidays = load_calendars(STAMP)
    data = load_data()
    return ev.site_deltas(
        load_panel(), matches[matches["tier"].isin(tiers)], holidays, data.sites,
        window=window, weeks=weeks, include_zeros=include_zeros,
        min_samples=min_samples, dry_only=dry_only, all_events=matches,
    )


@st.cache_data(show_spinner="Working out the headline findings…")
def key_findings(_key: tuple, weeks: int, min_samples: int) -> dict:
    """The three numbers the page exists to report, over every fixture on file.

    Deliberately independent of the sidebar. These are conclusions about the
    panel, not about whatever is currently selected, and recomputing them from
    the selection would let a reader change a finding by clicking a filter.

    Computed rather than written down so that editing a calendar moves them.
    """
    matches, holidays = load_calendars(STAMP)
    panel, data = load_panel(), load_data()
    shared = dict(weeks=weeks, min_samples=min_samples, all_events=matches)

    def during(tier: str) -> dict:
        sel = matches[matches["tier"] == tier]
        if sel["kickoff_bucket"].notna().sum() == 0:
            return {}
        study = ev.event_study(panel, sel, holidays, **shared)
        null = ev.placebo(panel, sel, holidays, draws=200, **shared)
        effect = ev.window_effect(study, ev.DURING_WINDOW)
        return {**effect, "p": null.p_value(study, ev.DURING_WINDOW)}

    # The best-covered ground, among fixtures that are actually in the study.
    # Without the tier filter this picks the Centenario, which has the most
    # sensors around it and whose only fixtures are the two held out on purpose
    # -- a headline finding resting on the rows the page refuses to report.
    studied = matches[matches["tier"].isin(TIERS)]
    best, bands, city = None, None, None
    for ground in sorted(set(studied.loc[studied["in_montevideo"], "venue"])):
        here = studied[studied["venue"] == ground]
        if here["kickoff_bucket"].notna().sum() == 0:
            continue
        spot = (float(here["venue_lat"].iloc[0]), float(here["venue_lon"].iloc[0]))
        reach = ev.site_distances(data.sites[data.sites["is_usable"]], *spot)
        n_close = int((reach <= ev.NEAR_RING_KM).sum())
        if best is not None and n_close <= best[1]:
            continue
        rings = ev.distance_study(panel, here, holidays, data.sites, venue=spot,
                                  **shared)
        if rings.empty:
            continue
        best = (ground, n_close)
        bands, city = rings, ev.event_study(panel, here, holidays, **shared)

    gradient = {}
    if bands is not None:
        per_band = [
            (front, ev.window_effect(group, ev.PRE_WINDOW)["delta"])
            for front, group in bands.groupby("band_from")
        ]
        per_band.sort()
        gradient = {
            "ground": best[0].replace("Estadio ", ""),
            "near_band": bands.loc[bands["band_from"] == per_band[0][0], "band"].iloc[0],
            "near": per_band[0][1],
            "far_band": bands.loc[bands["band_from"] == per_band[-1][0], "band"].iloc[0],
            "far": per_band[-1][1],
            "city": ev.window_effect(city, ev.PRE_WINDOW)["delta"],
        }

    rain = mvd.rain_headline(load_data(), dows=mvd.DAY_SCOPES["Weekdays (Mon-Fri)"])
    return {
        "national": during("national"),
        "home": during("libertadores"),
        "away": during("libertadores_away"),
        "gradient": gradient,
        "rain": rain.get("delta", float("nan")),
    }


@st.cache_data(show_spinner=False)
def table_for(
    _key: tuple, weeks: int, include_zeros: bool, min_samples: int, dry_only: bool
) -> pd.DataFrame:
    matches, holidays = load_calendars(STAMP)
    return ev.event_table(
        load_panel(), matches[matches["tier"].isin(TIERS)], holidays,
        all_events=matches, weeks=weeks, include_zeros=include_zeros,
        min_samples=min_samples, dry_only=dry_only,
    )


@st.cache_data(show_spinner=False)
def peaks_for(_key: tuple, tiers: tuple[str, ...], weeks: int) -> pd.DataFrame:
    matches, holidays = load_calendars(STAMP)
    return ev.peak_shift(
        load_panel(), matches[matches["tier"].isin(tiers)], holidays,
        weeks=weeks, all_events=matches,
    )


try:
    dataset = load_data()
    matches, holidays = load_calendars(STAMP)
except FileNotFoundError as exc:
    st.error(str(exc))
    st.stop()

panel = load_panel()
CACHE_KEY = (len(dataset.measurements), len(matches), STAMP)

# Venues come from the fixture list rather than a hardcoded dict, so a match
# added to the CSV brings its ground with it.
# Grounds come from the fixture list rather than a hardcoded dict, so a match
# added to the CSV brings its ground with it -- but only from fixtures that are
# in the study. The Centenario is in the file and is never analysable: its two
# fixtures are the held-out ones, so it has no business appearing as a place the
# page can measure.
venues = (
    matches.loc[
        matches["in_montevideo"] & matches["tier"].isin(TIERS),
        ["venue", "venue_lat", "venue_lon"],
    ]
    .drop_duplicates("venue")
    .set_index("venue")
)

# --- sidebar ------------------------------------------------------------------
with st.sidebar:
    st.subheader("Which matches")
    available = [t for t in TIERS if t in set(matches["tier"])]
    chosen = st.multiselect(
        "Competitions",
        options=available,
        default=[t for t in ("national",) if t in available] or available[:1],
        format_func=lambda t: TIERS.get(t, t),
        help="Every fixture on file blocks its own date from being used as a "
             "control day, whether or not it is selected here.",
    )
    if not chosen:
        st.warning("Pick at least one competition.")
        st.stop()

    selection = matches[matches["tier"].isin(chosen)]
    n_usable = int(selection["kickoff_bucket"].notna().sum())
    held_out = matches[~matches["tier"].isin(TIERS)]
    st.caption(
        f"{n_usable} of {len(selection)} selected fixtures carry a kick-off time "
        f"and can be measured."
        + (
            f" A further {len(held_out)} on file are held out of the study but "
            f"still barred from serving as control days."
            if not held_out.empty else ""
        )
    )
    if n_usable == 0:
        st.warning("None of the selected fixtures has a kick-off time on file.")
        st.stop()

    st.subheader("Method")
    weeks = st.slider(
        "Control days: weeks either side", 1, 8, CONTROL_WEEKS,
        help="Control days are the same weekday within this many weeks, with no "
             "fixture and no holiday. Widen it for more data, narrow it to hold "
             "the season more tightly fixed.",
    )
    include_zeros = st.checkbox(
        "Count standstill readings in the average", value=False,
        help="Off, the average is of moving traffic only. On, readings of 0 km/h "
             "join the denominator and a jam pulls the mean down.",
    )
    min_samples = st.slider(
        "Minimum readings per sensor per half hour", 1, 40, 6,
        help="Applied to both the match night and its baseline.",
    )
    dry_only = st.checkbox(
        "Compare dry hours only", value=False,
        disabled=not dataset.has_weather,
        help="Drops wet and recently wet half hours from both sides. Rain costs "
             "about 0.9 km/h city-wide.",
    )
    draws = st.select_slider(
        "Placebo runs", options=[100, 250, 500, 1000], value=500,
        help="More runs make a smoother band and take longer.",
    )
    st.divider()
    st.caption(
        "Speeds: Intendencia de Montevideo, via catalogodatos.gub.uy. Fixtures "
        "and holidays: hand-curated in data/events/, one source URL per row."
    )

tiers = tuple(chosen)
knobs = (weeks, include_zeros, min_samples, dry_only)

# The top of the page is always the whole city, because the broadcast effect is
# a city-wide thing and has no ground. Anything local gets its own panel further
# down, one per ground, so that a reader is not asked to pick a scope out of a
# sidebar before knowing which grounds are even measurable.
study = study_for(CACHE_KEY, tiers, None, *knobs)
null = placebo_for(CACHE_KEY, tiers, None, *knobs, draws)

# --- header -------------------------------------------------------------------
st.title("Fútbol y tránsito · Montevideo · 2026")
st.markdown(
    f"<p style='color:{TEXT_MUTED};margin-top:-0.6rem'>"
    f"What {n_usable} big matches did to the city's traffic, measured against the "
    f"same sensors on the same weekday at the same half hour, on nearby dates "
    f"with no fixture and no holiday.</p>",
    unsafe_allow_html=True,
)

# --- key findings -------------------------------------------------------------
# Fixed conclusions about the panel, above the interactive part rather than
# buried under it, and not affected by the sidebar: a reader should not be able
# to change a finding by clicking a filter.
found = key_findings(CACHE_KEY, weeks, min_samples)
nat, home, away, grad = (
    found["national"], found["home"], found["away"], found["gradient"],
)

st.subheader("Three findings")
if nat:
    base = nat["baseline_speed"]
    st.markdown(
        f"**1 · Uruguay playing empties the city.** During the match, speeds "
        f"across Montevideo run **{nat['delta']:+.1f} km/h** above their matched "
        f"baseline — {base:.1f} to {base + nat['delta']:.1f} km/h, "
        f"{nat['pct']:+.0%} — at p = {nat['p']:.3f}. Rain, the other thing that "
        f"moves the whole city, costs {found['rain']:+.1f} km/h."
    )
if home and away:
    st.markdown(
        f"**2 · Club football does not. Its stadium does.** Away ties are the "
        f"same clubs and no ground in town: they move **{away['delta']:+.1f} km/h** "
        f"(p = {away['p']:.2f}), nothing. Home ties move "
        f"**{home['delta']:+.1f} km/h** (p = {home['p']:.2f}). The gap is the "
        f"ground, not the television."
    )
if grad:
    # Only claim a reversal when the two ends really do have opposite signs;
    # otherwise state the gradient and let the numbers say how steep it is.
    reverses = grad["near"] * grad["far"] < 0
    st.markdown(
        f"**3 · And the stadium effect "
        f"{'reverses' if reverses else 'fades'} with distance.** Before kick-off "
        f"at the {grad['ground']}: **{grad['near']:+.1f} km/h** within "
        f"{grad['near_band']}, **{grad['far']:+.1f}** out at {grad['far_band']}. "
        f"Averaged over the whole city it is {grad['city']:+.1f} — a number that "
        f"describes neither end."
    )
st.divider()

# --- headline numbers ---------------------------------------------------------
peaks = peaks_for(CACHE_KEY, tiers, weeks)
shift = peaks["shift"].median() if not peaks.empty else float("nan")
st.subheader("The selected fixtures")

def levels(effect: dict[str, float]) -> str:
    """"31.9 → 35.3 km/h", or nothing when there is no level to quote.

    A change on its own is unreadable: +3.4 km/h is a rounding error on a
    motorway and a transformation of a jammed avenue, and the reader cannot
    tell which without the speeds it moved between. A ring difference has no
    single level underneath it, so it gets no line rather than a made-up one.
    """
    if not np.isfinite(effect.get("baseline_speed", float("nan"))):
        return ""
    base = effect["baseline_speed"]
    return f"{base:.1f} → {base + effect['delta']:.1f} km/h"


kpi = st.columns(len(WINDOWS) + 1)
for column, (name, window) in zip(kpi, WINDOWS.items()):
    effect = ev.window_effect(study, window)
    p = null.p_value(study, window)
    with column:
        st.metric(
            name,
            "—" if not np.isfinite(effect["delta"]) else f"{effect['delta']:+.2f} km/h",
            delta=("" if not np.isfinite(p) else
                   f"{effect['pct']:+.1%} · p = {p:.3f}"),
            delta_color="off",
            help=f"Minutes {window[0]:+d} to {window[1]:+d} around kick-off. "
                 f"p is the share of placebo runs that produced a swing at least "
                 f"this big in either direction.",
        )
        if levels(effect):
            st.caption(levels(effect))
with kpi[-1]:
    st.metric(
        "Evening peak moved",
        "—" if not np.isfinite(shift) else f"{shift:+.0f} min",
        delta="median across fixtures",
        delta_color="off",
        help="Clock time of the slowest half hour between 13:00 and 22:30 on the "
             "match day, against the same statistic on its control days. Negative "
             "means rush hour arrived earlier.",
    )

# --- the event study ----------------------------------------------------------
st.subheader("How speed moved around kick-off")

band = null.band()
plot = study.merge(band, on=["rel", "minutes"], how="left")
plot["time_label"] = plot["minutes"].map(
    lambda m: "kick-off" if m == 0 else f"{m:+.0f} min"
)

axis = make_axis()
minute_axis = make_axis(values=list(range(-240, 241, 60)), format="+d")
base = alt.Chart(plot)

null_band = base.mark_area(color=DE_EMPHASIS, opacity=0.45).encode(
    x=alt.X("minutes:Q", title="Minutes from kick-off",
            scale=alt.Scale(nice=False), axis=minute_axis),
    y=alt.Y("p05:Q", title="km/h vs the matched baseline", axis=axis),
    y2=alt.Y2("p95:Q"),
)
zero = base.mark_rule(color=TEXT_MUTED, strokeWidth=1).encode(y=alt.datum(0))
kickoff = (
    alt.Chart(pd.DataFrame({"minutes": [0]}))
    .mark_rule(color=SERIES_HUES[1], strokeWidth=2)
    .encode(x="minutes:Q")
)
whistle = (
    alt.Chart(pd.DataFrame({"minutes": [MATCH_MINUTES]}))
    .mark_rule(color=TEXT_MUTED, strokeWidth=1, strokeDash=[4, 3])
    .encode(x="minutes:Q")
)
line = base.mark_line(color=SERIES_HUES[0], strokeWidth=2.5, point=True).encode(
    x="minutes:Q",
    y="delta:Q",
    tooltip=[
        alt.Tooltip("time_label:N", title="When"),
        alt.Tooltip("delta:Q", title="km/h vs baseline", format="+.2f"),
        alt.Tooltip("pct:Q", title="Change", format="+.1%"),
        alt.Tooltip("baseline_speed:Q", title="Baseline km/h", format=".1f"),
        alt.Tooltip("p05:Q", title="Placebo 5th", format="+.2f"),
        alt.Tooltip("p95:Q", title="Placebo 95th", format="+.2f"),
        alt.Tooltip("n_sites:Q", title="Sensors", format=".0f"),
        alt.Tooltip("samples:Q", title="Readings", format=","),
    ],
)
st.altair_chart(
    (null_band + zero + kickoff + whistle + line)
    .properties(height=320, background=SURFACE_LIGHT)
    .configure_view(strokeWidth=0),
    use_container_width=True,
)
st.caption(
    f"Grey band: the middle 90% of {draws} placebo runs — the same estimator on "
    f"the same fixtures moved to days when nothing happened. A line inside it is "
    f"not distinguishable from an ordinary week. Orange rule is kick-off; the "
    f"dashed rule is roughly the final whistle at +{MATCH_MINUTES} minutes."
)

with st.expander("Table view of this line"):
    view = plot[["minutes", "delta", "pct", "baseline_speed", "treated_speed",
                 "standstill_delta", "p05", "p95", "n_sites", "samples"]]
    st.dataframe(
        view, hide_index=True, use_container_width=True,
        column_config={
            "minutes": st.column_config.NumberColumn("Min from kick-off", format="%+d"),
            "delta": st.column_config.NumberColumn("Δ km/h", format="%+.2f"),
            "pct": st.column_config.NumberColumn("Δ %", format="%.1f%%"),
            "baseline_speed": st.column_config.NumberColumn("Baseline", format="%.1f"),
            "treated_speed": st.column_config.NumberColumn("On the day", format="%.1f"),
            "standstill_delta": st.column_config.NumberColumn(
                "Δ standstill share", format="%+.4f"
            ),
            "p05": st.column_config.NumberColumn("Placebo 5th", format="%+.2f"),
            "p95": st.column_config.NumberColumn("Placebo 95th", format="%+.2f"),
            "n_sites": st.column_config.NumberColumn("Sensors", format="%d"),
            "samples": st.column_config.NumberColumn("Readings", format="%d"),
        },
    )
    st.download_button(
        "Download this slice as CSV",
        view.to_csv(index=False).encode(),
        file_name=f"football-{'-'.join(tiers)}-city-wide.csv",
        mime="text/csv",
    )

# --- one panel per measurable ground ------------------------------------------
# A ground only gets a section if there are sensors around it. That is not most
# of them: there is nothing within 8 km of the Campeón del Siglo, so Peñarol's
# home ties are simply not measurable at the ground and the page says nothing
# about them rather than drawing an approach road and hoping it reads as a
# stadium effect. What each panel shows is the effect binned by distance, which
# is the shape that makes the city-wide average's uselessness visible.
played_here = sorted(set(selection.loc[selection["in_montevideo"], "venue"]))
for ground in played_here:
    here = _played_at(matches, tiers, ground)
    n_here = int(here["kickoff_bucket"].notna().sum())
    if n_here == 0:
        continue
    spot = (float(venues.loc[ground, "venue_lat"]),
            float(venues.loc[ground, "venue_lon"]))
    reach = ev.site_distances(dataset.sites[dataset.sites["is_usable"]], *spot)
    n_close = int((reach <= ev.NEAR_RING_KM).sum())
    if n_close == 0:
        continue

    club = here["home"].mode().iloc[0]
    st.subheader(f"{club} at the {ground.replace('Estadio ', '')}")
    st.caption(
        f"{n_here} home fixture{'s' if n_here != 1 else ''} selected. "
        f"**{n_close} sensors sit within {ev.NEAR_RING_KM:g} km** of this ground, "
        f"the nearest {float(reach.min()) * 1000:.0f} m away, so the effect here "
        f"can be measured directly."
    )

    window_name = st.radio(
        "Window", list(WINDOWS), horizontal=True, key=f"decay_window_{ground}",
        help="Which slice of the evening the distance curve is measured over.",
    )
    window = WINDOWS[window_name]
    bands, whole_city = decay_for(CACHE_KEY, tiers, ground, spot, *knobs)
    if bands.empty:
        continue

    points = (
        bands.groupby(["band", "band_from", "band_mid"], as_index=False)
        .apply(
            lambda g: pd.Series(
                {
                    "delta": ev.window_effect(g, window)["delta"],
                    "n_sites": g["n_sites"].max(),
                    "samples": ev.window_effect(g, window)["samples"],
                }
            ),
            include_groups=False,
        )
        .sort_values("band_from")
    )
    city_delta = ev.window_effect(whole_city, window)["delta"]
    rules = alt.Chart(pd.DataFrame({"y": [city_delta]}))
    st.altair_chart(
        (
            alt.Chart(points).mark_line(
                color=SERIES_HUES[0], strokeWidth=2.5, point=True
            ).encode(
                x=alt.X(
                    "band_mid:Q", title="Kilometres from the ground",
                    scale=alt.Scale(domain=[0, 12], nice=False),
                    axis=make_axis(values=[0, 2, 4, 6, 8, 10, 12], format="d"),
                ),
                y=alt.Y("delta:Q", title="km/h vs the matched baseline", axis=axis),
                tooltip=[
                    alt.Tooltip("band:N", title="Ring"),
                    alt.Tooltip("delta:Q", title="km/h vs baseline", format="+.2f"),
                    alt.Tooltip("n_sites:Q", title="Sensors", format=".0f"),
                    alt.Tooltip("samples:Q", title="Readings", format=","),
                ],
            )
            + alt.Chart(points).mark_rule(
                color=TEXT_MUTED, strokeWidth=1
            ).encode(y=alt.datum(0))
            + rules.mark_rule(
                color=SERIES_HUES[1], strokeWidth=2, strokeDash=[6, 4]
            ).encode(y="y:Q")
            + rules.mark_text(
                align="left", dx=6, dy=-8, color=SERIES_HUES[1], fontSize=11,
                text=f"city-wide average  {city_delta:+.2f} km/h",
            ).encode(y="y:Q", x=alt.datum(0.2))
        )
        .properties(height=300, background=SURFACE_LIGHT)
        .configure_view(strokeWidth=0),
        use_container_width=True,
    )
    near_band, far_band = points.iloc[0], points.iloc[-1]
    st.caption(
        f"The nearest ring moves **{near_band['delta']:+.2f} km/h** and the "
        f"farthest **{far_band['delta']:+.2f}**, while averaging every sensor in "
        f"the city together gives **{city_delta:+.2f}** — the orange line, a "
        f"number that describes neither end. Where the curve crosses zero is "
        f"where the ground's congestion stops outweighing the emptier streets "
        f"everyone left to go and watch. This is why a city-wide figure is the "
        f"wrong instrument for a club match."
    )

# --- the map ------------------------------------------------------------------
st.subheader("Where in the city it happened")
window_name = st.radio(
    "Window", list(WINDOWS), horizontal=True, index=1,
    help="Which slice of the evening the map colours by.",
)
map_window = WINDOWS[window_name]
per_site = sites_for(CACHE_KEY, tiers, map_window, *knobs)
per_site = per_site[per_site["has_location"] & per_site["delta"].notna()]

if per_site.empty:
    st.info("No sensor had enough readings in both the match window and its baseline.")
else:
    # A robust limit: single sensors over three evenings produce a few enormous
    # values, and letting them set the scale flattens everything else to grey.
    limit = float(np.nanpercentile(per_site["delta"].abs(), 95)) or 1.0
    per_site["color"] = colors.diverging(per_site["delta"].clip(-limit, limit), limit)

    field = surface.kernel_surface(
        per_site, "delta", step_km=SURFACE_STEP_KM, bandwidth_km=SURFACE_BANDWIDTH_KM,
        cutoff_km=SURFACE_CUTOFF_KM,
    )
    per_site["metric_label"] = per_site["delta"].map(lambda v: f"{v:+.1f} km/h")
    per_site["subtitle"] = per_site["from_street"] + " → " + per_site["to_street"]
    per_site["footnote"] = (
        per_site["samples"].map(lambda v: f"{v:,.0f}") + " readings · baseline "
        + per_site["baseline_speed"].map(lambda v: f"{v:.1f}") + " km/h"
    )
    per_site["detail"] = per_site["pct"].map(lambda v: f"<br/>Change: {v:+.0%}")

    layers = []
    if field.n_supported:
        # Coloured with the same function as the dots so the two cannot drift
        # onto different scales; unsupported cells keep a colour and are hidden
        # with alpha, so bilinear filtering has no black to bleed in from.
        flat = pd.Series(field.values.ravel()).clip(-limit, limit)
        alpha = surface.support_alpha(
            field.support.ravel(), alpha_min=SURFACE_ALPHA_MIN,
            alpha_max=SURFACE_ALPHA_MAX, reference=SURFACE_ALPHA_REF,
            gamma=SURFACE_ALPHA_GAMMA,
        )
        alpha = np.where(np.isfinite(field.values.ravel()), alpha, 0.0)

        ny, nx = field.shape
        raster = np.empty((ny * nx, 4), dtype=np.uint8)
        raster[:, :3] = np.asarray(colors.diverging(flat, limit), dtype=np.uint8)
        raster[:, 3] = np.clip(alpha * 255, 0, 255).astype(np.uint8)
        layers.append(
            pdk.Layer(
                "BitmapLayer",
                # String(), or pydeck serialises the data URI as an accessor
                # expression and deck.gl fails parsing it at the colon.
                image=pdk.types.String(
                    surface.to_png_data_uri(raster.reshape(ny, nx, 4))
                ),
                bounds=list(field.bounds),
                opacity=1.0,
                pickable=False,
            )
        )
    layers.append(
        pdk.Layer(
            "ScatterplotLayer",
            data=per_site,
            get_position=["lon", "lat"],
            get_fill_color="color",
            get_radius=85,
            radius_min_pixels=4,
            radius_max_pixels=14,
            stroked=True,
            line_width_min_pixels=1.5,
            get_line_color=colors.hex_to_rgb_list(SURFACE_LIGHT),
            pickable=True,
            auto_highlight=True,
        )
    )

    st.pydeck_chart(
        pdk.Deck(
            layers=layers,
            initial_view_state=pdk.ViewState(
                latitude=-34.869, longitude=-56.163, zoom=11.6, pitch=0, bearing=0
            ),
            map_provider="carto",
            map_style="light",
            tooltip={
                "html": (
                    "<div style='font-family:system-ui,sans-serif;font-size:12px;"
                    "max-width:260px'><b>{street}</b><br/>"
                    "<span style='opacity:.75'>{subtitle}</span><hr "
                    "style='margin:4px 0;border:none;border-top:1px solid "
                    "rgba(255,255,255,.2)'/>"
                    "vs baseline: <b>{metric_label}</b>{detail}"
                    "<br/><span style='opacity:.75'>{footnote}</span></div>"
                ),
                "style": {"backgroundColor": "#0b0b0b", "color": "#ffffff",
                          "borderRadius": "6px"},
            },
        ),
        use_container_width=True,
        height=520,
    )
    st.markdown(
        colors.legend_html(
            label=f"km/h vs the matched baseline · {window_name.lower()}",
            ticks=[f"{-limit:+.1f}", "", "0", "", f"{+limit:+.1f}"],
            kind="diverging", text_color=TEXT_PRIMARY, muted_color=TEXT_MUTED,
        ),
        unsafe_allow_html=True,
    )
    st.caption(
        f"Blue is faster than the baseline, red is slower. The scale is clipped at "
        f"the 95th percentile of the absolute change (±{limit:.1f} km/h): a single "
        f"sensor over a handful of evenings throws up a few very large numbers, and "
        f"letting them set the range flattens the rest of the city to grey."
    )

# --- per-fixture ---------------------------------------------------------------
st.subheader("Match by match")
table = table_for(CACHE_KEY, *knobs)
st.dataframe(
    table[["date", "label", "competition", "kickoff", "pre", "during", "post",
           "samples", "note"]],
    hide_index=True,
    use_container_width=True,
    column_config={
        "date": st.column_config.DateColumn("Date", format="ddd DD MMM"),
        "label": st.column_config.TextColumn("Match"),
        "competition": st.column_config.TextColumn("Competition"),
        "kickoff": st.column_config.TextColumn("Kick-off"),
        "pre": st.column_config.NumberColumn("Before (Δ km/h)", format="%+.2f"),
        "during": st.column_config.NumberColumn("During (Δ km/h)", format="%+.2f"),
        "post": st.column_config.NumberColumn("After (Δ km/h)", format="%+.2f"),
        "samples": st.column_config.NumberColumn("Readings", format="%d"),
        "note": st.column_config.TextColumn("Note"),
    },
)
st.caption(
    "City-wide, every fixture in the study, whatever the sidebar has selected — "
    "a dozen matches is far too few for any single row to be worth much, and the "
    "spread between them is the honest picture of how much this varies. Fixtures "
    "held out of the study are not listed, but their dates are still kept out of "
    "every baseline above."
)

with st.expander("Method and limits"):
    st.markdown(
        f"Every figure compares a match half hour against **the same sensor, the "
        f"same half hour, the same weekday**, on dates within {weeks} weeks with "
        f"no fixture and no holiday. p is {draws} placebo runs of that same "
        f"estimator on days when nothing happened — not a t-test, which 4.4 M "
        f"serially correlated rows would drive to zero regardless."
    )
    for caveat in ev.event_caveats(
        len(matches), int(matches["kickoff_bucket"].notna().sum())
    ):
        st.markdown(f"- {caveat}")
