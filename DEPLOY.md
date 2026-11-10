# Vinylgen deployment

Vinylgen runs on a workstation as a user service, bound only to
`127.0.0.1:8275`. A persistent SSH reverse tunnel exposes that backend only at
`127.0.0.1:8275` on an edge host; Gandalf (an identity-aware reverse proxy)
exposes it at `https://vinylgen.example.com` with TLS and `vinylgen:app` ACL
checks. The backend rejects every mutation that lacks Gandalf's trusted
identity header; direct localhost health checks remain read-only.

`just deploy` finishes by checking `$VINYLGEN_PUBLIC_URL/api/health`. Set
`VINYLGEN_PUBLIC_URL` to the deployment's real public origin, and
`VINYLGEN_TUNNEL_HOST` to the edge host's SSH destination, in the untracked
`.vinylgen-data/vinylgen.env`; the template values are placeholders.

`just deploy` runs the test suite and builds the frontend into its own
directory, `.tmp/deploy-web`, then (unless a job is active) installs
`requirements-web.txt` into `.venv` (the `t3api` wheel under `wheels/` among
them), warms the t3 engine's kernel cache with `scripts/warm_t3_cache.py` (one
trace of the test array; a cold cache otherwise costs the first trace 28 to
45 s), checks again that no job is active, publishes the build, restarts the
service and checks local and public health.

The service serves `web/live` (`WebConfig.frontend_dir`), never `web/dist`. A
build (`just build`, a gate, `npm --prefix web run build`) writes `web/dist` and
changes nothing a visitor sees. `scripts/publish_web.py` is the only step that
does: it copies the build into a new `web/releases/<UTC stamp>` and renames a
new symlink over `web/live` (one `rename(2)`, so a request gets the old page or
the new one, never a mix), keeping the release it replaced and removing older
ones. Each request resolves `web/live` afresh. The cache is t3api's default, `~/.cache/t3api/<build id>`, keyed
per build; the warm records `{build, seconds, warm_s, workers}` in its
`warm.json`. The warm runs as the service's user, outside the unit's sandbox,
and the unit lists `%h/.cache/t3api` in `ReadWritePaths` so the service can read
and write that cache under `ProtectSystem=strict` (numba keeps a kernel only
where it can write). Neither the unit nor `vinylgen.env` sets
`T3API_CACHE_DIR`; setting it in one would leave the warm and the service on
different caches. `just deploy` does not reinstall the units; after a change to a template in
`deploy/`, run `just install-services`.

The web client's `@nib/core` (nib's editor core and chrome, the Nodes stage)
is not fetched from a registry: it is the committed tarball
`web/vendor/nib-core-0.10.0.tgz`, packed from nib at tag `core-v0.10.0`.
`web/package.json` names it by `file:` path, `web/package-lock.json` records
its integrity, and `npm --prefix web ci` installs it like any other package,
with its one dependency, `lucide`, from the registry.
`web/vendor/README.md` holds its sha256 and the steps to replace it.

Persistent state lives in `.vinylgen-data/` on the workstation. Explicit desktop
handoff saves go to `.vinylgen-data/inkscape/`. The upload limit is
100 MiB, at most two jobs may be active, and completed job metadata is pruned
after 90 days when the service starts. Project revisions and content-addressed artifacts
are retained until an explicit project-deletion workflow exists.
On startup, a persisted `queued` or `running` job is marked failed with an
interruption reason and completion timestamp only when the process that owned it
is no longer alive. Each job records its owning process (`<pid>:<starttime>`), so
a genuine restart reclaims its own orphaned jobs while a second process opened
against the same database (a dev server, migration, or REPL) leaves the running
server's in-flight jobs untouched. This releases interrupted slots; provider work
is not resumed. `just deploy` refuses to restart while a job is active so a
deployment cannot interrupt one; pass `force=true` to override.

Artwork generation launches one owned `codex exec --ephemeral --json` process
tree per request. The `vinylgen.service` user must have Codex installed on its
`PATH` and must already be authenticated with `codex login`; Vinylgen never
reads or serves that user's Codex OAuth credentials. Verify this after CLI
upgrades because image-tool availability and structured events are an evolving
Codex surface. Cancellation and the five-minute default timeout terminate the
owned process tree and discard its `.tmp/codex-image-*` directory.

## Install or update workstation services

```bash
.venv/bin/pip install -r requirements-web.txt
.venv/bin/python scripts/warm_t3_cache.py   # creates and warms ~/.cache/t3api
npm --prefix web ci
npm --prefix web run build
.venv/bin/python scripts/publish_web.py --dist web/dist --web web   # the served page
just install-services
```

`just install-services` creates `.vinylgen-data/vinylgen.env` from
`deploy/vinylgen.env` only when it is absent (an existing one is never
overwritten), creates `~/.cache/t3api`, installs both units, reloads systemd, and
enables and restarts both services. The units are rendered at install, not copied:
`scripts/render_units.py` reads the templates in `deploy/`, writes the
checkout's own absolute path (the justfile's directory) for every `%h/vinylgen`
path in `vinylgen-user.service` (`WorkingDirectory`, `EnvironmentFile`,
`ExecStart`, `ReadWritePaths`, `NUMBA_CACHE_DIR`; `%h/.cache/t3api` is kept), and
writes `VINYLGEN_TUNNEL_HOST` from `.vinylgen-data/vinylgen.env` as the SSH
destination of `vinylgen-tunnel-user.service`. It refuses, installing nothing,
when `VINYLGEN_TUNNEL_HOST` is unset or the checkout path holds a character a
unit cannot carry (whitespace, `%`, quotes, backslash). The templates themselves
are never edited for a deployment, so a checkout anywhere installs correctly.

Before the first generation, verify the same user/session used by systemd:

```bash
codex --version
codex login status
codex exec --ephemeral --json --help
```

Generation consumes the ChatGPT/Codex subscription's applicable allowance and
can fail when that allowance or the image tool is unavailable. Vinylgen has no
automatic separately billed API fallback.

## Verify and recover

```bash
systemctl --user status vinylgen vinylgen-tunnel --no-pager
curl -fsS http://127.0.0.1:8275/api/health
ssh edge-host 'curl -fsS http://127.0.0.1:8275/api/health'
journalctl --user -u vinylgen -u vinylgen-tunnel -n 100 --no-pager
systemctl --user restart vinylgen vinylgen-tunnel
```

Use the Gandalf dashboard to grant `vinylgen:app`. Do not open port 8275 or
bind uvicorn to a non-loopback address. A phone can download/upload handoff
files, but cannot launch Inkscape on another workstation.
