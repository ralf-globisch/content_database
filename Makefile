IMAGE   := content-catalogue
DB_DIR  := $(CURDIR)/data
DB_FILE := /data/content_catalogue.duckdb
BUCKET  := bitmovin-api-eu-west1-ci-input

# AWS credentials — mounts ~/.aws so all credential types work (keys, assumed roles, SSO)
# Override the profile with: export AWS_PROFILE=my-profile
AWS_FLAGS := -v "$(HOME)/.aws:/root/.aws:ro" \
	-e AWS_DEFAULT_REGION=$(or $(AWS_DEFAULT_REGION),eu-west-1)
ifdef AWS_PROFILE
AWS_FLAGS += -e AWS_PROFILE=$(AWS_PROFILE)
PROFILE_ARG := --profile $(AWS_PROFILE)
else
PROFILE_ARG :=
endif

# Ollama — vision runs on host directly; UI passes these through for NL search in the browser
OLLAMA_HOST         ?= http://localhost:11434
OLLAMA_VISION_MODEL ?= moondream   # CPU default; override with llava for GPU hosts
OLLAMA_SQL_MODEL    ?= llama3.2
OLLAMA_FLAGS := -e OLLAMA_HOST=$(OLLAMA_HOST) \
	-e OLLAMA_VISION_MODEL=$(OLLAMA_VISION_MODEL) \
	-e OLLAMA_SQL_MODEL=$(OLLAMA_SQL_MODEL)

# Claude — used when ANTHROPIC_API_KEY is set; takes precedence over Ollama
CLAUDE_VISION_MODEL ?= claude-haiku-4-5-20251001
CLAUDE_SQL_MODEL    ?= claude-haiku-4-5-20251001
# ANTHROPIC_API_KEY is inherited from the shell environment — set it with:
#   export ANTHROPIC_API_KEY=sk-ant-...
CLAUDE_FLAGS := -e CLAUDE_SQL_MODEL=$(CLAUDE_SQL_MODEL) \
	$(if $(ANTHROPIC_API_KEY),-e ANTHROPIC_API_KEY=$(ANTHROPIC_API_KEY),)

# Python interpreter for the vision phase (runs on host, not in Docker)
PYTHON ?= python3

DOCKER_RUN := docker run --rm \
	$(AWS_FLAGS) \
	-v "$(DB_DIR):/data" \
	$(IMAGE)

.PHONY: build inventory metadata vision both summary query shell ui ui-local help check-auth

check-auth:
	@aws sts get-caller-identity > /dev/null || \
		{ echo ""; \
		  echo "ERROR: AWS credentials are missing or expired."; \
		  echo "  For SSO:         aws sso login"; \
		  echo "  For access keys: aws configure"; \
		  echo "  For env vars:    export AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=..."; \
		  echo ""; exit 1; }

## Build the Docker image
build:
	docker build -t $(IMAGE) .

## Phase 1 — list all video and audio files in the bucket
inventory: check-auth $(DB_DIR)
	$(DOCKER_RUN) --phase inventory --bucket $(BUCKET) --db $(DB_FILE) $(PROFILE_ARG) $(ARGS)

## Phase 2 — extract metadata via ffprobe (resumes from where it left off)
metadata: check-auth $(DB_DIR)
	$(DOCKER_RUN) --phase metadata --bucket $(BUCKET) --db $(DB_FILE) $(PROFILE_ARG) $(ARGS)

## Phase 3 — analyse video frames with Ollama (runs on host, not Docker)
## Requires: pip install ollama boto3 duckdb  +  ollama pull moondream llama3.2
## Override models:  OLLAMA_VISION_MODEL=llava make vision  (e.g. on GPU hosts)
## Override workers: ARGS="--vision-workers 4" make vision  (GPU hosts only)
## Override python:  PYTHON=/path/to/venv/bin/python make vision
vision: check-auth $(DB_DIR)
	OLLAMA_HOST=$(OLLAMA_HOST) \
	OLLAMA_VISION_MODEL=$(OLLAMA_VISION_MODEL) \
	OLLAMA_SQL_MODEL=$(OLLAMA_SQL_MODEL) \
	CLAUDE_VISION_MODEL=$(CLAUDE_VISION_MODEL) \
	$(if $(ANTHROPIC_API_KEY),ANTHROPIC_API_KEY=$(ANTHROPIC_API_KEY),) \
	DOCKER_IMAGE=$(IMAGE) \
	$(or $(AWS_PROFILE:%=AWS_PROFILE=%),) \
	$(PYTHON) catalogue.py --phase vision --bucket $(BUCKET) --db $(CURDIR)/data/content_catalogue.duckdb $(PROFILE_ARG) $(ARGS)

