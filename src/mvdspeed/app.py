"""Streamlit dashboard: Montevideo average speed by time of day.

Run with:  uv run streamlit run src/mvdspeed/app.py
"""

from __future__ import annotations

import time

import altair as alt
import numpy as np
import pandas as pd
import pydeck as pdk
import streamlit as st

from mvdspeed import colors, data as mvd, streets, surface
from mvdspeed.config import (
    BUCKET_MINUTES,
    GRIDLINE,
    MAX_PLAUSIBLE_SPEED,
    STREET_ALPHA_GAMMA,
    STREET_ALPHA_MAX,
    STREET_ALPHA_MIN,
    STREET_ALPHA_REF,
    STREET_CUTOFF_KM,
    STREET_STUB_KM,
    STREET_WIDTH_M,
    STREET_WIDTH_MAX_PX,
    STREET_WIDTH_MIN_PX,
    STREETS_PARQUET,
    SURFACE_ALPHA_GAMMA,
    SURFACE_ALPHA_MAX,
    SURFACE_ALPHA_MIN,
    SURFACE_ALPHA_REF,
    SURFACE_CUTOFF_KM,
    SURFACE_DARK,
    SURFACE_LIGHT,
    TEXT_MUTED,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
)

st.set_page_config(
    page_title="Velocidad promedio · Montevideo",
    page_icon="🚗",
    layout="wide",
)

BUCKETS = mvd.BUCKETS_PER_DAY
DEFAULT_BUCKET = 35  # 17:30, the slowest half hour of the average weekday


# --- metric definitions -------------------------------------------------------
# Each metric declares the color *job* it needs, which fixes its scale.
METRICS = {
    "Congestion": {
        "column": "congestion",
        "kind": "sequential",
        "ramp": "heat",
        "invert": False,
        "domain": (0.0, 0.9),
        "legend": "Congestion — share of this sensor's own free-flow speed lost",
        "format": lambda v: f"{v:.0%}",
        "tick_format": lambda v: f"{v:.0%}",
        "help": (
            "How far below its own free-flow speed (85th percentile) a sensor is "
            "right now. Comparable between a 30 km/h side street and a 60 km/h "
            "avenue, unlike raw km/h."
        ),
    },
    "Average speed": {
        "column": "speed",
        "kind": "sequential",
        "ramp": "heat",
        "invert": True,  # hot = slow, so the eye lands on the problem
        "domain": (5.0, 50.0),
        "legend": "Average speed (km/h) — hotter is slower",
        "format": lambda v: f"{v:.1f} km/h",
        "tick_format": lambda v: f"{v:.0f}",
        "help": "Mean of the reported 5-minute averages, weighted by reading count.",
    },
    "vs. its own typical": {
        "column": "vs_typical",
        "kind": "diverging",
        "ramp": "heat",  # unused: diverging has its own pair
        "invert": False,
        "domain": (-12.0, 12.0),
        "legend": "Difference from this sensor's all-hours average (km/h)",
        "format": lambda v: f"{v:+.1f} km/h",
        "tick_format": lambda v: f"{v:+.0f}",
        "help": (
            "Where the selected time of day is unusual *for that spot*. Red is "
            "slower than that sensor's own average, blue faster. The surface "
            "averages the signed differences, so it carries the sign too."
        ),
    },
    "Reading volume": {
        "column": "samples",
        "kind": "sequential",
        # A neutral count, not a severity: no heat, no good/bad reading.
        "ramp": "blue",
        "invert": False,
        "domain": None,  # data-driven
        "legend": "Number of 5-minute readings behind the average",
        "format": lambda v: f"{v:,.0f}",
        "tick_format": lambda v: f"{v:,.0f}",
        "help": "A sanity layer: which sensors actually reported at this time of day.",
    },
}


