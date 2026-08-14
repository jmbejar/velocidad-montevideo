"""Streamlit page: what a big match does to Montevideo traffic.

Run with:  uv run streamlit run src/mvdspeed/app.py   (then pick this page)

The estimator lives in mvdspeed/events.py and is documented there. This file is
the presentation: which fixtures, which sensors, which window, and how to draw a
number next to the range of numbers the same method produces on days when
nothing happened.
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

st.set_page_config(
    page_title="Fútbol y tránsito · Montevideo · 2026",
    page_icon="⚽",
    layout="wide",
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
    "broadcast": "Other big broadcasts",
}

WINDOWS = {
    "Before kick-off": ev.PRE_WINDOW,
    "During the match": ev.DURING_WINDOW,
    "After the whistle": ev.POST_WINDOW,
}

CITY, RING, CORRIDOR = "The whole city", "Near the ground", "The road east"

# Peñarol's ground, out in Bañados de Carrasco. It gets a constant of its own
# rather than coming from the fixture list because the corridor view is about
# the road *to* it, and that view has to exist even when no fixture there is
# selected.
CAMPEON_DEL_SIGLO = (-34.796917, -56.067167)


# --- loaders ------------------------------------------------------------------
@st.cache_resource(show_spinner="Loading sensor data…")
def load_data() -> mvd.Dataset:
    return mvd.load()


@st.cache_resource(show_spinner="Laying out the event panel…")
def load_panel() -> ev.EventPanel:
    return ev.build_panel(load_data())


@st.cache_resource(show_spinner="Reading the fixture list…")
def load_calendars() -> tuple[pd.DataFrame, pd.DataFrame]:
    return ev.load_events(), ev.load_holidays()


@st.cache_data(show_spinner=False)
def study_for(
    _key: tuple, tiers: tuple[str, ...], site_ids: tuple[int, ...] | None,
    weeks: int, include_zeros: bool, min_samples: int, dry_only: bool,
) -> pd.DataFrame:
    matches, holidays = load_calendars()
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
    matches, holidays = load_calendars()
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


@st.cache_data(show_spinner=False)
def ring_for(
    _key: tuple, tiers: tuple[str, ...], venue_name: str,
    venue: tuple[float, float],
    weeks: int, include_zeros: bool, min_samples: int, dry_only: bool,
) -> pd.DataFrame:
    matches, holidays = load_calendars()
    data = load_data()
    return ev.ring_study(
        load_panel(), _played_at(matches, tiers, venue_name), holidays, data.sites,
        venue=venue, weeks=weeks, include_zeros=include_zeros,
        min_samples=min_samples, dry_only=dry_only, all_events=matches,
    )


@st.cache_data(show_spinner="Running placebo fixtures for the ring…")
def ring_placebo_for(
    _key: tuple, tiers: tuple[str, ...], venue_name: str,
    venue: tuple[float, float],
    weeks: int, include_zeros: bool, min_samples: int, dry_only: bool, draws: int,
) -> ev.Placebo:
    matches, holidays = load_calendars()
    data = load_data()
    return ev.ring_placebo(
        load_panel(), _played_at(matches, tiers, venue_name), holidays, data.sites,
        venue=venue, weeks=weeks, include_zeros=include_zeros,
        min_samples=min_samples, dry_only=dry_only, draws=draws, all_events=matches,
    )


@st.cache_data(show_spinner=False)
def corridor_for(
    _key: tuple, tiers: tuple[str, ...], weeks: int, include_zeros: bool,
    min_samples: int, dry_only: bool,
) -> pd.DataFrame:
    matches, holidays = load_calendars()
    data = load_data()
    return ev.corridor_study(
        load_panel(), matches[matches["tier"].isin(tiers)], holidays, data.sites,
        venue=CAMPEON_DEL_SIGLO, weeks=weeks, include_zeros=include_zeros,
        min_samples=min_samples, dry_only=dry_only, all_events=matches,
    )


@st.cache_data(show_spinner=False)
def sites_for(
    _key: tuple, tiers: tuple[str, ...], window: tuple[int, int], weeks: int,
    include_zeros: bool, min_samples: int, dry_only: bool,
) -> pd.DataFrame:
    matches, holidays = load_calendars()
    data = load_data()
    return ev.site_deltas(
        load_panel(), matches[matches["tier"].isin(tiers)], holidays, data.sites,
        window=window, weeks=weeks, include_zeros=include_zeros,
        min_samples=min_samples, dry_only=dry_only, all_events=matches,
    )


@st.cache_data(show_spinner=False)
def table_for(
    _key: tuple, weeks: int, include_zeros: bool, min_samples: int, dry_only: bool
) -> pd.DataFrame:
    matches, holidays = load_calendars()
    return ev.event_table(
        load_panel(), matches, holidays, weeks=weeks, include_zeros=include_zeros,
        min_samples=min_samples, dry_only=dry_only,
    )


@st.cache_data(show_spinner=False)
def peaks_for(_key: tuple, tiers: tuple[str, ...], weeks: int) -> pd.DataFrame:
    matches, holidays = load_calendars()
    return ev.peak_shift(
        load_panel(), matches[matches["tier"].isin(tiers)], holidays,
        weeks=weeks, all_events=matches,
    )


try:
    dataset = load_data()
    matches, holidays = load_calendars()
except FileNotFoundError as exc:
    st.error(str(exc))
    st.stop()

panel = load_panel()
CACHE_KEY = (len(dataset.measurements), len(matches))

# Venues come from the fixture list rather than a hardcoded dict, so a match
# added to the CSV brings its ground with it.
venues = (
    matches.loc[matches["in_montevideo"], ["venue", "venue_lat", "venue_lon"]]
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

    st.subheader("Where to look")
    scope_names = [CITY, RING, CORRIDOR]
    scope = st.radio(
        "Sensors",
        scope_names,
        help="The whole city sees the broadcast effect. A ring around a ground "
             "sees the stadium on top of it, which is why the near ring is "
             "reported against a far one.",
    )
    venue_name = None
    if scope == RING:
        in_play = sorted(set(selection.loc[selection["in_montevideo"], "venue"]))
        if not in_play:
            st.info(
                "None of the selected fixtures was played in Montevideo, so "
                "there is no ground to draw a ring around."
            )
            scope = CITY
        else:
            venue_name = st.selectbox("Ground", in_play)

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

site_ids = None
if scope == RING and venue_name:
    venue = (float(venues.loc[venue_name, "venue_lat"]),
             float(venues.loc[venue_name, "venue_lon"]))
elif scope == CORRIDOR:
    venue = CAMPEON_DEL_SIGLO
    distance = ev.site_distances(dataset.sites, *venue)
    on_corridor = dataset.sites["street"].isin(ev.CORRIDOR_STREETS)
    site_ids = tuple(dataset.sites.loc[on_corridor & np.isfinite(distance), "site_id"])
else:
    venue = None

tiers = tuple(chosen)
knobs = (weeks, include_zeros, min_samples, dry_only)

study = study_for(CACHE_KEY, tiers, site_ids, *knobs)
null = placebo_for(CACHE_KEY, tiers, site_ids, *knobs, draws)

# --- header -------------------------------------------------------------------
st.title("Fútbol y tránsito · Montevideo · 2026")
st.markdown(
    f"<p style='color:{TEXT_MUTED};margin-top:-0.6rem'>"
    f"What {n_usable} big matches did to the city's traffic, measured against the "
    f"same sensors on the same weekday at the same half hour, on nearby dates "
    f"with no fixture and no holiday.</p>",
    unsafe_allow_html=True,
)

if scope == CORRIDOR:
    st.caption(
        f"Showing the {len(site_ids)} sensors on 8 de Octubre and Camino Carrasco. "
        "There is no sensor within 5 km of the Campeón del Siglo, so this is the "
        "road there, not the ground."
    )

# --- headline numbers ---------------------------------------------------------
peaks = peaks_for(CACHE_KEY, tiers, weeks)
shift = peaks["shift"].median() if not peaks.empty else float("nan")

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
        file_name=f"football-{'-'.join(tiers)}-{scope.replace(' ', '-')}.csv",
        mime="text/csv",
    )

# --- separating the stadium from the television -------------------------------
if scope == RING and venue_name:
    at_venue = _played_at(matches, tiers, venue_name)
    n_here = int(at_venue["kickoff_bucket"].notna().sum())
    st.subheader(f"Near the {venue_name} against the rest of the city")
    st.caption(
        f"The {n_here} selected fixture{'s' if n_here != 1 else ''} actually "
        f"played here. Matches the same clubs played elsewhere are left out: a "
        f"ring only means something on the nights something happened inside it."
    )
    ring = (
        ring_for(CACHE_KEY, tiers, venue_name, venue, *knobs)
        if n_here else pd.DataFrame()
    )
    if ring.empty or ring["n_near"].max() == 0:
        st.info(
            "No sensors close enough to that ground to draw a ring."
            if n_here else "None of the selected fixtures was played here."
        )
    else:
        ring_null = ring_placebo_for(
            CACHE_KEY, tiers, venue_name, venue, *knobs, draws
        )
        did = ring.rename(columns={"did": "delta"})[
            ["rel", "minutes", "delta", "samples"]
        ]
        ribbon = did.merge(ring_null.band(), on=["rel", "minutes"], how="left")

        did_kpi = st.columns(len(WINDOWS))
        for column, (name, window) in zip(did_kpi, WINDOWS.items()):
            value = ev.window_effect(did, window)["delta"]
            p = ring_null.p_value(did, window)
            with column:
                st.metric(
                    name,
                    "—" if not np.isfinite(value) else f"{value:+.2f} km/h",
                    delta="" if not np.isfinite(p) else f"p = {p:.3f}",
                    delta_color="off",
                    help="Near minus far, so the city-wide broadcast swing is "
                         "already subtracted out.",
                )

        tidy = ring.melt(
            id_vars=["minutes"], value_vars=["near", "far", "did"],
            var_name="series", value_name="delta",
        )
        names = {"near": "Near the ground", "far": "Rest of the city",
                 "did": "Difference (the stadium's own share)"}
        tidy["series"] = tidy["series"].map(names)
        st.altair_chart(
            (
                alt.Chart(ribbon).mark_area(color=DE_EMPHASIS, opacity=0.45).encode(
                    x=alt.X("minutes:Q", scale=alt.Scale(nice=False), axis=minute_axis),
                    y=alt.Y("p05:Q", axis=axis),
                    y2=alt.Y2("p95:Q"),
                )
                + alt.Chart(tidy).mark_line(strokeWidth=2).encode(
                    x=alt.X("minutes:Q", title="Minutes from kick-off",
                            scale=alt.Scale(nice=False), axis=minute_axis),
                    y=alt.Y("delta:Q", title="km/h vs the matched baseline", axis=axis),
                    color=alt.Color(
                        "series:N", title=None,
                        scale=alt.Scale(domain=list(names.values()), range=SERIES_HUES),
                        legend=alt.Legend(orient="top", labelColor=TEXT_SECONDARY),
                    ),
                    tooltip=[
                        alt.Tooltip("series:N", title=""),
                        alt.Tooltip("minutes:Q", title="Min from kick-off", format="+d"),
                        alt.Tooltip("delta:Q", title="km/h", format="+.2f"),
                    ],
                )
                + alt.Chart(tidy).mark_rule(color=TEXT_MUTED, strokeWidth=1).encode(
                    y=alt.datum(0)
                )
                + kickoff
            )
            .properties(height=300, background=SURFACE_LIGHT)
            .configure_view(strokeWidth=0),
            use_container_width=True,
        )
        st.caption(
            f"{int(ring['n_near'].max())} sensors within {ev.NEAR_RING_KM:g} km, "
            f"{int(ring['n_far'].max())} past {ev.FAR_RING_KM:g} km. The far ring "
            "gets the television and not the stadium, so the difference between "
            "the two is the part that is actually about people travelling to a "
            "ground — the only line here that is not also a broadcast effect. The "
            "band is the placebo null for that difference, drawn from the same "
            "placebo dates for both rings so the two are subtracted like for like."
        )

if scope == CORRIDOR:
    st.subheader("Along 8 de Octubre and Camino Carrasco, by distance from the ground")
    corridor = corridor_for(CACHE_KEY, tiers, *knobs)
    if corridor.empty:
        st.info("No corridor sensors reported for these fixtures.")
    else:
        order = (
            corridor[["band", "band_from"]].drop_duplicates()
            .sort_values("band_from")["band"].tolist()
        )
        st.altair_chart(
            (
                alt.Chart(corridor).mark_line(strokeWidth=2).encode(
                    x=alt.X("minutes:Q", title="Minutes from kick-off",
                            scale=alt.Scale(nice=False), axis=minute_axis),
                    y=alt.Y("delta:Q", title="km/h vs the matched baseline", axis=axis),
                    color=alt.Color(
                        "band:N", title="Distance from the Campeón del Siglo",
                        scale=alt.Scale(domain=order, scheme="viridis"),
                        legend=alt.Legend(orient="top", labelColor=TEXT_SECONDARY),
                    ),
                    tooltip=[
                        alt.Tooltip("band:N", title="Band"),
                        alt.Tooltip("minutes:Q", title="Min from kick-off", format="+d"),
                        alt.Tooltip("delta:Q", title="km/h", format="+.2f"),
                        alt.Tooltip("n_sites:Q", title="Sensors", format=".0f"),
                    ],
                )
                + alt.Chart(corridor).mark_rule(
                    color=TEXT_MUTED, strokeWidth=1
                ).encode(y=alt.datum(0))
                + kickoff
            )
            .properties(height=300, background=SURFACE_LIGHT)
            .configure_view(strokeWidth=0),
            use_container_width=True,
        )
        st.caption(
            "If a Peñarol home match shows up on the road out, the near bands "
            "should move more than the far ones. Four to nine sensors per band, "
            "so read the shape rather than any one point."
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
    "City-wide, every fixture on file, whatever the sidebar has selected — a "
    "dozen matches is far too few for any single row to be worth much, and the "
    "spread between them is the honest picture of how much this varies."
)

with st.expander("When the evening peak arrived"):
    st.dataframe(
        peaks[["date", "label", "event_low", "control_low", "shift", "n_controls"]],
        hide_index=True, use_container_width=True,
        column_config={
            "date": st.column_config.DateColumn("Date", format="ddd DD MMM"),
            "label": st.column_config.TextColumn("Match"),
            "event_low": st.column_config.TextColumn("Slowest half hour"),
            "control_low": st.column_config.TextColumn("On control days"),
            "shift": st.column_config.NumberColumn("Shift (min)", format="%+d"),
            "n_controls": st.column_config.NumberColumn("Control days", format="%d"),
        },
    )
    st.caption(
        "Half-hour buckets, so this statistic can only ever move in steps of 30 "
        "minutes and a small real shift will read as zero."
    )

with st.expander("How the counterfactual is built, and what went wrong without it"):
    st.markdown(
        f"""
