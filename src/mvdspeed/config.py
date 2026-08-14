"""Paths, data-cleaning constants and the visualization palette."""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_RAW = PROJECT_ROOT / "data" / "raw"
DATA_PROCESSED = PROJECT_ROOT / "data" / "processed"

DATA_OSM = PROJECT_ROOT / "data" / "osm"

# ETL outputs
MEASUREMENTS_PARQUET = DATA_PROCESSED / "measurements.parquet"
DETECTORS_PARQUET = DATA_PROCESSED / "detectors.parquet"
WEATHER_PARQUET = DATA_PROCESSED / "weather.parquet"

# Road geometry. Unlike the parquet above this is committed: it is small, it
# changes only when OSM does, and the app's one network dependency should stay
# the basemap CDN. The raw Overpass responses under data/osm/ are the cache it
# is built from and are not committed.
STREETS_PARQUET = PROJECT_ROOT / "data" / "streets.parquet"

# Hand-curated calendars. Nothing in the open data says when a football match
# kicked off or when a holiday fell, and no feed we can reach publishes either,
# so these two are typed in by hand with a per-row source URL and committed. The
# `verified` column in matches.csv is the record of what has been checked; rows
# without a kick-off time are carried rather than guessed at, and skipped by the
# estimator until someone fills one in.
DATA_EVENTS = PROJECT_ROOT / "data" / "events"
MATCHES_CSV = DATA_EVENTS / "matches.csv"
HOLIDAYS_CSV = DATA_EVENTS / "holidays.csv"

# --- Source catalogue --------------------------------------------------------
# The monthly files are enumerated through CKAN's package_show, not by scraping
# the dataset page and not by guessing URLs. Both alternatives break on things
# the catalogue actually does:
#
#   - The filenames change era to era. The 2021 files are named
#     `autoscope_01_2021_velocidad.csv`; from mid-2025 they are
#     `velocidad_promedio_julio_2026.zip`. Nothing about the first pattern
#     predicts the second.
#   - The `format` field lies. July 2026 is labelled "CSV" and its URL ends in
#     `.zip`, and the distinct values of `format` across the resources include
#     the string "csv zip". So the archive type is read off the URL extension
#     and from nothing else.
#
# 68 resources as of August 2026, January 2021 onward, one per month with no gaps.
# Eight of them (July-December 2023, July and August 2025) carry a null `size`,
# which looks like a missing file and is not one: all eight serve normal CSVs when
# asked, verified by range request. So `size` is used as a resume check when it is
# present and simply skipped when it is not -- never as evidence that a month does
# not exist.
CKAN_API = "https://catalogodatos.gub.uy/api/3/action/package_show"
DATASET_SLUG = "velocidad-promedio-vehicular-en-las-principales-avenidas-de-montevideo"

# Downloads are stored exactly as published -- a `.zip` stays zipped. The ETL
# extracts one month at a time to a scratch file and deletes it after ingest, so
# peak disk is the archive plus one expanded month (~800 MB) rather than the
# ~51 GB the whole history would occupy uncompressed. Keeping the bytes as
# published is also what lets the ingest log verify a file against the size the
# catalogue reports for it.
#
# The month is read from the CKAN resource *title* ("Velocidad promedio - Marzo
# 2024") and then checked against the modal `fecha` in the readings themselves.
# Titles are the part most likely to be renamed; the timestamps are the data.
MONTHS_ES = (
    "Enero",
    "Febrero",
    "Marzo",
    "Abril",
    "Mayo",
    "Junio",
    "Julio",
    "Agosto",
    "Septiembre",
    "Octubre",
    "Noviembre",
    "Diciembre",
)

