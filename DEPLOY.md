# Vinylgen deployment

Vinylgen runs on a workstation as a user service, bound only to
`127.0.0.1:8275`. A persistent SSH reverse tunnel exposes that backend only at
`127.0.0.1:8275` on an edge host; Gandalf (an identity-aware reverse proxy)
exposes it at `https://vinylgen.example.com` with TLS and `vinylgen:app` ACL
checks. The backend rejects every mutation that lacks Gandalf's trusted
identity header; direct localhost health checks remain read-only.

`just deploy` finishes by checking `$VINYLGEN_PUBLIC_URL/api/health`. Set
`VINYLGEN_PUBLIC_URL` to the deployment's real public origin in the untracked
`.vinylgen-data/vinylgen.env`; the template value is a placeholder.

Persistent state lives in `.vinylgen-data/` on the workstation. Explicit desktop
handoff saves go to `.vinylgen-data/inkscape/`. The upload limit is
25 MiB, at most two jobs may be active, and completed job metadata is pruned
after 90 days when the service starts. Project revisions and content-addressed artifacts
are retained until an explicit project-deletion workflow exists.
On startup, any persisted `queued` or `running` jobs from the previous process
are marked failed with an interruption reason and completion timestamp. This
releases their capacity slots; interrupted provider work is not resumed.

Artwork generation launches one owned `codex exec --ephemeral --json` process
tree per request. The `vinylgen.service` user must have Codex installed on its
`PATH` and must already be authenticated with `codex login`; Vinylgen never
reads or serves that user's Codex OAuth credentials. Verify this after CLI
upgrades because image-tool availability and structured events are an evolving
Codex surface. Cancellation and the five-minute default timeout terminate the
owned process tree and discard its `.tmp/codex-image-*` directory.

## Install or update workstation services

```bash
npm --prefix web ci
npm --prefix web run build
install -Dm600 deploy/vinylgen.env .vinylgen-data/vinylgen.env
install -Dm644 deploy/vinylgen-user.service ~/.config/systemd/user/vinylgen.service
install -Dm644 deploy/vinylgen-tunnel-user.service ~/.config/systemd/user/vinylgen-tunnel.service
systemctl --user daemon-reload
systemctl --user enable --now vinylgen.service vinylgen-tunnel.service
```

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
