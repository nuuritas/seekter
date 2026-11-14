#!/usr/bin/env python3
"""Build the job-board site and publish it to the Worker behind jobs.nuritas.tr.

    python3 scripts/site_publish.py              # build, deploy, verify
    python3 scripts/site_publish.py --no-build   # deploy what is already in site/
    python3 scripts/site_publish.py --check      # build and report, deploy nothing
    python3 scripts/site_publish.py --status     # what is live right now

Why the cf CLI and not wrangler: the board is a Worker with static assets, not a legacy Pages
project. `cf pages deploy` refuses legacy Pages outright ("Legacy Pages is not supported in cf"),
while `cf deploy` uploads a directory of static assets as a Worker version. `wrangler pages deploy`
used to do this, but it takes an API token in CLOUDFLARE_API_TOKEN — and handing it the cf CLI's
OAuth token produced `Authentication error [code: 10000] / Invalid access token [code: 9109]`,
which is what failed the 2026-11-14 run. cf deploy signs in with the cf CLI's own login instead.

`cf deploy` normally expects a dev-server toolchain in the project (Vite plugin, wrangler...). For a
plain directory that is dead weight, so this script writes the Build Output Specification itself —
`.cloudflare/output/v0/config.json` plus `workers/default/worker.config.json` and `assets/` — and
calls `cf deploy --prebuilt`, which uploads it as-is. Everything here is stdlib.

Credentials: `cf auth login` once. `CLOUDFLARE_API_TOKEN` is only used for the read-back calls to the
API; the deploy itself uses whatever `cf` is logged in with. No secret is ever printed.

Verification is read back from Cloudflare, not assumed: the Worker's custom domain and its cert
come from the API, and the domain is fetched to confirm it answers — a 302 to
`<team>.cloudflareaccess.com` is the expected answer for an unauthenticated client and proves both
the site and its Access gate are live.
"""
import argparse
import datetime as dt
import json
import os
import pathlib
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

ACCOUNT_ID = os.environ.get("SEEKTER_CF_ACCOUNT", "fd8920a9f28bb14514272af921939fd4")
WORKER = os.environ.get("SEEKTER_CF_WORKER", "jobs")
DOMAIN = os.environ.get("SEEKTER_CF_WORKER_DOMAIN", "jobs.nuritas.tr")
COMPAT_DATE = os.environ.get("SEEKTER_CF_COMPAT_DATE", "2026-11-14")
API = "https://api.cloudflare.com/client/v4"
CF_CONFIG_DIRS = [pathlib.Path.home() / "Library/Preferences/cloudflare/config",
                  pathlib.Path.home() / ".config/cloudflare"]

# Build Output Specification (`.cloudflare/output/`), version v0: one root config, then one
# directory per Worker holding its config plus the assets uploaded with it.
BOS = ROOT / ".cloudflare/output/v0"
BOS_WORKER = BOS / "workers/default"


# ---------------------------------------------------------------- credentials
def cf_cli() -> str:
    """The cf CLI, wherever it is — a cron agent's PATH may not have ~/.local/bin."""
    for cand in (shutil.which("cf"), str(pathlib.Path.home() / ".local/bin/cf"),
                 "/opt/homebrew/bin/cf"):
        if cand and pathlib.Path(cand).exists():
            return cand
    return ""


def _read_config(path: pathlib.Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return {}


def _stale(cfg: dict, skew_s: int = 120) -> bool:
    """True when the stored OAuth access token is expired or about to be.

    The cf CLI's access token lasts about an hour and is renewed whenever the CLI itself runs.
    Reading the file directly therefore works right after an interactive `cf auth login` and
    fails an hour later — which is exactly what a daily cron publish would hit.
    """
    exp = cfg.get("expiration_time")
    if not exp:
        return False
    try:
        when = dt.datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
    except ValueError:
        return True
    return when <= dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=skew_s)