# --- Weather ------------------------------------------------------------------
# From INUMET, published on the same catalogue as the speed data, so the whole
# dashboard has one provenance story. Four separate datasets, one per variable,
# each a single national CSV covering 2020 onward -- about 45 MB in total, which
# is small enough to refetch whole rather than incrementally.
#
# INUMET publishes seven G3 stations nationwide and exactly one of them is in
# Montevideo: Aeropuerto Melilla, in the northwest of the city. That is the
# honest limitation of this crossing and it is stated in the app rather than
# buried here. The station sits roughly 10 km from the centre of the sensor
# field, so it reports the weather *system* over Montevideo well and a single
# convective cell over 18 de Julio badly. Frontal rain -- which is what most
# Montevideo rain is -- covers the whole city and is measured fine; a summer
# thunderstorm may be caught at the airport and missed downtown, or the reverse.
#
# Coverage for January to mid-August 2026, measured: rain, temperature and
# humidity are hourly at 99.4-99.5% of hours. Wind is reported roughly every
# three hours (48% of hours), so it is carried but not used for the headline
# wet/dry split.
INUMET_DATASETS = {
    # variable -> (CKAN slug, column in that dataset's CSV)
    "precip_mm": (
        "inumet-observaciones-meteorologicas-precipitacion-puntual-en-el-uruguay",
        "precip_horario",
    ),
    "temp_c": (
        "inumet-observaciones-meteorologicas-temperatura-del-aire-en-el-uruguay",
        "temp_aire",
    ),
    "humidity_pct": (
        "inumet-observaciones-meteorologicas-humedad-relativa-en-el-uruguay",
        "hum_relativa",
    ),
    "wind_kmh": (
        "inumet-observaciones-meteorologicas-direccion-e-intensidad-del-viento-en-el-uruguay",
        "int_viento",
    ),
}
INUMET_STATION = "Aeropuerto Melilla G3"

# Where that station is, for the distance note in the app.
INUMET_STATION_LATLON = (-34.7883, -56.2645)

# A bucket counts as wet when the hour it falls in accumulated at least this
# much. 0.1 mm is the smallest amount a tipping-bucket gauge resolves, so it is
# the boundary between "measurably rained" and "did not", not a judgement about
# how much rain matters.
RAIN_WET_MM = 0.1

# Heavy rain, kept as a separate band because the interesting question is whether
# the speed penalty scales with intensity or saturates as soon as the road is wet.
# 2.5 mm/h is the conventional moderate/heavy boundary.
RAIN_HEAVY_MM = 2.5

# A road stays wet after the rain stops, so an hour with no accumulation that
# follows a wet one is not really a dry-road hour. Buckets are additionally
# flagged as `recently_wet` when rain fell in the preceding this-many hours, and
# the app's dry baseline excludes them -- otherwise the "dry" side of the
# comparison quietly contains wet tarmac and the measured penalty shrinks.
RAIN_LAG_HOURS = 2

# --- The football crossing ----------------------------------------------------
# How far either side of kick-off the event study looks, in buckets. Eight is
# four hours, which is wider than any plausible effect and deliberately so: the
# hypothesis on the table says the interesting moment is the half hour *before*
# kick-off, and a window drawn tightly around that assumption cannot ever
# contradict it. A wide window lets the data say where the effect sits, and
# gives the placebo band somewhere quiet to be measured against.
EVENT_WINDOW_BUCKETS = 8

# A match occupies roughly this long from kick-off to final whistle: two halves
# of 45 plus a 15-minute interval, before stoppage time. Used only to place the
# "during" window and to draw the second rule on the chart.
MATCH_MINUTES = 105

# Control days are drawn from the same weekday within this many weeks either
# side of the match. Four weeks gives up to eight candidates, which is enough to
# average the day-to-day noise down without reaching so far that the season
# changes underneath the comparison.
#
# The width matters more than it looks. Measured against a baseline pooled over
# the whole Jan-Aug panel, *every* June weekday afternoon comes out 0.54 km/h
# slow -- not because of football but because January, when the city empties for
# the summer, sits in the same average and pulls it up by 2.07. That artefact is
# the same size and the same sign as the pre-kick-off congestion this page is
# looking for, and a pooled baseline would have reported it as a finding.
CONTROL_WEEKS = 4

# Sensors nearer than this to a stadium are the "near ring"; sensors past
# FAR_RING_KM are the comparison. The gap between them is left empty on purpose,
# so the two groups are not arguing over the same kerb.
NEAR_RING_KM = 1.5
FAR_RING_KM = 5.0

# How many placebo event sets the null band is built from. Each draw keeps the
# real fixtures' weekday and kick-off time and moves them to an eligible date, so
# the band answers "how big a swing does this estimator produce on a day when
# nothing happened?" -- which a t-test over 4.4 M serially correlated rows cannot.
PLACEBO_DRAWS = 500

# Fixed so the page's numbers do not move between reruns. A band that shifted
# every time someone touched a slider would be impossible to argue with.
PLACEBO_SEED = 20260615