# --- reach models -------------------------------------------------------------
# How far one sensor's reading is drawn along the avenue it sits on. The three
# answer different questions, so the choice is the reader's rather than ours.
REACH_MODELS = {
    "Blend along the avenue": {
        "mode": streets.BLEND,
        "help": (
            "A distance-weighted average of the sensors on that avenue — the "
            "surface's estimate, confined to the road. Reads best as a picture "
            "of a corridor, and interpolates between sensors."
        ),
        "caption": (
            f"Each stretch is a distance-weighted average of the sensors on that "
            f"same avenue, fading out and stopping entirely more than "
            f"{STREET_CUTOFF_KM:g} km along the road from any of them. Unlike the "
            f"surface it never crosses to a neighbouring street, so the value "
            f"stays on the road it was measured on."
        ),
    },
    "Nearest sensor owns the stretch": {
        "mode": streets.NEAREST,
        "help": (
            "Each stretch takes the colour of the closest sensor on its avenue, "
            "handing over at the midpoint. Nothing is averaged, so every painted "
            "metre is one real measurement."
        ),
        "caption": (
            f"Each stretch takes the reading of the nearest sensor on its own "
            f"avenue outright, handing over at the midpoint between two sensors "
            f"and stopping {STREET_CUTOFF_KM:g} km out. Nothing is averaged: every "
            f"painted metre carries one sensor's actual measurement, which is why "
            f"the boundaries are abrupt."
        ),
    },
    "Only the measured stretch": {
        "mode": streets.STUB,
        "help": (
            "Paint only the road immediately around each sensor and leave the "
            "rest bare. The most literal reading of the data, and the emptiest "
            "map."
        ),
        "caption": (
            f"Only the {STREET_STUB_KM * 1000:.0f} m of road either side of each "
            f"sensor is painted and the rest is left bare. This is the most "
            f"literal thing the data supports — a sensor watches one stretch of "
            f"one avenue — and the gaps are the honest extent of what is known."
        ),
    },
}


@st.cache_resource(show_spinner="Loading sensor data…")
def load_data() -> mvd.Dataset:
    return mvd.load()


@st.cache_resource(show_spinner="Loading street geometry…")
def load_streets() -> tuple[pd.DataFrame, pd.Series]:
    """Road chunks, and the corridor each site was matched to.

    The matching depends only on where the sensors are and what they are called,
    both static, so it runs once per session rather than per slice.
    """
    chunks = pd.read_parquet(STREETS_PARQUET)
    sites = load_data().sites
    mappable = sites[sites["has_location"]]
    assigned = streets.assign_corridors(mappable, chunks)
    return chunks, pd.Series(assigned.to_numpy(), index=mappable["site_id"].to_numpy())


@st.cache_data(show_spinner=False)
def sites_at(
    _key: tuple, dows: tuple[int, ...], buckets: tuple[int, ...], include_zeros: bool,
    min_samples: int,
) -> pd.DataFrame:
    return mvd.by_site(
        load_data(),
        dows=list(dows),
        buckets=list(buckets),
        include_zeros=include_zeros,
        min_samples=min_samples,
    )


@st.cache_data(show_spinner=False)
def surface_for(
    _key: tuple, dows: tuple[int, ...], buckets: tuple[int, ...], include_zeros: bool,
    min_samples: int, column: str, _metric_name: str,
) -> surface.Surface:
    """The kernel surface for one slice. Cached so the ▶ Play loop stays smooth.

    Keyed on everything that changes the sensor values, but not on colour: the
    ramp is applied afterwards, so switching light/dark reuses the same grid.
    """
    return surface.kernel_surface(
        sites_at(_key, dows, buckets, include_zeros, min_samples), column
    )


@st.cache_data(show_spinner=False)
def street_field_for(
    _key: tuple, dows: tuple[int, ...], buckets: tuple[int, ...], include_zeros: bool,
    min_samples: int, column: str, mode: str, _metric_name: str,
) -> tuple[np.ndarray, np.ndarray]:
    """Value and support per road chunk for one slice, cached like the surface.

    Keyed on the slice and the reach model but not on colour, for the same
    reason `surface_for` is: the ramp is applied afterwards.
    """
    chunks, assigned = load_streets()
    frame = sites_at(_key, dows, buckets, include_zeros, min_samples)
    frame = frame.assign(corridor_id=frame["site_id"].map(assigned))
    return streets.street_field(chunks, frame, column, mode=mode)


