import os
import duckdb
import streamlit as st
import yaml

DB_PATH      = os.environ.get("DB_PATH",      "/data/content_catalogue.duckdb")
QUERIES_PATH = os.environ.get("QUERIES_PATH", "/app/queries.yaml")
PAGE_SIZE    = 200

st.set_page_config(page_title="Content Catalogue", layout="wide")
st.title("Content Catalogue")

@st.cache_resource
def get_conn():
    return duckdb.connect(DB_PATH, read_only=True)

with open(QUERIES_PATH) as f:
    saved = yaml.safe_load(f)

choice = st.selectbox(
    "Saved query",
    ["— ad hoc —"] + list(saved.keys()),
    format_func=lambda k: k if k == "— ad hoc —" else saved[k]["label"],
)

default_sql = "SELECT * FROM media_files LIMIT 20" if choice == "— ad hoc —" else saved[choice]["sql"]

# Reset page when query changes
if st.session_state.get("last_choice") != choice:
    st.session_state["page"] = 1
    st.session_state["last_choice"] = choice

sql = st.text_area("SQL", default_sql, height=140)

if st.button("Run", type="primary"):
    st.session_state["page"] = 1
    try:
        st.session_state["df"] = get_conn().execute(sql).df()
    except Exception as e:
        st.session_state.pop("df", None)
        st.error(str(e))

if "df" in st.session_state:
    df = st.session_state["df"]
    total = len(df)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = st.session_state.get("page", 1)

    if total_pages > 1:
        col_info, col_prev, col_num, col_next = st.columns([4, 1, 1, 1])
        col_info.caption(f"{total:,} rows — page {page} of {total_pages}")
        if col_prev.button("← Prev", disabled=page <= 1):
            st.session_state["page"] = page - 1
            st.rerun()
        col_num.number_input(
            "Page", min_value=1, max_value=total_pages, value=page,
            step=1, label_visibility="collapsed",
            key="page_input",
            on_change=lambda: st.session_state.update({"page": st.session_state["page_input"]}),
        )
        if col_next.button("Next →", disabled=page >= total_pages):
            st.session_state["page"] = page + 1
            st.rerun()
    else:
        st.caption(f"{total:,} rows")

    start = (page - 1) * PAGE_SIZE
    st.dataframe(df.iloc[start : start + PAGE_SIZE], use_container_width=True)