# --- Site identity across months ---------------------------------------------
# A measuring site is identified by its coordinate and its three street labels,
# and the coordinate has to be rounded before it is compared. The publisher
# changed coordinate precision between March and April 2026: January through
# March carry up to eight decimal places, April onward exactly six. The same
# physical sensor is therefore written two different ways, and on the exact tuple
# it splits into two sites -- 120 of them, each holding part of the year and each
# computing its own "lifetime" free-flow reference from three or five months.
# Nothing crashes; the map just grows a second dot per sensor and every reference
# silently narrows.
#
# Six decimals is the fix, and the arithmetic is what fixes the value rather than
# a tuned tolerance:
#
#   - Rounding to 6 dp moves a coordinate by at most 0.5e-6 deg, which is 5.6 cm
#     in latitude and 4.6 cm in longitude at this latitude -- so two spellings of
#     one point land at most ~7.3 cm apart. Measured worst case: 6.9 cm.
#   - Two *distinct* 6-dp coordinates differ by at least 1e-6 deg, which is
#     11.1 cm in latitude and 9.1 cm in longitude. Measured closest pair of
#     genuinely different sites that report in the same month: 11.1 cm.
#
# The artifact is strictly smaller than the smallest real separation, so rounding
# cannot merge two sensors that are actually different. Verified across the eight
# 2026 files: 120 coordinate pairs merged, none of which ever reported in the same
# month, which is the independent check -- two sites present in one month are
# distinct by construction.
COORD_DECIMALS = 6

# --- Data cleaning -----------------------------------------------------------
# Readings above this are sensor errors (the raw file tops out at 540 km/h on
# urban streets). ~0.24% of rows.
MAX_PLAUSIBLE_SPEED = 120

# A `velocidad` of 0 is ambiguous: it means either "no vehicle crossed the lane
# in these 5 minutes" (an empty street at 4am) or "traffic is fully stopped".
# The two cannot be told apart from this file alone, so zeros are stored
# separately from the speed sum and the app decides how to treat them.
# Default: excluded from speed averages, surfaced as a coverage number.

# Minutes per time bucket used for the time-of-day slider.
BUCKET_MINUTES = 30

# Free-flow speed per detector is estimated as this percentile of its non-zero
# readings — the standard proxy used in traffic engineering, robust to both
# jams and to the occasional speeding outlier.
FREE_FLOW_PERCENTILE = 0.85

# A bucket needs at least this many non-zero readings to be plotted, so that a
# single stray reading never paints a hotspot.
MIN_SAMPLES = 3

# Congestion is a ratio against a sensor's own free-flow speed, so a tiny
# denominator makes it meaningless: speeds are whole km/h, so at a 10 km/h
# reference the +/-0.5 quantization is already worth 5%. Sites below this get no
# congestion figure at all rather than a fake one -- this is what stopped a
# sensor with a 1 km/h reference from reporting "0% congested" while crawling.
MIN_FREE_FLOW_FOR_RATIO = 10

# A sensor whose lifetime average never reaches this has not observed moving
# traffic at any point in the month -- a stuck detector, not a slow street.
# Excluded from the map and rankings by default, and counted in the UI.
FLATLINE_SPEED = 3.0

# A detector can read plausibly overnight and still be useless by day. Seven of
# them sit pinned at walking pace for fifteen hours straight and then report
# 15-28 km/h at 3am, while their free-flow reference lands at a third of what
# neighbouring sensors on the same avenue see (12-14 km/h against a median of 45
# along Av Italia). Real congestion clears by late evening; theirs never does,
# so they are not measuring through traffic -- most likely a turn lane, a bus
# bay or a queue that is permanently occupied.
#
# Measured on weekdays only: the day/night contrast is defined by the commute,
# and weekend mornings dilute the daytime window enough to hide one of them.
STALLED_DAY_HOURS = (7, 22)
STALLED_NIGHT_HOURS = (2, 6)
STALLED_DAY_SPEED = 3.0
STALLED_NIGHT_RATIO = 3.0

# The feed falls back to a single placeholder coordinate when it has no real
# one, which lands a pile of unrelated sensors on one spot in the bay. Rather
# than hardcode that coordinate (next month's file may use another), detect it
# structurally: a genuine point carries at most two street names -- an
# intersection, or one street under two spellings ("Ariel" / "Camino Ariel").
# Three or more distinct streets at identical coordinates means the coordinate
# is a fallback value, not a place.
MAX_STREETS_PER_COORD = 2

