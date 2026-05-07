IMAGE   := content-catalogue
DB_DIR  := $(CURDIR)/data
DB_FILE := /data/content_catalogue.duckdb
BUCKET  := bitmovin-api-eu-west1-ci-input

# AWS credentials — set AWS_PROFILE or AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY/AWS_DEFAULT_REGION
ifdef AWS_PROFILE
AWS_FLAGS := -v "$(HOME)/.aws:/root/.aws:ro" -e AWS_PROFILE=$(AWS_PROFILE)
PROFILE_ARG := --profile $(AWS_PROFILE)
else
AWS_FLAGS := \
	-e AWS_ACCESS_KEY_ID=$(AWS_ACCESS_KEY_ID) \
	-e AWS_SECRET_ACCESS_KEY=$(AWS_SECRET_ACCESS_KEY) \
	-e AWS_DEFAULT_REGION=$(AWS_DEFAULT_REGION)
PROFILE_ARG :=
endif

DOCKER_RUN := docker run --rm \
	$(AWS_FLAGS) \
	-v "$(DB_DIR):/data" \
	$(IMAGE)

.PHONY: build inventory metadata both summary query shell help

## Build the Docker image
build:
	docker build -t $(IMAGE) .

## Phase 1 — list all video and audio files in the bucket
inventory: $(DB_DIR)
	$(DOCKER_RUN) --phase inventory --bucket $(BUCKET) --db $(DB_FILE) $(PROFILE_ARG) $(ARGS)

## Phase 2 — extract metadata via ffprobe (resumes from where it left off)
metadata: $(DB_DIR)
	$(DOCKER_RUN) --phase metadata --bucket $(BUCKET) --db $(DB_FILE) $(PROFILE_ARG) $(ARGS)

## Run both phases in sequence
both: $(DB_DIR)
	$(DOCKER_RUN) --phase both --bucket $(BUCKET) --db $(DB_FILE) $(PROFILE_ARG) $(ARGS)

## Print a summary of what has been collected so far
summary: $(DB_DIR)
	$(DOCKER_RUN) --phase summary --db $(DB_FILE)

## Run a SQL query against the local database
## Usage: make query Q="SELECT count(*) FROM media_files"
query: $(DB_DIR)
	duckdb $(DB_DIR)/content_catalogue.duckdb "$(Q)"

## Open an interactive DuckDB shell against the local database
shell: $(DB_DIR)
	duckdb $(DB_DIR)/content_catalogue.duckdb

$(DB_DIR):
	mkdir -p $(DB_DIR)

help:
	@echo ""
	@echo "Usage:"
	@echo "  make build                          Build the Docker image"
	@echo "  make inventory  [ARGS=...]           Phase 1: list all media files"
	@echo "  make metadata   [ARGS=...]           Phase 2: extract attributes via ffprobe"
	@echo "  make both       [ARGS=...]           Run Phase 1 + 2"
	@echo "  make summary                        Print collected stats"
	@echo "  make query Q=\"<sql>\"                 Run a SQL query on the DB"
	@echo "  make shell                          Open interactive DuckDB shell"
	@echo ""
	@echo "AWS credentials (pick one):"
	@echo "  export AWS_PROFILE=my-profile"
	@echo "  export AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... AWS_DEFAULT_REGION=..."
	@echo ""
	@echo "Optional ARGS examples:"
	@echo "  ARGS=\"--prefix analysis/jan-ozer-per-title-files/\"   scope to a prefix"
	@echo "  ARGS=\"--limit 50\"                                    probe only 50 files"
	@echo "  ARGS=\"--workers 40\"                                  increase parallelism"
	@echo ""
