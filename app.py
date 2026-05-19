import os
import duckdb
import streamlit as st
import yaml

DB_PATH           = os.environ.get("DB_PATH",           "/data/content_catalogue.duckdb")
QUERIES_PATH      = os.environ.get("QUERIES_PATH",      "/app/queries.yaml")
OLLAMA_HOST       = os.environ.get("OLLAMA_HOST",       "http://localhost:11434")
SQL_MODEL         = os.environ.get("OLLAMA_SQL_MODEL",  "llama3.2")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
CLAUDE_SQL_MODEL  = os.environ.get("CLAUDE_SQL_MODEL",  "claude-haiku-4-5-20251001")
S3_BUCKET         = os.environ.get("S3_BUCKET",         "bitmovin-api-eu-west1-ci-input")
PAGE_SIZE         = 200

try:
    import anthropic as _anthropic
    HAS_CLAUDE = bool(ANTHROPIC_API_KEY)
except ImportError:
    HAS_CLAUDE = False

try:
    import ollama as _ollama
    HAS_OLLAMA = True
except ImportError:
    HAS_OLLAMA = False

HAS_NL = HAS_CLAUDE or HAS_OLLAMA

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

  audio_tracks(s3_key TEXT, track_index INT, codec TEXT, channels INT,
    sample_rate INT, language TEXT, dolby_atmos BOOL, dolby_codec_family TEXT)
    -- language is ISO code e.g. 'eng', 'fra'. No analyzed_at column here.

  content_vision(s3_key TEXT PK, description TEXT,
    style TEXT,       -- one of: 'live_action', 'animated', 'cgi', 'mixed'
    has_credits BOOL, -- true if ending/opening credits detected
    brightness TEXT,  -- one of: 'bright', 'normal', 'dark', 'mixed'
    genre_tags VARCHAR[], -- array, e.g. ['drama','sports']. ONLY content_vision has this.
    analyzed_at TEXT, -- ONLY in content_vision, not in any other table
    source_key TEXT)  -- non-NULL = copy of another row; join to the representative

When aliasing, always write the full table name followed by the alias, e.g.:
  media_files mf, media_metadata mm, audio_tracks atr, content_vision cv
Never use an alias alone in the FROM clause — always write the full table name first.

Rules:
- Return ONLY the SQL statement, no explanation, no markdown fences.
- Use DuckDB syntax.
- JOIN tables via s3_key when needed. Only JOIN tables actually needed for the query.
- To search genre_tags use: list_contains(cv.genre_tags, 'sports')
- NEVER use 'at' as a table alias — it is a reserved keyword in DuckDB.
- Always write alias.* with a dot, never alias* (e.g. cv.* not cv*).
- Always qualify every column reference with its table alias, including inside subqueries.

Example — content with more than one audio track:
SELECT f.s3_key,
  count(atr.track_index)  AS audio_tracks,
  string_agg(coalesce(atr.language,'?') || ':' || coalesce(atr.codec,'?') || '/' || coalesce(atr.channels::TEXT,'?') || 'ch', '  ' ORDER BY atr.track_index) AS tracks_detail,
  bool_or(atr.dolby_atmos) AS has_atmos,
  round(f.size_bytes / 1e6, 1) AS size_mb
FROM media_files f
JOIN audio_tracks atr USING (s3_key)
GROUP BY f.s3_key, f.size_bytes
HAVING count(atr.track_index) > 1
ORDER BY audio_tracks DESC
- NEVER JOIN audio_tracks unless the question is about audio tracks or languages.
- When joining audio_tracks, always GROUP BY and use string_agg to show track details.
- analyzed_at exists ONLY in content_vision — never reference it from other tables.