# --- Heat surface ------------------------------------------------------------
# The map's smooth layer is computed here rather than by deck.gl's HeatmapLayer,
# which estimates point *density* -- the wrong statistical object for an
# attribute measured at fixed stations. Binning-then-averaging made intensity
# depend on how many neighbours a sensor happens to have: the east of the city
# has a median of 3 sensors within 1 km against 30 in the centre, so a lone
# sensor rendered undiluted while a clustered one was averaged toward the local
# mean. Two sensors with identical congestion drew differently.
#
# Instead, every grid cell gets the same expression -- a Gaussian
# distance-weighted mean of the sensors near it -- so density no longer changes
# intensity. It changes the *support* for the estimate, which is encoded as
# opacity instead of being hidden.
SURFACE_STEP_KM = 0.2       # 10,200 cells over the sensor bbox, ~45 ms to build
SURFACE_BANDWIDTH_KM = 0.5  # 38% of cells end up supported; the rest is empty
SURFACE_CUTOFF_KM = 1.2     # beyond this we assert nothing at all

# Support (the sum of kernel weights, roughly a proximity-weighted sensor count)
# spans 0.02 to 52 on real slices, so opacity has to be sublinear or almost
# every cell renders invisible.
#
# The reference matters more than it looks. At 2.0 everything from three sensors
# up saturated, and a lone sensor still drew at 0.64 -- 75% as opaque as a cell
# backed by fifty, which is visually indistinguishable. The confidence channel
# was therefore saying nothing, and a single working lane detector in Carrasco
# painted 600 m of crimson that read as authoritative. At 8.0 the spread lands
# where the sensors actually are: 1 sensor -> 0.20, 3 -> 0.43, 10+ -> full.
SURFACE_ALPHA_MAX = 0.85
SURFACE_ALPHA_MIN = 0.10
SURFACE_ALPHA_REF = 8.0     # support at which a cell counts as well-backed
SURFACE_ALPHA_GAMMA = 0.7

# Local equirectangular conversion, good enough across a single city.
KM_PER_DEG_LAT = 111.0
CITY_LAT = -34.9

# --- Street corridors ---------------------------------------------------------
# The surface paints an *area* for what is really a linear measurement: a sensor
# watches one stretch of one avenue, not the neighbourhood around it. The street
# layer draws the roads themselves, using OSM geometry fetched once by
# `mvdspeed-osm` and committed as data/streets.parquet.
#
# Sensor -> road matching is done by proximity first and name second, which is
# what makes it tractable. Every sensor already carries a coordinate, so the name
# only has to disambiguate between the two-to-four roads meeting at the
# intersection it sits on -- a much easier job than identifying a street from
# `Bv. Artgias` alone. Names are compared on normalized tokens (accents stripped,
# generic words like Avenida/Bulevar/General dropped), so `L A de Herrera`
# reaches `Avenida Luis Alberto de Herrera`.
OSM_BBOX = (-34.94, -56.29, -34.79, -56.03)  # south, west, north, east
OSM_HIGHWAY_CLASSES = {
    # Fetched as separate queries: asking Overpass for all of them at once times
    # out on the public endpoints, while each half returns comfortably.
    "major": "motorway|trunk|primary|secondary|tertiary",
    "minor": "residential|unclassified|living_street",
}

# A way's own nodes are the chunks, but OSM digitizes a straight kilometre with
# very few of them, and a chunk is one flat colour. Anything longer is split.
STREET_CHUNK_M = 60.0

# How far a sensor may sit from a road and still be considered to be on it.
# Sensors are placed at intersections and the coordinates are rounded, so a true
# match lands 5-40 m off the centreline; the dual carriageways (Bv Artigas, Av
# Italia) are mapped as two ways and the far one can be ~30 m further still.
STREET_SNAP_M = 120.0

# Name-similarity floor, on the 0-1 token score. Below this the site keeps no
# corridor at all rather than being snapped to whatever road happens to be
# nearest -- painting a whole avenue from a sensor that was actually measuring
# the cross street is a worse failure than leaving it as a dot. The count of
# unmatched sites is reported in the app.
STREET_NAME_FLOOR = 0.6

# The reach of one sensor along its corridor. Same Gaussian as the surface, but
# the bandwidth is tighter: the surface is smoothing across a neighbourhood,
# where this is interpolating along a single avenue between sensors that sit
# roughly 400-800 m apart.
STREET_BANDWIDTH_KM = 0.4
STREET_CUTOFF_KM = 1.0      # past this the corridor is simply not painted
STREET_STUB_KM = 0.25       # the "measured stretch" reach model

