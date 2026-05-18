#!/usr/bin/env python3
"""
S3 Media Content Cataloguer
Phase 1: Inventory video and audio files in an S3 bucket
Phase 2: Extract media attributes via ffprobe on presigned URLs
         Includes HDR format, Dolby Vision, and Dolby Atmos detection
"""

import argparse
import base64
import json
import logging
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import boto3
import duckdb

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

VIDEO_EXTENSIONS = {
    "mp4", "mov", "mkv", "mxf", "ts", "m2ts", "mts", "avi", "wmv",
    "flv", "webm", "mpg", "mpeg", "m4v", "3gp", "f4v", "vob", "ogv",
    "dv", "r3d",
}

AUDIO_EXTENSIONS = {
    "mp3", "aac", "wav", "flac", "ogg", "opus", "m4a", "wma",
    "aiff", "aif", "ac3", "eac3", "ec3", "dts", "dtshd", "mka",
    "truehd", "mlp", "caf", "ra", "ape", "adts", "ac4",
}

DOLBY_CODEC_FAMILIES = {
    "ac3": "AC-3",
    "eac3": "E-AC-3",
    "truehd": "TrueHD",
    "ac4": "AC-4",
    "dts": "DTS",
    "dtshd": "DTS-HD",
}

PRESIGN_TTL = 3600  # seconds
MEDIAINFO_AVAILABLE = shutil.which("mediainfo") is not None


def init_db(db_path: str) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(db_path)
    con.execute("""
        CREATE TABLE IF NOT EXISTS media_files (
            s3_key        TEXT PRIMARY KEY,
            size_bytes    BIGINT,
            last_modified TEXT,
            extension     TEXT,
            top_prefix    TEXT,
            media_type    TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS media_metadata (
            s3_key             TEXT PRIMARY KEY,
            duration_s         DOUBLE,
            format_name        TEXT,
            width              INTEGER,
            height             INTEGER,
            fps                DOUBLE,
            video_codec        TEXT,
            video_bitrate      INTEGER,
            scan_type          TEXT,
            color_primaries    TEXT,
            color_transfer     TEXT,
            color_space        TEXT,
            hdr_format         TEXT,
            dolby_vision       BOOLEAN,
            dv_profile         INTEGER,
            dv_level           INTEGER,
            audio_codec        TEXT,
            audio_channels     INTEGER,
            audio_sample_rate  INTEGER,
            dolby_atmos        BOOLEAN,
            dolby_codec_family TEXT,
            error              TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS audio_tracks (
            s3_key             TEXT,
            track_index        INTEGER,
            codec              TEXT,
            channels           INTEGER,
            sample_rate        INTEGER,
            language           TEXT,
            dolby_atmos        BOOLEAN,
            dolby_codec_family TEXT,
            PRIMARY KEY (s3_key, track_index)
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS content_vision (
            s3_key      TEXT PRIMARY KEY,
            description TEXT,
            style       TEXT,
            has_credits BOOLEAN,
            brightness  TEXT,
            genre_tags  VARCHAR[],
            analyzed_at TEXT,
            source_key  TEXT  -- NULL = directly analysed; set = copied from this key
        )
    """)
    # migrate existing databases that predate source_key
    try:
        con.execute("ALTER TABLE content_vision ADD COLUMN source_key TEXT")
    except Exception:
        pass
    return con


# ---------------------------------------------------------------------------
# Phase 1 — inventory
# ---------------------------------------------------------------------------

def list_media_files(bucket: str, prefix: str, profile: str | None) -> list[dict]:
    session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    s3 = session.client("s3")
    paginator = s3.get_paginator("list_objects_v2")

    media = []
    total = 0
    log.info("Listing objects in s3://%s/%s ...", bucket, prefix)

    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            total += 1
            key: str = obj["Key"]
            ext = key.rsplit(".", 1)[-1].lower() if "." in key else ""
            if ext in VIDEO_EXTENSIONS:
                media_type = "video"
            elif ext in AUDIO_EXTENSIONS:
                media_type = "audio"
            else:
                continue
            top_prefix = key.split("/")[0] if "/" in key else ""
            media.append({
                "s3_key": key,
                "size_bytes": obj["Size"],
                "last_modified": obj["LastModified"].isoformat(),
                "extension": ext,
                "top_prefix": top_prefix,
                "media_type": media_type,
            })

    video_count = sum(1 for m in media if m["media_type"] == "video")
    audio_count = sum(1 for m in media if m["media_type"] == "audio")
    log.info(
        "Scanned %d objects — found %d video, %d audio files.",
        total, video_count, audio_count,
    )
    return media


