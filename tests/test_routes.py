"""Routing tests against the real thing: `wrangler dev` (the pinned wrangler, the same asset routing and Worker as
production) serving public/ (run: python tests/test_routes.py; needs `npm ci` first). No request leaves this machine:
none of the URLs checked here reach the MLB Stats API.
Checks every page URL, the redirects html_handling makes, the 404 page for unknown paths, the Worker's /game routes,
and that every HTML response carries the same security headers as public/_headers."""
import http.client, os, socket, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
PUBLIC = os.path.join(ROOT, "public")
PORT = 8790
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else "  -> " + str(detail)))


def security_headers():
    """The headers public/_headers sets for /* (the rule every page gets)."""
    out, cur = {}, None
    for raw in open(os.path.join(PUBLIC, "_headers"), encoding="utf-8"):
        line = raw.rstrip("\n")
        if not line.strip():
            continue
        if not line.startswith((" ", "\t")):
            cur = line.strip()
        elif cur == "/*":
            k, v = line.strip().split(":", 1)
            out[k.strip().lower()] = v.strip()
    return out


def req(path, method="GET", headers=None):
    """One request, redirects not followed: (status, headers dict lower-case, body bytes)."""
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=15)
    c.request(method, path, headers=headers or {})
    r = c.getresponse()
    body = r.read()
    out = (r.status, {k.lower(): v for k, v in r.getheaders()}, body)
    c.close()
    return out


def start():
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", PORT)) == 0:
            sys.exit(f"port {PORT} is already in use (a server left over from an earlier run?); stop it and run again")
    exe = os.path.join(ROOT, "node_modules", ".bin", "wrangler.cmd" if os.name == "nt" else "wrangler")
    env = dict(os.environ, WRANGLER_SEND_METRICS="false", CI="1")
    p = subprocess.Popen([exe, "dev", "--port", str(PORT), "--ip", "127.0.0.1"], cwd=ROOT, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=False)
    for _ in range(120):
        try:
            req("/")
            return p
        except OSError:
            time.sleep(0.5)
    stop(p)
    sys.exit("wrangler dev did not start")


def stop(p):
    # wrangler starts node and workerd as children; stop the whole tree
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        p.terminate()
    p.wait(timeout=20)


def main():
    sec = security_headers()
    check("_headers: the five security headers for every page", sorted(sec) == ["content-security-policy", "permissions-policy", "referrer-policy", "x-content-type-options", "x-frame-options"], sec)
    file = lambda name: open(os.path.join(PUBLIC, name), "rb").read()
    p = start()
    try:
        def page(path, status, name, label):
            st, hd, body = req(path)
            check(f"{label}: {path} is {status} with {name}", st == status and body == file(name), (st, body[:80]))
            missing = {k: (hd.get(k), v) for k, v in sec.items() if hd.get(k) != v}
            check(f"{label}: {path} carries every security header from _headers", not missing, missing)
            check(f"{label}: {path} is HTML", hd.get("content-type", "").startswith("text/html"), hd.get("content-type"))

        page("/", 200, "index.html", "home")
        page("/beat-the-streak", 200, "beat-the-streak.html", "Beat the Streak")
        for gid in ("849799", "1", "12345678"):
            page(f"/game/{gid}", 200, "game.html", "game (Worker)")
        for bad in ("/game", "/game/", "/game/abc", "/game/123456789", "/game/1/x", "/game/849799/"):
            page(bad, 404, "404.html", "bad game path (Worker)")
        for unknown in ("/nope", "/how-the-model-works", "/data/nope.json", "/games", "/beat-the-streak/x"):
            page(unknown, 404, "404.html", "unknown path")

        for path, to in (("/beat-the-streak.html", "/beat-the-streak"), ("/beat-the-streak/", "/beat-the-streak"),
                         ("/index.html", "/"), ("/game.html", "/game"), ("/404.html", "/404")):
            st, hd, _ = req(path)
            check(f"redirect: {path} is a 307 to {to}", st == 307 and hd.get("location") == to, (st, hd.get("location")))
        st, _, body = req("/game.html", headers={})
        st2, _, body2 = req("/game")
        check("redirect: /game.html ends on the 404 page (its target, /game, is the Worker's 404)", st == 307 and st2 == 404 and body2 == file("404.html"))

        st, hd, body = req("/game/849799", "HEAD")
        check("game: HEAD is 200 with no body", st == 200 and body == b"", (st, body[:40]))
        st, hd, _ = req("/game/849799", "POST")
        check("game: POST is 405 (GET and HEAD only)", st == 405 and hd.get("allow") == "GET, HEAD", (st, hd.get("allow")))

        for path in ("/styles.css", "/app.js", "/data/page.json", "/fonts/big-shoulders-latin-wght-normal.woff2", "/favicon.svg"):
            st, hd, body = req(path)
            check(f"asset: {path} is served from the site root", st == 200 and body == file(path.lstrip("/")) and hd.get("content-security-policy") == sec["content-security-policy"], st)

        st, hd, body = req("/api/slate/2026-02-30")
        check("api: /api/slate with an impossible date is 400 from the Worker", st == 400 and hd.get("content-type") == "application/json; charset=utf-8" and b"YYYY-MM-DD" in body, (st, body))
        st, hd, body = req("/api/linescore/abc")
        check("api: the other endpoints still answer (400 for a bad gamePk)", st == 400, st)
    finally:
        stop(p)
    bad = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(bad)} of {len(RESULTS)} checks passed")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
