"""Value -> color mapping and the matching scale legends.

Two color jobs appear on this map and they use different scales, per the
data-viz rules: magnitude (speed, congestion, volume) gets a single-hue
sequential ramp; polarity (slower / faster than a sensor's own typical speed)
gets a two-hue diverging ramp with a neutral gray midpoint. No rainbows.

Every scale is selected per surface. The `dark` flag picks the steps for a dark
basemap rather than flipping the light ones: on dark the sequential ramp runs
dark -> light so the extreme value is the one that survives against near-black,
and the diverging midpoint becomes a dark gray so "no difference" still recedes
instead of glowing.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from mvdspeed.config import (
    DIVERGING_COOL,
    DIVERGING_COOL_DARK,
    DIVERGING_MID,
    DIVERGING_MID_DARK,
    DIVERGING_WARM,
    DIVERGING_WARM_DARK,
    SEQUENTIAL_BLUE,
    SEQUENTIAL_BLUE_DARK,
    SEQUENTIAL_HEAT,
    SEQUENTIAL_HEAT_DARK,
    hex_to_rgb,
)

hex_to_rgb_list = hex_to_rgb

# Two sequential ramps, picked by what the number means rather than by taste:
# "heat" for traffic severity (the semantic-heat exception to one-hue
# sequential), "blue" for a neutral count that carries no good/bad reading.
_RAMP_HEX = {
    ("blue", False): SEQUENTIAL_BLUE,
    ("blue", True): SEQUENTIAL_BLUE_DARK,
    ("heat", False): SEQUENTIAL_HEAT,
    ("heat", True): SEQUENTIAL_HEAT_DARK,
}
_SEQ = {
    key: np.array([hex_to_rgb(h) for h in steps], dtype=float)
    for key, steps in _RAMP_HEX.items()
}
_DIV = {
    False: np.array(
        [hex_to_rgb(DIVERGING_WARM), hex_to_rgb(DIVERGING_MID), hex_to_rgb(DIVERGING_COOL)],
        dtype=float,
    ),
    True: np.array(
        [
            hex_to_rgb(DIVERGING_WARM_DARK),
            hex_to_rgb(DIVERGING_MID_DARK),
            hex_to_rgb(DIVERGING_COOL_DARK),
        ],
        dtype=float,
    ),
}


def _interpolate(ramp: np.ndarray, t: np.ndarray) -> np.ndarray:
    """Sample a ramp of RGB stops at positions t in [0, 1]."""
    t = np.clip(np.nan_to_num(t, nan=0.0), 0.0, 1.0)
    position = t * (len(ramp) - 1)
    low = np.floor(position).astype(int)
    high = np.minimum(low + 1, len(ramp) - 1)
    frac = (position - low)[:, None]
    return ramp[low] * (1 - frac) + ramp[high] * frac


def sequential(
    values: pd.Series, vmin: float, vmax: float, *, invert: bool = False,
    dark: bool = False, ramp: str = "heat",
):
    span = vmax - vmin
    t = (
        (values.to_numpy(dtype=float) - vmin) / span
        if span > 0
        else np.zeros(len(values))
    )
    if invert:
        t = 1 - t
    return [
        [int(r), int(g), int(b)] for r, g, b in _interpolate(_SEQ[(ramp, dark)], t)
    ]


def diverging(values: pd.Series, vmax_abs: float, *, dark: bool = False):
    """Warm (red) below zero, neutral at zero, cool (blue) above."""
    vmax_abs = max(vmax_abs, 1e-6)
    t = (values.to_numpy(dtype=float) / vmax_abs + 1) / 2
    return [[int(r), int(g), int(b)] for r, g, b in _interpolate(_DIV[dark], t)]


def _swatches(ramp: np.ndarray, n: int = 24) -> str:
    stops = _interpolate(ramp, np.linspace(0, 1, n))
    return ",".join(f"rgb({int(r)},{int(g)},{int(b)})" for r, g, b in stops)


def legend_html(
    *,
    label: str,
    ticks: list[str],
    kind: str,
    text_color: str,
    muted_color: str,
    dark: bool = False,
    ramp: str = "heat",
) -> str:
    """A horizontal gradient legend with values labelled along it.

    `ticks` are laid out at even positions from the low end to the high end, so
    a ramp with distinguishable middle steps can actually be decoded in the
    middle rather than only at its two ends.

    The gradient is drawn from the same ramp the marks use, so when the dark
    surface reverses the sequential direction the legend reverses with it and
    the labels stay attached to the right ends.
    """
    steps = _DIV[dark] if kind == "diverging" else _SEQ[(ramp, dark)]
    gradient = f"linear-gradient(to right, {_swatches(steps)})"

    # First and last hug the ends; the rest are centred on their position.
    last = len(ticks) - 1
    cells = []
    for i, tick in enumerate(ticks):
        align = "left" if i == 0 else "right" if i == last else "center"
        cells.append(
            f'<span style="flex:1;text-align:{align}">{tick}</span>'
        )
    return f"""
    <div style="font-family:system-ui,-apple-system,'Segoe UI',sans-serif;
                margin:2px 0 6px;max-width:440px">
      <div style="font-size:0.78rem;color:{muted_color};margin-bottom:4px">{label}</div>
      <div style="height:10px;border-radius:5px;background:{gradient};
                  box-shadow:inset 0 0 0 1px rgba(0,0,0,0.08)"></div>
      <div style="display:flex;font-size:0.75rem;color:{text_color};
                  margin-top:3px;font-variant-numeric:tabular-nums">
        {"".join(cells)}
      </div>
    </div>
    """