@st.cache_data(show_spinner=False)
def profile(_key: tuple, dows: tuple[int, ...], include_zeros: bool) -> pd.DataFrame:
    return mvd.city_profile(load_data(), dows=list(dows), include_zeros=include_zeros)


@st.cache_data(show_spinner=False)
def street_curves(_key: tuple, names: tuple[str, ...], dows: tuple[int, ...],
                  include_zeros: bool) -> pd.DataFrame:
    return mvd.street_profile(
        load_data(), dows=list(dows), streets=list(names), include_zeros=include_zeros
    )


try:
    dataset = load_data()
except FileNotFoundError as exc:
    st.error(str(exc))
    st.stop()

CACHE_KEY = (len(dataset.measurements),)

# --- sidebar ------------------------------------------------------------------
with st.sidebar:
    st.subheader("View")
    metric_name = st.radio(
        "Colour the map by",
        list(METRICS),
        help="Each metric uses the colour scale its data actually calls for.",
    )
    metric = METRICS[metric_name]
    st.caption(metric["help"])

    layer_choice = st.radio(
        "Map layer",
        [
            "Surface + sensors",
            "Surface only",
            "Streets + sensors",
            "Streets only",
            "Sensors only",
        ],
        index=0,
        help=(
            "The surface is a distance-weighted estimate between sensors, faded "
            "where few sensors support it. The streets carry the same estimate "
            "along the actual roads, which is what a fixed sensor really "
            "measures. The dots are the measurements themselves."
        ),
    )

    show_streets = layer_choice in ("Streets + sensors", "Streets only")
    reach_name = next(iter(REACH_MODELS))
    if show_streets:
        reach_name = st.radio(
            "How far a sensor reaches",
            list(REACH_MODELS),
            index=0,
            help=(
                "A sensor sits at one intersection. This is how much of the "
                "avenue it is allowed to speak for."
            ),
        )
        st.caption(REACH_MODELS[reach_name]["help"])

    st.subheader("Days")
    scope_name = st.radio("Day scope", list(mvd.DAY_SCOPES), index=0)
    dows = tuple(mvd.DAY_SCOPES[scope_name])

    st.subheader("Method")
    include_zeros = st.checkbox(
        "Count zero readings as 0 km/h",
        value=False,
        help=(
            "A reading of 0 means either that no vehicle crossed the lane in those "
            "5 minutes or that traffic was fully stopped — the file cannot tell "
            "them apart. Off by default, so averages describe moving traffic; "
            "turning it on drags quiet streets and night hours down."
        ),
    )
    min_samples = st.slider(
        "Minimum readings per sensor", 1, 60, 3,
        help="Hides sensors with too little data at the selected time to average.",
    )
    dark_map = st.toggle(
        "Dark basemap",
        value=False,
        help=(
            "Switches the basemap and re-steps the map's colour scale for a dark "
            "surface, so the extreme end stays visible against near-black."
        ),
    )

    st.divider()
    st.caption(
        f"Source: Montevideo open data, Aug 2026. Readings above "
        f"{MAX_PLAUSIBLE_SPEED} km/h and empty readings are dropped by the ETL."
    )

# The page itself is always the light theme (see .streamlit/config.toml), so the
# text and chart tokens are fixed. Only the map swaps surface, and with it the
# ramp its marks are drawn from.
chart_surface = SURFACE_LIGHT
ink = TEXT_PRIMARY
ink_secondary = TEXT_SECONDARY
map_surface = SURFACE_DARK if dark_map else SURFACE_LIGHT
basemap = pdk.map_styles.CARTO_DARK if dark_map else pdk.map_styles.CARTO_LIGHT

# --- header & time control ----------------------------------------------------
st.title("Velocidad promedio · Montevideo")
st.markdown(
    f"<p style='color:{TEXT_MUTED};margin-top:-0.6rem'>"
    f"{len(dataset.sites)} measuring points · "
    f"{len(dataset.dates)} days · {BUCKET_MINUTES}-minute resolution</p>",
    unsafe_allow_html=True,
)

