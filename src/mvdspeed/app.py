"""Streamlit dashboard: Montevideo average speed by time of day.

Run with:  uv run streamlit run src/mvdspeed/app.py
"""

from __future__ import annotations

import time

import altair as alt
import pandas as pd
import pydeck as pdk
import streamlit as st

from mvdspeed import colors, data as mvd, surface
from mvdspeed.config import (
    BUCKET_MINUTES,
    GRIDLINE,
    MAX_PLAUSIBLE_SPEED,
    SURFACE_ALPHA_GAMMA,
    SURFACE_ALPHA_MAX,
    SURFACE_ALPHA_MIN,
    SURFACE_ALPHA_REF,
    SURFACE_CUTOFF_KM,
    SURFACE_DARK,
    SURFACE_LIGHT,
    SURFACE_STEP_KM,
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


@st.cache_resource(show_spinner="Loading sensor data…")
def load_data() -> mvd.Dataset:
    return mvd.load()


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
) -> pd.DataFrame:
    """The kernel surface for one slice. Cached so the ▶ Play loop stays smooth.

    Keyed on everything that changes the sensor values, but not on colour: the
    ramp is applied afterwards, so switching light/dark reuses the same grid.
    """
    return surface.kernel_surface(
        sites_at(_key, dows, buckets, include_zeros, min_samples), column
    )


@st.cache_data(show_spinner=False)
def profile(_key: tuple, dows: tuple[int, ...], include_zeros: bool) -> pd.DataFrame:
    return mvd.city_profile(load_data(), dows=list(dows), include_zeros=include_zeros)


@st.cache_data(show_spinner=False)
def streets(_key: tuple, names: tuple[str, ...], dows: tuple[int, ...],
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
        ["Surface + sensors", "Surface only", "Sensors only"],
        index=0,
        help=(
            "The surface is a distance-weighted estimate between sensors, faded "
            "where few sensors support it. The dots are the actual measurements."
        ),
    )

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


frame["speed_label"] = frame["speed"].map(lambda v: f"{v:.1f}")
frame["metric_label"] = frame[column].map(lambda v: label(v, metric["format"]))
frame["congestion_label"] = frame["congestion"].map(
    lambda v: label(v, lambda x: f"{x:.0%}")
)

layers = []
if layer_choice in ("Surface + sensors", "Surface only"):
    cells = surface_for(
        CACHE_KEY, dows, buckets, include_zeros, min_samples, column, metric_name
    )
    if not cells.empty:
        # Coloured with the *same* function as the dots, so the two can never
        # drift onto different scales, then faded by how well each cell is
        # supported by nearby sensors.
        if metric["kind"] == "diverging":
            rgb = colors.diverging(cells["value"], limit, dark=dark_map)
        else:
            rgb = colors.sequential(
                cells["value"], vmin, vmax, invert=metric["invert"],
                dark=dark_map, ramp=metric["ramp"],
            )
        alpha = surface.support_alpha(
            cells["support"],
            alpha_min=SURFACE_ALPHA_MIN,
            alpha_max=SURFACE_ALPHA_MAX,
            reference=SURFACE_ALPHA_REF,
            gamma=SURFACE_ALPHA_GAMMA,
        )
        if layer_choice == "Surface only":
            alpha = alpha * 1.1
        cells = cells.assign(
            color=[[*c, int(a * 255)] for c, a in zip(rgb, alpha)]
        )
        layers.append(
            pdk.Layer(
                "GridCellLayer",
                data=cells,
                get_position=["lon", "lat"],
                cell_size=SURFACE_STEP_KM * 1000,
                get_fill_color="color",
                extruded=False,
                pickable=False,
            )
        )
if layer_choice in ("Surface + sensors", "Sensors only"):
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

# Lead with the metric being mapped, then the other two -- skipping whichever of
# them the metric already is, so nothing is printed twice.
tooltip_rows = [f"{metric_name}: <b>{{metric_label}}</b>"]
if column != "speed":
    tooltip_rows.append("Average speed: {speed_label} km/h")
if column != "congestion":
    tooltip_rows.append("Congestion: {congestion_label}")

tooltip = {
    "html": (
        "<div style='font-family:system-ui,sans-serif;font-size:12px;max-width:260px'>"
        "<b>{street}</b><br/>"
        "<span style='opacity:.75'>{from_street} → {to_street}</span><hr "
        "style='margin:4px 0;border:none;border-top:1px solid rgba(255,255,255,.2)'/>"
        + "<br/>".join(tooltip_rows)
        + "<br/><span style='opacity:.75'>{samples} readings · {n_lanes} lanes</span>"
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
if layer_choice != "Sensors only":
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
        street_frame = streets(CACHE_KEY, tuple(chosen), dows, include_zeros)
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
