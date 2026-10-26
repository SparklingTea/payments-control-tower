import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import duckdb  # noqa: E402
import streamlit as st  # noqa: E402

from core import config, db  # noqa: E402

ASSUMPTION_NOTE = ("Synthetic data. Every cost, rate and fee is a labelled assumption from "
                   "`config/assumptions.yaml`, not a real industry figure or any company's performance.")


def page(title, icon):
    st.set_page_config(page_title=f"{title} · Control Tower", page_icon=icon, layout="wide")


@st.cache_resource
def _connection():
    return duckdb.connect(str(config.DB_PATH), read_only=True)


def con():
    """Per-call cursor on a shared read-only connection; stops the page if no data exists yet."""
    if not config.DB_PATH.exists():
        st.warning("No data yet. Generate the synthetic payments book first (about 15 seconds).")
        if st.button("Generate data", type="primary"):
            with st.spinner("Generating 20,000 customers and ~300,000 payments..."):
                from generator.generate import build
                build()
            st.cache_resource.clear()
            st.rerun()
        st.code("python -m generator.generate", language="bash")
        st.stop()
    c = _connection().cursor()
    if not db.is_prepared(c):
        st.error("Database exists but is not prepared. Re-run `python -m generator.generate`.")
        st.stop()
    return c


def show_sql(sql, label="Show SQL"):
    with st.expander(label):
        st.code(sql.strip(), language="sql")


def assumption_badge():
    st.caption(f"⚠️ {ASSUMPTION_NOTE}")


def sidebar_filters(c):
    a = config.assumptions()
    st.sidebar.header("Filters")
    rails = st.sidebar.multiselect("Rail", list(a["rails"]), default=list(a["rails"]))
    segs = st.sidebar.multiselect("Segment", list(a["segments"]), default=list(a["segments"]))
    lo, hi = c.execute("SELECT min(created_at)::DATE, max(created_at)::DATE FROM payments").fetchone()
    dates = st.sidebar.date_input("Date range", (lo, hi), min_value=lo, max_value=hi)
    start, end = (dates if isinstance(dates, (list, tuple)) and len(dates) == 2 else (lo, hi))
    st.sidebar.caption("Filters apply to every tile on this page.")
    return {"rails": rails or list(a["rails"]), "segments": segs or list(a["segments"]),
            "start": str(start), "end": str(end)}
