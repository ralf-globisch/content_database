#!/usr/bin/env python3
"""
S3 Media Content Cataloguer
Phase 1: Inventory video and audio files in an S3 bucket
Phase 2: Extract media attributes via ffprobe on presigned URLs
         Includes HDR format, Dolby Vision, and Dolby Atmos detection
"""

import argparse
import json
import logging
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

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
        fps = round(int(num) / int(den), 3) if int(den) else None

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
    primary = audio_tracks[0] if audio_tracks else {}

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


def already_probed(con: duckdb.DuckDBPyConnection) -> set[str]:
    return {r[0] for r in con.execute("SELECT s3_key FROM media_metadata").fetchall()}


def run_metadata_phase(
    con: duckdb.DuckDBPyConnection,
    bucket: str,
    profile: str | None,
    workers: int,
    limit: int | None,
) -> None:
    session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    s3 = session.client("s3")

    done = already_probed(con)
    keys = [r[0] for r in con.execute("SELECT s3_key FROM media_files").fetchall()
            if r[0] not in done]
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
        "--phase", choices=["inventory", "metadata", "both", "summary"],
        default="both",
    )
    parser.add_argument("--limit", type=int, default=None,
                        help="Cap files to probe (useful for testing)")
    args = parser.parse_args()

    con = init_db(args.db)

    if args.phase in ("inventory", "both"):
        media = list_media_files(args.bucket, args.prefix, args.profile)
        save_inventory(con, media)

    if args.phase in ("metadata", "both"):
        run_metadata_phase(con, args.bucket, args.profile, args.workers, args.limit)

    if args.phase in ("summary", "both"):
        print_summary(con)

    con.close()


if __name__ == "__main__":
    main()
