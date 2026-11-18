SHELL := /bin/bash
.DEFAULT_GOAL := help

VENV    ?= $(HOME)/.venvs/dayline
PY      := $(VENV)/bin/python
TAG     ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo dev)
PROFILE ?= server
COMPOSE      := docker compose -f docker-compose.yml
COMPOSE_DEV  := $(COMPOSE) -f docker-compose.dev.yml
COMPOSE_PROD := DAYLINE_IMAGE_TAG=$(TAG) DAYLINE_PROFILE=$(PROFILE) $(COMPOSE) -f docker-compose.prod.yml
BACKUP_DIR  ?= backups
# rsync target on another machine, e.g. backup-host:/srv/backups/dayline
BACKUP_DEST ?=
# local archives to keep
BACKUP_KEEP ?= 14

COMPOSE_TOOLS := $(COMPOSE_PROD) -f docker-compose.tools.yml

.PHONY: help venv test lint fmt eval probe dev up down logs build package backup restore restore-drill web-install web-test web-build tools-review tools-compose tools-up

help:  ## list targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-14s %s\n", $$1, $$2}'

venv:  ## create the Python venv on the Linux filesystem (not on /mnt/f)
	test -x $(PY) || python3.12 -m venv $(VENV)
	$(PY) -m pip install -q -e './backend[dev]'

test:  ## backend + web unit tests
	cd backend && $(PY) -m pytest
	cd web && npm test --silent

web-install:  ## install web dependencies
	cd web && npm ci --no-audit --no-fund

web-test:  ## web typecheck + unit tests
	cd web && npx tsc --noEmit && npm test --silent

web-build:  ## production build of the web app
	cd web && NEXT_TELEMETRY_DISABLED=1 npx next build

lint:  ## ruff lint + format check
	cd backend && $(VENV)/bin/ruff check . && $(VENV)/bin/ruff format --check .

fmt:  ## format code
	cd backend && $(VENV)/bin/ruff check --fix . && $(VENV)/bin/ruff format .

eval:  ## run skill evals against real endpoints (billed)
	cd backend && $(PY) -m evals $(ARGS)

probe:  ## probe LLM endpoints' features (billed, small)
	$(VENV)/bin/dl llm probe

dev:  ## dev stack with hot reload (foreground)
	$(COMPOSE_DEV) up --build

up:  ## production stack (TAG=<sha|version> PROFILE=server|intranet|cloud)
	$(COMPOSE_PROD) up -d

down:  ## stop the stack (data volume is kept)
	$(COMPOSE) down

logs:  ## follow logs
	$(COMPOSE) logs -f --tail=200

build:  ## build the backend and web images tagged with the git sha
	docker build -f docker/backend.Dockerfile --target runtime -t dayline-backend:$(TAG) .
	docker build -f docker/web.Dockerfile --target runtime -t dayline-web:$(TAG) web

tools-review:  ## CI review of a member tool locally: make tools-review TOOL=tools/<name>
	@test -n "$(TOOL)" || (echo "usage: make tools-review TOOL=tools/<name>" && exit 1)
	PATH=$(VENV)/bin:$$PATH tools/ci/review.sh $(TOOL)

tools-compose:  ## regenerate docker-compose.tools.yml + deploy/tools/*.env from approved tools
	$(VENV)/bin/dl tools compose

tools-up: tools-compose  ## deploy member tools and the egress proxy next to the stack
	$(COMPOSE_TOOLS) up -d --remove-orphans

package: build  ## offline bundle for hosts that cannot pull images (docker load -i ...)
	mkdir -p dist
	docker pull mongo:8.0
	docker pull caddy:2.11
	docker save dayline-backend:$(TAG) dayline-web:$(TAG) mongo:8.0 caddy:2.11 | gzip > dist/dayline-$(TAG).tar.gz
	@echo "dist/dayline-$(TAG).tar.gz  (load with: gunzip -c FILE | docker load)"

backup:  ## mongodump to $(BACKUP_DIR) (keeps $(BACKUP_KEEP)), then copy to $(BACKUP_DEST) if set
	mkdir -p $(BACKUP_DIR)
	set -euo pipefail; f=$(BACKUP_DIR)/dayline-$$(date +%Y%m%d-%H%M%S).archive.gz; \
	$(COMPOSE) exec -T mongo mongodump --db=dayline --archive --gzip > $$f.part && mv $$f.part $$f && echo "wrote $$f"; \
	if [ -n "$(BACKUP_DEST)" ]; then rsync -av $$f $(BACKUP_DEST)/; fi; \
	ls -1t $(BACKUP_DIR)/dayline-*.archive.gz | tail -n +$$(( $(BACKUP_KEEP) + 1 )) | xargs -r rm -v --

restore-drill:  ## restore FILE into a scratch DB, compare counts with live, drop it (run monthly)
	@test -n "$(FILE)" || (echo "usage: make restore-drill FILE=backups/xxx.archive.gz" && exit 1)
	$(COMPOSE) exec -T mongo mongorestore --archive --gzip --drop --nsFrom='dayline.*' --nsTo='dayline_drill.*' < $(FILE)
	$(COMPOSE) exec -T mongo mongosh --quiet < docker/mongo/restore-drill.js

restore:  ## restore FILE=backups/....archive.gz (drops existing collections)
	@test -n "$(FILE)" || (echo "usage: make restore FILE=backups/xxx.archive.gz" && exit 1)
	$(COMPOSE) exec -T mongo mongorestore --archive --gzip --drop < $(FILE)
