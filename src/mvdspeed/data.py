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
)

BUCKETS_PER_DAY = 24 * 60 // BUCKET_MINUTES

DAY_SCOPES = {
    "Weekdays (Mon-Fri)": [1, 2, 3, 4, 5],
    "Weekend (Sat-Sun)": [6, 0],
    "Every day": list(range(7)),
}


def bucket_label(bucket: int) -> str:
    minutes = bucket * BUCKET_MINUTES
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


@dataclass(frozen=True)
class Dataset:
    measurements: pd.DataFrame
    sites: pd.DataFrame

    @property
    def dates(self) -> list[pd.Timestamp]:
        return sorted(self.measurements["date"].unique())

    @property
    def n_flatlined(self) -> int:
        return int((~self.sites["is_live"]).sum())

    @property
    def n_without_reference(self) -> int:
        return int((self.sites["is_live"] & ~self.sites["has_reference"]).sum())

    @property
    def n_without_location(self) -> int:
        return int((self.sites["is_live"] & ~self.sites["has_location"]).sum())


def load() -> Dataset:
    if not MEASUREMENTS_PARQUET.exists():
        raise FileNotFoundError(
            f"{MEASUREMENTS_PARQUET} not found -- run the ETL first:\n"
            "  uv run mvdspeed-etl /path/to/Velocidad_promedio_Agosto_2026.csv"
        )
    measurements = pd.read_parquet(MEASUREMENTS_PARQUET)
    sites = pd.read_parquet(DETECTORS_PARQUET)

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
    # The feed's placeholder coordinate: real measurements, but we do not know
    # where they were taken, so they cannot be mapped or ranked as a place.
    sites["has_location"] = sites["streets_at_coord"] <= MAX_STREETS_PER_COORD
    return Dataset(measurements=measurements, sites=sites)


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


def by_site(
    data: Dataset,
    *,
    dows: list[int],
    buckets: list[int],
    include_zeros: bool = False,
    min_samples: int = MIN_SAMPLES,
    only_live: bool = True,
    located_only: bool = True,
) -> pd.DataFrame:
    """One row per measuring site for the selected days and times of day.

    This is the spatial view, so by default it drops sensors that are stuck or
    that carry the feed's placeholder coordinate. Their readings still count in
    `city_profile` and `street_profile`, which do not depend on position.
    """
    frame = data.measurements
    frame = frame[frame["dow"].isin(dows) & frame["bucket"].isin(buckets)]
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
    if only_live:
        merged = merged[merged["is_live"]]
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
    data: Dataset, *, dows: list[int], include_zeros: bool = False
) -> pd.DataFrame:
    """City-wide mean speed for each time-of-day bucket."""
    frame = data.measurements
    frame = _weighted(frame[frame["dow"].isin(dows)], include_zeros)
    profile = frame.groupby("bucket", as_index=False).agg(
        speed_sum=("speed_sum", "sum"),
        samples=("denominator", "sum"),
    )
    profile["speed"] = profile["speed_sum"] / profile["samples"]
    profile["time"] = profile["bucket"].map(bucket_label)
    profile["hour"] = profile["bucket"] * BUCKET_MINUTES / 60
    return profile


def street_profile(
    data: Dataset, *, dows: list[int], streets: list[str], include_zeros: bool = False
) -> pd.DataFrame:
    """Mean speed per time bucket for a handful of named streets."""
    site_ids = data.sites.loc[data.sites["street"].isin(streets), ["site_id", "street"]]
    frame = data.measurements.merge(site_ids, on="site_id", how="inner")
    frame = _weighted(frame[frame["dow"].isin(dows)], include_zeros)
    profile = frame.groupby(["street", "bucket"], as_index=False).agg(
        speed_sum=("speed_sum", "sum"),
        samples=("denominator", "sum"),
    )
    profile["speed"] = profile["speed_sum"] / profile["samples"]
    profile["time"] = profile["bucket"].map(bucket_label)
    profile["hour"] = profile["bucket"] * BUCKET_MINUTES / 60
    return profile
