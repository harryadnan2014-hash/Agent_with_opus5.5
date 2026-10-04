"""The app's pages, registered once by `app.py` so any page can link to another."""

from __future__ import annotations

from typing import Any

import streamlit as st

PAGES: dict[str, Any] = {}


def link(name: str, label: str, icon: str | None = None) -> None:
    page = PAGES.get(name)
    if page is not None:
        st.page_link(page, label=label, icon=icon)
