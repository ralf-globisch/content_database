# S3 Media Content Cataloguer

Inventories video and audio files in an S3 bucket, extracts technical metadata via `ffprobe` (using presigned URLs — no full file downloads), and stores everything in a local [DuckDB](https://duckdb.org/) database for ad-hoc querying.

## What it collects

| Category | Fields |
|---|---|
| File | S3 key, size, last modified, extension, top-level prefix |
| Video | Resolution, fps, codec, bitrate, scan type |
| HDR | Format (SDR / HDR10 / HLG / Dolby Vision), color primaries/transfer/space |
| Dolby Vision | Detected, profile, level |
| Audio (primary track) | Codec, channels, sample rate |
| Audio (all tracks) | Per-track: codec, channels, sample rate, language, Dolby Atmos flag |
| Dolby Audio | Atmos detection, codec family (AC-3 / E-AC-3 / TrueHD / AC-4 / DTS) |

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/)
- AWS credentials with `s3:ListBucket` and `s3:GetObject` on the target bucket
- [DuckDB CLI](https://duckdb.org/docs/installation/) for querying results locally (optional)

## Quick start

```bash
# 1. Build the image
make build

# 2. Set AWS credentials
export AWS_PROFILE=my-profile
# or
export AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... AWS_DEFAULT_REGION=eu-west-1

# 3. Inventory all media files in the bucket (Phase 1)
make inventory

# 4. Extract video/audio attributes via ffprobe (Phase 2)
make metadata

# 5. Print a summary of what was collected
make summary
```

Results are written to `./data/content_catalogue.duckdb`.

## Phased workflow

The two phases are independent and idempotent — Phase 2 skips files already probed, so you can safely interrupt and resume.

### Phase 1 — Inventory

Lists every video and audio file in the bucket and stores file-level info (key, size, date, extension).

```bash
make inventory

# Scope to a specific prefix
make inventory ARGS="--prefix analysis/jan-ozer-per-title-files/"
```

### Phase 2 — Metadata extraction

Generates a presigned URL for each file and runs `ffprobe` against it (reads only the container header — typically under 1 MB per file). Runs 20 workers in parallel by default.

```bash
make metadata

# Test with 50 files first
make metadata ARGS="--limit 50"

# Increase parallelism for large runs
make metadata ARGS="--workers 40"

# Scope to a prefix
make metadata ARGS="--prefix analysis/jan-ozer-per-title-files/"
```

### Run both phases together

```bash
make both
make both ARGS="--prefix my/prefix/ --limit 100"
```

### Summary report

```bash
make summary
```

Prints: file counts by type/extension, top prefixes, resolution distribution, HDR format breakdown, Dolby Vision profiles, Dolby audio codec table, multi-track distribution, and top audio languages.

## Querying the database

```bash
# Interactive shell
make shell

# One-off query
make query Q="SELECT count(*), media_type FROM media_files GROUP BY media_type"

# Or directly with duckdb CLI
duckdb data/content_catalogue.duckdb
```

### Useful queries

```sql
-- 4K files
SELECT f.s3_key, m.width, m.height, m.fps, m.video_codec, m.hdr_format,
       round(m.duration_s/60, 1) AS duration_min
FROM media_metadata m JOIN media_files f USING (s3_key)
WHERE m.width >= 3840
ORDER BY m.width DESC;

-- All Dolby Vision content
SELECT f.s3_key, m.dv_profile, m.dv_level, m.hdr_format, m.audio_codec, m.dolby_atmos
FROM media_metadata m JOIN media_files f USING (s3_key)
WHERE m.dolby_vision = true;

-- All Dolby Atmos files
SELECT s3_key, dolby_codec_family, audio_channels, round(duration_s/60, 1) AS duration_min
FROM media_metadata
WHERE dolby_atmos = true;

-- HDR format breakdown
SELECT hdr_format, count(*) AS files, round(avg(duration_s)/60, 1) AS avg_duration_min
FROM media_metadata
WHERE error IS NULL
GROUP BY hdr_format ORDER BY files DESC;

-- Files with more than one audio track
SELECT s3_key, count(*) AS tracks
FROM audio_tracks
GROUP BY s3_key HAVING count(*) > 1
ORDER BY tracks DESC;

-- Multi-language files
SELECT s3_key, track_index, language, codec, channels
FROM audio_tracks
WHERE language IS NOT NULL
ORDER BY s3_key, track_index;

-- Files with both TrueHD and EAC3 (Atmos + compatibility track)
SELECT s3_key FROM audio_tracks WHERE codec = 'truehd'
INTERSECT
SELECT s3_key FROM audio_tracks WHERE codec = 'eac3';

-- Long-form content (> 60 min) — likely features
SELECT f.s3_key, round(m.duration_s/60, 1) AS duration_min, m.width, m.height, m.video_codec
FROM media_metadata m JOIN media_files f USING (s3_key)
WHERE m.duration_s > 3600
ORDER BY m.duration_s DESC;

-- Audio-only files
SELECT f.s3_key, m.audio_codec, m.audio_channels, m.audio_sample_rate, m.dolby_atmos
FROM media_metadata m JOIN media_files f USING (s3_key)
WHERE f.media_type = 'audio';

-- Resolution distribution
SELECT m.width || 'x' || m.height AS resolution, count(*) AS files,
       round(avg(m.fps), 3) AS avg_fps,
       round(sum(f.size_bytes)/1e9, 1) AS total_gb
FROM media_metadata m JOIN media_files f USING (s3_key)
WHERE m.width IS NOT NULL AND m.error IS NULL
GROUP BY m.width, m.height ORDER BY m.width DESC;

-- Large files over 10 GB
SELECT s3_key, round(size_bytes/1e9, 1) AS size_gb, extension, media_type
FROM media_files
WHERE size_bytes > 10e9
ORDER BY size_bytes DESC;
```

## Database schema

```
media_files        — one row per S3 object (video or audio)
media_metadata     — one row per probed file (primary audio track summary + video + HDR)
audio_tracks       — one row per audio stream per file (multi-track support)
```

## Docker without Make

```bash
# Build
docker build -t content-catalogue .

# Run with AWS profile
docker run --rm \
  -v "$HOME/.aws:/root/.aws:ro" \
  -e AWS_PROFILE=my-profile \
  -v "$PWD/data:/data" \
  content-catalogue \
  --phase both --db /data/content_catalogue.duckdb --profile my-profile

# Run with explicit credentials
docker run --rm \
  -e AWS_ACCESS_KEY_ID=... \
  -e AWS_SECRET_ACCESS_KEY=... \
  -e AWS_DEFAULT_REGION=eu-west-1 \
  -v "$PWD/data:/data" \
  content-catalogue \
  --phase both --db /data/content_catalogue.duckdb
```

## All CLI options

```
--bucket    S3 bucket name (default: bitmovin-api-eu-west1-ci-input)
--prefix    Limit to a specific S3 prefix
--db        Path to DuckDB output file (default: content_catalogue.duckdb)
--profile   AWS profile name
--workers   Parallel ffprobe workers (default: 20)
--phase     inventory | metadata | both | summary
--limit     Cap number of files to probe — useful for testing
```
