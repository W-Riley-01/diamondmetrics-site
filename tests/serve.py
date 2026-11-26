"""Tiny static server for the browser tests: applies the rules in _headers and mimics the site's routing on Cloudflare
(tests/test_routes.py checks the real thing with wrangler dev):
  /                      index.html
  /beat-the-streak       beat-the-streak.html (any extensionless path with a .html file beside it)
  /game/{1-8 digits}     game.html (the Worker's job in production)
  /game, /game/...       404.html with status 404
  anything else missing  404.html with status 404 (assets not_found_handling "404-page")
It doesn't do the 307 redirects for /x.html and /x/; test_routes.py covers those."""
import argparse, fnmatch, http.server, os, re, socketserver

ap = argparse.ArgumentParser()
ap.add_argument("root")
ap.add_argument("--port", type=int, default=8765)
a = ap.parse_args()
ROOT = os.path.abspath(a.root)


def parse_headers(path):
    rules, cur = [], None
    if not os.path.exists(path):
        return rules
    for raw in open(path, encoding="utf-8"):
        line = raw.rstrip("\n")
        if not line.strip():
            continue
        if not line.startswith((" ", "\t")):
            cur = (line.strip(), []); rules.append(cur)
        else:
            k, v = line.strip().split(":", 1); cur[1].append((k.strip(), v.strip()))
    return rules


RULES = parse_headers(os.path.join(ROOT, "_headers"))


def resolve(p):
    """Request path -> (file path under ROOT, status)."""
    if p == "/":
        return "index.html", 200
    if re.fullmatch(r"/game/\d{1,8}", p):
        return "game.html", 200
    if p == "/game" or p.startswith("/game/"):
        return "404.html", 404
    rel = p.lstrip("/")
    fs = os.path.join(ROOT, rel)
    if ".." not in rel.split("/") and os.path.isfile(fs):
        return rel, 200
    if ".." not in rel.split("/") and os.path.isfile(fs + ".html"):
        return rel + ".html", 200
    return "404.html", 404


class H(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kw):
        super().__init__(*args, directory=ROOT, **kw)

    def log_message(self, *args):
        pass

    def send_head(self):
        rel, status = resolve(self.path.split("?")[0])
        fs = os.path.join(ROOT, rel)
        f = open(fs, "rb")
        self.send_response(status)
        self.send_header("Content-Type", self.guess_type(fs))
        self.send_header("Content-Length", str(os.fstat(f.fileno()).st_size))
        self.end_headers()
        return f

    def end_headers(self):
        orig = self.requestline.split(" ")[1].split("?")[0]
        for pat, hdrs in RULES:
            if fnmatch.fnmatch(orig, pat):
                for k, v in hdrs:
                    self.send_header(k, v)
        super().end_headers()


socketserver.TCPServer.allow_reuse_address = True
with socketserver.ThreadingTCPServer(("127.0.0.1", a.port), H) as s:
    s.serve_forever()
