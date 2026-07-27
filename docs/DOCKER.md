# Running the dashboard in Docker

The web dashboard can run in a container. The app is pure standard-library
Python, so the image is just a slim official `python` base plus four `.py`
files — no `pip install`, no build step, no dependency lockfile.

The container runs as a **non-root** user (`appuser`, uid 10001) and needs two
things from the host: your Claude Code transcripts (read-only) and somewhere
writable to keep the SQLite database it derives from them.

> **Known limitation:** the live plan-limits API does **not** work in a
> container. See [Plan limits](#plan-limits-in-a-container) below.

---

## Quick start (Docker Compose)

```
docker compose up -d --build
open http://127.0.0.1:8080          # macOS; xdg-open on Linux
```

Stop it, and clean up:

```
docker compose down                 # stop + remove the container
docker compose down -v              # ...and delete the database volume too
```

The compose file sets `TZ` from your shell's `TZ` variable (default `UTC`), so
`TZ=Europe/Warsaw docker compose up -d` renders times in that zone.

## Quick start (plain Docker)

```
docker build -t claude-usage:latest .

docker run -d \
  --name claude-usage-dashboard \
  -p 127.0.0.1:8080:8080 \
  -v "$HOME/.claude/projects:/home/appuser/.claude/projects:ro" \
  -v claude-usage-db:/home/appuser/.claude \
  claude-usage:latest
```

Tear down:

```
docker rm -f claude-usage-dashboard         # stop + remove
docker volume rm claude-usage-db            # optional: drop the database
docker rmi claude-usage:latest              # optional: drop the image
```

On **native Linux**, the bind-mounted transcripts keep their host ownership, so
build the image with your own uid/gid to be able to read them:

```
docker build -t claude-usage:latest --build-arg APP_UID=$(id -u) --build-arg APP_GID=$(id -g) .
```

Docker Desktop (macOS/Windows) remaps ownership for you; the default uid works.

---

## Mounts

Every path in the app is resolved from `$HOME`, which is `/home/appuser` in the
image. There is no environment variable or flag for the database location, so
the mounts have to land on the paths the code already looks at.

| Host path | Container path | Mode | Why |
|---|---|---|---|
| `~/.claude/projects` | `/home/appuser/.claude/projects` | **read-only** | The JSONL transcripts. Mounted `:ro` so the container can never modify or truncate your real Claude Code logs. |
| named volume `claude-usage-db` | `/home/appuser/.claude` | read-write | Where `usage.db` and `claude-usage-limits.json` are written. |
| `~/Library/Developer/Xcode/CodingAssistant/ClaudeAgentConfig/projects` | same path under `/home/appuser` | read-only | *Optional*, macOS only. Commented out in `docker-compose.yml`; uncomment if you use Claude in Xcode. Missing directories are skipped silently. |

**Why a named volume for the database, not a bind mount into `~/.claude`?**
The database is 100% derived state — it can be rebuilt from the transcripts at
any time by rescanning — while `~/.claude` is a live directory belonging to
Claude Code itself. Keeping the container's only writable mount in a Docker
volume means a containerized run cannot write anything at all into your real
`~/.claude`, and the worst case of a corrupted DB is `docker compose down -v`
plus one rescan. The trade-off: the container maintains its **own** copy of
`usage.db`, separate from the one `python cli.py today` uses natively, so the
first start does a full scan (a few seconds).

If you would rather share the host's existing database — accepting that the
container then writes into your real `~/.claude` — swap the volume line in
`docker-compose.yml` for:

```yaml
- "${HOME}/.claude:/home/appuser/.claude"
```

and remove the `claude-usage-db` volume entry. The read-only `projects` mount
still applies on top of it, so transcripts stay protected either way.

---

## Networking: bound to localhost on purpose

Inside the container the server **must** bind `0.0.0.0` — the app's default of
`localhost` would be unreachable from outside the container's network
namespace. `HOST=0.0.0.0` is therefore baked into the image (and repeated in the
compose file next to the port mapping).

That is safe only because the port is published to the loopback interface:

```
-p 127.0.0.1:8080:8080          # not -p 8080:8080
```

The dashboard has **no authentication** and displays your complete usage
history, project names included. Publishing it as `-p 8080:8080` would expose it
to everyone on your Wi-Fi. Keep the `127.0.0.1:` prefix unless you have
deliberately put an authenticating proxy in front of it.

To use a different host port (e.g. 8080 is taken), change only the left-hand
side: `-p 127.0.0.1:9000:8080`, then browse to `http://127.0.0.1:9000`.

---

## Plan limits in a container

**The live limits API is unavailable in a container.** `limits.py` gets its
OAuth token from the macOS keychain by shelling out to the `security` binary
(keychain item `Claude Code-credentials`). That binary and that keychain do not
exist inside a Linux container, and there is deliberately no attempt here to
forward keychain access, copy the token into the image, or stash it in an env
file.

The failure is graceful, not fatal: `read_oauth_credential()` returns `None`,
`/api/limits` responds with `"api_ok": false`, and the Plan Limits card falls
back to the other two cap sources:

1. **Calibrated** — if `cap_usd` values exist in
   `claude-usage-limits.json` on the writable volume, percentages are estimated
   from them.
2. **Uncalibrated** — otherwise the card still shows dollar-equivalent
   consumption, turn counts and the reset countdown, but the bars read `—`.

Consumption, turns and reset times are computed entirely from the local
transcripts, so those remain correct in the container. Only the *exact
percentages and caps* need the API.

**To get live percentages, run the dashboard natively** on your Mac:

```
python3 cli.py dashboard        # or: claude-usage dashboard
```

You can also seed the container with calibrated caps: run
`python3 cli.py limits --calibrate-session 20` natively (or read the
percentages off the desktop app), then copy the resulting
`~/.claude/claude-usage-limits.json` into the volume:

```
docker cp ~/.claude/claude-usage-limits.json claude-usage-dashboard:/home/appuser/.claude/
```

To stop the container attempting the (always-failing) keychain lookup entirely,
put `{"use_api": false}` in that same file.

---

## Operational notes

- **Health check.** The image declares a `HEALTHCHECK` that fetches `/` every
  30s; `docker ps` shows `healthy` once the initial scan finishes.
  `docker inspect --format '{{.State.Health.Status}}' claude-usage-dashboard`
  reports it directly.
- **Scanning.** An incremental scan runs at container start. `/api/limits`
  (polled by the UI every 30s) also triggers an incremental scan, so the
  container picks up new transcripts while it runs without a restart.
- **Rescan from scratch.** The dashboard's "Rescan" button (`POST /api/rescan`)
  deletes and rebuilds the container's copy of the DB. It never touches the
  read-only transcripts.
- **Logs.** `docker compose logs -f` (or `docker logs -f claude-usage-dashboard`).
- **Chart.js is loaded from a CDN**, so the browser needs internet access for
  the charts to render; the container itself does not.
- The CLI subcommands work in the container too, against its own DB:
  `docker exec claude-usage-dashboard python cli.py today`.