if "bucket" not in st.session_state:
    st.session_state.bucket = DEFAULT_BUCKET

control, playback, whole_day = st.columns([6, 1, 1.4], vertical_alignment="bottom")
with whole_day:
    all_day = st.toggle("All-day average", value=False)
with playback:
    playing = st.toggle("▶ Play", value=False, disabled=all_day)
with control:
    # No `key=` on purpose: the animation writes st.session_state.bucket, which
    # Streamlit forbids for a key bound to an instantiated widget.
    selected = st.select_slider(
        "Time of day",
        options=list(range(BUCKETS)),
        value=st.session_state.bucket,
        format_func=mvd.bucket_label,
        disabled=all_day,
        label_visibility="collapsed" if all_day else "visible",
    )
st.session_state.bucket = selected

buckets = tuple(range(BUCKETS)) if all_day else (selected,)
window_label = "all day" if all_day else (
    f"{mvd.bucket_label(selected)}–{mvd.bucket_label((selected + 1) % BUCKETS)}"
)

frame = sites_at(CACHE_KEY, dows, buckets, include_zeros, min_samples)
day_profile = profile(CACHE_KEY, dows, include_zeros)

if frame.empty:
    st.warning(
        "No sensor reported enough readings for this combination. "
        "Lower the minimum-readings threshold or widen the day scope."
    )
    st.stop()

# The congestion metric is a ratio, so it simply does not exist for sensors
# without a usable free-flow reference. Drop them from that view rather than
# drawing them at a made-up zero.
column = METRICS[metric_name]["column"]
n_no_metric = 0
if column == "congestion":
    n_no_metric = int(frame["congestion"].isna().sum())
    frame = frame[frame["congestion"].notna()].copy()
    if frame.empty:
        st.warning("No sensor has a usable free-flow reference in this slice.")
        st.stop()

# --- headline numbers ---------------------------------------------------------
mean_speed = frame["speed_sum"].sum() / frame["samples"].sum()
all_day_mean = day_profile["speed_sum"].sum() / day_profile["samples"].sum()
worst = frame.nsmallest(1, "speed").iloc[0]

kpi = st.columns(4)
kpi[0].metric(
    f"City average · {window_label}",
    f"{mean_speed:.1f} km/h",
    delta=None if all_day else f"{mean_speed - all_day_mean:+.1f} vs all-day",
    delta_color="normal",
)
kpi[1].metric("Sensors reporting", f"{len(frame)} / {len(dataset.sites)}")
kpi[2].metric(
    "Slowest point", f"{worst['speed']:.1f} km/h", worst["street"], delta_color="off"
)
idle_share = frame["n_zero"].sum() / max(
    frame["n_zero"].sum() + frame["n_moving"].sum(), 1
)
kpi[3].metric(
    "Zero readings",
    f"{idle_share:.1%}",
    help=(
        "Share of readings in this slice that came back as 0 km/h — an empty "
        "lane or stopped traffic, indistinguishable in this file."
    ),
)

notes = []
if dataset.n_flatlined:
    notes.append(
        f"{dataset.n_flatlined} sensor(s) never recorded moving traffic all month "
        "and are excluded as stuck"
    )
if dataset.n_stalled:
    notes.append(
        f"{dataset.n_stalled} sit pinned under 3 km/h for fifteen hours a day yet "
        "run freely at 3am — excluded as not watching through traffic"
    )
if dataset.n_dead_lanes:
    notes.append(
        f"{dataset.n_dead_lanes} individual lane detectors never measured movement "
        f"and are dropped, {dataset.n_sites_with_dead_lanes} of them at sites that "
        "are otherwise fine — lane counts and zero shares here exclude them"
    )
if dataset.n_without_location:
    notes.append(
        f"{dataset.n_without_location} share the feed's placeholder coordinate "
        "(out in the bay) so cannot be mapped — their readings still count in the "
        "daily curve below"
    )
if n_no_metric:
    notes.append(
        f"{n_no_metric} hidden here for having no usable free-flow reference "
        "(under 10 km/h, too small to form a ratio)"
    )
