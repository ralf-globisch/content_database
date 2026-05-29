IMAGE   := content-catalogue
DB_DIR  := $(CURDIR)/data
DB_FILE := /data/content_catalogue.duckdb
BUCKET  := bitmovin-api-eu-west1-ci-input

# AWS credentials — mounts ~/.aws so all credential types work (keys, assumed roles, SSO)
# For local runs, set the profile in your shell before running make:
#   export AWS_PROFILE=enc-dev
# On EC2, no profile is needed — the instance IAM role is used automatically.
AWS_FLAGS := -v "$(HOME)/.aws:/root/.aws:ro" \
	-e AWS_DEFAULT_REGION=$(or $(AWS_DEFAULT_REGION),eu-west-1) \
	$(if $(AWS_PROFILE),-e AWS_PROFILE=$(AWS_PROFILE),)
PROFILE_ARG := $(if $(AWS_PROFILE),--profile $(AWS_PROFILE),)

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

S3_BUCKET ?= $(BUCKET)

# Python interpreter for the vision phase (runs on host, not in Docker)
PYTHON ?= python3

DOCKER_RUN := docker run --rm \
	$(AWS_FLAGS) \
	-v "$(DB_DIR):/data" \
	$(IMAGE)

.PHONY: build init inventory metadata vision both summary query shell ui ui-local help check-auth \
        ignore-prefix unignore-prefix list-prefixes force-rescan unknown-extensions \
        ec2-setup ec2-deploy cloud-init cloud-inventory cloud-metadata cloud-run \
        db-pull db-push ec2-stop ec2-terminate

check-auth:
	@aws sts get-caller-identity $(if $(AWS_PROFILE),--profile $(AWS_PROFILE),) > /dev/null || \
		{ echo ""; \
		  echo "ERROR: AWS credentials are missing or expired$(if $(AWS_PROFILE), for profile '$(AWS_PROFILE)',)."; \
		  echo "  Re-authenticate with MFA externally, then retry."; \
		  $(if $(AWS_PROFILE),echo "  Check: aws sts get-caller-identity --profile $(AWS_PROFILE)";,) \
		  echo ""; exit 1; }

## Build the Docker image
build:
	docker build -t $(IMAGE) .

## Interactive bucket setup wizard — lists all top-level prefixes and asks what to scan
## Usage: make init  (uses BUCKET variable, runs on host so stdin/stdout work)
init: check-auth $(DB_DIR)
	DB_PATH=$(CURDIR)/data/content_catalogue.duckdb \
	$(or $(AWS_PROFILE:%=AWS_PROFILE=%),) \
	$(PYTHON) catalogue.py --phase init --bucket $(BUCKET) --db $(CURDIR)/data/content_catalogue.duckdb $(PROFILE_ARG) $(ARGS)

## Phase 1 — list video and audio files (incremental: skips recently-scanned prefixes)
## Use ARGS="--staleness-days 0" to force a full rescan, or ARGS="--no-skip-stale"
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
		-e S3_BUCKET=$(S3_BUCKET) \
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
	S3_BUCKET=$(S3_BUCKET) \
	$(if $(ANTHROPIC_API_KEY),ANTHROPIC_API_KEY=$(ANTHROPIC_API_KEY),) \
	streamlit run app.py

## Add a prefix to the ignore list (persists across scans)
## Usage: make ignore-prefix PREFIX=shows/
ignore-prefix: $(DB_DIR)
ifndef PREFIX
	$(error Usage: make ignore-prefix PREFIX=<prefix>)
endif
	$(PYTHON) catalogue.py --db $(CURDIR)/data/content_catalogue.duckdb --bucket $(BUCKET) --ignore-prefix $(PREFIX)

## Remove a prefix from the ignore list
## Usage: make unignore-prefix PREFIX=shows/
unignore-prefix: $(DB_DIR)
ifndef PREFIX
	$(error Usage: make unignore-prefix PREFIX=<prefix>)
endif
	$(PYTHON) catalogue.py --db $(CURDIR)/data/content_catalogue.duckdb --bucket $(BUCKET) --unignore-prefix $(PREFIX)

## Show all known prefixes with scan status
list-prefixes: $(DB_DIR)
	$(PYTHON) catalogue.py --db $(CURDIR)/data/content_catalogue.duckdb --bucket $(BUCKET) --list-prefixes

## Force a rescan of a specific prefix regardless of staleness
## Usage: make force-rescan PREFIX=shows/
force-rescan: check-auth $(DB_DIR)
ifndef PREFIX
	$(error Usage: make force-rescan PREFIX=<prefix>)
endif
	$(DOCKER_RUN) --phase inventory --bucket $(BUCKET) --db $(DB_FILE) $(PROFILE_ARG) --force-prefix $(PREFIX) $(ARGS)

