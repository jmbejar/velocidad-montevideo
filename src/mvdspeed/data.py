"""Loading and slicing of the pre-aggregated parquet files."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from mvdspeed.config import (
    BUCKET_MINUTES,
    DETECTORS_PARQUET,
    FLATLINE_SPEED,
    MAX_STREETS_PER_COORD,
    MEASUREMENTS_PARQUET,
    MIN_FREE_FLOW_FOR_RATIO,
    MIN_SAMPLES,
    RAIN_HEAVY_MM,
    RAIN_LAG_HOURS,
    RAIN_WET_MM,
    STALLED_DAY_SPEED,
    STALLED_NIGHT_RATIO,
    WEATHER_PARQUET,
)

BUCKETS_PER_DAY = 24 * 60 // BUCKET_MINUTES

DAY_SCOPES = {
    "Weekdays (Mon-Fri)": [1, 2, 3, 4, 5],
    "Weekend (Sat-Sun)": [6, 0],
    "Every day": list(range(7)),
}

# How the weather column narrows a slice. The dry baseline is the strict one: it
# excludes the hours after rain as well as the rain itself, because a road stays
# wet and a "dry" set that quietly contains wet tarmac understates every penalty
# measured against it. Hours the station did not report are excluded from all
# three named scopes -- a missing reading is not evidence of dry weather.
RAIN_SCOPES = {
    "Any weather": None,
    "Dry roads only": "dry",
    "While it rained": "wet",
    "Heavy rain only": "heavy",
}

WEATHER_COLUMNS = ["precip_mm", "temp_c", "humidity_pct", "is_wet", "is_heavy",
                   "recently_wet"]


def bucket_label(bucket: int) -> str:
    minutes = bucket * BUCKET_MINUTES
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def month_label(month: str) -> str:
    """`"2026-03"` -> `"Mar 2026"`, for axes and legends."""
    stamp = pd.Timestamp(f"{month}-01")
    return f"{stamp.strftime('%b')} {stamp.year}"


@dataclass(frozen=True)
class Dataset:
    measurements: pd.DataFrame
    sites: pd.DataFrame
    weather: pd.DataFrame | None

    @property
    def months(self) -> list[str]:
        return sorted(self.measurements["month"].unique())

    @property
    def dates(self) -> list[pd.Timestamp]:
        return sorted(self.measurements["date"].unique())

    @property
    def has_weather(self) -> bool:
        return self.weather is not None

    @property
    def n_flatlined(self) -> int:
        return int((~self.sites["is_live"]).sum())

    @property
    def n_without_reference(self) -> int:
        # Counted among usable sensors only, so the stalled ones are not
        # reported twice under two different reasons.
        return int((self.sites["is_usable"] & ~self.sites["has_reference"]).sum())

    @property
    def n_without_location(self) -> int:
        return int((self.sites["is_usable"] & ~self.sites["has_location"]).sum())

    @property
    def n_stalled(self) -> int:
        return int(self.sites["is_stalled"].sum())

    @property
    def n_dead_lanes(self) -> int:
        """Lane detectors dropped by the ETL for never measuring movement."""
        return int(self.sites["n_dead_lanes"].fillna(0).sum())

    @property
    def n_sites_with_dead_lanes(self) -> int:
        """Still-usable sites that had at least one lane detector dropped."""
        return int((self.sites["is_usable"] & (self.sites["n_dead_lanes"] > 0)).sum())

    @property
    def n_partial_panel(self) -> int:
        """Usable sensors that did not report in every ingested month."""
        full = self.sites["n_months"].max()
        return int((self.sites["is_usable"] & (self.sites["n_months"] < full)).sum())

    @property
    def n_weather_gaps(self) -> int:
        """Hours in the panel's range with no rain reading at all."""
        if self.weather is None:
            return 0
        return int(self.weather["precip_mm"].isna().sum())

    @property
    def usable_site_ids(self) -> pd.Series:
        return self.sites.loc[self.sites["is_usable"], "site_id"]

    def _usable(self, frame: pd.DataFrame) -> pd.DataFrame:
        """Drop readings from sensors that are stuck or not watching traffic.

        Applies to the non-spatial views too: a detector pinned at 1.5 km/h all
        year would otherwise drag the city-wide curve down. Sensors that are
        merely *unlocatable* stay in -- their readings are sound, only their
        coordinates are missing.
        """
        return frame[frame["site_id"].isin(set(self.usable_site_ids))]


