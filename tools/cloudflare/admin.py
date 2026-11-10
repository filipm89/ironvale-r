"""Ironvale (Rust) server admin tool — for the Cloudflare Worker + D1 (see SETUP.md).

    python tools/cloudflare/admin.py check                             show Worker status and current versions
    python tools/cloudflare/admin.py release EXE URL                   tell the server about a new .exe
    python tools/cloudflare/admin.py release                           which release the server hands out now
    python tools/cloudflare/admin.py world WORLD_JSON URL              register a new world.json URL
    python tools/cloudflare/admin.py world                             which world version the server names
    python tools/cloudflare/admin.py backup [FILE]                     download everything to a JSON file
    python tools/cloudflare/admin.py import FILE                       restore from a backup file
    python tools/cloudflare/admin.py count                             row counts per table

Pass --url ADDRESS to override the Worker URL (default: IRONVALE_WORKER_URL env var).
Pass --key KEY or set IRONVALE_ADMIN_KEY to skip the interactive prompt.

"release" takes the .exe you just built and the GitHub download URL for it, verifies they match, then
posts the metadata to the server so every game updates itself on its next launch.

"world" does the same for worlds/world.json — the game downloads a fresh copy on launch when the
sha256 differs from its local cache.
"""

import hashlib
import json
import os
import ssl
import sys
import time
import urllib.request

TABLES = ("accounts", "sessions", "chars", "blocked")


def _ssl():
    """SSL context that works around the Python 3.8 Windows cert-store ASN1 bug."""
    try:
        return ssl.create_default_context()
    except ssl.SSLError:
        pass
    # Windows cert store has a malformed cert — build a context without it.
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    try:
        import certifi  # pip install certifi
        ctx.load_verify_locations(certifi.where())
    except Exception:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


_CTX = _ssl()
PAGE = 25
PAGE_BYTES = 4_000_000

HINTS = {
    "denied":   "the admin key doesn't match the Worker's ADMIN_KEY secret",
    "no_db":    "no D1 binding named DB — add it in the Cloudflare dashboard and redeploy",
    "server":   "the Worker threw an error — check Worker Logs in the Cloudflare dashboard",
}


def post(url, req, tries=4):
    body = json.dumps(req).encode("utf-8")
    for i in range(tries):
        try:
            r = urllib.request.Request(url, data=body,
                                       headers={"Content-Type": "application/json",
                                                "User-Agent": "Ironvale-admin/1"})
            with urllib.request.urlopen(r, timeout=60, context=_CTX) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            if i == tries - 1:
                raise
            print("  (retrying: %r)" % e)
            time.sleep(2 + 3 * i)


def explain(out):
    err = out.get("error", "unknown")
    return "%s — %s" % (err, HINTS[err]) if err in HINTS else str(err)


def pages(rows):
    out, size = [], 0
    for row in rows:
        n = len(json.dumps(row))
        if out and (len(out) >= PAGE or size + n > PAGE_BYTES):
            yield out
            out, size = [], 0
        out.append(row)
        size += n
    if out:
        yield out


def file_hash(path):
    h, n = hashlib.sha256(), 0
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
            n += len(block)
    return h.hexdigest(), n


def url_hash(url):
    h, n = hashlib.sha256(), 0
    r = urllib.request.Request(url, headers={"User-Agent": "Ironvale-admin/1"})
    with urllib.request.urlopen(r, timeout=120, context=_CTX) as resp:
        for block in iter(lambda: resp.read(1 << 20), b""):
            h.update(block)
            n += len(block)
    return h.hexdigest(), n


# -- check --------------------------------------------------------------------------------------------
def do_check(url):
    out = post(url, {"op": "hello"})
    if not out.get("ok"):
        print("Worker error: %s" % explain(out))
        return
    print("Worker OK — app=%s api=%s" % (out.get("app"), out.get("api")))
    rel = out.get("release")
    if rel:
        print("  Release:  v%s  (%d bytes)" % (rel["version"], rel["size"]))
        print("            %s" % rel["url"])
    else:
        print("  Release:  (none)")
    world = out.get("world")
    if world:
        print("  World:    %s" % world["version"][:16] + "...")
        print("            %s" % world["url"])
    else:
        print("  World:    (none)")


# -- release ------------------------------------------------------------------------------------------
def do_release_show(url):
    out = post(url, {"op": "hello"})
    rel = out.get("release")
    if rel:
        print("The server hands out version %s (%d bytes):\n  %s" % (rel["version"], rel["size"], rel["url"]))
    else:
        print("The server names no release yet.")


def do_release(url, key, exe, link, version=None):
    if not link.startswith("https://"):
        sys.exit("The URL must start with https://")
    sha, size = file_hash(exe)
    print("%s: %d bytes, sha256 %s" % (exe, size, sha))
    print("Downloading %s to verify..." % link)
    try:
        got_sha, got_size = url_hash(link)
    except Exception as e:
        sys.exit("Couldn't download: %r\nIs the GitHub release published (not a draft) and the repo public?" % e)
    if (got_sha, got_size) != (sha, size):
        sys.exit("The file at that URL doesn't match this .exe (%d bytes, sha256 %s)." % (got_size, got_sha))
    if not version:
        # read version from Cargo.toml beside this script's root
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        cargo = os.path.join(root, "Cargo.toml")
        if os.path.exists(cargo):
            for line in open(cargo, encoding="utf-8"):
                if line.startswith("version"):
                    version = line.split("=", 1)[1].strip().strip('"')
                    break
    if not version:
        sys.exit("Couldn't read version from Cargo.toml. Pass --version V.")
    out = post(url, {"op": "release", "key": key, "version": version, "url": link, "sha256": sha, "size": size})
    if not out.get("ok"):
        sys.exit("The server refused: %s" % explain(out))
    print("Released %s — every ironvale.exe older than that updates on next launch." % version)