if show_streets:
    n_unmatched = int(frame["site_id"].map(load_streets()[1]).isna().sum())
    if n_unmatched:
        notes.append(
            f"{n_unmatched} could not be matched to a road confidently enough to "
            "paint one — mostly slip roads and tunnel approaches the feed names "
            "as streets — so they appear only as dots"
        )
if notes:
    st.caption("Data quality: " + "; ".join(notes) + ".")

# --- map ---------------------------------------------------------------------
if metric["domain"] is not None:
    vmin, vmax = metric["domain"]
else:
    vmin, vmax = float(frame[column].min()), float(frame[column].max())

def ticks_for(vmin: float, vmax: float, invert: bool, formatter, n: int = 5) -> list[str]:
    """Evenly spaced value labels running low-end -> high-end of the gradient."""
    values = [vmin + (vmax - vmin) * i / (n - 1) for i in range(n)]
    if invert:
        values.reverse()
    return [formatter(v) for v in values]


if metric["kind"] == "diverging":
    limit = max(abs(vmin), abs(vmax))
    frame["color"] = colors.diverging(frame[column], limit, dark=dark_map)
    legend = colors.legend_html(
        label=metric["legend"],
        ticks=ticks_for(-limit, limit, False, metric["tick_format"]),
        kind="diverging", text_color=ink, muted_color=TEXT_MUTED, dark=dark_map,
    )
else:
    frame["color"] = colors.sequential(
        frame[column], vmin, vmax, invert=metric["invert"], dark=dark_map,
        ramp=metric["ramp"],
    )
    legend = colors.legend_html(
        label=metric["legend"],
        ticks=ticks_for(vmin, vmax, metric["invert"], metric["tick_format"]),
        kind="sequential", text_color=ink, muted_color=TEXT_MUTED, dark=dark_map,
        ramp=metric["ramp"],
    )

def label(value, formatter) -> str:
    return "no data" if pd.isna(value) else formatter(value)


frame["metric_label"] = frame[column].map(lambda v: label(v, metric["format"]))
frame["subtitle"] = frame["from_street"] + " → " + frame["to_street"]
frame["footnote"] = (
    frame["samples"].map(lambda v: f"{v:,.0f}") + " readings · "
    + frame["n_lanes"].astype(str) + " lanes"
)

# Lead with the metric being mapped, then the other two -- skipping whichever of
# them the metric already is, so nothing is printed twice. The street layer has
# only the mapped metric, so this extra detail is a per-layer column rather than
# part of the shared template.
detail = pd.Series("", index=frame.index)
if column != "speed":
    detail += "<br/>Average speed: " + frame["speed"].map(lambda v: f"{v:.1f}") + " km/h"
if column != "congestion":
    detail += "<br/>Congestion: " + frame["congestion"].map(
        lambda v: label(v, lambda x: f"{x:.0%}")
    )
frame["detail"] = detail

layers = []
if layer_choice in ("Surface + sensors", "Surface only"):
    field = surface_for(
        CACHE_KEY, dows, buckets, include_zeros, min_samples, column, metric_name
    )
    if field.n_supported:
        flat = pd.Series(field.values.ravel())
        # Coloured with the *same* function as the dots, so the two can never
        # drift onto different scales. Unsupported cells still get a colour --
        # the ramp's low end, since colors treats NaN as zero -- and are hidden
        # with alpha instead, so bilinear filtering has no black to bleed in from.
        if metric["kind"] == "diverging":
            rgb = colors.diverging(flat, limit, dark=dark_map)
        else:
            rgb = colors.sequential(
                flat, vmin, vmax, invert=metric["invert"],
                dark=dark_map, ramp=metric["ramp"],
            )
        alpha = surface.support_alpha(
            field.support.ravel(),
            alpha_min=SURFACE_ALPHA_MIN,
            alpha_max=SURFACE_ALPHA_MAX,
            reference=SURFACE_ALPHA_REF,
            gamma=SURFACE_ALPHA_GAMMA,
        )
        if layer_choice == "Surface only":
            alpha = np.minimum(alpha * 1.15, 1.0)
        alpha = np.where(np.isfinite(flat.to_numpy()), alpha, 0.0)

        ny, nx = field.shape
        raster = np.empty((ny * nx, 4), dtype=np.uint8)
        raster[:, :3] = np.asarray(rgb, dtype=np.uint8)
        raster[:, 3] = np.clip(alpha * 255, 0, 255).astype(np.uint8)

        layers.append(
            pdk.Layer(
                "BitmapLayer",
                # String() again, for the same reason as `aggregation` before it:
                # a bare str is serialised as the accessor expression
                # "@@=data:image/png;..." and deck.gl fails parsing it at the
                # colon. pydeck does this to every plain string prop.
                image=pdk.types.String(
                    surface.to_png_data_uri(raster.reshape(ny, nx, 4))
                ),
                bounds=list(field.bounds),
                opacity=1.0,
                pickable=False,
            )
        )