# Support here is a sum of kernel weights over the sensors on *one corridor*, so
# it lands between 0 and ~3 where the surface's spans 0-52. Reusing
# SURFACE_ALPHA_REF (8.0) drew every avenue at the 0.10 floor, i.e. invisible.
STREET_ALPHA_MAX = 0.95
STREET_ALPHA_MIN = 0.15
STREET_ALPHA_REF = 1.2      # one sensor at ~250 m already counts as backed
STREET_ALPHA_GAMMA = 0.6

# Drawn in metres so the line keeps its real width as you zoom, with pixel
# bounds so it stays visible at city zoom and does not swallow the dots up close.
STREET_WIDTH_M = 22.0
STREET_WIDTH_MIN_PX = 2.5   # at city zoom the real width is under a pixel
STREET_WIDTH_MAX_PX = 10.0

# --- Palette (see dataviz skill: sequential = one hue, diverging = 2 + gray) --
SURFACE_LIGHT = "#fcfcfb"
SURFACE_DARK = "#1a1a19"

# Sequential blue ramp, light -> dark, for magnitude (speed, volume).
SEQUENTIAL_BLUE = [
    "#cde2fb",
    "#b7d3f6",
    "#9ec5f4",
    "#86b6ef",
    "#6da7ec",
    "#5598e7",
    "#3987e5",
    "#2a78d6",
    "#256abf",
    "#1c5cab",
    "#184f95",
    "#104281",
    "#0d366b",
]

# Semantic heat, for the traffic-severity metrics. Multi-hue sequential is only
# allowed for analogous neighbours or semantic heat, and this is both: yellow ->
# orange -> red are neighbours, and "hot = congested" is the convention every
# traffic map already uses. Verified strictly monotonic in OKLab lightness
# (0.988 -> 0.381, step deltas 0.044-0.123), which is what keeps it readable as
# an ordered scale -- including under colour-vision deficiency, where the
# lightness ordering survives even if the hue shift does not.
SEQUENTIAL_HEAT = [
    "#ffffcc",
    "#ffeda0",
    "#fed976",
    "#feb24c",
    "#fd8d3c",
    "#fc4e2a",
    "#e31a1c",
    "#bd0026",
    "#800026",
]

# On a dark basemap the same ramp runs the other way: the *lightest* step now
# carries the highest value and the darkest recedes into the surface, which is
# what keeps a fully congested sensor visible against near-black. This is a
# deliberate re-selection for the dark surface, not an automatic inversion of
# the light styling.
SEQUENTIAL_BLUE_DARK = list(reversed(SEQUENTIAL_BLUE))
SEQUENTIAL_HEAT_DARK = list(reversed(SEQUENTIAL_HEAT))

# Diverging blue <-> red with a neutral gray midpoint, for "slower / faster
# than this sensor's own typical speed". Each mode gets poles stepped for its
# own surface and a midpoint that recedes into it.
DIVERGING_COOL = "#2a78d6"
DIVERGING_WARM = "#d03b3b"
DIVERGING_MID = "#f0efec"

DIVERGING_COOL_DARK = "#3987e5"
DIVERGING_WARM_DARK = "#e66767"
DIVERGING_MID_DARK = "#383835"

TEXT_PRIMARY = "#0b0b0b"
TEXT_SECONDARY = "#52514e"
TEXT_MUTED = "#898781"
GRIDLINE = "#e1e0d9"

# De-emphasis, for context marks in an emphasis chart -- the series that are
# present to be compared against rather than read individually. Deliberately low
# contrast against the chart surface: the accent series has to win. This is
# furniture rather than a categorical slot, so it is exempt from the chroma floor
# every series colour has to clear.
#
# It exists because eight months cannot be told apart by hue. Stepping the blue
# ramp into eight series puts adjacent months at OKLab dE 5.4 against a floor of
# 15 -- a full-colour reader cannot separate April from May, let alone a
# colour-blind one. Rather than a ninth attempt at a palette, the month curves use
# one accent against this gray, which is the documented remedy for too many
# series.
DE_EMPHASIS = "#c4c2ba"
ACCENT = "#2a78d6"


def km_per_deg_lon(lat: float = CITY_LAT) -> float:
    """Longitude scale at a given latitude, for the local flat approximation."""
    from math import cos, radians

    return KM_PER_DEG_LAT * cos(radians(lat))


def hex_to_rgb(value: str) -> list[int]:
    value = value.lstrip("#")
    return [int(value[i : i + 2], 16) for i in (0, 2, 4)]