def save_inventory(con: duckdb.DuckDBPyConnection, media: list[dict]) -> None:
    if not media:
        return
    con.executemany(
        """
        INSERT OR REPLACE INTO media_files
            (s3_key, size_bytes, last_modified, extension, top_prefix, media_type)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [(m["s3_key"], m["size_bytes"], m["last_modified"],
          m["extension"], m["top_prefix"], m["media_type"])
         for m in media],
    )
    log.info("Saved %d media file records to database.", len(media))


# ---------------------------------------------------------------------------
# Phase 2 — metadata via ffprobe (+ optional mediainfo)
# ---------------------------------------------------------------------------

def _s3_client(profile: str | None):
    """S3 client with SigV4 forced — required for presigned URLs with STS credentials."""
    from botocore.config import Config
    session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    return session.client("s3", config=Config(signature_version="s3v4"))


def _check_aws_auth(profile: str | None) -> None:
    """Fail fast with a clear message if AWS credentials are missing or expired."""
    try:
        session = boto3.Session(profile_name=profile) if profile else boto3.Session()
        identity = session.client("sts").get_caller_identity()
        log.info("AWS auth OK — account=%s  arn=%s", identity["Account"], identity["Arn"])
    except Exception as exc:
        msg = str(exc)
        if "ExpiredToken" in msg or "expired" in msg.lower():
            hint = "credentials have expired — refresh with: aws sso login"
        elif "NoCredentialProviders" in msg or "Unable to locate credentials" in msg:
            hint = "no credentials found — run: aws configure  or set AWS_ACCESS_KEY_ID"
        else:
            hint = f"run: aws sts get-caller-identity  to debug ({msg})"
        raise SystemExit(f"ERROR: AWS auth failed — {hint}") from None


def presign(s3_client, bucket: str, key: str) -> str:
    return s3_client.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=PRESIGN_TTL,
    )


def run_ffprobe(url: str) -> dict:
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_streams", "-show_format",
        url,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "ffprobe non-zero exit")
    return json.loads(result.stdout)


def run_mediainfo(url: str) -> dict:
    """Return mediainfo General/Video track dict, or {} on failure."""
    cmd = ["mediainfo", "--Output=JSON", url]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return {}
        data = json.loads(result.stdout)
        tracks = data.get("media", {}).get("track", [])
        return {t["@type"]: t for t in tracks}
    except Exception:
        return {}


def _detect_dolby_vision(video_stream: dict) -> tuple[bool, int | None, int | None]:
    """Return (dolby_vision, dv_profile, dv_level) from ffprobe video stream."""
    dovi = next(
        (sd for sd in video_stream.get("side_data_list", [])
         if sd.get("type") == "DOVI configuration record"),
        None,
    )
    if dovi:
        return True, dovi.get("dv_profile"), dovi.get("dv_level")
    return False, None, None


def _detect_hdr_format(
    dolby_vision: bool,
    color_transfer: str | None,
    mediainfo_video: dict,
) -> str:
    # mediainfo gives cleaner HDR_Format strings when available
    if mi_hdr := mediainfo_video.get("HDR_Format"):
        if "Dolby Vision" in mi_hdr:
            return "Dolby Vision"
        if "HDR10+" in mi_hdr:
            return "HDR10+"
        if "HDR10" in mi_hdr:
            return "HDR10"
        if "HLG" in mi_hdr:
            return "HLG"
    if dolby_vision:
        return "Dolby Vision"
    if color_transfer == "smpte2084":
        return "HDR10"
    if color_transfer == "arib-std-b67":
        return "HLG"
    return "SDR"


def _detect_dolby_atmos(audio_stream: dict) -> bool:
    """Heuristic: TrueHD always carries Atmos in modern delivery; EAC3 with JOC flag."""
    codec = audio_stream.get("codec_name", "")
    if codec == "truehd":
        return True
    # EAC3 Joint Object Coding (Atmos) exposes dmix_mode in ffprobe stream tags
    if codec == "eac3" and audio_stream.get("dmix_mode") is not None:
        return True
    return False


def _parse_audio_tracks(streams: list[dict]) -> list[dict]:
    """Return a row dict for every audio stream in the ffprobe streams list."""
    tracks = []
    for i, stream in enumerate(s for s in streams if s.get("codec_type") == "audio"):
        codec = stream.get("codec_name")
        tracks.append({
            "track_index": i,
            "codec": codec,
            "channels": stream.get("channels"),
            "sample_rate": int(stream["sample_rate"]) if stream.get("sample_rate") else None,
            "language": stream.get("tags", {}).get("language"),
            "dolby_atmos": _detect_dolby_atmos(stream),
            "dolby_codec_family": DOLBY_CODEC_FAMILIES.get(codec) if codec else None,
        })
    return tracks


def parse_ffprobe(data: dict, mediainfo_tracks: dict | None = None) -> dict:
    mi_video = (mediainfo_tracks or {}).get("Video", {})

    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    fmt = data.get("format", {})

    # fps
    fps = None
    r_frame_rate = video.get("r_frame_rate", "")
    if "/" in r_frame_rate:
        num, den = r_frame_rate.split("/")
        val = round(int(num) / int(den), 3) if int(den) else None
        fps = val if val and val >= 1 else None

    # bitrate
    bitrate = fmt.get("bit_rate") or video.get("bit_rate")

    # scan type
    field_order = video.get("field_order", "")
    if field_order in ("tt", "bb", "tb", "bt"):
        scan_type = "interlaced"
    elif field_order == "progressive":
        scan_type = "progressive"
    else:
        scan_type = None

    # Dolby Vision
    dolby_vision, dv_profile, dv_level = _detect_dolby_vision(video)

    # color
    color_transfer = video.get("color_transfer")
    color_primaries = video.get("color_primaries")
    color_space = video.get("color_space")
    hdr_format = _detect_hdr_format(dolby_vision, color_transfer, mi_video)

    # all audio tracks
    audio_tracks = _parse_audio_tracks(streams)
    primary = max(audio_tracks, key=lambda t: t.get("channels") or 0) if audio_tracks else {}

    return {
        "duration_s": float(fmt["duration"]) if fmt.get("duration") else None,
        "format_name": fmt.get("format_name"),
        "width": video.get("width"),
        "height": video.get("height"),
        "fps": fps,
        "video_codec": video.get("codec_name"),
        "video_bitrate": int(bitrate) // 1000 if bitrate else None,
        "scan_type": scan_type,
        "color_primaries": color_primaries,
        "color_transfer": color_transfer,
        "color_space": color_space,
        "hdr_format": hdr_format,
        "dolby_vision": dolby_vision,
        "dv_profile": dv_profile,
        "dv_level": dv_level,
        # primary track summary (kept for simple queries)
        "audio_codec": primary.get("codec"),
        "audio_channels": primary.get("channels"),
        "audio_sample_rate": primary.get("sample_rate"),
        "dolby_atmos": primary.get("dolby_atmos", False),
        "dolby_codec_family": primary.get("dolby_codec_family"),
        # full track list written to audio_tracks table
        "audio_tracks": audio_tracks,
        "error": None,
    }


_EMPTY_META: dict = {
    **{k: None for k in (
        "duration_s", "format_name", "width", "height", "fps",
        "video_codec", "video_bitrate", "scan_type",
        "color_primaries", "color_transfer", "color_space", "hdr_format",
        "dolby_vision", "dv_profile", "dv_level",
        "audio_codec", "audio_channels", "audio_sample_rate",
        "dolby_atmos", "dolby_codec_family",
    )},
    "audio_tracks": [],
}


def probe_one(s3_client, bucket: str, key: str) -> tuple[str, dict]:
    try:
        url = presign(s3_client, bucket, key)
        ffprobe_data = run_ffprobe(url)
        mediainfo_tracks = run_mediainfo(url) if MEDIAINFO_AVAILABLE else None
        meta = parse_ffprobe(ffprobe_data, mediainfo_tracks)
    except Exception as exc:
        meta = dict(_EMPTY_META)
        meta["error"] = str(exc)[:500]
    return key, meta


def run_metadata_phase(
    con: duckdb.DuckDBPyConnection,
    bucket: str,
    profile: str | None,
    workers: int,
    limit: int | None,
) -> None:
    s3 = _s3_client(profile)

    keys = [r[0] for r in con.execute("""
        SELECT s3_key FROM media_files
        WHERE s3_key NOT IN (SELECT s3_key FROM media_metadata)
    """).fetchall()]
    if limit:
        keys = keys[:limit]

    if MEDIAINFO_AVAILABLE:
        log.info("mediainfo found — will enrich HDR metadata.")
    else:
        log.info("mediainfo not found — using ffprobe only.")

    log.info("Running ffprobe on %d files (%d workers) ...", len(keys), workers)

    meta_batch: list[tuple] = []
    track_batch: list[tuple] = []

    def flush() -> None:
        if meta_batch:
            con.executemany(
                """
                INSERT OR REPLACE INTO media_metadata (
                    s3_key, duration_s, format_name,
                    width, height, fps, video_codec, video_bitrate, scan_type,
                    color_primaries, color_transfer, color_space, hdr_format,
                    dolby_vision, dv_profile, dv_level,
                    audio_codec, audio_channels, audio_sample_rate,
                    dolby_atmos, dolby_codec_family, error
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                meta_batch,
            )
            meta_batch.clear()
        if track_batch:
            con.executemany(
                """
                INSERT OR REPLACE INTO audio_tracks
                    (s3_key, track_index, codec, channels, sample_rate,
                     language, dolby_atmos, dolby_codec_family)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                track_batch,
            )
            track_batch.clear()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(probe_one, s3, bucket, key): key for key in keys}
        for i, future in enumerate(as_completed(futures), 1):
            key, m = future.result()
            meta_batch.append((
                key,
                m["duration_s"], m["format_name"],
                m["width"], m["height"], m["fps"],
                m["video_codec"], m["video_bitrate"], m["scan_type"],
                m["color_primaries"], m["color_transfer"], m["color_space"], m["hdr_format"],
                m["dolby_vision"], m["dv_profile"], m["dv_level"],
                m["audio_codec"], m["audio_channels"], m["audio_sample_rate"],
                m["dolby_atmos"], m["dolby_codec_family"], m["error"],
            ))
            for t in m["audio_tracks"]:
                track_batch.append((
                    key, t["track_index"], t["codec"], t["channels"],
                    t["sample_rate"], t["language"], t["dolby_atmos"], t["dolby_codec_family"],
                ))
            if i % 50 == 0 or m["error"]:
                status = "ERR" if m["error"] else "OK"
                log.info("[%d/%d] %s %s", i, len(keys), status, key)
            if len(meta_batch) >= 100:
                flush()

    flush()
    log.info("Metadata phase complete.")


# ---------------------------------------------------------------------------
# Phase 3 — vision analysis via Ollama
# ---------------------------------------------------------------------------

OLLAMA_HOST         = os.environ.get("OLLAMA_HOST",         "http://localhost:11434")
OLLAMA_VISION_MODEL = os.environ.get("OLLAMA_VISION_MODEL", "moondream")
OLLAMA_SQL_MODEL    = os.environ.get("OLLAMA_SQL_MODEL",    "llama3.2")
DOCKER_IMAGE        = os.environ.get("DOCKER_IMAGE",        "content-catalogue")

ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY")
CLAUDE_VISION_MODEL = os.environ.get("CLAUDE_VISION_MODEL", "claude-haiku-4-5-20251001")

HASH_MATCH_THRESHOLD = 10  # max dHash bit distance (out of 64) to consider files same-source

_VISION_PROMPT = """\
Describe what you see in these video frames. Include: what is happening, \
whether there is any text or credits visible, how bright or dark the image is, \
and what kind of content this appears to be (e.g. movie, TV show, sports, documentary, \
animation, commercial). Be concise and factual."""

_STRUCTURE_PROMPT = """\
Given this description of video frames, respond with ONLY a JSON object — no markdown.

Description: {description}
Filename: {filename}

{{
  "description": "2-3 sentence summary",
  "style": "live_action or animated or cgi or mixed",
  "has_credits": true or false,
  "brightness": "bright or normal or dark or mixed",
  "genre_tags": ["tag1", "tag2"]
}}

For genre_tags pick 1-5 from: action, drama, comedy, documentary, sports, news, \
animation, nature, music, commercial, trailer, educational, gaming, film."""

_CLAUDE_PROMPT = """\
Analyse these video frames and respond with ONLY a JSON object — no markdown fences.
Filename: {filename}

{{
  "description": "2-3 sentence summary",
  "style": "live_action or animated or cgi or mixed",
  "has_credits": true or false,
  "brightness": "bright or normal or dark or mixed",
  "genre_tags": ["tag1", "tag2"]
}}

For genre_tags pick 1-5 from: action, drama, comedy, documentary, sports, news, \
animation, nature, music, commercial, trailer, educational, gaming, film."""


def _extract_json(text: str) -> dict:
    """Pull a JSON object out of model output that may contain surrounding text."""
    if "```" in text:
        for block in text.split("```")[1::2]:
            candidate = block.lstrip("json\n").strip()
            if candidate.startswith("{"):
                text = candidate
                break
    start, end = text.find("{"), text.rfind("}") + 1
    if start >= 0 and end > start:
        return json.loads(text[start:end])
    raise ValueError(f"No JSON object in model output: {text[:300]!r}")


def extract_frame_base64(url: str, offset_s: float) -> str | None:
    """Extract one JPEG frame at offset_s seconds from a URL, return base64 or None."""
    cmd = [
        "docker", "run", "--rm", "--entrypoint", "ffmpeg",
        DOCKER_IMAGE,
        "-v", "quiet",
        "-ss", str(int(offset_s)),
        "-i", url,
        "-frames:v", "1",
        "-f", "image2pipe",
        "-vcodec", "mjpeg",
        "-q:v", "5",
        "-vf", "scale=480:-1",
        "pipe:1",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=60)
        if r.returncode != 0 or not r.stdout:
            log.warning("ffmpeg failed (rc=%d): %s", r.returncode, r.stderr.decode(errors="replace")[-500:])
            return None
        return base64.b64encode(r.stdout).decode()
    except FileNotFoundError:
        raise RuntimeError(
            "docker not found — ensure Docker is running and 'docker' is on PATH"
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("ffmpeg timed out after 60s extracting frame")
    except Exception as exc:
        log.warning("Frame extraction error (%s): %s", type(exc).__name__, exc)
        return None


def analyze_frames(frames_b64: list[str], filename: str) -> dict:
    """Classify video frames. Uses Claude when ANTHROPIC_API_KEY is set, else Ollama."""
    if ANTHROPIC_API_KEY:
        return _analyze_frames_claude(frames_b64, filename)
    return _analyze_frames_ollama(frames_b64, filename)


def _analyze_frames_claude(frames_b64: list[str], filename: str) -> dict:
    try:
        import anthropic
    except ImportError:
        raise RuntimeError("pip install anthropic")
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": f}}
        for f in frames_b64
    ]
    content.append({"type": "text", "text": _CLAUDE_PROMPT.format(filename=filename)})
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model=CLAUDE_VISION_MODEL,
        max_tokens=512,
        messages=[{"role": "user", "content": content}],
    )
    return _extract_json(resp.content[0].text)


def _analyze_frames_ollama(frames_b64: list[str], filename: str) -> dict:
    try:
        import ollama
    except ImportError:
        raise RuntimeError("pip install ollama")

    client = ollama.Client(host=OLLAMA_HOST)

    # Step 1: vision model — describe what's in the frames
    vision_resp = client.chat(
        model=OLLAMA_VISION_MODEL,
        messages=[{
            "role": "user",
            "content": _VISION_PROMPT,
            "images": frames_b64,
        }],
    )
    description = vision_resp.message.content.strip()

    # Step 2: text model — extract structured JSON from the description
    struct_resp = client.chat(
        model=OLLAMA_SQL_MODEL,
        messages=[{
            "role": "user",
            "content": _STRUCTURE_PROMPT.format(
                description=description,
                filename=filename,
            ),
        }],
        format="json",
    )
    return _extract_json(struct_resp.message.content)


def already_vision_analyzed(con: duckdb.DuckDBPyConnection) -> set[str]:
    return {r[0] for r in con.execute("SELECT s3_key FROM content_vision").fetchall()}


def vision_one(
    s3_client, bucket: str, key: str, duration_s: float | None
) -> tuple[str, dict]:
    try:
        url = presign(s3_client, bucket, key)
        if duration_s and duration_s > 20:
            offsets = [duration_s * p for p in (0.10, 0.30, 0.55, 0.80, 0.92)]
        else:
            offsets = [2.0, 5.0, 10.0, 20.0, 30.0]

        frames = [f for offset in offsets if (f := extract_frame_base64(url, offset))]
        if not frames:
            raise RuntimeError("No frames extracted")
        result = analyze_frames(frames, key.rsplit("/", 1)[-1])
        return key, result
    except Exception as exc:
        return key, {"_error": str(exc)[:500]}


def _compute_dhash(frame_b64: str):
    """Return a perceptual dHash for a base64-encoded JPEG frame, or None on error."""
    try:
        import imagehash
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(base64.b64decode(frame_b64)))
        return imagehash.dhash(img)
    except Exception:
        return None


def _confirm_with_hash(
    items: list[tuple],
    s3_client,
    bucket: str,
    dur_s: float,
) -> list[list[tuple]]:
    """Extract one mid-point frame per item, compute dHash, and decide grouping.

    Returns a list of sub-groups:
    - Single list with all items if max pairwise dHash distance ≤ HASH_MATCH_THRESHOLD.
    - Multiple lists split by parent directory otherwise.
    """
    from collections import defaultdict

    frames: dict[str, str] = {}
    for key, dur, w, h, sz in items:
        url = presign(s3_client, bucket, key)
        offset = (dur if dur else dur_s) * 0.5
        frame = extract_frame_base64(url, offset)
        if frame:
            frames[key] = frame

    hashes: dict[str, object] = {}
    for k, f in frames.items():
        h = _compute_dhash(f)
        if h is not None:
            hashes[k] = h

    if len(hashes) >= 2:
        keys = list(hashes)
        max_dist = 0
        for i, a in enumerate(keys):
            for b in keys[i + 1:]:
                max_dist = max(max_dist, hashes[a] - hashes[b])
                if max_dist > HASH_MATCH_THRESHOLD:
                    break
            if max_dist > HASH_MATCH_THRESHOLD:
                break
        if max_dist <= HASH_MATCH_THRESHOLD:
            log.info("Cross-dir hash match (dist=%d): %d files → 1 group", max_dist, len(items))
            return [items]
        log.info("Cross-dir hash mismatch (dist=%d): keeping separate", max_dist)

    by_dir: dict[str, list] = defaultdict(list)
    for item in items:
        parent = item[0].rsplit("/", 1)[0] if "/" in item[0] else ""
        by_dir[parent].append(item)
    return list(by_dir.values())


def _common_filename_prefix(keys: list[str]) -> str:
    """Common prefix of bare filenames (no extension, no path), trimmed to last word boundary."""
    stems = [os.path.basename(k).rsplit(".", 1)[0] for k in keys]
    prefix = os.path.commonprefix(stems)
    for i in range(len(prefix) - 1, -1, -1):
        if prefix[i] in ("_", "-") or (i > 0 and prefix[i].isdigit() and not prefix[i - 1].isdigit()):
            return prefix[:i]
    return prefix


def _group_by_content(
    rows: list[tuple],
    s3_client=None,
    bucket: str | None = None,
) -> list[tuple[str, float | None, list[str]]]:
    """Group video files by content to detect encoding variants.

    Tier 1 (same directory, same duration): no hash check needed.
    Tier 2 (different directories, same content scope + duration): confirmed by dHash.

    content_scope = first 2 path segments (e.g. "analysis/content_database").

    Returns list of (representative_key, duration_s, [variant_keys]).
    The representative is the highest-resolution file in the group (falls back to
    largest file size). Files without duration metadata are kept as solo groups.
    """
    from collections import defaultdict

    def _content_scope(key: str) -> str:
        parts = key.split("/")
        return "/".join(parts[:2]) if len(parts) >= 2 else parts[0]

    scope_buckets: dict[tuple, list] = defaultdict(list)
    for key, dur, w, h, sz in rows:
        dur_key = round(dur) if dur is not None else None
        scope_buckets[(_content_scope(key), dur_key)].append((key, dur, w, h, sz))

    groups = []
    for (scope, dur_key), items in scope_buckets.items():
        dirs = {item[0].rsplit("/", 1)[0] if "/" in item[0] else "" for item in items}

        if len(dirs) <= 1:
            # Tier 1: same directory — only group if filenames share a common prefix,
            # otherwise same-duration coincidences (e.g. intro.mp4 + feature.mp4)
            # would be wrongly treated as variants.
            if len(items) > 1:
                prefix = _common_filename_prefix([item[0] for item in items])
                if len(prefix) < 4:
                    for item in items:
                        groups.append((item[0], item[1], []))
                    continue
            items.sort(key=lambda x: ((x[2] or 0) * (x[3] or 0), x[4] or 0), reverse=True)
            groups.append((items[0][0], items[0][1], [x[0] for x in items[1:]]))
        else:
            # Tier 1.5: cross-directory but shared filename prefix → same content, no hash
            prefix = _common_filename_prefix([item[0] for item in items])
            if len(prefix) >= 4:
                log.info("Cross-dir prefix match (%r): %d files → 1 group", prefix, len(items))
                items.sort(key=lambda x: ((x[2] or 0) * (x[3] or 0), x[4] or 0), reverse=True)
                groups.append((items[0][0], items[0][1], [x[0] for x in items[1:]]))
                continue

            # Tier 2: files across multiple directories — confirm with perceptual hash
            if s3_client and bucket and dur_key is not None:
                sub_groups = _confirm_with_hash(items, s3_client, bucket, dur_key)
            else:
                sub_groups_dict: dict[str, list] = defaultdict(list)
                for item in items:
                    parent = item[0].rsplit("/", 1)[0] if "/" in item[0] else ""
                    sub_groups_dict[parent].append(item)
                sub_groups = list(sub_groups_dict.values())

            for sg in sub_groups:
                sg.sort(key=lambda x: ((x[2] or 0) * (x[3] or 0), x[4] or 0), reverse=True)
                groups.append((sg[0][0], sg[0][1], [x[0] for x in sg[1:]]))

    return groups


def run_vision_phase(
    con: duckdb.DuckDBPyConnection,
    bucket: str,
    profile: str | None,
    workers: int,
    limit: int | None,
    no_dedup: bool = False,
    retry_errors: bool = False,
    hash_dedup: bool = False,
) -> None:
    s3 = _s3_client(profile)

    if retry_errors:
        deleted = con.execute(
            "DELETE FROM content_vision WHERE description LIKE '[error:%' RETURNING s3_key"
        ).fetchall()
        if deleted:
            log.info("Cleared %d error sentinel row(s) — will re-analyse.", len(deleted))

    rows = con.execute("""
        SELECT f.s3_key, m.duration_s, m.width, m.height, f.size_bytes
        FROM media_files f
        LEFT JOIN media_metadata m USING (s3_key)
        WHERE f.media_type = 'video'
    """).fetchall()

    done = already_vision_analyzed(con)
    pending_rows = [row for row in rows if row[0] not in done]

    if no_dedup:
        all_groups = [(key, dur, []) for key, dur, w, h, sz in pending_rows]
    else:
        if pending_rows:
            log.info("Grouping %d pending video files for deduplication ...", len(pending_rows))
        s3_arg = s3 if hash_dedup else None
        bucket_arg = bucket if hash_dedup else None
        all_groups = _group_by_content(pending_rows, s3_client=s3_arg, bucket=bucket_arg)

    todo_groups = all_groups[:limit] if limit else all_groups

    total_files = sum(1 + len(v) for rep, dur, v in todo_groups)
    log.info(
        "Vision analysis: %d groups (%d files total), %d workers",
        len(todo_groups), total_files, workers,
    )

    batch: list[tuple] = []

    def flush() -> None:
        if batch:
            con.executemany(
                """
                INSERT OR REPLACE INTO content_vision
                    (s3_key, description, style, has_credits, brightness,
                     genre_tags, analyzed_at, source_key)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                batch,
            )
            batch.clear()

    now = datetime.now(timezone.utc).isoformat()
    # map future → variant keys so we can copy results on completion
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(vision_one, s3, bucket, rep, dur): (rep, variants)
            for rep, dur, variants in todo_groups
        }
        for i, future in enumerate(as_completed(futures), 1):
            rep_key, variants = futures[future]
            key, result = future.result()
            if "_error" in result:
                err = result["_error"]
                log.warning("[%d/%d] ERR %s: %s", i, len(todo_groups), key, err)
                batch.append((key, f"[error: {err}]", None, None, None, [], now, None))
                continue

            row = (
                result.get("description"),
                result.get("style"),
                result.get("has_credits"),
                result.get("brightness"),
                result.get("genre_tags") or [],
                now,
            )
            batch.append((key, *row, None))           # source_key=None (directly analysed)
            for vkey in variants:
                batch.append((vkey, *row, key))        # source_key=representative

            variant_note = f"  (+{len(variants)} variants)" if variants else ""
            log.info(
                "[%d/%d] OK  style=%-11s credits=%-5s brightness=%-6s  %s%s",
                i, len(todo_groups),
                result.get("style", "?"),
                result.get("has_credits"),
                result.get("brightness", "?"),
                key.rsplit("/", 1)[-1],
                variant_note,
            )
            if len(batch) >= 10:
                flush()

    flush()
    log.info("Vision phase complete.")


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_summary(con: duckdb.DuckDBPyConnection) -> None:
    print("\n=== Inventory by Type ===")
    print(con.execute("""
        SELECT media_type, extension, count(*) AS files,
               round(sum(size_bytes)/1e9, 2) AS total_gb
        FROM media_files
        GROUP BY media_type, extension
        ORDER BY media_type, files DESC
    """).df().to_string(index=False))

    print("\n=== Top Prefixes ===")
    print(con.execute("""
        SELECT top_prefix, media_type, count(*) AS files,
               round(sum(size_bytes)/1e9, 2) AS total_gb
        FROM media_files
        GROUP BY top_prefix, media_type
        ORDER BY files DESC LIMIT 20
    """).df().to_string(index=False))

    probed = con.execute("SELECT count(*) FROM media_metadata WHERE error IS NULL").fetchone()[0]
    errors = con.execute("SELECT count(*) FROM media_metadata WHERE error IS NOT NULL").fetchone()[0]
    if probed + errors == 0:
        return

    print(f"\n=== Metadata ({probed} probed, {errors} errors) ===")

    print("\n-- Resolutions (video) --")
    print(con.execute("""
        SELECT width || 'x' || height AS resolution,
               round(avg(fps), 2) AS avg_fps,
               count(*) AS files
        FROM media_metadata
        WHERE width IS NOT NULL AND error IS NULL
        GROUP BY width, height
        ORDER BY files DESC LIMIT 10
    """).df().to_string(index=False))

    print("\n-- HDR Format --")
    print(con.execute("""
        SELECT hdr_format, count(*) AS files
        FROM media_metadata
        WHERE hdr_format IS NOT NULL AND error IS NULL
        GROUP BY hdr_format ORDER BY files DESC
    """).df().to_string(index=False))

    print("\n-- Dolby Vision profiles --")
    print(con.execute("""
        SELECT dv_profile, dv_level, count(*) AS files
        FROM media_metadata
        WHERE dolby_vision = true
        GROUP BY dv_profile, dv_level ORDER BY files DESC
    """).df().to_string(index=False))

    print("\n-- Dolby Audio --")
    print(con.execute("""
        SELECT dolby_codec_family,
               sum(CASE WHEN dolby_atmos THEN 1 ELSE 0 END) AS atmos_files,
               count(*) AS total_files
        FROM media_metadata
        WHERE dolby_codec_family IS NOT NULL AND error IS NULL
        GROUP BY dolby_codec_family ORDER BY total_files DESC
    """).df().to_string(index=False))

    track_count = con.execute("SELECT count(*) FROM audio_tracks").fetchone()[0]
    if track_count > 0:
        print("\n-- Multi-track files --")
        print(con.execute("""
            SELECT track_count, count(*) AS files
            FROM (
                SELECT s3_key, count(*) AS track_count
                FROM audio_tracks GROUP BY s3_key
            )
            GROUP BY track_count ORDER BY track_count
        """).df().to_string(index=False))

        print("\n-- Audio track languages --")
        print(con.execute("""
            SELECT coalesce(language, 'unknown') AS language,
                   codec, count(*) AS tracks
            FROM audio_tracks
            GROUP BY language, codec ORDER BY tracks DESC LIMIT 15
        """).df().to_string(index=False))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Catalogue S3 media content")
    parser.add_argument("--bucket", default="bitmovin-api-eu-west1-ci-input")
    parser.add_argument("--prefix", default="", help="Limit to a specific S3 prefix")
    parser.add_argument("--db", default="content_catalogue.duckdb")
    parser.add_argument("--profile", default=None, help="AWS profile name")
    parser.add_argument("--workers", type=int, default=20, help="Parallel ffprobe workers")
    parser.add_argument(
        "--phase",
        choices=["inventory", "metadata", "vision", "both", "summary"],
        default="both",
    )
    parser.add_argument("--limit", type=int, default=None,
                        help="Cap files to probe (useful for testing)")
    parser.add_argument("--no-dedup", action="store_true",
                        help="Skip variant grouping and classify every video file individually")
    parser.add_argument("--retry-errors", action="store_true",
                        help="Clear error sentinel rows before running vision so failed files are re-analysed")
    parser.add_argument("--hash-dedup", action="store_true",
                        help="Use perceptual hash (dHash) to confirm cross-directory variant grouping "
                             "(slower — runs docker ffmpeg per file; omit for large catalogues)")
    parser.add_argument("--vision-workers", type=int, default=1,
                        help="Parallel workers for vision phase (default 1; increase on GPU hosts)")
    args = parser.parse_args()

    con = init_db(args.db)

    if args.phase in ("inventory", "metadata", "vision", "both"):
        _check_aws_auth(args.profile)

    if args.phase in ("inventory", "both"):
        media = list_media_files(args.bucket, args.prefix, args.profile)
        save_inventory(con, media)

    if args.phase in ("metadata", "both"):
        run_metadata_phase(con, args.bucket, args.profile, args.workers, args.limit)

    if args.phase == "vision":
        run_vision_phase(con, args.bucket, args.profile, args.vision_workers, args.limit,
                         no_dedup=args.no_dedup, retry_errors=args.retry_errors,
                         hash_dedup=args.hash_dedup)

    if args.phase in ("summary", "both"):
        print_summary(con)

    con.close()


if __name__ == "__main__":
    main()