# -- world --------------------------------------------------------------------------------------------
def do_world_show(url):
    out = post(url, {"op": "hello"})
    world = out.get("world")
    if world:
        print("World sha256: %s\n  %s" % (world["version"], world["url"]))
    else:
        print("The server names no world yet.")


def do_world(url, key, path, link):
    if not link.startswith("https://"):
        sys.exit("The URL must start with https://")
    sha, size = file_hash(path)
    print("%s: %d bytes, sha256 %s" % (path, size, sha))
    print("Downloading %s to verify..." % link)
    try:
        got_sha, got_size = url_hash(link)
    except Exception as e:
        sys.exit("Couldn't download: %r" % e)
    if (got_sha, got_size) != (sha, size):
        sys.exit("The file at that URL doesn't match this world.json (%d bytes, sha256 %s)." % (got_size, got_sha))
    out = post(url, {"op": "world", "key": key, "version": sha, "url": link})
    if not out.get("ok"):
        sys.exit("The server refused: %s" % explain(out))
    print("World registered — games will download the new world.json on next launch.")


# -- backup / import / count --------------------------------------------------------------------------
def fetch_all(url, key):
    data = {"app": "ironvale_rust", "exported": time.time()}
    for table in TABLES:
        rows, after = [], ""
        while True:
            out = post(url, {"op": "export", "key": key, "table": table, "after": after})
            if not out.get("ok"):
                sys.exit("Server refused export of %s: %s" % (table, explain(out)))
            rows += out["rows"]
            if not out["more"] or not out["rows"]:
                break
            after = out["rows"][-1][next(iter(out["rows"][-1]))]
        data[table] = rows
    return data


def do_backup(url, key, path):
    data = fetch_all(url, key)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    print("Saved %s: %s" % (path, ", ".join("%d %s" % (len(data[t]), t) for t in TABLES)))


def do_import(url, key, path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if data.get("app") not in ("ironvale_rust", "ironvale"):
        sys.exit("%s doesn't look like an Ironvale backup." % path)
    for table in TABLES:
        rows = data.get(table) or []
        done = 0
        for chunk in pages(rows):
            out = post(url, {"op": "import", "key": key, "table": table, "rows": chunk})
            if not out.get("ok"):
                sys.exit("Server refused %s import: %s" % (table, explain(out)))
            done += out["added"]
        print("%-9s %d" % (table, done))
    print("Done. Run 'count' to verify.")


def do_count(url, key):
    data = fetch_all(url, key)
    for t in TABLES:
        print("%-9s %d" % (t, len(data[t])))
    print("saves     %d" % sum(1 for c in data["chars"] if c.get("data")))


def ask_key():
    print("Paste the admin key (right-click or Ctrl+V), then press Enter.")
    return input("Admin key: ")


def main(args):
    url = key = None
    extra_ver = None

    for flag in ("--url", "--key", "--version"):
        if flag in args:
            i = args.index(flag)
            val = args[i + 1]
            del args[i:i + 2]
            if flag == "--url":
                url = val
            elif flag == "--key":
                key = val
            elif flag == "--version":
                extra_ver = val  # noqa: F841 (used only for release override if added later)

    cmds = ("check", "release", "world", "backup", "import", "count")
    if not args or args[0] not in cmds:
        print(__doc__)
        return 2

    if url is None:
        url = os.environ.get("IRONVALE_WORKER_URL", "").strip()
    if not url:
        sys.exit("No Worker URL. Set IRONVALE_WORKER_URL or pass --url ADDRESS.")

    cmd = args[0]

    if cmd == "check":
        do_check(url)
        return 0

    if cmd == "release" and len(args) == 1:
        do_release_show(url)
        return 0

    if cmd == "world" and len(args) == 1:
        do_world_show(url)
        return 0

    key = key or os.environ.get("IRONVALE_ADMIN_KEY") or ask_key()
    key = key.strip().strip("\"'")

    if cmd == "release":
        if len(args) != 3:
            sys.exit("Usage: python tools/cloudflare/admin.py release target\\release\\ironvale.exe https://github.com/.../ironvale.exe")
        do_release(url, key, args[1], args[2], version=extra_ver)

    elif cmd == "world":
        if len(args) != 3:
            sys.exit("Usage: python tools/cloudflare/admin.py world worlds\\world.json https://github.com/.../world.json")
        do_world(url, key, args[1], args[2])

    elif cmd == "backup":
        do_backup(url, key, args[1] if len(args) > 1 else time.strftime("ironvale-backup-%Y-%m-%d.json"))

    elif cmd == "import":
        if len(args) < 2:
            sys.exit("Usage: python tools/cloudflare/admin.py import ironvale-backup-YYYY-MM-DD.json")
        do_import(url, key, args[1])

    elif cmd == "count":
        do_count(url, key)

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
