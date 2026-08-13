"""A fair heat surface over point sensors.

deck.gl's HeatmapLayer estimates point *density*: it bins points and averages
within each bin. That makes intensity depend on how many neighbours a sensor
happens to have, which is not a property of the traffic. This module estimates
the value instead, with one expression applied identically at every point in
space:

    d_i     = distance from the cell to sensor i
    w_i     = exp(-(d_i / bandwidth)^2), and 0 beyond the cutoff
    value   = sum(w_i * v_i) / sum(w_i)
    support = sum(w_i)

Local sensor density no longer changes `value`; it changes `support`, which the
caller renders as opacity. Cells with no sensor inside the cutoff are never
emitted, so ground we did not measure stays bare instead of being interpolated
across.

Deliberately free of colour and Streamlit so it can be checked on its own.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from mvdspeed.config import (
    CITY_LAT,
    KM_PER_DEG_LAT,
    SURFACE_BANDWIDTH_KM,
    SURFACE_CUTOFF_KM,
    SURFACE_STEP_KM,
)


def _km_per_deg_lon(lat: float = CITY_LAT) -> float:
    return KM_PER_DEG_LAT * float(np.cos(np.radians(lat)))


def kernel_surface(
    frame: pd.DataFrame,
    column: str,
    *,
    step_km: float = SURFACE_STEP_KM,
    bandwidth_km: float = SURFACE_BANDWIDTH_KM,
    cutoff_km: float = SURFACE_CUTOFF_KM,
) -> pd.DataFrame:
    """Kernel-weighted surface over the sensors in `frame`.

    Returns one row per supported cell with `lon`/`lat` at the cell's
    **south-west corner**, which is the anchor deck.gl's GridCellLayer expects --
    getting this wrong offsets the whole surface by half a cell against the dots.
    """
    values = pd.to_numeric(frame[column], errors="coerce")
    usable = frame.loc[values.notna()]
    values = values[values.notna()].to_numpy(dtype=float)
    if usable.empty:
        return pd.DataFrame(columns=["lon", "lat", "value", "support"])

    km_lon = _km_per_deg_lon()
    sx = usable["lon"].to_numpy(dtype=float) * km_lon
    sy = usable["lat"].to_numpy(dtype=float) * KM_PER_DEG_LAT

    # Cell centres, padded by the cutoff so edge sensors get their full footprint.
    xs = np.arange(sx.min() - cutoff_km, sx.max() + cutoff_km + step_km, step_km)
    ys = np.arange(sy.min() - cutoff_km, sy.max() + cutoff_km + step_km, step_km)
    grid_x, grid_y = np.meshgrid(xs, ys)
    cx, cy = grid_x.ravel(), grid_y.ravel()

    # ~10k cells x ~400 sensors: one dense distance matrix is simpler than a
    # spatial index and takes well under a tenth of a second.
    dist = np.hypot(cx[:, None] - sx[None, :], cy[:, None] - sy[None, :])
    weights = np.exp(-((dist / bandwidth_km) ** 2))
    weights[dist > cutoff_km] = 0.0

    support = weights.sum(axis=1)
    supported = support > 0
    if not supported.any():
        return pd.DataFrame(columns=["lon", "lat", "value", "support"])

    weights = weights[supported]
    support = support[supported]
    estimate = (weights * values[None, :]).sum(axis=1) / support

    half = step_km / 2.0
    return pd.DataFrame(
        {
            "lon": (cx[supported] - half) / km_lon,
            "lat": (cy[supported] - half) / KM_PER_DEG_LAT,
            "value": estimate,
            "support": support,
        }
    )


def support_alpha(
    support: np.ndarray | pd.Series,
    *,
    alpha_min: float,
    alpha_max: float,
    reference: float,
    gamma: float,
) -> np.ndarray:
    """Map kernel support to opacity, sublinearly.

    Support spans three orders of magnitude, so a linear map would leave all but
    the densest cells invisible.
    """
    s = np.asarray(support, dtype=float)
    scaled = alpha_max * np.power(np.maximum(s, 0.0) / reference, gamma)
    return np.clip(scaled, alpha_min, alpha_max)
