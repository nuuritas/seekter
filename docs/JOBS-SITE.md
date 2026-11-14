# The job board: jobs.nuritas.tr

Where the two daily runs' output ends up, how it gets there, and what to change when something
about it has to change. Everything here is static: no server process, no database, no request-time
code.

## What it is

    https://jobs.nuritas.tr/                     both versions, latest day each, totals
    https://jobs.nuritas.tr/postings/            every posting either version has seen, filterable
    https://jobs.nuritas.tr/<version>/           that version's days   (linkedin, non-linkedin)
    https://jobs.nuritas.tr/<version>/<date>/    the day's report, exactly what the run wrote
    https://jobs.nuritas.tr/data/postings.json   the same data, machine-readable
    https://jobs.nuritas.tr/data/index.json      days + counts per version

The site is a **build product**: `runs/` and `applications/` are the sources, `site/` is generated,
and nothing is edited by hand. Filtering and search on the postings page run in the page itself, and
the postings data is embedded in that page, so it works from `file://` as well as over HTTPS.

## How it is hosted

| Piece | Value |
|---|---|
| Host | Cloudflare Worker `jobs`, static assets — not Git-connected. The legacy Pages project `jobs` (`22152776`) is kept but no longer owns the hostname |
| Custom domain | `jobs.nuritas.tr` (Worker custom domain, certificate issued by Cloudflare) |
| DNS | Worker-owned record for `jobs.nuritas.tr`, proxied, in zone `nuritas.tr` (`7257baf3aabec6330454cf8107569807`). It is created by the custom domain, not by hand |
| `workers.dev` | disabled — the deploy does not turn it on, so only the gated hostname answers |
| Account | `fd8920a9f28bb14514272af921939fd4` |
| Access app | `Seekter jobs (jobs.nuritas.tr)`, id `fdecba1a-7ee0-4e0f-b544-11289905026b` |
| Access policy | `Allow jobs.nuritas.tr` — `allow`, one email include, the account owner's address |
| Access session | 24h, `allowed_idps` = the `cloudflare` IdP (`9eb6a274-970f-41e7-9e21-f8658f87508`), `auto_redirect_to_identity` on |

That mirrors `wsdb.nuritas.tr` / `wslive.nuritas.tr` (see `/home/nuri/codes/docker/caddy/access/`
on the VPS for the older, scripted version of the same setup). The difference is the host: wsdb is
a Caddy vhost on the VPS, this is a Worker serving static assets, so there is no origin server and
nothing to keep running. Access is attached to the hostname, so it kept working across the move from
Pages to the Worker.

`robots.txt` disallows everything and every page carries `noindex, nofollow` — the board is
private, and behind Access it is unreachable without a session anyway.

## Publishing

    python3 scripts/site_build.py       # runs/ + applications/  ->  site/
    python3 scripts/site_publish.py     # build, deploy, verify   (this is the one the runs call)

`site_publish.py` uploads with the **cf CLI**, not wrangler: `cf pages deploy` refuses legacy Pages
outright ("Legacy Pages is not supported in cf"), so the board is a Worker with static assets.
`cf deploy` normally wants a dev-server toolchain in the project; for a plain directory that is dead
weight, so the script writes the Build Output Specification itself — `.cloudflare/output/v0/config.json`
plus `workers/default/worker.config.json` and `assets/` — and runs `cf deploy --prebuilt`, which
uploads it as-is. The Worker name, compatibility date and custom domain live in that generated
`worker.config.json`.

Credentials come from the cf CLI's own login (`cf auth login`), which the deploy uses directly and
which carries everything needed here. `CLOUDFLARE_API_TOKEN` is only read for the verification calls
to the API. No secret is ever printed, and none is stored in this repository.

Do **not** hand `cf`'s OAuth token to wrangler as `CLOUDFLARE_API_TOKEN`: wrangler expects an API
token there and answers `Authentication error [code: 10000] / Invalid access token [code: 9109]`.
That was the 2026-11-14 publish failure.