Example — find sports content:
SELECT cv.s3_key, cv.description, cv.style, cv.genre_tags
FROM content_vision cv
WHERE list_contains(cv.genre_tags, 'sports')"""


def _generate_sql(nl_query: str) -> str:
    if HAS_CLAUDE:
        return _generate_sql_claude(nl_query)
    return _generate_sql_ollama(nl_query)


def _generate_sql_claude(nl_query: str) -> str:
    client = _anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model=CLAUDE_SQL_MODEL,
        max_tokens=512,
        system=_NL_SCHEMA,
        messages=[{"role": "user", "content": nl_query}],
    )
    return _clean_sql(resp.content[0].text)


def _generate_sql_ollama(nl_query: str) -> str:
    client = _ollama.Client(host=OLLAMA_HOST, timeout=20.0)
    resp = client.chat(
        model=SQL_MODEL,
        messages=[
            {"role": "system", "content": _NL_SCHEMA},
            {"role": "user", "content": nl_query},
        ],
    )
    return _clean_sql(resp.message.content)


def _clean_sql(sql: str) -> str:
    sql = sql.strip()
    if "```" in sql:
        parts = sql.split("```")
        sql = parts[1].lstrip("sql\n").strip() if len(parts) > 1 else sql
    lower = sql.lower()
    for keyword in ("select", "with"):
        idx = lower.find(keyword)
        if idx > 0:
            sql = sql[idx:]
            break
    return sql.strip()


def _run_sql(sql: str) -> None:
    try:
        get_conn().execute(f"EXPLAIN {sql}")
        st.session_state["df"] = get_conn().execute(sql).df()
        st.session_state["page"] = 1
    except Exception as e:
        st.session_state.pop("df", None)
        st.error(f"Query error: {e}")


@st.cache_resource
def get_conn():
    return duckdb.connect(DB_PATH, read_only=True)


st.set_page_config(page_title="Content Catalogue", layout="wide")
st.title("Content Catalogue")

try:
    _s = get_conn().execute("""
        SELECT
            count(*) FILTER (WHERE f.media_type = 'video')           AS video_files,
            count(*) FILTER (WHERE f.media_type = 'audio')           AS audio_files,
            round(sum(f.size_bytes) / 1e9, 1)                        AS total_gb,
            sum(m.duration_s) FILTER (WHERE f.media_type = 'video')  AS video_s
        FROM media_files f
        LEFT JOIN media_metadata m USING (s3_key)
    """).fetchone()
    if _s and _s[0]:
        _h, _rem = divmod(int(_s[3] or 0), 3600)
        _m = _rem // 60
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Video files",    f"{_s[0]:,}")
        c2.metric("Audio files",    f"{_s[1]:,}")
        c3.metric("Total size",     f"{_s[2]} GB")
        c4.metric("Total duration", f"{_h:,}h {_m:02d}m")
        st.write("")
except Exception:
    pass


with open(QUERIES_PATH) as f:
    saved = yaml.safe_load(f)

# --- Natural language search (primary interface) ---
if HAS_NL:
    _backend_label = (
        f"Claude · model: `{CLAUDE_SQL_MODEL}`"
        if HAS_CLAUDE
        else f"Ollama · model: `{SQL_MODEL}` · host: `{OLLAMA_HOST}`"
    )
    _spinner_label = CLAUDE_SQL_MODEL if HAS_CLAUDE else SQL_MODEL
    with st.expander("Search", expanded=True):
        st.caption(f"Powered by {_backend_label}")
        nl_col, btn_col = st.columns([5, 1])
        nl_query = nl_col.text_input(
            "nl",
            placeholder='e.g. "find animated content"  or  "show files with ending credits"',
            label_visibility="collapsed",
            key="nl_input",
        )
        if btn_col.button("Search", type="primary", use_container_width=True) and nl_query.strip():
            with st.spinner(f"Asking {_spinner_label}..."):
                try:
                    generated = _generate_sql(nl_query.strip())
                    st.session_state["sql_area"] = generated
                    _run_sql(generated)
                except Exception as e:
                    st.error(f"Search failed: {e}")
    st.write("")

# --- Advanced: saved queries + SQL editor ---
if "sql_area" not in st.session_state:
    st.session_state["sql_area"] = "SELECT * FROM media_files LIMIT 20"

def _on_query_choice_change():
    key = st.session_state["query_choice"]
    st.session_state["sql_area"] = (
        "SELECT * FROM media_files LIMIT 20"
        if key == "— ad hoc —"
        else saved[key]["sql"]
    )
    st.session_state["page"] = 1

with st.expander("Advanced — view / edit SQL", expanded=not HAS_NL):
    st.selectbox(
        "Saved query",
        ["— ad hoc —"] + list(saved.keys()),
        format_func=lambda k: k if k == "— ad hoc —" else saved[k]["label"],
        key="query_choice",
        on_change=_on_query_choice_change,
    )
    sql = st.text_area("SQL", height=140, key="sql_area")
    if st.button("Run", type="primary"):
        _run_sql(sql)

# --- Results ---
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
    page_df = df.iloc[start : start + PAGE_SIZE]
    event = st.dataframe(
        page_df,
        use_container_width=True,
        on_select="rerun",
        selection_mode="multi-row",
    )
    st.download_button("Download CSV", df.to_csv(index=False), "results.csv", "text/csv")

    selected_rows = event.selection.rows
    if selected_rows and "s3_key" in page_df.columns:
        cmds = "\n".join(
            f"aws s3 cp s3://{S3_BUCKET}/{page_df.iloc[i]['s3_key']} ."
            for i in selected_rows
        )
        st.caption(f"{len(selected_rows)} file(s) selected — click the copy icon to copy the command(s):")
        st.code(cmds, language="bash")
