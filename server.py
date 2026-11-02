"""
A caching tile proxy for NSW Spatial Services imagery, plus a map viewer.

    python server.py            ->  open http://localhost:8080

What happens when the viewer asks for /tiles/z/x/y:

  1. Coverage check  - is the tile anywhere near NSW? If not, answer with a
                       transparent tile and never touch the upstream server.
  2. Disk cache      - already have it? Serve it (HIT).
  3. In-flight dedupe- is another thread already fetching this exact tile?
                       Wait for its result instead of asking twice.
  4. Rate limiter    - take a token (max N upstream requests/sec), then fetch.
  5. Overzoom        - upstream has nothing this deep? Crop and enlarge the
                       nearest ancestor tile that does exist.

Licence: NSW Imagery is CC BY 4.0. The viewer shows the required attribution.
"""

import argparse
import io
import json
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image

import tilemath

HERE = Path(__file__).resolve().parent

# ArcGIS cached services put ROW (y) before COLUMN (x): .../tile/{z}/{y}/{x}.
# Slippy maps use z/x/y. Getting this backwards is the classic bug: you get
# real-looking tiles from somewhere else entirely, mirrored across the diagonal.
DEFAULT_UPSTREAM = ("https://maps.six.nsw.gov.au/arcgis/rest/services/public/"
                    "NSW_Imagery/MapServer/tile/{z}/{y}/{x}")

# fullExtent from the service's ?f=json metadata, in Web Mercator metres.
NSW_EXTENT = (15409760.4672, -4633381.2352, 17732075.8283, -3058843.9011)

ATTRIBUTION = ("© State of New South Wales (Spatial Services, a business unit "
               "of the Department of Customer Service NSW), CC BY 4.0")
USER_AGENT = "nsw-tiles-learning-project/0.1 (personal caching proxy)"


class TokenBucket:
    """Allow `rate` events per second on average, with bursts up to `burst`.

    Tokens refill continuously. acquire() blocks until one is available.
    This is what keeps us a polite client of a free government service.
    """

    def __init__(self, rate: float, burst: int):
        self.rate, self.capacity = rate, burst
        self.tokens = float(burst)
        self.last = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self):
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
                self.last = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                wait = (1 - self.tokens) / self.rate
            time.sleep(wait)  # sleep OUTSIDE the lock so other threads can check


class TileStore:
    """Disk cache + upstream fetcher. Thread-safe."""

    def __init__(self, cache_dir: Path, upstream: str, rate: float, overzoom_levels: int):
        self.cache_dir = cache_dir
        self.upstream = upstream
        self.bucket = TokenBucket(rate, burst=max(1, int(rate * 2)))
        self.overzoom_levels = overzoom_levels
        self.inflight: dict[tuple, threading.Event] = {}
        self.inflight_lock = threading.Lock()
        self.stats = {k: 0 for k in ("hit", "miss", "overzoom", "outside", "empty",
                                     "error", "upstream_requests", "deduped")}
        self.stats_lock = threading.Lock()

    def count(self, key):
        with self.stats_lock:
            self.stats[key] += 1

    # ---------- cache layout: cache/z/x/y.jpg | .png | .none ----------
    # ".none" is a NEGATIVE cache entry: upstream said "no tile here".
    # Without it we'd re-ask for every empty tile on every pan.

    def _cached(self, z, x, y):
        d = self.cache_dir / str(z) / str(x)
        for ext in ("jpg", "png", "none"):
            p = d / f"{y}.{ext}"
            if p.exists():
                return p
        return None

    def _write(self, z, x, y, ext, data: bytes):
        d = self.cache_dir / str(z) / str(x)
        d.mkdir(parents=True, exist_ok=True)
        final = d / f"{y}.{ext}"
        tmp = final.with_suffix(f".{ext}.tmp{threading.get_ident()}")
        tmp.write_bytes(data)
        os.replace(tmp, final)  # atomic: readers never see a half-written file

    # ---------- the pipeline ----------

    def get(self, z, x, y):
        """Return (bytes | None, source). None means 'nothing to show here'."""
        if not intersects_nsw(z, x, y):
            self.count("outside")
            return None, "outside"

        data, source = self._get_native(z, x, y)
        if data is not None:
            return data, source

        # Nothing native at this zoom. Try enlarging an ancestor.
        for k in range(1, self.overzoom_levels + 1):
            if z - k < 0:
                break
            az, ax, ay = tilemath.parent(z, x, y, k)
            anc, _ = self._get_native(az, ax, ay)
            if anc is not None:
                self.count("overzoom")
                return overzoom(anc, k, x - (ax << k), y - (ay << k)), f"overzoom-{k}"
        self.count("empty")
        return None, "empty"

    def _get_native(self, z, x, y):
        p = self._cached(z, x, y)
        if p is not None:
            self.count("hit")
            return (None if p.suffix == ".none" else p.read_bytes()), "hit"

        key = (z, x, y)
        with self.inflight_lock:
            event = self.inflight.get(key)
            leader = event is None
            if leader:
                event = self.inflight[key] = threading.Event()

        if not leader:
            # Someone else is fetching it. Wait, then read what they cached.
            self.count("deduped")
            event.wait(timeout=30)
            p = self._cached(z, x, y)
            if p is None:
                return None, "error"
            return (None if p.suffix == ".none" else p.read_bytes()), "hit"

        try:
            self.count("miss")
            return self._fetch_and_cache(z, x, y), "miss"
        finally:
            with self.inflight_lock:
                del self.inflight[key]
            event.set()

    def _fetch_and_cache(self, z, x, y):
        self.bucket.acquire()
        self.count("upstream_requests")
        url = self.upstream.format(z=z, x=x, y=y)
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:  # ArcGIS's way of saying "not cached at this level"
                self._write(z, x, y, "none", b"")
                return None
            self.count("error")
            raise
        ext = sniff(data)
        if ext is None or is_blank(data, ext):
            # A 200 with an error page or a fully transparent tile counts as empty.
            self._write(z, x, y, "none", b"")
            return None
        self._write(z, x, y, ext, data)
        return data


