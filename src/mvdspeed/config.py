"""Paths, data-cleaning constants and the visualization palette."""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_RAW = PROJECT_ROOT / "data" / "raw"
DATA_PROCESSED = PROJECT_ROOT / "data" / "processed"

# ETL outputs
MEASUREMENTS_PARQUET = DATA_PROCESSED / "measurements.parquet"
DETECTORS_PARQUET = DATA_PROCESSED / "detectors.parquet"

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


def hex_to_rgb(value: str) -> list[int]:
    value = value.lstrip("#")
    return [int(value[i : i + 2], 16) for i in (0, 2, 4)]
