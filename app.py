import base64
import os
import duckdb
import streamlit as st
import yaml

try:
    import plotly.express as px
    HAS_PLOTLY = True
except ImportError:
    HAS_PLOTLY = False

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
  media_files(s3_key PK, size_bytes BIGINT, last_modified TEXT, extension TEXT, top_prefix TEXT, media_type TEXT, bucket TEXT)
    media_type is 'video' or 'audio'; bucket is the S3 bucket name

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


def _generate_sql(nl_query: str, bucket_filter: str = "") -> str:
    system = _NL_SCHEMA
    if bucket_filter:
        system += (
            f"\n\nActive bucket filter: always append `{bucket_filter}` to WHERE clauses "
            f"involving media_files or media_metadata."
        )
    if HAS_CLAUDE:
        return _generate_sql_claude(nl_query, system)
    return _generate_sql_ollama(nl_query, system)


def _generate_sql_claude(nl_query: str, system: str = _NL_SCHEMA) -> str:
    client = _anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model=CLAUDE_SQL_MODEL,
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": nl_query}],
    )
    return _clean_sql(resp.content[0].text)


def _generate_sql_ollama(nl_query: str, system: str = _NL_SCHEMA) -> str:
    client = _ollama.Client(host=OLLAMA_HOST, timeout=20.0)
    resp = client.chat(
        model=SQL_MODEL,
        messages=[
            {"role": "system", "content": system},
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


@st.cache_data(ttl=300)
def _load_filter_options() -> dict:
    try:
        con = get_conn()
        def _q(sql):
            return [r[0] for r in con.execute(sql).fetchall() if r[0] is not None]
        return {
            "video_codecs":   _q("SELECT DISTINCT video_codec FROM media_metadata WHERE video_codec IS NOT NULL ORDER BY video_codec"),
            "audio_codecs":   _q("SELECT DISTINCT audio_codec FROM media_metadata WHERE audio_codec IS NOT NULL ORDER BY audio_codec"),
            "formats":        _q("SELECT DISTINCT format_name FROM media_metadata WHERE format_name IS NOT NULL ORDER BY format_name"),
            "dolby_families": _q("SELECT DISTINCT dolby_codec_family FROM media_metadata WHERE dolby_codec_family IS NOT NULL ORDER BY dolby_codec_family"),
            "languages":      _q("SELECT DISTINCT language FROM audio_tracks WHERE language IS NOT NULL ORDER BY language"),
            "top_prefixes":   _q("SELECT DISTINCT top_prefix FROM media_files WHERE top_prefix IS NOT NULL AND top_prefix != '' ORDER BY top_prefix"),
        }
    except Exception:
        return {"video_codecs": [], "audio_codecs": [], "formats": [],
                "dolby_families": [], "languages": [], "top_prefixes": []}


def _build_metadata_sql(filters: dict, selected_bucket: str) -> str:
    conditions: list[str] = ["f.media_type = 'video'"]

    # Only include successfully probed files unless the user explicitly asks for errors
    if filters.get("errors", "(any)") == "With errors":
        conditions.append("mm.error IS NOT NULL")
    else:
        conditions.append("mm.error IS NULL")

    if selected_bucket != "(all)":
        conditions.append(f"f.bucket = '{selected_bucket}'")

    def _add(cond: str) -> None:
        conditions.append(cond)

    vc = filters.get("video_codec", "(any)")
    if vc and vc != "(any)":
        _add(f"mm.video_codec = '{vc}'")

    res = filters.get("resolution", "(any)")
    if res and res != "(any)":
        _add({
            "4K":    "mm.height >= 2160",
            "1080p": "mm.height BETWEEN 1080 AND 2159",
            "720p":  "mm.height BETWEEN 720 AND 1079",
            "SD":    "mm.height < 720",
        }[res])

    fmt = filters.get("format_name", "(any)")
    if fmt and fmt != "(any)":
        _add(f"mm.format_name = '{fmt}'")

    scan = filters.get("scan_type", "(any)")
    if scan and scan != "(any)":
        _add(f"mm.scan_type = '{scan}'")

    hdr_list = filters.get("hdr_formats", [])
    if hdr_list:
        quoted = ", ".join(f"'{h}'" for h in hdr_list)
        _add(f"coalesce(mm.hdr_format, 'SDR') IN ({quoted})")

    dv = filters.get("dolby_vision", "Any")
    if dv == "Yes":
        _add("mm.dolby_vision = true")
    elif dv == "No":
        _add("(mm.dolby_vision IS NULL OR mm.dolby_vision = false)")

    da = filters.get("dolby_atmos", "Any")
    if da == "Yes":
        _add("mm.dolby_atmos = true")
    elif da == "No":
        _add("(mm.dolby_atmos IS NULL OR mm.dolby_atmos = false)")

    df_fam = filters.get("dolby_codec_family", "(any)")
    if df_fam and df_fam != "(any)":
        _add(f"mm.dolby_codec_family = '{df_fam}'")

    if filters.get("min_bitrate"):
        _add(f"mm.video_bitrate >= {int(filters['min_bitrate'])}")
    if filters.get("max_bitrate"):
        _add(f"mm.video_bitrate <= {int(filters['max_bitrate'])}")

    if filters.get("min_duration"):
        _add(f"mm.duration_s >= {float(filters['min_duration'])}")
    if filters.get("max_duration"):
        _add(f"mm.duration_s <= {float(filters['max_duration'])}")

    if filters.get("min_size_mb"):
        _add(f"f.size_bytes >= {int(filters['min_size_mb']) * 1_000_000}")
    if filters.get("max_size_mb"):
        _add(f"f.size_bytes <= {int(filters['max_size_mb']) * 1_000_000}")

    if filters.get("min_height"):
        _add(f"mm.height >= {int(filters['min_height'])}")
    if filters.get("max_height"):
        _add(f"mm.height <= {int(filters['max_height'])}")

    ac = filters.get("audio_codec", "(any)")
    if ac and ac != "(any)":
        _add(f"mm.audio_codec = '{ac}'")

    min_ch = filters.get("min_channels")
    if min_ch and min_ch != "(any)":
        _add(f"mm.audio_channels >= {int(min_ch)}")

    lang = filters.get("language", "(any)")
    if lang and lang != "(any)":
        _add(f"EXISTS (SELECT 1 FROM audio_tracks atr WHERE atr.s3_key = f.s3_key AND atr.language = '{lang}')")

    tp = filters.get("top_prefix", "(any)")
    if tp and tp != "(any)":
        _add(f"f.top_prefix = '{tp}'")

    where = " AND ".join(conditions)
    return f"""
SELECT
    f.bucket,
    split_part(f.s3_key, '/', -1)           AS filename,
    regexp_replace(f.s3_key, '/[^/]+$', '') AS folder,
    f.s3_key,
    round(f.size_bytes / 1e6, 1)            AS size_mb,
    round(mm.duration_s, 1)                 AS duration_s,
    mm.video_codec,
    mm.width,
    mm.height,
    round(mm.fps::DOUBLE, 3)                AS fps,
    mm.video_bitrate                        AS bitrate_kbps,
    mm.hdr_format,
    mm.dolby_vision,
    mm.dv_profile,
    mm.scan_type,
    mm.format_name,
    mm.audio_codec,
    mm.audio_channels,
    mm.dolby_atmos,
    mm.dolby_codec_family,
    mm.error
FROM media_files f
JOIN media_metadata mm USING (s3_key)
WHERE {where}
ORDER BY size_mb DESC
LIMIT 1000
"""


st.set_page_config(page_title="BitQuery", layout="wide")

st.markdown("""
<style>
/* ── Page background ── */
[data-testid="stAppViewContainer"] { background: #0d1117; }
[data-testid="stSidebar"]          { background: #161b22; }

/* ── Metric cards ── */
[data-testid="metric-container"] {
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 10px;
    padding: 1rem 1.2rem;
}
[data-testid="stMetricValue"] {
    color: #58a6ff !important;
    font-size: 1.8rem !important;
    font-weight: 700 !important;
}
[data-testid="stMetricLabel"] { color: #8b949e !important; }

/* ── Expanders ── */
[data-testid="stExpander"] {
    background: #161b22;
    border: 1px solid #30363d !important;
    border-radius: 10px;
    margin-bottom: 0.8rem;
}

/* ── Buttons ── */
[data-testid="stButton"] > button[kind="primary"] {
    background: linear-gradient(135deg, #58a6ff, #a371f7);
    border: none;
    color: #0d1117;
    font-weight: 700;
    border-radius: 6px;
}
[data-testid="stButton"] > button[kind="primary"]:hover {
    opacity: 0.88;
    transform: translateY(-1px);
}

/* ── Text input / text area ── */
[data-testid="stTextInput"] input,
[data-testid="stTextArea"]  textarea {
    background: #0d1117 !important;
    border: 1px solid #30363d !important;
    border-radius: 6px;
    color: #e6edf3 !important;
}

/* ── Dataframe ── */
[data-testid="stDataFrame"] { border-radius: 8px; overflow: hidden; }

/* ── Selectbox ── */
[data-testid="stSelectbox"] > div > div {
    background: #161b22 !important;
    border: 1px solid #30363d !important;
    border-radius: 6px;
}

/* ── Scrollbar ── */
::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-track { background: #0d1117; }
::-webkit-scrollbar-thumb { background: #30363d; border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: #58a6ff; }
</style>
""", unsafe_allow_html=True)

_logo_path = os.path.join(os.path.dirname(__file__), "bitmovin_logo.svg")
_logo_b64 = base64.b64encode(open(_logo_path, "rb").read()).decode()
st.markdown(f"""
<div style="display:flex; align-items:center; gap:18px; margin-bottom:0.5rem;">
  <img src="data:image/svg+xml;base64,{_logo_b64}"
       style="height:36px; filter:brightness(0) invert(1);" />
  <span style="
    background: linear-gradient(90deg, #58a6ff 0%, #a371f7 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    font-size: 2.2rem;
    font-weight: 800;
    letter-spacing: -0.5px;
    line-height: 1;
  ">BitQuery</span>
</div>
""", unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Sidebar: bucket selector + prefix status
# ---------------------------------------------------------------------------
with st.sidebar:
    st.markdown("### Bucket")
    try:
        _buckets = [r[0] for r in get_conn().execute(
            "SELECT bucket FROM scan_buckets ORDER BY bucket"
        ).fetchall()]
    except Exception:
        _buckets = []

    _bucket_options = ["(all)"] + _buckets
    _selected_bucket = st.selectbox(
        "Filter by bucket",
        _bucket_options,
        label_visibility="collapsed",
        key="selected_bucket",
    )

    if _selected_bucket != "(all)":
        st.caption(f"`s3://{_selected_bucket}`")
        S3_BUCKET = _selected_bucket

    st.write("")

    _bucket_filter_sql = f"WHERE bucket = '{_selected_bucket}'" if _selected_bucket != "(all)" else ""

    with st.expander("Prefix status", expanded=False):
        try:
            _prefix_df = get_conn().execute(f"""
                SELECT prefix,
                       CASE WHEN ignored THEN '⛔ ignored' ELSE '✓' END AS status,
                       last_scanned_at,
                       file_count
                FROM scan_prefixes
                {_bucket_filter_sql}
                ORDER BY ignored DESC, prefix
            """).df()
            if _prefix_df.empty:
                st.caption("No prefixes indexed yet. Run `make init` or `make inventory`.")
            else:
                st.dataframe(_prefix_df, use_container_width=True, hide_index=True)
        except Exception as _e:
            st.caption(f"Could not load prefix data: {_e}")

    with st.expander("Unknown extensions", expanded=False):
        try:
            _unk_df = get_conn().execute(f"""
                SELECT extension, count, example_key
                FROM scan_unknown_extensions
                {_bucket_filter_sql}
                ORDER BY count DESC
            """).df()
            if _unk_df.empty:
                st.caption("No unknown extensions found.")
            else:
                st.dataframe(_unk_df, use_container_width=True, hide_index=True)
        except Exception:
            st.caption("No unknown extension data yet.")

# ---------------------------------------------------------------------------
# Bucket filter clause injected into NL queries
# ---------------------------------------------------------------------------
_bucket_clause = (
    f"AND f.bucket = '{_selected_bucket}' AND mm.bucket = '{_selected_bucket}'"
    if _selected_bucket != "(all)"
    else ""
)

_metrics_bucket_filter = f"WHERE f.bucket = '{_selected_bucket}'" if _selected_bucket != "(all)" else ""

try:
    _s = get_conn().execute(f"""
        SELECT
            count(*) FILTER (WHERE f.media_type = 'video')           AS video_files,
            count(*) FILTER (WHERE f.media_type = 'audio')           AS audio_files,
            round(sum(f.size_bytes) / 1e9, 1)                        AS total_gb,
            sum(m.duration_s) FILTER (WHERE f.media_type = 'video')  AS video_s
        FROM media_files f
        LEFT JOIN media_metadata m USING (s3_key)
        {_metrics_bucket_filter}
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


if HAS_PLOTLY:
    import pandas as _pd

    def _dark(extra: dict | None = None) -> dict:
        base = dict(
            paper_bgcolor="#161b22", plot_bgcolor="#161b22",
            font=dict(color="#e6edf3"), title_font=dict(color="#58a6ff"),
            margin=dict(l=0, r=0, t=40, b=0),
            legend=dict(bgcolor="#0d1117", bordercolor="#30363d", borderwidth=1),
        )
        if extra:
            base.update(extra)
        return base

    def _hbar(df, x, y, title, *, flip=True):
        fig = px.bar(df, x=x, y=y, orientation="h", title=title,
                     color=x, color_continuous_scale="Blues",
                     labels={x: x.replace("_", " ").title(), y: ""})
        fig.update_layout(**_dark({"showlegend": False, "coloraxis_showscale": False,
                                   "yaxis": dict(autorange="reversed" if flip else True)}))
        return fig

    with st.expander("Charts", expanded=True):

        # ── Row 1: Genre  |  Content map ──────────────────────────────────────
        _c1, _c2 = st.columns(2)

        with _c1:
            try:
                _df = get_conn().execute("""
                    SELECT genre, count(*) AS files
                    FROM (SELECT UNNEST(genre_tags) AS genre FROM content_vision
                          WHERE genre_tags IS NOT NULL AND len(genre_tags) > 0
                            AND description NOT LIKE '[error:%')
                    GROUP BY genre ORDER BY files DESC LIMIT 20
                """).df()
                if not _df.empty:
                    st.plotly_chart(_hbar(_df, "files", "genre", "Content by Genre"),
                                    use_container_width=True)
                else:
                    st.caption("No genre data yet — run `make vision`.")
            except Exception as _e:
                st.caption(f"Genre chart unavailable: {_e}")

        with _c2:
            try:
                _df = get_conn().execute("""
                    SELECT f.s3_key,
                        round(m.duration_s / 60, 1)   AS duration_min,
                        round(f.size_bytes / 1e9, 2)  AS size_gb,
                        coalesce(m.hdr_format, 'SDR') AS hdr_format,
                        coalesce(CASE WHEN m.height >= 2160 THEN '4K'
                                      WHEN m.height >= 1080 THEN '1080p'
                                      WHEN m.height >=  720 THEN '720p'
                                      ELSE 'SD' END, 'Unknown') AS resolution_tier,
                        m.video_codec
                    FROM media_files f JOIN media_metadata m USING (s3_key)
                    WHERE f.media_type = 'video' AND m.duration_s IS NOT NULL AND m.error IS NULL
                """).df()
                if not _df.empty:
                    _fig = px.scatter(_df, x="duration_min", y="size_gb",
                        color="hdr_format", symbol="resolution_tier",
                        hover_name="s3_key",
                        hover_data={"video_codec": True, "duration_min": True, "size_gb": True},
                        title="Content Map — Duration vs Size",
                        labels={"duration_min": "Duration (min)", "size_gb": "Size (GB)",
                                "hdr_format": "HDR", "resolution_tier": "Resolution"},
                        color_discrete_map={"SDR": "#4C78A8", "HDR10": "#F58518",
                                            "HDR10+": "#E45756", "HLG": "#72B7B2",
                                            "Dolby Vision": "#B279A2"})
                    _fig.update_layout(**_dark())
                    _fig.update_xaxes(gridcolor="#21262d", zerolinecolor="#30363d")
                    _fig.update_yaxes(gridcolor="#21262d", zerolinecolor="#30363d")
                    st.plotly_chart(_fig, use_container_width=True)
                else:
                    st.caption("No metadata yet — run `make metadata`.")
            except Exception:
                pass

        # ── Row 2: Codec  |  HDR donut ────────────────────────────────────────
        _c1, _c2 = st.columns(2)

        with _c1:
            try:
                _df = get_conn().execute("""
                    SELECT video_codec, count(*) AS files
                    FROM media_metadata WHERE video_codec IS NOT NULL
                    GROUP BY video_codec ORDER BY files DESC
                """).df()
                if not _df.empty:
                    st.plotly_chart(_hbar(_df, "files", "video_codec", "Video Codec Distribution"),
                                    use_container_width=True)
            except Exception:
                pass

        with _c2:
            try:
                _df = get_conn().execute("""
                    SELECT coalesce(hdr_format, 'SDR') AS hdr_format, count(*) AS files
                    FROM media_metadata WHERE error IS NULL
                    GROUP BY hdr_format ORDER BY files DESC
                """).df()
                if not _df.empty:
                    _fig = px.pie(_df, names="hdr_format", values="files",
                                  title="HDR Format Breakdown", hole=0.55,
                                  color_discrete_map={"SDR": "#4C78A8", "HDR10": "#F58518",
                                                      "HDR10+": "#E45756", "HLG": "#72B7B2",
                                                      "Dolby Vision": "#B279A2"})
                    _fig.update_layout(**_dark({"showlegend": True}))
                    _fig.update_traces(textfont_color="#e6edf3")
                    st.plotly_chart(_fig, use_container_width=True)
            except Exception:
                pass

        # ── Row 3: Resolution treemap  |  Content over time ───────────────────
        _c1, _c2 = st.columns(2)

        with _c1:
            try:
                _df = get_conn().execute("""
                    SELECT
                        CASE WHEN height >= 2160 THEN '4K'
                             WHEN height >= 1080 THEN '1080p'
                             WHEN height >=  720 THEN '720p'
                             ELSE 'SD' END                         AS tier,
                        width::TEXT || 'x' || height::TEXT         AS resolution,
                        count(*)                                   AS files
                    FROM media_metadata
                    WHERE width IS NOT NULL AND error IS NULL
                    GROUP BY tier, resolution ORDER BY files DESC
                """).df()
                if not _df.empty:
                    _fig = px.treemap(_df, path=["tier", "resolution"], values="files",
                                      title="Resolution Distribution",
                                      color="files", color_continuous_scale="Blues")
                    _fig.update_layout(**_dark({"coloraxis_showscale": False}))
                    _fig.update_traces(textfont_color="#e6edf3",
                                       marker_line_color="#0d1117", marker_line_width=2)
                    st.plotly_chart(_fig, use_container_width=True)
            except Exception:
                pass

        with _c2:
            try:
                _df = get_conn().execute("""
                    SELECT substr(last_modified, 1, 7) AS month, count(*) AS files
                    FROM media_files
                    GROUP BY month ORDER BY month
                """).df()
                if not _df.empty:
                    _fig = px.area(_df, x="month", y="files", title="Content Added Over Time",
                                   labels={"month": "", "files": "Files added"},
                                   color_discrete_sequence=["#58a6ff"])
                    _fig.update_layout(**_dark())
                    _fig.update_xaxes(gridcolor="#21262d", zerolinecolor="#30363d")
                    _fig.update_yaxes(gridcolor="#21262d", zerolinecolor="#30363d")
                    _fig.update_traces(fillcolor="rgba(88,166,255,0.15)", line_width=2)
                    st.plotly_chart(_fig, use_container_width=True)
            except Exception:
                pass

        # ── Row 4: Bitrate by codec  |  Audio languages ───────────────────────
        _c1, _c2 = st.columns(2)

        with _c1:
            try:
                _df = get_conn().execute("""
                    SELECT video_codec, round(video_bitrate / 1000.0, 1) AS bitrate_mbps
                    FROM media_metadata
                    WHERE video_codec IS NOT NULL AND video_bitrate IS NOT NULL
                      AND video_bitrate > 0 AND error IS NULL
                """).df()
                if not _df.empty:
                    _fig = px.box(_df, x="video_codec", y="bitrate_mbps",
                                  color="video_codec", title="Bitrate by Codec (Mbps)",
                                  labels={"video_codec": "", "bitrate_mbps": "Mbps"})
                    _fig.update_layout(**_dark({"showlegend": False}))
                    _fig.update_xaxes(gridcolor="#21262d")
                    _fig.update_yaxes(gridcolor="#21262d", zerolinecolor="#30363d")
                    st.plotly_chart(_fig, use_container_width=True)
            except Exception:
                pass

        with _c2:
            try:
                _df = get_conn().execute("""
                    SELECT coalesce(language, 'und') AS language, count(*) AS tracks
                    FROM audio_tracks
                    GROUP BY language ORDER BY tracks DESC LIMIT 15
                """).df()
                if not _df.empty:
                    st.plotly_chart(_hbar(_df, "tracks", "language", "Audio Track Languages"),
                                    use_container_width=True)
            except Exception:
                pass

        # ── Row 5: Style × Brightness heatmap  |  Dolby coverage ──────────────
        _c1, _c2 = st.columns(2)

        with _c1:
            try:
                _df = get_conn().execute("""
                    SELECT style, brightness, count(*) AS files
                    FROM content_vision
                    WHERE style IS NOT NULL AND brightness IS NOT NULL
                      AND description NOT LIKE '[error:%'
                    GROUP BY style, brightness
                """).df()
                if not _df.empty:
                    _hm = _df.pivot_table(index="style", columns="brightness",
                                          values="files", fill_value=0)
                    _fig = px.imshow(_hm, text_auto=True, title="Style × Brightness",
                                     color_continuous_scale="Blues",
                                     labels={"x": "Brightness", "y": "Style", "color": "Files"})
                    _fig.update_layout(**_dark({"coloraxis_showscale": False}))
                    _fig.update_xaxes(side="bottom")
                    st.plotly_chart(_fig, use_container_width=True)
                else:
                    st.caption("No vision data yet — run `make vision`.")
            except Exception:
                pass

        with _c2:
            try:
                _df = get_conn().execute("""
                    SELECT
                        CASE WHEN dolby_vision AND dolby_atmos THEN 'Vision + Atmos'
                             WHEN dolby_vision                 THEN 'Dolby Vision'
                             WHEN dolby_atmos                  THEN 'Dolby Atmos'
                             ELSE 'Neither' END AS dolby_tier,
                        count(*) AS files
                    FROM media_metadata WHERE error IS NULL
                    GROUP BY dolby_tier ORDER BY files DESC
                """).df()
                if not _df.empty:
                    _fig = px.bar(_df, x="dolby_tier", y="files", title="Dolby Format Coverage",
                                  color="dolby_tier", text="files",
                                  labels={"dolby_tier": "", "files": "Files"},
                                  color_discrete_map={
                                      "Vision + Atmos": "#B279A2",
                                      "Dolby Vision":   "#a371f7",
                                      "Dolby Atmos":    "#58a6ff",
                                      "Neither":        "#30363d",
                                  })
                    _fig.update_layout(**_dark({"showlegend": False}))
                    _fig.update_traces(textposition="outside", textfont_color="#e6edf3")
                    _fig.update_yaxes(gridcolor="#21262d")
                    st.plotly_chart(_fig, use_container_width=True)
            except Exception:
                pass

        # ── Row 6: Content style donut ────────────────────────────────────────
        _c1, _c2 = st.columns(2)

        with _c1:
            try:
                _df = get_conn().execute("""
                    SELECT style, count(*) AS files
                    FROM content_vision
                    WHERE style IS NOT NULL AND description NOT LIKE '[error:%'
                    GROUP BY style ORDER BY files DESC
                """).df()
                if not _df.empty:
                    _fig = px.pie(_df, names="style", values="files",
                                  title="Content Style", hole=0.55,
                                  color_discrete_map={"live_action": "#58a6ff",
                                                      "animated":    "#F58518",
                                                      "cgi":         "#B279A2",
                                                      "mixed":       "#72B7B2"})
                    _fig.update_layout(**_dark({"showlegend": True}))
                    _fig.update_traces(textfont_color="#e6edf3")
                    st.plotly_chart(_fig, use_container_width=True)
                else:
                    st.caption("No vision data yet — run `make vision`.")
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
                    generated = _generate_sql(nl_query.strip(), _bucket_clause)
                    st.session_state["sql_area"] = generated
                    _run_sql(generated)
                except Exception as e:
                    st.error(f"Search failed: {e}")
    st.write("")

# --- Metadata filter search ---
_ANY = "(any)"
_MF_KEYS = [
    "mf_video_codec", "mf_resolution", "mf_format_name", "mf_scan_type",
    "mf_hdr_formats", "mf_dolby_vision", "mf_dolby_atmos", "mf_dolby_codec_family",
    "mf_min_bitrate", "mf_max_bitrate", "mf_min_duration", "mf_max_duration",
    "mf_min_size_mb", "mf_max_size_mb", "mf_min_height", "mf_max_height",
    "mf_audio_codec", "mf_min_channels", "mf_language", "mf_top_prefix", "mf_errors",
]

with st.expander("Search via Metadata", expanded=False):
    try:
        _opts = _load_filter_options()
    except Exception:
        _opts = {"video_codecs": [], "audio_codecs": [], "formats": [],
                 "dolby_families": [], "languages": [], "top_prefixes": []}

    # Row 1 — Video identity
    _r1a, _r1b, _r1c, _r1d = st.columns(4)
    with _r1a:
        st.selectbox("Video codec", [_ANY] + _opts["video_codecs"], key="mf_video_codec")
    with _r1b:
        st.selectbox("Resolution", [_ANY, "4K", "1080p", "720p", "SD"], key="mf_resolution")
    with _r1c:
        st.selectbox("Container", [_ANY] + _opts["formats"], key="mf_format_name")
    with _r1d:
        st.selectbox("Scan type", [_ANY, "progressive", "interlaced"], key="mf_scan_type")

    # Row 2 — HDR & Dolby
    _r2a, _r2b, _r2c, _r2d = st.columns(4)
    with _r2a:
        st.multiselect("HDR format", ["SDR", "HDR10", "HDR10+", "HLG", "Dolby Vision"],
                       key="mf_hdr_formats")
    with _r2b:
        st.radio("Dolby Vision", ["Any", "Yes", "No"], horizontal=True, key="mf_dolby_vision")
    with _r2c:
        st.radio("Dolby Atmos", ["Any", "Yes", "No"], horizontal=True, key="mf_dolby_atmos")
    with _r2d:
        st.selectbox("Dolby family", [_ANY] + _opts["dolby_families"], key="mf_dolby_codec_family")

    # Row 3 — Numeric ranges
    _r3a, _r3b, _r3c, _r3d = st.columns(4)
    with _r3a:
        st.caption("Bitrate (kbps)")
        _bc1, _bc2 = st.columns(2)
        _bc1.number_input("Min", min_value=0, step=1000, value=0, key="mf_min_bitrate", label_visibility="collapsed")
        _bc2.number_input("Max", min_value=0, step=1000, value=0, key="mf_max_bitrate", label_visibility="collapsed")
        st.caption("↑ Min / Max  (0 = no limit)")
    with _r3b:
        st.caption("Duration (seconds)")
        _dc1, _dc2 = st.columns(2)
        _dc1.number_input("Min", min_value=0, step=60, value=0, key="mf_min_duration", label_visibility="collapsed")
        _dc2.number_input("Max", min_value=0, step=60, value=0, key="mf_max_duration", label_visibility="collapsed")
        st.caption("↑ Min / Max  (0 = no limit)")
    with _r3c:
        st.caption("File size (MB)")
        _sc1, _sc2 = st.columns(2)
        _sc1.number_input("Min", min_value=0, step=100, value=0, key="mf_min_size_mb", label_visibility="collapsed")
        _sc2.number_input("Max", min_value=0, step=100, value=0, key="mf_max_size_mb", label_visibility="collapsed")
        st.caption("↑ Min / Max  (0 = no limit)")
    with _r3d:
        st.caption("Height (px)")
        _hc1, _hc2 = st.columns(2)
        _hc1.number_input("Min", min_value=0, step=1, value=0, key="mf_min_height", label_visibility="collapsed")
        _hc2.number_input("Max", min_value=0, step=1, value=0, key="mf_max_height", label_visibility="collapsed")
        st.caption("↑ Min / Max  (0 = no limit)")

    # Row 4 — Audio & Location
    _r4a, _r4b, _r4c, _r4d = st.columns(4)
    with _r4a:
        st.selectbox("Audio codec", [_ANY] + _opts["audio_codecs"], key="mf_audio_codec")
    with _r4b:
        st.selectbox("Min audio channels", [_ANY, "1", "2", "6", "8", "16"], key="mf_min_channels")
    with _r4c:
        st.selectbox("Audio language", [_ANY] + _opts["languages"], key="mf_language")
    with _r4d:
        st.selectbox("Top prefix", [_ANY] + _opts["top_prefixes"], key="mf_top_prefix")

    # Row 5 — Errors filter + actions
    _r5a, _r5b, _r5c = st.columns([2, 1, 1])
    with _r5a:
        st.selectbox("Probe errors", [_ANY, "Without errors", "With errors"], key="mf_errors")
    with _r5b:
        st.write("")
        if st.button("Search", type="primary", use_container_width=True, key="mf_search"):
            _filters = {
                "video_codec":       st.session_state.get("mf_video_codec", _ANY),
                "resolution":        st.session_state.get("mf_resolution", _ANY),
                "format_name":       st.session_state.get("mf_format_name", _ANY),
                "scan_type":         st.session_state.get("mf_scan_type", _ANY),
                "hdr_formats":       st.session_state.get("mf_hdr_formats", []),
                "dolby_vision":      st.session_state.get("mf_dolby_vision", "Any"),
                "dolby_atmos":       st.session_state.get("mf_dolby_atmos", "Any"),
                "dolby_codec_family": st.session_state.get("mf_dolby_codec_family", _ANY),
                "min_bitrate":       st.session_state.get("mf_min_bitrate") or 0,
                "max_bitrate":       st.session_state.get("mf_max_bitrate") or 0,
                "min_duration":      st.session_state.get("mf_min_duration") or 0,
                "max_duration":      st.session_state.get("mf_max_duration") or 0,
                "min_size_mb":       st.session_state.get("mf_min_size_mb") or 0,
                "max_size_mb":       st.session_state.get("mf_max_size_mb") or 0,
                "min_height":        st.session_state.get("mf_min_height") or 0,
                "max_height":        st.session_state.get("mf_max_height") or 0,
                "audio_codec":       st.session_state.get("mf_audio_codec", _ANY),
                "min_channels":      st.session_state.get("mf_min_channels", _ANY),
                "language":          st.session_state.get("mf_language", _ANY),
                "top_prefix":        st.session_state.get("mf_top_prefix", _ANY),
                "errors":            st.session_state.get("mf_errors", _ANY),
            }
            # Zero means "no limit" for numeric fields
            for _k in ("min_bitrate", "max_bitrate", "min_duration", "max_duration",
                       "min_size_mb", "max_size_mb", "min_height", "max_height"):
                if _filters[_k] == 0:
                    _filters[_k] = None
            _sql = _build_metadata_sql(_filters, _selected_bucket)
            st.session_state["sql_area"] = _sql.strip()
            _run_sql(_sql)
    with _r5c:
        st.write("")
        if st.button("Clear filters", use_container_width=True, key="mf_clear"):
            for _k in _MF_KEYS:
                if _k in st.session_state:
                    del st.session_state[_k]
            st.rerun()

    st.caption("Video files only · up to 1,000 results · 0 in numeric fields = no limit · download CSV for full set")

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
        _has_bucket_col = "bucket" in page_df.columns
        _uris = [
            f"s3://{page_df.iloc[i]['bucket'] if _has_bucket_col else S3_BUCKET}/{page_df.iloc[i]['s3_key']}"
            for i in selected_rows
        ]
        st.caption(f"{len(selected_rows)} file(s) selected")
        _uri_tab, _cmd_tab = st.tabs(["S3 URIs", "aws s3 cp commands"])
        with _uri_tab:
            st.code("\n".join(_uris), language="text")
        with _cmd_tab:
            st.code("\n".join(f"aws s3 cp {u} ." for u in _uris), language="bash")