## Run both phases in sequence
both: check-auth $(DB_DIR)
	$(DOCKER_RUN) --phase both --bucket $(BUCKET) --db $(DB_FILE) $(PROFILE_ARG) $(ARGS)

## Print a summary of what has been collected so far
summary: $(DB_DIR)
	$(DOCKER_RUN) --phase summary --db $(DB_FILE)

## Run a SQL query against the local database
## Usage: make query Q="SELECT count(*) FROM media_files"
query: $(DB_DIR)
ifndef Q
	$(error Usage: make query Q="SELECT ...")
endif
	docker run --rm -v "$(DB_DIR):/data" --entrypoint python $(IMAGE) \
		-c "import duckdb,sys; print(duckdb.connect(sys.argv[1],read_only=True).execute(sys.argv[2]).df().to_string(index=False))" \
		$(DB_FILE) "$(Q)"

## Open an interactive DuckDB shell against the local database
shell: $(DB_DIR)
	docker run --rm -it -v "$(DB_DIR):/data" \
		--entrypoint python $(IMAGE) -m duckdb $(DB_FILE)

## Open the Streamlit query UI (http://localhost:8501) — runs inside Docker
## Natural language search requires Ollama running on host with: ollama pull llama3.2
ui: $(DB_DIR)
	docker run --rm -it -p 8501:8501 \
		$(OLLAMA_FLAGS) \
		$(CLAUDE_FLAGS) \
		-v "$(DB_DIR):/data" \
		--entrypoint streamlit $(IMAGE) \
		run /app/app.py \
		--server.address=0.0.0.0 \
		--server.headless=true

## Open the Streamlit query UI running directly on the host (http://localhost:8501)
## Use this when Ollama is running locally — avoids Docker networking issues
ui-local: $(DB_DIR)
	DB_PATH=$(CURDIR)/data/content_catalogue.duckdb \
	QUERIES_PATH=$(CURDIR)/queries.yaml \
	OLLAMA_HOST=$(OLLAMA_HOST) \
	OLLAMA_SQL_MODEL=$(OLLAMA_SQL_MODEL) \
	CLAUDE_SQL_MODEL=$(CLAUDE_SQL_MODEL) \
	$(if $(ANTHROPIC_API_KEY),ANTHROPIC_API_KEY=$(ANTHROPIC_API_KEY),) \
	streamlit run app.py

$(DB_DIR):
	mkdir -p $(DB_DIR)

help:
	@echo ""
	@echo "Usage:"
	@echo "  make build                          Build the Docker image"
	@echo "  make inventory  [ARGS=...]           Phase 1: list all media files"
	@echo "  make metadata   [ARGS=...]           Phase 2: extract attributes via ffprobe"
	@echo "  make vision     [ARGS=...]           Phase 3: analyse frames with Ollama llava"
	@echo "  make both       [ARGS=...]           Run Phase 1 + 2"
	@echo "  make summary                        Print collected stats"
	@echo "  make query Q=\"<sql>\"                 Run a SQL query on the DB"
	@echo "  make shell                          Open interactive DuckDB shell"
	@echo "  make ui                             Open Streamlit query UI in Docker (http://localhost:8501)"
	@echo "  make ui-local                       Open Streamlit query UI on host (avoids Docker networking)"
	@echo ""
	@echo "AWS credentials (pick one):"
	@echo "  export AWS_PROFILE=my-profile"
	@echo "  export AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... AWS_DEFAULT_REGION=..."
	@echo ""
	@echo "AI backend (pick one or both — Claude takes precedence when key is set):"
	@echo "  export ANTHROPIC_API_KEY=sk-ant-...   # enables Claude backend"
	@echo "  ollama pull moondream                  # Ollama vision model (~800 MB)"
	@echo "  ollama pull llama3.2                   # Ollama SQL/structure model (~2 GB)"
	@echo "  Override Ollama host:  OLLAMA_HOST=http://other-host:11434 make vision"
	@echo "  Override Ollama model: OLLAMA_VISION_MODEL=llava make vision  (GPU hosts)"
	@echo "  Override Claude model: CLAUDE_VISION_MODEL=claude-sonnet-4-6 make vision"
	@echo "  Override python:       PYTHON=/path/to/venv/bin/python make vision"
	@echo ""
	@echo "Optional ARGS examples:"
	@echo "  ARGS=\"--prefix analysis/jan-ozer-per-title-files/\"   scope to a prefix"
	@echo "  ARGS=\"--limit 50\"                                    probe only 50 files"
	@echo "  ARGS=\"--workers 40\"                                  increase parallelism"
	@echo "  ARGS=\"--retry-errors\"                               re-analyse previously failed vision files"
	@echo "  ARGS=\"--vision-workers 4\"                          parallel vision workers (GPU hosts only)"
	@echo ""