if show_streets:
    chunks, _ = load_streets()
    values, support = street_field_for(
        CACHE_KEY, dows, buckets, include_zeros, min_samples, column,
        REACH_MODELS[reach_name]["mode"], metric_name,
    )
    painted = np.isfinite(values)
    if painted.any():
        drawn = chunks.loc[painted]
        readings = pd.Series(values[painted])
        # The same two functions the dots and the surface call, so a sensor and
        # the road under it can never end up on different scales.
        if metric["kind"] == "diverging":
            rgb = colors.diverging(readings, limit, dark=dark_map)
        else:
            rgb = colors.sequential(
                readings, vmin, vmax, invert=metric["invert"],
                dark=dark_map, ramp=metric["ramp"],
            )
        alpha = surface.support_alpha(
            support[painted],
            alpha_min=STREET_ALPHA_MIN,
            alpha_max=STREET_ALPHA_MAX,
            reference=STREET_ALPHA_REF,
            gamma=STREET_ALPHA_GAMMA,
        )
        paths = pd.DataFrame(
            {
                "path": [
                    [[x0, y0], [x1, y1]]
                    for x0, y0, x1, y1 in zip(
                        drawn["lon0"], drawn["lat0"], drawn["lon1"], drawn["lat1"]
                    )
                ],
                "color": [
                    [int(r), int(g), int(b), int(a)]
                    for (r, g, b), a in zip(rgb, np.clip(alpha * 255, 0, 255))
                ],
                "street": drawn["name"].to_numpy(),
                "subtitle": reach_name.lower(),
                "metric_label": [metric["format"](v) for v in readings],
                "detail": "",
                "footnote": "estimated along the road, not measured here",
            }
        )
        layers.append(
            pdk.Layer(
                "PathLayer",
                data=paths,
                get_path="path",
                get_color="color",
                # Metres, so the line keeps its real width as you zoom, with
                # pixel bounds so it survives at city zoom without swallowing
                # the dots up close.
                get_width=STREET_WIDTH_M,
                width_min_pixels=STREET_WIDTH_MIN_PX,
                width_max_pixels=STREET_WIDTH_MAX_PX,
                # Butt caps, not rounded. Each chunk is its own two-point path
                # so it can carry its own colour, and at city zoom a 60 m chunk
                # is about as long as the line is wide -- rounded caps turn every
                # one of them into a circle and the avenue renders as a string of
                # beads. Butt caps let consecutive chunks tile exactly.
                cap_rounded=False,
                joint_rounded=True,
                pickable=True,
                auto_highlight=True,
            )
        )

if layer_choice in ("Surface + sensors", "Streets + sensors", "Sensors only"):
    layers.append(
        pdk.Layer(
            "ScatterplotLayer",
            data=frame,
            get_position=["lon", "lat"],
            get_fill_color="color",
            get_radius=85,
            radius_min_pixels=4,
            radius_max_pixels=14,
            stroked=True,
            line_width_min_pixels=1.5,
            get_line_color=colors.hex_to_rgb_list(map_surface),
            pickable=True,
            auto_highlight=True,
        )
    )

