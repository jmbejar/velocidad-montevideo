"""Streamlit entry point: the navigation between the dashboard's pages.

Run with:  uv run streamlit run src/mvdspeed/app.py

This file exists only to name the pages. It used to *be* the speed dashboard,
with the football page picked up from a `pages/` directory -- Streamlit's own
convention, which costs nothing to set up and takes the sidebar label for each
page from its filename. That is fine until the entry point is one of the pages,
because then the label is "app": accurate about the file and useless about the
contents. `st.navigation` is the way to say what a page is called without
naming the file after it.

The pages themselves are unchanged and live in views/. They are listed here as
script paths rather than imported, so each still runs top to bottom as its own
script and neither has to know the other exists.
"""

from __future__ import annotations

import streamlit as st

# Called once here rather than in each page: with st.navigation the entry point
# and the selected page run in the same pass, and a second call raises.
st.set_page_config(
    page_title="Velocidad promedio · Montevideo · 2026",
    page_icon="🚗",
    layout="wide",
)

navigation = st.navigation(
    [
        st.Page(
            "views/home.py",
            title="Tránsito vs Clima",
            icon="🚗",
            url_path="transito-vs-clima",
            default=True,
        ),
        st.Page(
            "views/football.py",
            title="Tránsito vs Fútbol",
            icon="⚽",
            url_path="transito-vs-futbol",
        ),
    ]
)
navigation.run()