Verification is read back from Cloudflare rather than assumed: the Worker's latest deployment and its
custom domain come from the API, and the domain is fetched to confirm it answers. A `302` to
`stonetr.cloudflareaccess.com` is the expected answer for an unauthenticated client and proves both
the site and its gate are live.

    python3 scripts/site_publish.py --status    # what is live right now, changes nothing
    python3 scripts/site_publish.py --check     # build and report, deploy nothing
    python3 scripts/site_publish.py --no-build  # deploy the existing site/

Rollback is a Worker operation: versions are immutable and the deployments list is the history, so
`cf workers deployments` / `cf workers versions` can point production back at an earlier version.

## The daily jobs

Two launchd agents run the launcher; nothing else schedules a seekter run.

| When | Runs | Artifacts | Log |
|---|---|---|---|
| 09:00 | `python3 scripts/daily_alert.py --version linkedin` | `runs/linkedin/<date>/` | `/tmp/seekter-linkedin.log` |
| 09:30 | `python3 scripts/daily_alert.py --version non-linkedin` | `runs/non-linkedin/<date>/` | `/tmp/seekter-non-linkedin.log` |

Each launcher run does one OpenCode turn (the model route is `profile/route.json`, the keys come from
the macOS Keychain through `~/.local/bin/secret`), then `python3 scripts/site_publish.py`, then prints
the report. `apply` is false in both versions, so a run never submits anything — it collects, filters
and publishes. A non-zero exit posts a failure alert to T3 Code, which notifies the phone. `docs/HERMES-PORT.md` covers the route, the host, the keychain/launchd reason and the alert.

## The design system

One system, in `scripts/theme.py`, shared by the board (`site_build.py`) and the day pages
(`report_html.py`) — so a page cannot drift from the other. The visual vocabulary is **Vercel's
Geist** system, taken from the `popular-web-designs` skill (`templates/vercel.md`) with
`claude-design` driving the process:

- achromatic canvas (`#ffffff` / `#171717`), the gray scale carrying every layer of text
- **shadow-as-border** (`0 0 0 1px rgba(0,0,0,.08)`) instead of CSS borders on cards and tables
- Geist for text (400/500/600, negative tracking on headings), Geist Mono in uppercase for
  numbers, paths and technical labels
- colour is functional, never decorative: one link accent plus the status pills a job board
  genuinely needs (queued · pack ready · applied · interviewing · skipped), each a tinted pill
- surface archetype: **Monitor** — counts and status lead, no hero

The fonts are **self-hosted** from `assets/fonts/*.woff2` (Geist + Geist Mono, variable, latin +
latin-ext so Turkish names render). The earlier pair — Instrument Sans and JetBrains Mono — was
loaded from a Google Fonts link that only the day pages had, so the board fell back to system
defaults and the two never matched. A webfont that silently falls back is the most common
invisible break: the check is `document.fonts.check('16px Geist')` in the browser, and the tests
assert no page carries a CDN font link.

Theme is one control — `auto` / `light` / `dark`, key `sk-theme` — rendered by `theme.toggle()`
and wired by `theme.THEME_JS` in `<head>`, so the saved choice applies before the first paint and
follows you from the board to a day page and back. (It used to be wired after an early `return`
in the page script, which is why it did nothing on any page without a postings table.)

## Changing things

- **A third version** → add `profile/settings.<name>.json` (an overlay), give it a `run_dir` if the
  suffix is not the name you want, and it appears in `runs/<name>/` and on the board automatically;
  `site_build.py` reads whatever folders exist.
- **Another day or another posting field** → change `site_build.py`; the pages are generated, so
  nothing has to be migrated.
- **A different host** → `site/` is a plain directory. Caddy on the VPS would serve it from
  `/srv/jobs` with the same Access app; only `site_publish.py`'s deploy step changes.
- **Public instead of gated** → delete the Access app (or narrow its policy), and drop the
  `noindex` from `site_build.py` if the board should be findable.