def load() -> Dataset:
    if not MEASUREMENTS_PARQUET.exists():
        raise FileNotFoundError(
            f"{MEASUREMENTS_PARQUET} not found -- fetch and build first:\n"
            "  uv run mvdspeed-fetch --from 2026-01\n"
            "  uv run mvdspeed-etl\n"
            "  uv run mvdspeed-weather"
        )
    measurements = pd.read_parquet(MEASUREMENTS_PARQUET)
    sites = pd.read_parquet(DETECTORS_PARQUET)

    # Normalised datetime, which is what the weather table joins on. Left as
    # date objects the merge silently matches nothing.
    measurements["date"] = pd.to_datetime(measurements["date"])

    weather = None
    if WEATHER_PARQUET.exists():
        weather = pd.read_parquet(WEATHER_PARQUET)
        weather["date"] = pd.to_datetime(weather["date"])
        measurements = _join_weather(measurements, weather)

    # Lifetime counters, renamed so they never collide with the per-slice
    # counters computed in by_site().
    sites = sites.rename(
        columns={
            "n_zero": "n_zero_total",
            "n_readings": "n_readings_total",
            "n_missing": "n_missing_total",
        }
    )

    # Two separate quality judgements, both about the sensor rather than about
    # any one time slice:
    #   is_live        -- did this detector ever see moving traffic?
    #   has_reference  -- is its free-flow speed big enough to divide by?
    sites["is_live"] = sites["mean_speed"].notna() & (
        sites["mean_speed"] >= FLATLINE_SPEED
    )
    sites["has_reference"] = sites["free_flow_speed"].notna() & (
        sites["free_flow_speed"] >= MIN_FREE_FLOW_FOR_RATIO
    )
    # Pinned at walking pace all day yet free-flowing at 3am: not watching
    # through traffic. See STALLED_* in config for the evidence.
    day, night = sites["day_speed"], sites["night_speed"]
    sites["is_stalled"] = (
        day.notna()
        & night.notna()
        & (day < STALLED_DAY_SPEED)
        & (night > day * STALLED_NIGHT_RATIO)
    )

    # Everything a speed average should be built from.
    sites["is_usable"] = sites["is_live"] & ~sites["is_stalled"]

    # The feed's placeholder coordinate: real measurements, but we do not know
    # where they were taken, so they cannot be mapped or ranked as a place.
    sites["has_location"] = sites["streets_at_coord"] <= MAX_STREETS_PER_COORD
    return Dataset(measurements=measurements, sites=sites, weather=weather)