def _refresh() -> bool:
    """Ask the cf CLI to renew its token (a read-only call; it writes the new one to its config)."""
    cf = cf_cli()
    if not cf:
        return False
    env = dict(os.environ, PATH=run_path())
    try:
        p = subprocess.run([cf, "auth", "whoami"], capture_output=True, text=True,
                           env=env, timeout=90)
    except (OSError, subprocess.SubprocessError):
        return False
    return p.returncode == 0


_TOKEN_CACHE = ""


def token() -> str:
    """The API token for the read-back calls: environment first, else the cf CLI's own login,
    renewed first when the stored one has expired."""
    global _TOKEN_CACHE
    if _TOKEN_CACHE:
        return _TOKEN_CACHE
    for var in ("CLOUDFLARE_API_TOKEN", "CF_API_TOKEN"):
        if os.environ.get(var, "").strip():
            _TOKEN_CACHE = os.environ[var].strip()
            return _TOKEN_CACHE
    for d in CF_CONFIG_DIRS:
        if not d.is_dir():
            continue
        files = sorted(d.glob("*.json"), key=lambda p: (p.stem != "default", p.name))
        for p in files:
            cfg = _read_config(p)
            if not cfg.get("oauth_token"):
                continue
            if _stale(cfg):
                if _refresh():
                    cfg = _read_config(p)
                    print(f"auth: refreshed the cf CLI token for profile {p.stem!r}", file=sys.stderr)
                else:
                    print("auth: the stored token looks expired and cf could not renew it "
                          "(run `cf auth login`)", file=sys.stderr)
            tok = (cfg.get("oauth_token") or "").strip()
            if tok:
                print(f"auth: cf CLI profile {p.stem!r} (oauth token)", file=sys.stderr)
                _TOKEN_CACHE = tok
                return tok
    sys.exit("no Cloudflare token: set CLOUDFLARE_API_TOKEN, or log in with `cf auth login`")


def api(path: str, method: str = "GET", body=None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token()}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read() or b"{}"
        try:
            return json.loads(raw)
        except ValueError:
            return {"success": False, "raw": raw.decode(errors="replace")[:200]}


# ---------------------------------------------------------------- deploy
def build_site() -> dict:
    import site_build
    return site_build.build(ROOT / "site")


def node_dir() -> str:
    """Directory holding `node`, for the cf CLI.

    A cron agent does not necessarily inherit the login shell's PATH, and `cf` is a node script:
    without node on PATH it dies with `env: node: No such file or directory` (exit 127) even
    though the cf binary itself was found. Look where node actually lives.
    """
    for cand in (shutil.which("node"), "/opt/homebrew/bin/node", "/usr/local/bin/node"):
        if cand and pathlib.Path(cand).exists():
            return str(pathlib.Path(cand).parent)
    for p in sorted(pathlib.Path.home().glob(".hermes/tools/node-*/bin/node")):
        if p.exists():
            return str(p.parent)
    return ""


def run_path() -> str:
    ndir = node_dir()
    return ((ndir + os.pathsep) if ndir else "") + os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")


def write_bos() -> dict:
    """Write `.cloudflare/output/v0/` from site/, ready for `cf deploy --prebuilt`."""
    site = ROOT / "site"
    if not (site / "index.html").is_file():
        sys.exit("site/ has no index.html — run the build first (or drop --no-build)")
    if BOS_WORKER.exists():
        shutil.rmtree(BOS_WORKER)
    assets = BOS_WORKER / "assets"
    shutil.copytree(site, assets, ignore=shutil.ignore_patterns(".DS_Store"))
    BOS.mkdir(parents=True, exist_ok=True)
    root_cfg = {"buildContext": {"isPreview": False}, "accountId": ACCOUNT_ID}
    worker_cfg = {
        "name": WORKER,
        "compatibilityDate": COMPAT_DATE,
        "assets": {"notFoundHandling": "404-page"},
        "domains": [DOMAIN],
    }
    (BOS / "config.json").write_text(json.dumps(root_cfg, indent=2) + "\n", encoding="utf-8")
    (BOS_WORKER / "worker.config.json").write_text(json.dumps(worker_cfg, indent=2) + "\n",
                                                   encoding="utf-8")
    files = sum(1 for p in assets.rglob("*") if p.is_file())
    return {"files": files, "dir": str(BOS)}


