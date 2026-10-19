# Dev shortcuts. workbench has its OWN env in .venv — never the shared ~/envs/jura.
# The shell exports UV_PROJECT_ENVIRONMENT=~/envs/jura globally; override it here so `uv`
# can never sync (and so wipe) the shared env from this project.
export UV_PROJECT_ENVIRONMENT := $(CURDIR)/.venv

.DEFAULT_GOAL := help

APP_PC := webkit2gtk-4.1 javascriptcoregtk-4.1 libsoup-3.0 gtk+-3.0 dbus-1
APP_DEPS := libdbus-1-dev libwebkit2gtk-4.1-dev libgtk-3-dev libsoup-3.0-dev libjavascriptcoregtk-4.1-dev librsvg2-dev libssl-dev build-essential pkg-config
TINYMIST_VERSION := 0.15.8

.PHONY: help sync test fmt lint gui web app-build app-deps tool deps skill

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

sync: ## Create / update workbench's own .venv (never the shared jura env)
	uv sync

test: ## Run the test suite
	.venv/bin/python -m pytest -q

fmt: ## Format and fix imports in workbench's own modules (the treedit fork keeps its layout)
	.venv/bin/ruff check --fix src tests
	.venv/bin/ruff format src/workbench/*.py tests/shell

lint: ## Lint
	.venv/bin/ruff check src tests

gui: ## Open the workbench (builds workbench-app first when the Tauri libs are present)
	@if pkg-config --exists $(APP_PC); then cargo build -q --release --manifest-path app/Cargo.toml; \
	else echo "Tauri libraries missing (make app-deps) — using the browser"; fi
	.venv/bin/jura open

web: ## Open the workbench in the browser
	.venv/bin/jura open --browser

app-build: ## Build the workbench-app window (release)
	cargo build --release --manifest-path app/Cargo.toml

app-deps: ## Install the system libraries Tauri needs (Debian/Ubuntu)
	sudo apt-get install -y $(APP_DEPS)

tool: app-build ## Install `jura` as a uv tool from this checkout, and workbench-app on PATH
	uv tool install --force --editable .
	install -m 755 app/target/release/workbench-app $(HOME)/.local/bin/workbench-app
	ln -sf jura $(HOME)/.local/bin/workbench  # old name, for cases whose AGENTS.md still says `workbench`

deps: ## Install tinymist $(TINYMIST_VERSION) into ~/.local/bin (the Write tab's Typst preview)
	curl -fsSL -o /tmp/tinymist.tar.gz https://github.com/Myriad-Dreamin/tinymist/releases/download/v$(TINYMIST_VERSION)/tinymist-x86_64-unknown-linux-gnu.tar.gz
	tar -xzf /tmp/tinymist.tar.gz -C /tmp
	install -m 755 /tmp/tinymist-x86_64-unknown-linux-gnu/tinymist $(HOME)/.local/bin/tinymist

skill: ## Copy SKILL.md to ~/.claude/skills/workbench (a copy: the agent sandbox cannot follow a link into the repo)
	install -D -m 644 SKILL.md $(HOME)/.claude/skills/workbench/SKILL.md
