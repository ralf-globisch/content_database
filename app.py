import os
import duckdb
import streamlit as st
import yaml

DB_PATH      = os.environ.get("DB_PATH",      "/data/content_catalogue.duckdb")
QUERIES_PATH = os.environ.get("QUERIES_PATH", "/app/queries.yaml")
OLLAMA_HOST  = os.environ.get("OLLAMA_HOST",  "http://localhost:11434")
SQL_MODEL    = os.environ.get("OLLAMA_SQL_MODEL", "llama3.2")
PAGE_SIZE    = 200

try:
    import ollama as _ollama
    HAS_OLLAMA = True
except ImportError:
    HAS_OLLAMA = False

_NL_SCHEMA = """Convert the user's natural language request into a DuckDB SQL SELECT statement.

Database tables:
  media_files(s3_key PK, size_bytes BIGINT, last_modified TEXT, extension TEXT, top_prefix TEXT, media_type TEXT)
    media_type is 'video' or 'audio'

  media_metadata(s3_key PK, duration_s DOUBLE, format_name TEXT, width INT, height INT, fps DOUBLE,
    video_codec TEXT, video_bitrate INT [kbps], scan_type TEXT,
    color_primaries TEXT, color_transfer TEXT, color_space TEXT,
    hdr_format TEXT  -- 'SDR', 'HDR10', 'HDR10+', 'HLG', 'Dolby Vision'
    dolby_vision BOOL, dv_profile INT, dv_level INT,
    audio_codec TEXT, audio_channels INT, audio_sample_rate INT,
    dolby_atmos BOOL, dolby_codec_family TEXT, error TEXT)

  audio_tracks(s3_key, track_index INT, codec TEXT, channels INT, sample_rate INT,
    language TEXT  -- ISO code e.g. 'eng', dolby_atmos BOOL, dolby_codec_family TEXT)

  content_vision(s3_key PK, description TEXT,
    style TEXT,      -- 'live_action', 'animated', 'cgi', 'mixed'
    has_credits BOOL, -- true if ending/opening credits were detected
    brightness TEXT,  -- 'bright', 'normal', 'dark', 'mixed'
    genre_tags VARCHAR[],  -- e.g. ['drama', 'action']  use list_contains(genre_tags, 'sports') to search
    analyzed_at TEXT,
    source_key TEXT)  -- non-NULL means this row is a copy; source_key points to the analysed representative

Rules:
- Return ONLY the SQL statement, no explanation, no markdown fences.
- Use DuckDB syntax (list_contains for array search, :: for casting).
- JOIN tables via s3_key when needed.
- NEVER use 'at' as a table alias — it is a reserved keyword in DuckDB. Use 'atr' for audio_tracks."""


def _generate_sql(nl_query: str) -> str:
    client = _ollama.Client(host=OLLAMA_HOST)
    resp = client.chat(
        model=SQL_MODEL,
        messages=[
            {"role": "system", "content": _NL_SCHEMA},
            {"role": "user", "content": nl_query},
        ],
    )
    sql = resp.message.content.strip()
    if "```" in sql:
        parts = sql.split("```")
        sql = parts[1].lstrip("sql\n").strip() if len(parts) > 1 else sql
    # strip any leading explanation before SELECT
    lower = sql.lower()
    for keyword in ("select", "with"):
        idx = lower.find(keyword)
        if idx > 0:
            sql = sql[idx:]
            break
    return sql.strip()


st.set_page_config(page_title="Content Catalogue", layout="wide")
st.title("Content Catalogue")


@st.cache_resource
def get_conn():
    return duckdb.connect(DB_PATH, read_only=True)


with open(QUERIES_PATH) as f:
    saved = yaml.safe_load(f)

# --- Natural language search ---
if HAS_OLLAMA:
    with st.expander("Natural Language Search", expanded=True):
        st.caption(f"Powered by Ollama · model: `{SQL_MODEL}` · host: `{OLLAMA_HOST}`")
        nl_col, btn_col = st.columns([5, 1])
        nl_query = nl_col.text_input(
            "nl",
            placeholder='e.g. "find animated content"  or  "show files with ending credits"',
            label_visibility="collapsed",
            key="nl_input",
        )
        if btn_col.button("Generate SQL", use_container_width=True) and nl_query.strip():
            with st.spinner(f"Asking {SQL_MODEL}..."):
                try:
                    generated = _generate_sql(nl_query.strip())
                    st.session_state["sql_area"] = generated
                    st.session_state["page"] = 1
                except Exception as e:
                    st.error(f"SQL generation failed: {e}")
            st.rerun()
    st.write("")

# --- Saved queries dropdown ---
choice = st.selectbox(
    "Saved query",
    ["— ad hoc —"] + list(saved.keys()),
    format_func=lambda k: k if k == "— ad hoc —" else saved[k]["label"],
)

if st.session_state.get("last_choice") != choice:
    st.session_state["sql_area"] = (
        "SELECT * FROM media_files LIMIT 20"
        if choice == "— ad hoc —"
        else saved[choice]["sql"]
    )
    st.session_state["last_choice"] = choice
    st.session_state["page"] = 1

if "sql_area" not in st.session_state:
    st.session_state["sql_area"] = "SELECT * FROM media_files LIMIT 20"

sql = st.text_area("SQL", height=140, key="sql_area")

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
