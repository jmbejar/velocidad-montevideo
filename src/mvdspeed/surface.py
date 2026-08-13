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

import base64
import io
from dataclasses import dataclass

import numpy as np
import pandas as pd

from mvdspeed.config import (
    KM_PER_DEG_LAT,
    SURFACE_BANDWIDTH_KM,
    SURFACE_CUTOFF_KM,
    SURFACE_STEP_KM,
    km_per_deg_lon,
)


@dataclass(frozen=True)
class Surface:
    """The estimated field as a raster, ready to be tinted and drawn.

    `values` and `support` are (ny, nx) with row 0 at the *south* edge. `values`
    is NaN wherever no sensor lies inside the cutoff. `bounds` is the outer edge
    of the grid as (west, south, east, north) degrees -- the outer edge, not the
    centres of the corner cells, so it can be handed straight to a BitmapLayer.
    """

    values: np.ndarray
    support: np.ndarray
    bounds: tuple[float, float, float, float]

    @property
    def n_supported(self) -> int:
        return int(np.isfinite(self.values).sum())

    @property
    def shape(self) -> tuple[int, int]:
        return self.values.shape


def kernel_surface(
    frame: pd.DataFrame,
    column: str,
    *,
    step_km: float = SURFACE_STEP_KM,
    bandwidth_km: float = SURFACE_BANDWIDTH_KM,
    cutoff_km: float = SURFACE_CUTOFF_KM,
) -> Surface:
    """Kernel-weighted surface over the sensors in `frame`, as a raster."""
    numeric = pd.to_numeric(frame[column], errors="coerce")
    usable = frame.loc[numeric.notna()]
    values = numeric[numeric.notna()].to_numpy(dtype=float)
    empty = Surface(np.zeros((0, 0)), np.zeros((0, 0)), (0.0, 0.0, 0.0, 0.0))
    if usable.empty:
        return empty

    km_lon = km_per_deg_lon()
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
        return empty

    estimate = np.full(support.shape, np.nan)
    np.divide(
        (weights * values[None, :]).sum(axis=1), support,
        out=estimate, where=supported,
    )

    ny, nx = grid_x.shape
    half = step_km / 2.0
    return Surface(
        values=estimate.reshape(ny, nx),
        support=support.reshape(ny, nx),
        # Plain floats, not numpy scalars, so pydeck serialises them cleanly.
        bounds=(
            float((xs[0] - half) / km_lon),
            float((ys[0] - half) / KM_PER_DEG_LAT),
            float((xs[-1] + half) / km_lon),
            float((ys[-1] + half) / KM_PER_DEG_LAT),
        ),
    )


def to_png_data_uri(rgba: np.ndarray) -> str:
    """Encode an (ny, nx, 4) uint8 raster as a data URI for a BitmapLayer.

    Row 0 of `rgba` is treated as the *south* edge and flipped, since image rows
    run north-to-south. Drawing the field as one texture rather than thousands of
    cells is what makes it read as a smooth heatmap: the GPU interpolates between
    cell centres, and the payload is a few KB instead of a row per cell.
    """
    from PIL import Image

    flipped = np.ascontiguousarray(rgba[::-1])
    buffer = io.BytesIO()
    # No optimize=True: it costs ~2.7 s on this raster and saves a few KB on an
    # image that is already ~11 KB.
    Image.fromarray(flipped, mode="RGBA").save(buffer, format="PNG", compress_level=1)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


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