def deploy() -> dict:
    """Upload site/ as a new version of the Worker; returns the version id and domain."""
    cf = cf_cli()
    if not cf:
        sys.exit("cf not found — install the cf CLI (https://developers.cloudflare.com/agent-setup/)")
    if not node_dir():
        sys.exit("node not found — cf is a node script; install node or put it on PATH")
    bos = write_bos()
    print(f"  staged {bos['files']} files in {bos['dir']}")
    p = subprocess.run([cf, "deploy", "--prebuilt"], capture_output=True, text=True,
                       env=dict(os.environ, PATH=run_path()), cwd=str(ROOT))
    out = (p.stdout or "") + (p.stderr or "")
    if p.returncode != 0:
        sys.exit(f"cf deploy failed (exit {p.returncode}):\n{out.strip()[-1500:]}")
    version = ""
    for line in out.splitlines():
        if "Current Version ID:" in line:
            version = line.split("Current Version ID:", 1)[1].strip()
    print(f"  deployed version {version or '(none reported)'}")
    return {"version": version}


def status() -> dict:
    """What Cloudflare says is live: the Worker, its latest deployment, and the custom domain."""
    w = api(f"/accounts/{ACCOUNT_ID}/workers/scripts/{WORKER}").get("result") or {}
    dep = api(f"/accounts/{ACCOUNT_ID}/workers/scripts/{WORKER}/deployments").get("result") or {}
    latest = (dep.get("deployments") or [{}])[0]
    version = ((latest.get("versions") or [{}])[0]).get("version_id")
    d = api(f"/accounts/{ACCOUNT_ID}/workers/domains").get("result") or []
    dom = next((x for x in d if x.get("hostname") == DOMAIN), {}) or {}
    return {"worker": w.get("id") or WORKER, "version": version,
            "deployed_on": latest.get("created_on"),
            "domain": dom.get("hostname"), "service": dom.get("service"),
            "zone": dom.get("zone_name"), "cert": dom.get("cert_id") or ""}


def probe(url: str) -> str:
    """Fetch the custom domain as an anonymous client would. No redirects followed."""
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **kw):
            return None
    op = urllib.request.build_opener(NoRedirect)
    try:
        with op.open(urllib.request.Request(url, headers={"User-Agent": "seekter-publish/1"}), timeout=30) as r:
            return f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        loc = e.headers.get("Location") or ""
        if e.code in (301, 302, 303, 307, 308) and "cloudflareaccess.com" in loc:
            return f"HTTP {e.code} -> Access login (the gate is up)"
        return f"HTTP {e.code} -> {loc[:80]}"
    except Exception as e:  # DNS not propagated yet, TLS not issued yet, offline
        return f"unreachable: {type(e).__name__}"


def main() -> int:
    ap = argparse.ArgumentParser(description="Build and publish the seekter job board.")
    ap.add_argument("--no-build", action="store_true", help="deploy the existing site/ directory")
    ap.add_argument("--check", action="store_true", help="build and report, deploy nothing")
    ap.add_argument("--status", action="store_true", help="print what is live, change nothing")
    a = ap.parse_args()

    if a.status:
        st = status()
        for k in ("worker", "version", "deployed_on", "domain", "service", "zone", "cert"):
            print(f"{k:12} {st.get(k)}")
        print(f"{'domain':12} {DOMAIN}: {probe('https://' + DOMAIN + '/')}")
        return 0

    if not a.no_build:
        r = build_site()
        t = r["totals"]
        print(f"built site/ — {t['postings']} postings, {t['open']} open, {t['applied']} applied")
    if a.check:
        print("check only: nothing deployed")
        return 0

    print(f"deploying to Worker {WORKER} (custom domain {DOMAIN})")
    deploy()
    st = status()
    print(f"live worker {st.get('worker')} — deployed {st.get('deployed_on')}")
    print(f"https://{DOMAIN}/  {probe('https://' + DOMAIN + '/')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
