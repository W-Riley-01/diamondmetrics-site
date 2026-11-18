"""Tiny static server for testing: applies the rules in _headers (like Cloudflare Pages) and can mimic
Pages' single-page fallback (unknown path -> index.html with 200) when --spa is given."""
import argparse, fnmatch, http.server, os, socketserver, sys

ap = argparse.ArgumentParser()
ap.add_argument("root")
ap.add_argument("--port", type=int, default=8765)
ap.add_argument("--spa", action="store_true")
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

class H(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kw):
        super().__init__(*args, directory=ROOT, **kw)
    def log_message(self, *args): pass
    def send_head(self):
        p = self.path.split("?")[0]
        fs = os.path.join(ROOT, p.lstrip("/"))
        if a.spa and p != "/" and not os.path.exists(fs):
            self.path = "/index.html"
        return super().send_head()
    def end_headers(self):
        p = self.path.split("?")[0]
        orig = self.requestline.split(" ")[1].split("?")[0]
        for pat, hdrs in RULES:
            if fnmatch.fnmatch(orig, pat):
                for k, v in hdrs:
                    self.send_header(k, v)
        super().end_headers()

socketserver.TCPServer.allow_reuse_address = True
with socketserver.ThreadingTCPServer(("127.0.0.1", a.port), H) as s:
    s.serve_forever()