# One template both layers can fill: a sensor and a stretch of road carry
# different fields, so the parts that differ arrive as pre-rendered columns.
tooltip = {
    "html": (
        "<div style='font-family:system-ui,sans-serif;font-size:12px;max-width:260px'>"
        "<b>{street}</b><br/>"
        "<span style='opacity:.75'>{subtitle}</span><hr "
        "style='margin:4px 0;border:none;border-top:1px solid rgba(255,255,255,.2)'/>"
        f"{metric_name}: <b>{{metric_label}}</b>{{detail}}"
        "<br/><span style='opacity:.75'>{footnote}</span>"
        "</div>"
    ),
    "style": {"backgroundColor": "#0b0b0b", "color": "#ffffff", "borderRadius": "6px"},
}

st.pydeck_chart(
    pdk.Deck(
        layers=layers,
        initial_view_state=pdk.ViewState(
            latitude=-34.869, longitude=-56.163, zoom=11.6, pitch=0, bearing=0
        ),
        map_provider="carto",
        map_style=basemap,
        tooltip=tooltip,
    ),
    use_container_width=True,
    height=560,
)
st.markdown(legend, unsafe_allow_html=True)
if show_streets:
    note = (
        REACH_MODELS[reach_name]["caption"]
        + " Sensors are matched to roads by position first and street name "
        "second, so the name only has to tell apart the roads meeting at a "
        "known corner."
    )
    # Only the blend averages, and only the sensor layer shows the raw values,
    # so this advice is worth giving in exactly one of the six combinations.
    if reach_name == "Blend along the avenue" and layer_choice == "Streets + sensors":
        note += " Read the dots for the actual measurements."
    st.caption(note)
elif layer_choice != "Sensors only":
    st.caption(
        "The surface is a distance-weighted average of the sensors near each "
        "point — the same estimate everywhere, so it no longer matters how many "
        "neighbours a sensor happens to have. It fades where few sensors support "
        f"it and stops entirely more than {SURFACE_CUTOFF_KM:g} km from any "
        "sensor, on the same fixed colour scale as the dots. Read the dots for "
        "actual measurements: averaging necessarily softens the extremes."
    )

# --- time-of-day profile -----------------------------------------------------
st.subheader("How the whole city moves through the day")

def make_axis(**overrides) -> alt.Axis:
    """Recessive hairline grid and muted labels, per the chart-chrome rules."""
    return alt.Axis(
        grid=True, gridColor=GRIDLINE, gridWidth=1, domainColor=GRIDLINE,
        tickColor=GRIDLINE, labelColor=TEXT_MUTED, titleColor=ink_secondary,
        **overrides,
    )


axis = make_axis()
hour_axis = make_axis(values=list(range(0, 25, 3)), format="d")
base = alt.Chart(day_profile)
line = base.mark_line(color="#2a78d6", strokeWidth=2).encode(
    x=alt.X("hour:Q", title="Hour of day",
            scale=alt.Scale(domain=[0, 24], nice=False), axis=hour_axis),
    y=alt.Y("speed:Q", title="km/h",
            scale=alt.Scale(zero=False, nice=True), axis=axis),
    tooltip=[
        alt.Tooltip("time:N", title="Time"),
        alt.Tooltip("speed:Q", title="km/h", format=".1f"),
        alt.Tooltip("samples:Q", title="Readings", format=","),
    ],
)
hover = base.mark_rule(color=TEXT_MUTED, strokeWidth=1).encode(
    x="hour:Q", opacity=alt.value(0)
).add_params(alt.selection_point(on="mouseover", nearest=True, fields=["hour"], empty=False))
chart = line + hover
if not all_day:
    marker = (
        alt.Chart(pd.DataFrame({"hour": [selected * BUCKET_MINUTES / 60]}))
        .mark_rule(color="#eb6834", strokeWidth=2)
        .encode(x="hour:Q")
    )
    chart = chart + marker
st.altair_chart(
    chart.properties(height=220, background=chart_surface).configure_view(strokeWidth=0),
    use_container_width=True,
)
if not all_day:
    st.caption(
        f"The orange rule marks the selected {mvd.bucket_label(selected)} slot. "
        f"City-wide low is {day_profile['speed'].min():.1f} km/h at "
        f"{day_profile.loc[day_profile['speed'].idxmin(), 'time']}, high is "
        f"{day_profile['speed'].max():.1f} km/h at "
        f"{day_profile.loc[day_profile['speed'].idxmax(), 'time']}."
    )

