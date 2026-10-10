# syntax=docker/dockerfile:1
#
# Freya "Build the Builder" -- container image for the LangGraph builder.
#
# The image runs the SAME entry point the organisers use natively:
#
#     python3 builder/build.py <brief>... <tool_api> [options]
#
# with WORKDIR /app holding the whole repository, so the kit files sit at /app exactly as they do in a checkout:
# freya.py, tools/ (openapi.json, run_cases.py, score.py), briefs/, docs/, examples/, public-test-set.json and
# .claude/skills/. Nothing in this file imports or executes builder code, so the image builds even while builder/
# is incomplete.
#
# Stages (BuildKit only builds the stages the selected target needs):
#   uv         pinned uv binary (copied into the build stages only, never into the images you run)
#   build      resolves the locked runtime dependencies into /app/.venv, then installs the project (editable, so
#              /app/builder is the single copy of the code that runs)
#   build-dev  build + the `dev` extra (pytest, pytest-asyncio, ruff)
#   base       python:3.13-slim-trixie + non-root user `app` + runtime environment
#   dev        base + build-dev  -> `docker build --target dev`; runs the unit tests (compose service `tests`)
#   runtime    base + build      -> the DEFAULT target (last stage); runs builder/build.py (compose service `builder`)
#
# Writable paths for the non-root user: out/, traces/, results/, data/, state/ (mount them as volumes to keep what a
# run produces; see docker-compose.yml). The kit keeps its state files at the repository root (.freya-agents.json
# maps a slot to its Freya workflow so re-runs update in place; tools/run_cases.py caches persona ids in
# .personas.json). In the image both are symlinks into state/, so one mounted directory persists them between runs.
#
# Secrets are never baked in: .env is excluded by .dockerignore and every key arrives at run time as an environment
# variable (docker-compose.yml lists them explicitly).
#
# Build:   docker build -t freya-builder:dev .
# Run:     docker run --rm --init -e FREYA_API_KEY -e ANTHROPIC_API_KEY \
#            -v "$PWD/out:/app/out" -v "$PWD/traces:/app/traces" -v "$PWD/state:/app/state" \
#            freya-builder:dev briefs/brief-a.md tools/openapi.json
# Tests:   docker build --target dev -t freya-builder:tests . && docker run --rm --network none freya-builder:tests

# Pinned base images (override with --build-arg). Both FROM ${PYTHON_IMAGE} stages resolve the same tag in one build,
# so the interpreter the venv was created with exists at the same path in the image that runs it.
# (hadolint DL3006 cannot see through ARG; the tags are explicit here.)
ARG PYTHON_IMAGE=python:3.13-slim-trixie
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.24

# ---------------------------------------------------------------------------------------------------------------
# hadolint ignore=DL3006
FROM ${UV_IMAGE} AS uv

# ---------------------------------------------------------------------------------------------------------------
# hadolint ignore=DL3006
FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /uvx /bin/

# UV_COMPILE_BYTECODE  pre-compile site-packages so the CLI starts fast (the runtime never writes .pyc files)
# UV_LINK_MODE=copy    the uv cache is a cache mount on another filesystem, so hard links are impossible
# UV_PYTHON_DOWNLOADS  never download a managed CPython: the venv must point at the base image's interpreter,
#                      which the runtime stage (same base image) provides at the same path
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# 1) Third-party dependencies only, from the lock file. This layer is reused until pyproject.toml or uv.lock change,
#    so editing builder code does not re-download ~100 packages. --locked fails if the lock is stale.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --extra agentic --extra ingest --no-install-project

# 2) The repository (filtered by .dockerignore), then the project itself. The editable install only writes a .pth
#    pointing at /app (and the `freya-builder` console script); it does not import builder code.
COPY . /app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --extra agentic --extra ingest

# 3) Run-time directories and the kit state files redirected into state/ (relative links resolve inside /app).
RUN mkdir -p out traces results data state \
 && ln -sfn state/.freya-agents.json .freya-agents.json \
 && ln -sfn state/.personas.json .personas.json

# ---------------------------------------------------------------------------------------------------------------
FROM build AS build-dev
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --extra agentic --extra ingest --extra dev

# ---------------------------------------------------------------------------------------------------------------
# hadolint ignore=DL3006
FROM ${PYTHON_IMAGE} AS base

# Match APP_UID/APP_GID to your host user on Linux so bind-mounted out/, traces/, ... stay writable
# (Docker Desktop on Windows and macOS maps ownership itself; the defaults are fine there).
ARG APP_UID=1000
ARG APP_GID=1000
# /app itself belongs to `app` too: pytest writes .pytest_cache there and the agentic track runs the Claude CLI
# with /app as its working directory.
RUN groupadd --gid "${APP_GID}" app \
 && useradd --uid "${APP_UID}" --gid app --create-home --home-dir /home/app --shell /usr/sbin/nologin app \
 && install -d -o app -g app -m 0700 /home/app/.claude \
 && install -d -o app -g app -m 0755 /app

LABEL org.opencontainers.image.title="freya-collections-builder" \
      org.opencontainers.image.description="LangGraph builder that reads a collections brief and its tool API file and creates a Freya phone agent."

# PATH puts the project venv first, so `python` is the venv interpreter with every locked dependency.
# PYTHONUNBUFFERED streams trace/progress lines immediately; PYTHONUTF8 makes every open() UTF-8 regardless of locale.
ENV VIRTUAL_ENV=/app/.venv \
    PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUTF8=1

WORKDIR /app

# ---------------------------------------------------------------------------------------------------------------
FROM base AS dev
COPY --from=build-dev --chown=app:app /app /app
USER app
# Unit tests only: anything marked `live` talks to Freya or a real LLM and stays opt-in.
ENTRYPOINT ["python", "-m", "pytest"]
CMD ["-m", "not live", "tests"]

# ---------------------------------------------------------------------------------------------------------------
FROM base AS runtime
COPY --from=build --chown=app:app /app /app
USER app
ENTRYPOINT ["python", "builder/build.py"]
CMD ["--help"]