def intersects_nsw(z, x, y) -> bool:
    xmin, ymin, xmax, ymax = tilemath.tile_bounds_mercator(x, y, z)
    exmin, eymin, exmax, eymax = NSW_EXTENT
    return not (xmax < exmin or xmin > exmax or ymax < eymin or ymin > eymax)


def sniff(data: bytes):
    """Identify an image by its magic bytes, never by what the server claims."""
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    return None


def is_blank(data: bytes, ext: str) -> bool:
    """The service's format is MIXED: JPEG inside the coverage, PNG with
    transparency along its edges. A PNG with no opaque pixel is just padding."""
    if ext != "png":
        return False
    img = Image.open(io.BytesIO(data))
    if "A" not in img.getbands() and img.info.get("transparency") is None:
        return False
    alpha = img.convert("RGBA").getchannel("A")
    return alpha.getextrema()[1] == 0


def overzoom(ancestor: bytes, levels: int, dx: int, dy: int) -> bytes:
    """Cut the (dx, dy) sub-square out of an ancestor tile and enlarge it.

    `levels` up, the ancestor holds a 2^levels x 2^levels grid of our tile.
    No new detail appears. We're just stretching pixels, which is why the
    viewer marks these tiles.
    """
    img = Image.open(io.BytesIO(ancestor)).convert("RGBA")
    span = tilemath.TILE_SIZE >> levels  # e.g. 128 px for one level up
    box = (dx * span, dy * span, (dx + 1) * span, (dy + 1) * span)
    out = img.crop(box).resize((tilemath.TILE_SIZE,) * 2, Image.Resampling.BICUBIC)
    buf = io.BytesIO()
    if out.getchannel("A").getextrema()[0] == 255:
        out.convert("RGB").save(buf, "JPEG", quality=88)
    else:
        out.save(buf, "PNG")
    return buf.getvalue()


def make_handler(store: TileStore, max_zoom: int):
    transparent = io.BytesIO()
    Image.new("RGBA", (256, 256), (0, 0, 0, 0)).save(transparent, "PNG")
    transparent = transparent.getvalue()
    viewer_html = (HERE / "viewer" / "index.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass  # tile servers are chatty; stats are on /stats instead

        def _send(self, code, body, ctype, extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                return self._send(200, viewer_html, "text/html; charset=utf-8")
            if path == "/stats":
                with store.stats_lock:
                    s = dict(store.stats)
                s["attribution"] = ATTRIBUTION
                return self._send(200, json.dumps(s).encode(), "application/json",
                                  {"Cache-Control": "no-store"})
            parts = path.strip("/").split("/")
            if len(parts) == 4 and parts[0] == "tiles":
                try:
                    z, x, y = (int(p.split(".")[0]) for p in parts[1:])
                except ValueError:
                    return self._send(400, b"bad tile path", "text/plain")
                n = 2 ** z
                if not (0 <= z <= max_zoom and 0 <= x < n and 0 <= y < n):
                    return self._send(404, b"no such tile", "text/plain")
                try:
                    data, source = store.get(z, x, y)
                except Exception as e:  # upstream down, timeout...
                    return self._send(502, f"upstream error: {e}".encode(), "text/plain")
                headers = {"X-Tile-Source": source,
                           "Access-Control-Expose-Headers": "X-Tile-Source",
                           "Cache-Control": "public, max-age=86400"}
                if data is None:
                    return self._send(200, transparent, "image/png", headers)
                ctype = "image/jpeg" if sniff(data) == "jpg" else "image/png"
                return self._send(200, data, ctype, headers)
            self._send(404, b"not found", "text/plain")

    return Handler


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--cache-dir", type=Path, default=HERE / "cache")
    ap.add_argument("--upstream", default=DEFAULT_UPSTREAM)
    ap.add_argument("--rate", type=float, default=8.0,
                    help="max upstream requests per second (default 8)")
    ap.add_argument("--max-zoom", type=int, default=21)
    ap.add_argument("--overzoom-levels", type=int, default=3,
                    help="how many zooms past the real data to stretch (default 3)")
    args = ap.parse_args()

    store = TileStore(args.cache_dir, args.upstream, args.rate, args.overzoom_levels)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(store, args.max_zoom))
    print(f"Serving on http://localhost:{args.port}  (cache: {args.cache_dir})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
