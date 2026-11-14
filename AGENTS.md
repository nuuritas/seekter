# AGENTS.md

The agent notes for this repository live in **`CLAUDE.md`** — read it first. It says where things
are and the rules that hold in every session. This file only records how publishing works, because
that changed on 2026-11-14.

## Publishing the board

`scripts/site_publish.py` builds `site/` and deploys it to the Cloudflare **Worker** `jobs` (static
assets) behind `jobs.nuritas.tr`, using the **cf CLI**:

    python3 scripts/site_publish.py            # build, deploy, verify
    python3 scripts/site_publish.py --status   # what is live, changes nothing

- The upload is `cf deploy --prebuilt`, fed by the Build Output Specification the script writes to
  `.cloudflare/output/v0/` (`config.json`, `workers/default/worker.config.json`, `assets/`). Do not
  add a dev-server toolchain (Vite plugin, wrangler) just to satisfy `cf deploy`.
- **Do not go back to wrangler**, and never hand cf's OAuth token to it as `CLOUDFLARE_API_TOKEN`:
  wrangler expects an API token there and answers `Authentication error [code: 10000] / Invalid
  access token [code: 9109]` — that is the failure this replaced.
- `cf pages deploy` is not an option: the cf CLI refuses legacy Pages outright.
- Verification is read back, not assumed. `--status` prints the Worker version, the custom domain and
  a probe of the domain; a `302` to `cloudflareaccess.com` is the expected anonymous answer and means
  both the site and its Access gate are live.
