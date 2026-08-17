"""Chart chrome shared by every page.

These four helpers used to live inside app.py, which was fine while there was
one page. With two, a copy in each is a guarantee that the axes drift apart --
one page's gridlines a shade darker, one page's series orange where the other
is green -- and the drift is the kind nobody notices until the two charts are
side by side in a screenshot.

Deliberately free of Streamlit, like surface.py and streets.py, so it can be
checked on its own.
"""

from __future__ import annotations

import altair as alt
import pandas as pd

from mvdspeed.config import GRIDLINE, TEXT_MUTED, TEXT_SECONDARY

# Up to three categorical series. Blue leads because it is the app's accent;
# orange and green are the two hues that stay distinguishable from it and from
# each other for the commonest colour-vision deficiencies. Past three series a
# chart needs a different design, not a fourth hue.
SERIES_HUES = ["#2a78d6", "#eb6834", "#1baf7a"]


def make_axis(grid: bool = True, **overrides) -> alt.Axis:
    """Recessive hairline grid and muted labels, per the chart-chrome rules.

    `grid` is a named parameter rather than one more override because the bar
    chart's category axis wants it off, and passing it through `**overrides`
    collides with the default set here.
    """
    return alt.Axis(
        grid=grid, gridColor=GRIDLINE, gridWidth=1, domainColor=GRIDLINE,
        tickColor=GRIDLINE, labelColor=TEXT_MUTED, titleColor=TEXT_SECONDARY,
        **overrides,
    )


def hour_axis() -> alt.Axis:
    """Three-hourly ticks for a whole-day x axis."""
    return make_axis(values=list(range(0, 25, 3)), format="d")


def ticks_for(vmin: float, vmax: float, invert: bool, formatter, n: int = 5) -> list[str]:
    """Evenly spaced value labels running low-end -> high-end of the gradient."""
    values = [vmin + (vmax - vmin) * i / (n - 1) for i in range(n)]
    if invert:
        values.reverse()
    return [formatter(v) for v in values]


def label(value, formatter) -> str:
    """Format a value for a tooltip, saying so when there is nothing to format."""
    return "no data" if pd.isna(value) else formatter(value)
