# NSW imagery tile server

A caching proxy for NSW Spatial Services aerial imagery (CC BY 4.0), with a
map viewer. Python 3.10+ and Pillow, managed with [Poetry](https://python-poetry.org/).

```bash
poetry install            # creates a virtualenv and installs Pillow from poetry.lock
poetry run python server.py   # then open http://localhost:8080
```

Add a dependency with `poetry add <package>` (e.g. `poetry add rasterio` for
exercise 3); that updates both `pyproject.toml` and `poetry.lock`.

Pre-fetch an area (always check the estimate first):

```bash
poetry run python seed.py --bbox 151.19 -33.875 151.225 -33.85 --zmin 12 --zmax 19 --dry-run
poetry run python seed.py --bbox 151.19 -33.875 151.225 -33.85 --zmin 12 --zmax 19
```

## Running the cache on a droplet

Keep the cache (and all traffic to NSW) on a DigitalOcean droplet, and run
only the viewer locally:

```
browser ─► local server.py --remote ─(SSH tunnel)─► droplet server.py ─► NSW
           no cache, just forwards                  cache, dedupe, rate limit
```

The droplet's server stays bound to `127.0.0.1`, so it is **not** reachable
from the internet. The SSH tunnel is the only way in, which gives you
authentication and encryption without writing any of it yourself.

**On the droplet, once** (Ubuntu, as root):

```bash
apt install -y python3 python3-venv pipx git
adduser --system --group tiles
git clone https://github.com/JesseWilliamson/nsw-tiles.git /opt/nsw-tiles
cd /opt/nsw-tiles && pipx run poetry install --only main
cp deploy/nsw-tiles.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now nsw-tiles
curl -s localhost:8080/stats    # should print JSON
```

`poetry.toml` puts the virtualenv at `/opt/nsw-tiles/.venv`, which is the path
the systemd unit runs. The cache lives in `/var/lib/nsw-tiles`, which systemd
creates and owns for the `tiles` user. To update later: `git pull`, then
`systemctl restart nsw-tiles`.

**On your machine, each session:**

```bash
ssh -N -L 9000:localhost:8080 root@<droplet-ip>          # terminal 1: the tunnel
poetry run python server.py --remote http://localhost:9000   # terminal 2
```

Then open http://localhost:8080. `-L 9000:localhost:8080` means "connections
to my port 9000 come out of the droplet and go to *its* localhost:8080".
`-N` means "no shell, just the tunnel". If the tunnel drops, tiles return 502
until you reconnect.

Seeding runs on the droplet, where the cache is:
`cd /opt/nsw-tiles && sudo -u tiles .venv/bin/python seed.py --cache-dir /var/lib/nsw-tiles ...`

## Files

| File | What it teaches |
|---|---|
| `tilemath.py` | lon/lat ⇄ tile ⇄ Web Mercator metres, ground resolution. Compare it with your own `lonlat_to_tile`. |
| `server.py` | The request pipeline: coverage check → disk cache → in-flight dedupe → rate limiter → upstream → overzoom fallback. |
| `seed.py` | Why tile counts explode: each zoom level is 4× the one before. |
| `viewer/index.html` | A Leaflet layer that fetches tiles itself so it can read the `X-Tile-Source` header. Tick "Show tiles and sources" to see the grid. |

## Design decisions worth understanding

- **Axis order.** ArcGIS URLs are `tile/{z}/{y}/{x}`, row first. Slippy maps
  are `z/x/y`. Swap them and you get real imagery from the wrong place.
- **Negative cache.** A `.none` file records "upstream has nothing here",
  so empty areas cost one request ever, not one per page view.
- **Atomic writes.** Tiles are written to a temp file and `os.replace`d into
  place, so a concurrent reader never sees half a JPEG.
- **Dedupe.** The first thread to ask for a tile fetches it. Others wait on a
  `threading.Event`. Ten simultaneous viewers → one upstream request.
- **Overzoom.** Past the deepest real tile, the server crops a quarter (or a
  sixteenth...) of an ancestor and enlarges it. No new detail, just bigger
  pixels. The viewer marks these in red.
- **Magic bytes.** The service format is `MIXED` (JPEG inside the coverage,
  PNG at the edges), so the file type is sniffed from the first bytes,
  never trusted from headers. Fully transparent PNGs count as empty.

## Being a good client

The service reports `exportTilesAllowed: false`. Keep the rate limit
modest, seed only areas you need, and keep the attribution visible. It's a
licence condition of CC BY.

## Exercises

1. **Expiry.** Cached tiles and `.none` markers never expire, so new NSW
   imagery never shows up. Add a max age (hint: `Path.stat().st_mtime`) and
   refetch when it's exceeded.
2. **Smarter overzoom.** A z21 request currently tries z21, z20, z19... upstream
   in turn. Remember the deepest level that had data for each area so later
   requests skip straight to it.
3. **Second source.** Add Digital Earth Australia's 10 m Sentinel-2 geomedian
   for areas outside NSW. That's the raster half of the project: reading a COG
   window with `rasterio` and reprojecting it to EPSG:3857.
4. **PMTiles export.** Pack the `cache/` folder into one `.pmtiles` file that
   can be hosted on any static file host, with no server.