## Show file extensions found during scanning that are not video or audio
unknown-extensions: $(DB_DIR)
	$(PYTHON) catalogue.py --db $(CURDIR)/data/content_catalogue.duckdb --bucket $(BUCKET) --list-prefixes 2>/dev/null || true
	@echo ""
	docker run --rm -v "$(DB_DIR):/data" --entrypoint python $(IMAGE) \
		-c "import duckdb,sys; df=duckdb.connect(sys.argv[1],read_only=True).execute('SELECT * FROM scan_unknown_extensions ORDER BY count DESC').df(); print(df.to_string(index=False) if not df.empty else 'No unknown extensions found.')" \
		$(DB_FILE)

# ---------------------------------------------------------------------------
# Cloud (EC2) targets — run the pipeline on a remote EC2 instance in eu-west-1
# ---------------------------------------------------------------------------
# Uses SSM Session Manager — no key pair, no open SSH port, no public IP needed.
# Add this to ~/.ssh/config so ssh/scp/rsync route through SSM automatically:
#
#   Host i-*
#       User ubuntu
#       ProxyCommand sh -c "aws ssm start-session --target %h --document-name AWS-StartSSHSession --parameters 'portNumber=%p'"
#       StrictHostKeyChecking no
#
# Then set EC2_HOST to the instance ID (not an IP): EC2_HOST=i-0abc123 make <target>
# Run  make ec2-setup  first to bootstrap a fresh instance.
EC2_HOST     ?= $(error EC2_HOST is not set — use: EC2_HOST=ubuntu@<ip> make <target>)
EC2_REPO_DIR ?= /opt/content-database
EC2_DB_FILE  ?= $(EC2_REPO_DIR)/data/content_catalogue.duckdb

## Bootstrap a fresh EC2 instance (run once after launch)
ec2-setup:
	scp scripts/ec2-setup.sh $(EC2_HOST):/tmp/ec2-setup.sh
	ssh $(EC2_HOST) "bash /tmp/ec2-setup.sh"

## Deploy/update the source code to EC2
ec2-deploy:
	ssh $(EC2_HOST) "mkdir -p $(EC2_REPO_DIR)"
	rsync -az --exclude '.git' --exclude 'data/' \
		$(CURDIR)/ $(EC2_HOST):$(EC2_REPO_DIR)/

## Run the interactive init wizard on EC2 (allocates a TTY)
## Usage: EC2_HOST=ubuntu@<ip> make cloud-init
cloud-init: ec2-deploy
	ssh -t $(EC2_HOST) "cd $(EC2_REPO_DIR) && make init BUCKET=$(BUCKET)"

## Run inventory phase on EC2 (incremental — respects staleness)
## Usage: EC2_HOST=ubuntu@<ip> make cloud-inventory [ARGS="--staleness-days 0"]
cloud-inventory: ec2-deploy
	ssh $(EC2_HOST) "cd $(EC2_REPO_DIR) && make inventory BUCKET=$(BUCKET) ARGS='$(ARGS)'"

## Run metadata (ffprobe) phase on EC2
## Usage: EC2_HOST=ubuntu@<ip> make cloud-metadata [ARGS="--workers 40"]
cloud-metadata: ec2-deploy
	ssh $(EC2_HOST) "cd $(EC2_REPO_DIR) && make metadata BUCKET=$(BUCKET) ARGS='$(ARGS)'"

## Run inventory + metadata on EC2 (full pipeline, no vision)
## Usage: EC2_HOST=ubuntu@<ip> make cloud-run [ARGS="..."]
cloud-run: ec2-deploy
	ssh $(EC2_HOST) "cd $(EC2_REPO_DIR) && make both BUCKET=$(BUCKET) ARGS='$(ARGS)'"

## Sync the DuckDB file from EC2 to local data/ directory
## Usage: EC2_HOST=ubuntu@<ip> make db-pull
db-pull: $(DB_DIR)
	scp $(EC2_HOST):$(EC2_DB_FILE) $(DB_DIR)/content_catalogue.duckdb
	@echo "Database pulled to $(DB_DIR)/content_catalogue.duckdb"

## Push the local DuckDB file to EC2 (e.g. to seed a fresh instance)
## Usage: EC2_HOST=ubuntu@<ip> make db-push
db-push:
	scp $(DB_DIR)/content_catalogue.duckdb $(EC2_HOST):$(EC2_DB_FILE)
	@echo "Database pushed to $(EC2_HOST):$(EC2_DB_FILE)"

## Terminate the spot instance (one-time spot instances cannot be stopped, only terminated).
## DB is pulled first. To reuse the instance later, launch a new spot and push the DB back.
## Usage: EC2_INSTANCE_ID=i-0abc123 make ec2-stop
ec2-stop:
ifndef EC2_INSTANCE_ID
	$(error EC2_INSTANCE_ID is not set — find it in the AWS console or: aws ec2 describe-instances --filters "Name=ip-address,Values=<ip>")
endif
	@echo "Pulling database before terminating spot instance..."
	$(MAKE) db-pull
	aws ec2 terminate-instances --region eu-west-1 --instance-ids $(EC2_INSTANCE_ID)
	@echo "Spot instance $(EC2_INSTANCE_ID) is terminating."
	@echo "To resume later: launch a new spot instance and run: make ec2-setup ec2-deploy db-push"