def _join_weather(measurements: pd.DataFrame, weather: pd.DataFrame) -> pd.DataFrame:
    """Attach the hour's weather to every bucket.

    Weather is hourly and buckets are half-hourly, so the two buckets inside an
    hour share one reading. That is a real limitation of the join rather than an
    approximation to hide: rain starting at 10:40 is credited to the whole 10:00
    hour.
    """
    hour = (measurements["bucket"] * BUCKET_MINUTES // 60).astype("int16")
    out = measurements.assign(hour=hour).merge(
        weather[["date", "hour", *WEATHER_COLUMNS]],
        on=["date", "hour"],
        how="left",
        validate="many_to_one",
    )
    return out


def _rain_mask(frame: pd.DataFrame, scope: str | None) -> pd.Series:
    """Row mask for a weather scope. Unknown hours are never counted as dry."""
    if scope is None:
        return pd.Series(True, index=frame.index)
    if "is_wet" not in frame.columns:
        raise ValueError(
            "the panel carries no weather -- run `uv run mvdspeed-weather` first"
        )
    if scope == "dry":
        # Neither raining nor recently rained on, and known to be so.
        return (frame["recently_wet"] == False).fillna(False).astype(bool)  # noqa: E712
    if scope == "wet":
        return (frame["is_wet"] == True).fillna(False).astype(bool)  # noqa: E712
    if scope == "heavy":
        return (frame["is_heavy"] == True).fillna(False).astype(bool)  # noqa: E712
    raise ValueError(f"unknown weather scope {scope!r}")


def _weighted(frame: pd.DataFrame, include_zeros: bool) -> pd.DataFrame:
    """Attach the numerator/denominator used for every mean in the app.

    `speed_sum` only ever sums readings above zero, so including the zeros is
    just a matter of growing the denominator.
    """
    out = frame.copy()
    out["denominator"] = (
        out["n_moving"] + out["n_zero"] if include_zeros else out["n_moving"]
    )
    return out


def _slice(
    frame: pd.DataFrame,
    *,
    dows: list[int],
    months: list[str] | None = None,
    buckets: list[int] | None = None,
    rain: str | None = None,
) -> pd.DataFrame:
    """The four filters every view shares."""
    mask = frame["dow"].isin(dows)
    if months is not None:
        mask &= frame["month"].isin(months)
    if buckets is not None:
        mask &= frame["bucket"].isin(buckets)
    frame = frame[mask]
    if rain is not None:
        frame = frame[_rain_mask(frame, rain)]
    return frame


def by_site(
    data: Dataset,
    *,
    dows: list[int],
    buckets: list[int],
    months: list[str] | None = None,
    rain: str | None = None,
    include_zeros: bool = False,
    min_samples: int = MIN_SAMPLES,
    only_usable: bool = True,
    located_only: bool = True,
) -> pd.DataFrame:
    """One row per measuring site for the selected months, days and times of day.

    This is the spatial view, so on top of the untrustworthy sensors that every
    view drops, it also drops the ones carrying the feed's placeholder
    coordinate -- those keep their readings in `city_profile` and
    `street_profile`, which do not depend on position.
    """
    frame = _slice(
        data.measurements, dows=dows, months=months, buckets=buckets, rain=rain
    )
    frame = _weighted(frame, include_zeros)

    grouped = (
        frame.groupby("site_id", as_index=False)
        .agg(
            speed_sum=("speed_sum", "sum"),
            samples=("denominator", "sum"),
            n_zero=("n_zero", "sum"),
            n_moving=("n_moving", "sum"),
        )
        .query("samples >= @min_samples")
    )
    grouped["speed"] = grouped["speed_sum"] / grouped["samples"]

    merged = grouped.merge(data.sites, on="site_id", how="inner", validate="one_to_one")
    if only_usable:
        merged = merged[merged["is_usable"]]
    if located_only:
        merged = merged[merged["has_location"]]

    # Left undefined, not zero, where the free-flow reference is too small to
    # divide by -- a fake 0% would read as "wide open" on the map.
    merged["congestion"] = (1 - merged["speed"] / merged["free_flow_speed"]).clip(
        lower=0, upper=1
    )
    merged.loc[~merged["has_reference"], "congestion"] = pd.NA
    merged["congestion"] = merged["congestion"].astype("Float64")
    merged["vs_typical"] = merged["speed"] - merged["mean_speed"]
    merged["tramo"] = (
        merged["street"] + ": " + merged["from_street"] + " → " + merged["to_street"]
    )
    return merged


def city_profile(
    data: Dataset,
    *,
    dows: list[int],
    months: list[str] | None = None,
    rain: str | None = None,
    include_zeros: bool = False,
) -> pd.DataFrame:
    """City-wide mean speed for each time-of-day bucket."""
    frame = _slice(data._usable(data.measurements), dows=dows, months=months, rain=rain)
    frame = _weighted(frame, include_zeros)
    profile = frame.groupby("bucket", as_index=False).agg(
        speed_sum=("speed_sum", "sum"),
        samples=("denominator", "sum"),
    )
    profile["speed"] = profile["speed_sum"] / profile["samples"]
    profile["time"] = profile["bucket"].map(bucket_label)
    profile["hour"] = profile["bucket"] * BUCKET_MINUTES / 60
    return profile


def month_profile(
    data: Dataset,
    *,
    dows: list[int],
    months: list[str] | None = None,
    rain: str | None = None,
    include_zeros: bool = False,
) -> pd.DataFrame:
    """City-wide daily curve, one series per month.

    The comparison this panel exists for. Every month is measured against the
    same per-sensor references, so the curves are on one footing -- but they are
    not made of the same sensors: 85 of 480 sites do not report in every month,
    so a month-to-month gap is partly a change in the city and partly a change in
    who was watching. `n_sites` is carried so that can be read rather than
    guessed at.
    """
    frame = _slice(data._usable(data.measurements), dows=dows, months=months, rain=rain)
    frame = _weighted(frame, include_zeros)
    profile = frame.groupby(["month", "bucket"], as_index=False).agg(
        speed_sum=("speed_sum", "sum"),
        samples=("denominator", "sum"),
        n_sites=("site_id", "nunique"),
    )
    profile["speed"] = profile["speed_sum"] / profile["samples"]
    profile["time"] = profile["bucket"].map(bucket_label)
    profile["hour"] = profile["bucket"] * BUCKET_MINUTES / 60
    profile["label"] = profile["month"].map(month_label)
    return profile


def month_summary(
    data: Dataset,
    *,
    dows: list[int],
    include_zeros: bool = False,
) -> pd.DataFrame:
    """One row per month: mean speed, the peak-hour low, and who was reporting."""
    frame = _weighted(_slice(data._usable(data.measurements), dows=dows), include_zeros)
    per_bucket = frame.groupby(["month", "bucket"], as_index=False).agg(
        speed_sum=("speed_sum", "sum"), samples=("denominator", "sum")
    )
    per_bucket["speed"] = per_bucket["speed_sum"] / per_bucket["samples"]

    rows = []
    for month, group in per_bucket.groupby("month"):
        worst = group.loc[group["speed"].idxmin()]
        rows.append(
            {
                "month": month,
                "label": month_label(month),
                "speed": group["speed_sum"].sum() / group["samples"].sum(),
                "worst_speed": worst["speed"],
                "worst_time": bucket_label(int(worst["bucket"])),
            }
        )
    summary = pd.DataFrame(rows)

    counts = frame.groupby("month").agg(
        n_sites=("site_id", "nunique"),
        n_days=("date", "nunique"),
        samples=("denominator", "sum"),
    )
    return summary.merge(counts.reset_index(), on="month").sort_values("month")


def street_profile(
    data: Dataset,
    *,
    dows: list[int],
    streets: list[str],
    months: list[str] | None = None,
    rain: str | None = None,
    include_zeros: bool = False,
) -> pd.DataFrame:
    """Mean speed per time bucket for a handful of named streets."""
    site_ids = data.sites.loc[data.sites["street"].isin(streets), ["site_id", "street"]]
    frame = data._usable(data.measurements).merge(site_ids, on="site_id", how="inner")
    frame = _weighted(_slice(frame, dows=dows, months=months, rain=rain), include_zeros)
    profile = frame.groupby(["street", "bucket"], as_index=False).agg(
        speed_sum=("speed_sum", "sum"),
        samples=("denominator", "sum"),
    )
    profile["speed"] = profile["speed_sum"] / profile["samples"]
    profile["time"] = profile["bucket"].map(bucket_label)
    profile["hour"] = profile["bucket"] * BUCKET_MINUTES / 60
    return profile


# --- the weather crossing -----------------------------------------------------
# Rain does not fall evenly across the day, so pooling every wet hour against
# every dry hour compares a different mix of times of day as much as it compares
# weather. Both functions below therefore take the difference *within* each
# half-hour bucket and only then combine, which holds time of day fixed.
#
# It is worth the extra step: on weekdays across this panel the stratified
# city-wide penalty is -0.92 km/h and the unstratified one is -1.15, so pooling
# overstates it by about a quarter. `rain_headline` returns both, so the gap is
# visible in the app rather than being a claim about method taken on trust.


def rain_profile(
    data: Dataset,
    *,
    dows: list[int],
    months: list[str] | None = None,
    include_zeros: bool = False,
    min_samples: int = MIN_SAMPLES,
) -> pd.DataFrame:
    """Dry vs wet mean speed for each time-of-day bucket, side by side."""
    frame = _weighted(
        _slice(data._usable(data.measurements), dows=dows, months=months), include_zeros
    )
    bands = {
        "Dry": _rain_mask(frame, "dry"),
        "Wet": _rain_mask(frame, "wet"),
    }
    out = []
    for band, mask in bands.items():
        part = frame[mask].groupby("bucket", as_index=False).agg(
            speed_sum=("speed_sum", "sum"),
            samples=("denominator", "sum"),
            n_hours=("date", "nunique"),
        )
        part["band"] = band
        out.append(part)

    profile = pd.concat(out, ignore_index=True)
    profile = profile[profile["samples"] >= min_samples]
    profile["speed"] = profile["speed_sum"] / profile["samples"]
    profile["time"] = profile["bucket"].map(bucket_label)
    profile["hour"] = profile["bucket"] * BUCKET_MINUTES / 60
    return profile


def rain_penalty(
    data: Dataset,
    *,
    dows: list[int],
    months: list[str] | None = None,
    include_zeros: bool = False,
    min_samples: int = MIN_SAMPLES,
    by: str = "street",
) -> pd.DataFrame:
    """The speed cost of rain, stratified by time of day.

    For every (group, bucket) pair that has enough readings in both weather
    bands, the wet-minus-dry difference is taken inside that bucket. The group's
    figure is then the average of those differences weighted by how much wet data
    each bucket has -- so a group is never credited with a penalty measured at a
    time of day it had no rain in.

    `by` is a column of the sensor table: "street" for the avenue, or "tramo" for
    the individual stretch.
    """
    keys = data.sites[["site_id", "street", "from_street", "to_street"]].copy()
    keys["tramo"] = (
        keys["street"] + ": " + keys["from_street"] + " → " + keys["to_street"]
    )
    frame = data._usable(data.measurements).merge(
        keys[["site_id", by]], on="site_id", how="inner"
    )
    frame = _weighted(_slice(frame, dows=dows, months=months), include_zeros)

    parts = []
    for band, scope in (("dry", "dry"), ("wet", "wet")):
        part = (
            frame[_rain_mask(frame, scope)]
            .groupby([by, "bucket"], as_index=False)
            .agg(speed_sum=("speed_sum", "sum"), samples=("denominator", "sum"))
        )
        part["speed"] = part["speed_sum"] / part["samples"]
        parts.append(
            part.rename(
                columns={"speed": f"{band}_speed", "samples": f"{band}_samples"}
            )[[by, "bucket", f"{band}_speed", f"{band}_samples"]]
        )

    paired = parts[0].merge(parts[1], on=[by, "bucket"], how="inner")
    paired = paired[
        (paired["dry_samples"] >= min_samples) & (paired["wet_samples"] >= min_samples)
    ]
    if paired.empty:
        return pd.DataFrame(
            columns=[by, "delta", "pct", "dry_speed", "wet_speed", "wet_samples",
                     "dry_samples", "n_buckets"]
        )
    paired["delta"] = paired["wet_speed"] - paired["dry_speed"]

    def _combine(group: pd.DataFrame) -> pd.Series:
        weight = group["wet_samples"]
        return pd.Series(
            {
                "delta": (group["delta"] * weight).sum() / weight.sum(),
                "dry_speed": (group["dry_speed"] * weight).sum() / weight.sum(),
                "wet_speed": (group["wet_speed"] * weight).sum() / weight.sum(),
                "wet_samples": weight.sum(),
                "dry_samples": group["dry_samples"].sum(),
                "n_buckets": len(group),
            }
        )

    out = paired.groupby(by).apply(_combine, include_groups=False).reset_index()
    out["pct"] = out["delta"] / out["dry_speed"]
    return out.sort_values("delta")


def rain_headline(
    data: Dataset,
    *,
    dows: list[int],
    months: list[str] | None = None,
    include_zeros: bool = False,
) -> dict[str, float]:
    """One city-wide, time-of-day-stratified rain penalty, plus its support."""
    frame = _weighted(
        _slice(data._usable(data.measurements), dows=dows, months=months), include_zeros
    )
    rows = []
    for band, scope in (("dry", "dry"), ("wet", "wet")):
        part = frame[_rain_mask(frame, scope)].groupby("bucket", as_index=False).agg(
            speed_sum=("speed_sum", "sum"), samples=("denominator", "sum")
        )
        part[f"{band}_speed"] = part["speed_sum"] / part["samples"]
        rows.append(part[["bucket", f"{band}_speed", "samples"]].rename(
            columns={"samples": f"{band}_samples"}
        ))
    paired = rows[0].merge(rows[1], on="bucket", how="inner")
    if paired.empty:
        return {}
    weight = paired["wet_samples"]
    delta = ((paired["wet_speed"] - paired["dry_speed"]) * weight).sum() / weight.sum()
    dry = (paired["dry_speed"] * weight).sum() / weight.sum()

    # The same comparison without stratifying, reported so the difference between
    # the two is visible rather than a claim about method taken on trust.
    naive_dry = frame[_rain_mask(frame, "dry")]
    naive_wet = frame[_rain_mask(frame, "wet")]
    naive = (
        naive_wet["speed_sum"].sum() / naive_wet["denominator"].sum()
        - naive_dry["speed_sum"].sum() / naive_dry["denominator"].sum()
    )
    return {
        "delta": delta,
        "pct": delta / dry,
        "dry_speed": dry,
        "wet_speed": dry + delta,
        "naive_delta": naive,
        "wet_samples": float(weight.sum()),
        "dry_samples": float(paired["dry_samples"].sum()),
        "n_buckets": float(len(paired)),
    }


def weather_caveats() -> list[str]:
    """The things a reader has to know before trusting the rain numbers."""
    return [
        f"Rain is measured at one station, Aeropuerto Melilla, about 10 km "
        f"northwest of the middle of the sensor field. City-wide frontal rain is "
        f"caught well; a single summer cell over one avenue may be missed "
        f"entirely, or recorded when that avenue stayed dry.",
        f"Weather is hourly and the map's buckets are {BUCKET_MINUTES} minutes, so "
        f"both halves of an hour inherit its rainfall: rain starting at 10:40 is "
        f"credited to all of 10:00–11:00.",
        f"“Dry” excludes the {RAIN_LAG_HOURS} hours after rain stops, because the "
        f"road is still wet then and including them would shrink every penalty "
        f"below.",
        f"Wet means at least {RAIN_WET_MM:g} mm in the hour, heavy at least "
        f"{RAIN_HEAVY_MM:g} mm. Hours the station did not report are left out of "
        f"both sides rather than assumed dry.",
        "Differences are taken within each half hour and only then averaged, so "
        "they are not an artefact of rain falling at different times of day than "
        "the dry hours it is compared against.",
        "This is an association, not a causal estimate. Rain arrives with wind, "
        "cloud and different traffic volumes, and none of those are held fixed.",
    ]