# --- rankings & street comparison --------------------------------------------
left, right = st.columns(2, gap="large")
with left:
    st.subheader(f"Slowest stretches · {window_label}")
    ranking = frame.nsmallest(12, "speed").copy()
    ranking["congestion_pct"] = ranking["congestion"] * 100
    st.dataframe(
        ranking[["tramo", "speed", "congestion_pct", "samples"]].rename(
            columns={
                "tramo": "Stretch", "speed": "km/h",
                "congestion_pct": "Congestion", "samples": "Readings",
            }
        ),
        hide_index=True, use_container_width=True,
        column_config={
            "km/h": st.column_config.NumberColumn(format="%.1f"),
            "Congestion": st.column_config.ProgressColumn(
                format="%.0f%%", min_value=0, max_value=100,
            ),
            "Readings": st.column_config.NumberColumn(format="%d"),
        },
    )
    st.caption(
        "The table is the accessible read of the map: every value here is also "
        "encoded as colour above."
    )
    export = frame[
        ["street", "from_street", "to_street", "lat", "lon", "speed",
         "congestion", "vs_typical", "samples", "n_lanes"]
    ].sort_values("speed")
    st.download_button(
        "Download this slice as CSV",
        export.to_csv(index=False).encode(),
        file_name=f"mvd_speed_{'all_day' if all_day else mvd.bucket_label(selected).replace(':', '')}.csv",
        mime="text/csv",
    )
with right:
    st.subheader("Compare streets through the day")
    options = sorted(dataset.sites["street"].unique())
    default = [s for s in ["18 de Julio", "Av Italia", "Bv Artigas"] if s in options]
    chosen = st.multiselect(
        "Streets", options, default=default or options[:3], max_selections=3,
        label_visibility="collapsed",
        help="Up to three, so every line stays tellable apart under colour-vision deficiency.",
    )
    if chosen:
        street_frame = street_curves(CACHE_KEY, tuple(chosen), dows, include_zeros)
        # Slots 1-3 of the categorical theme, stepped for the active surface.
        # These three are the set that clears the all-pairs colour-vision gates.
        # The chart surface is always light, so these are the light steps.
        hues = ["#2a78d6", "#eb6834", "#1baf7a"]
        scale = alt.Scale(domain=chosen, range=hues[: len(chosen)])
        street_base = alt.Chart(street_frame).encode(
            x=alt.X("hour:Q", title="Hour of day",
                    scale=alt.Scale(domain=[0, 24], nice=False), axis=hour_axis),
            y=alt.Y("speed:Q", title="km/h", scale=alt.Scale(zero=False), axis=axis),
            color=alt.Color("street:N", title=None, scale=scale,
                            legend=alt.Legend(orient="top", labelColor=ink_secondary)),
        )
        st.altair_chart(
            street_base.mark_line(strokeWidth=2)
            .encode(
                tooltip=[
                    alt.Tooltip("street:N", title="Street"),
                    alt.Tooltip("time:N", title="Time"),
                    alt.Tooltip("speed:Q", title="km/h", format=".1f"),
                    alt.Tooltip("samples:Q", title="Readings", format=","),
                ]
            )
            .properties(height=260, background=chart_surface)
            .configure_view(strokeWidth=0),
            use_container_width=True,
        )
        # These lines converge in the evening, so end-of-line labels would
        # collide. The table is the non-colour read instead -- which the light
        # aqua's 2.74:1 contrast against the surface makes an obligation.
        with st.expander("Table view of these lines"):
            table = street_frame.pivot(
                index="time", columns="street", values="speed"
            ).round(1)
            st.dataframe(table, use_container_width=True)
    else:
        st.info("Pick at least one street.")

# --- animation ---------------------------------------------------------------
if playing and not all_day:
    time.sleep(0.45)
    st.session_state.bucket = (selected + 1) % BUCKETS
    st.rerun()
