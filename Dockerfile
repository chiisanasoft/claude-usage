# Claude Code Usage Dashboard — container image.
#
# The app is pure stdlib Python, so there is no dependency layer and no
# `pip install` step: copy four .py files onto a slim official base and run.
#
# See docs/DOCKER.md for how to run it (and for the one feature that cannot
# work in a container: the macOS-keychain-backed live limits API).

FROM python:3.12-slim

# UID/GID of the container user. Bind-mounted transcripts must be readable by
# it. Docker Desktop (macOS/Windows) remaps ownership automatically, so the
# default is fine there; on native Linux, build with
#   --build-arg APP_UID=$(id -u) --build-arg APP_GID=$(id -g)
# so the read-only mount of ~/.claude/projects is readable.
ARG APP_UID=10001
ARG APP_GID=10001

# HOST must be 0.0.0.0 inside a container: the app defaults to localhost, which
# would make it unreachable from the published port. Exposure is constrained on
# the host side instead (publish to 127.0.0.1 — see docs/DOCKER.md).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/appuser \
    HOST=0.0.0.0 \
    PORT=8080

RUN groupadd --gid "${APP_GID}" appuser \
 && useradd --uid "${APP_UID}" --gid "${APP_GID}" --create-home appuser \
 # Pre-create the data dir owned by appuser: a named volume mounted here
 # inherits this ownership, so the container can write usage.db without root.
 && mkdir -p /home/appuser/.claude/projects \
 && chown -R "${APP_UID}:${APP_GID}" /home/appuser

WORKDIR /app
COPY cli.py dashboard.py scanner.py limits.py ./

USER appuser
EXPOSE 8080

# Cheap liveness probe: the dashboard HTML is served from memory, so this
# touches no SQLite and no API. (/api/data would re-read the whole DB.)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ['PORT']+'/',timeout=5).read(1)" || exit 1

# `cli.py dashboard` runs an incremental scan, then serves. --no-browser stops
# it from trying to open a browser, which cannot work in a container.
CMD ["python", "cli.py", "dashboard", "--no-browser"]