## Terminate the EC2 instance permanently (destroys instance; EBS deleted unless configured otherwise)
## Always run  make db-pull  first. This cannot be undone.
## Usage: EC2_INSTANCE_ID=i-0abc123 make ec2-terminate
ec2-terminate:
ifndef EC2_INSTANCE_ID
	$(error EC2_INSTANCE_ID is not set — find it in the AWS console or: aws ec2 describe-instances --filters "Name=ip-address,Values=<ip>")
endif
	@echo "WARNING: This permanently terminates $(EC2_INSTANCE_ID)."
	@echo "Pulling database first..."
	$(MAKE) db-pull
	aws ec2 terminate-instances --region eu-west-1 --instance-ids $(EC2_INSTANCE_ID)
	@echo "Instance $(EC2_INSTANCE_ID) terminated."

$(DB_DIR):
	mkdir -p $(DB_DIR)

help:
	@echo ""
	@echo "Usage:"
	@echo "  make build                              Build the Docker image"
	@echo "  make init       [BUCKET=...]            Interactive setup: pick which prefixes to scan/ignore"
	@echo "  make inventory  [ARGS=...]              Phase 1: list media files (incremental)"
	@echo "  make metadata   [ARGS=...]              Phase 2: extract attributes via ffprobe"
	@echo "  make vision     [ARGS=...]              Phase 3: analyse frames with Ollama / Claude"
	@echo "  make both       [ARGS=...]              Run Phase 1 + 2"
	@echo "  make summary                            Print collected stats"
	@echo "  make query Q=\"<sql>\"                    Run a SQL query on the DB"
	@echo "  make shell                              Open interactive DuckDB shell"
	@echo "  make ui                                 Open Streamlit query UI in Docker (http://localhost:8501)"
	@echo "  make ui-local                           Open Streamlit query UI on host"
	@echo ""
	@echo "Prefix management:"
	@echo "  make list-prefixes  [BUCKET=...]        Show all known prefixes with scan status"
	@echo "  make ignore-prefix  PREFIX=<p> [BUCKET=...]  Add prefix to ignore list"
	@echo "  make unignore-prefix PREFIX=<p> [BUCKET=...]  Remove prefix from ignore list"
	@echo "  make force-rescan   PREFIX=<p> [BUCKET=...]  Force rescan of one prefix"
	@echo "  make unknown-extensions [BUCKET=...]    Show file extensions not recognised as video/audio"
	@echo ""
	@echo "Incremental scan ARGS:"
	@echo "  ARGS=\"--staleness-days 30\"              Skip prefixes scanned within 30 days (default 7)"
	@echo "  ARGS=\"--staleness-days 0\"               Rescan everything (force full scan)"
	@echo "  ARGS=\"--no-skip-stale\"                  Scan all non-ignored prefixes"
	@echo "  ARGS=\"--force-prefix shows/\"            Force rescan of one prefix"
	@echo ""
	@echo "AWS authentication (local):"
	@echo "  export AWS_PROFILE=enc-dev                 Required for local runs — enc-dev profile"
	@echo "                                             assumes the role via role_arn in ~/.aws/config"
	@echo "  export AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... AWS_DEFAULT_REGION=..."
	@echo "  On EC2: no profile needed — instance IAM role is used automatically"
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
	@echo "Cloud (EC2 eu-west-1) — free S3 transfer, 2-4x faster:"
	@echo "  EC2_HOST=ubuntu@<ip> make ec2-setup        Bootstrap a fresh EC2 instance"
	@echo "  EC2_HOST=ubuntu@<ip> make cloud-init        Interactive init wizard on EC2"
	@echo "  EC2_HOST=ubuntu@<ip> make cloud-run         Run inventory + metadata on EC2"
	@echo "  EC2_HOST=ubuntu@<ip> make cloud-inventory   Incremental inventory on EC2"
	@echo "  EC2_HOST=ubuntu@<ip> make db-pull           Sync DB from EC2 to local"
	@echo "  EC2_HOST=ubuntu@<ip> make db-push           Seed EC2 with local DB"
	@echo "  EC2_INSTANCE_ID=i-0abc make ec2-stop        Stop instance (DB pulled first, EBS kept)"
	@echo "  EC2_INSTANCE_ID=i-0abc make ec2-terminate   Terminate permanently (DB pulled first)"
	@echo "  See scripts/ec2-setup.sh for IAM role and EBS configuration."
	@echo ""
	@echo "Optional ARGS examples:"
	@echo "  ARGS=\"--prefix analysis/jan-ozer-per-title-files/\"   scope to a prefix"
	@echo "  ARGS=\"--limit 50\"                                    probe only 50 files"
	@echo "  ARGS=\"--workers 40\"                                  increase parallelism"
	@echo "  ARGS=\"--retry-errors\"                               re-analyse previously failed vision files"
	@echo "  ARGS=\"--vision-workers 4\"                          parallel vision workers (GPU hosts only)"
	@echo ""