For every sensor and every half hour touched by a match, the comparison is
**that same sensor, that same half hour, that same weekday**, on dates within
{weeks} weeks that carry no fixture and no holiday. The differences are taken
inside each sensor and only then averaged, weighted by how much data the match
night has — so a sensor that reported on the night but not on half its control
days cannot shift the answer just by being present on one side.

The obvious alternative — one pooled "typical Sunday at 19:00" over the whole
panel — does not work, and it fails in the direction that flatters the
hypothesis. Measured that way, **every June weekday afternoon in this panel
reads 0.54 km/h slow**, because January is in the same average and sits
**2.07 km/h above it**: Montevideo empties for the summer. That artefact has the
same sign and roughly the same size as the pre-kick-off congestion this page was
built to look for. A pooled baseline does not just add noise here — it
manufactures the finding.

Saturday 18 July 2026 makes the same point from the other side. In the raw panel
it lights up across 21 consecutive half hours and looks almost exactly like a
match evening. It is *Jura de la Constitución*, a public holiday, and it is in
`data/events/holidays.csv` precisely so it can never serve as a control.

The grey band is not a confidence interval from a formula. It is {draws} runs of
this same estimator on these same fixtures moved to dates when nothing happened,
which prices in the fact that one unusual Tuesday moves hundreds of rows
together — the assumption a t-test over 4.4 million serially correlated rows
would get badly wrong.
        """
    )

with st.expander("What these numbers do not show"):
    for caveat in ev.event_caveats(len(matches), int(matches["kickoff_bucket"].notna().sum())):
        st.markdown(f"- {caveat}")
