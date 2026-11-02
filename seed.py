"""
Pre-fetch ("seed") tiles for an area into the same cache server.py uses.

    python seed.py --bbox 151.19 -33.875 151.225 -33.85 --zmin 12 --zmax 19

It prints how many tiles that is and how long it will take BEFORE fetching
anything. Run the numbers before every seed: tile counts multiply by 4 with
each zoom level, so one extra level costs 3x everything above it combined.

Note: the NSW service reports exportTilesAllowed: false. That disables its
bulk-export endpoint, and is a signal that whole-state scraping isn't
welcome. Seed the areas you actually care about, at a polite rate.
"""

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import tilemath
from server import DEFAULT_UPSTREAM, HERE, TileStore, intersects_nsw

AVG_TILE_KB = 25  # rough figure for JPEG aerial tiles; varies with terrain
CONFIRM_ABOVE = 5000


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bbox", nargs=4, type=float, required=True,
                    metavar=("WEST", "SOUTH", "EAST", "NORTH"))
    ap.add_argument("--zmin", type=int, default=10)
    ap.add_argument("--zmax", type=int, default=18)
    ap.add_argument("--rate", type=float, default=4.0,
                    help="requests/sec, lower than the server default (default 4)")
    ap.add_argument("--cache-dir", type=Path, default=HERE / "cache")
    ap.add_argument("--upstream", default=DEFAULT_UPSTREAM)
    ap.add_argument("--yes", action="store_true", help=f"skip the confirm above {CONFIRM_ABOVE} tiles")
    ap.add_argument("--dry-run", action="store_true", help="print the estimate and exit")
    args = ap.parse_args()

    w, s, e, n = args.bbox
    print(f"{'zoom':>4} {'tiles':>10} {'m/px here':>10}")
    for z in range(args.zmin, args.zmax + 1):
        print(f"{z:>4} {tilemath.count_tiles(w, s, e, n, z, z):>10,} "
              f"{tilemath.meters_per_pixel(z, (s + n) / 2):>10.2f}")
    total = tilemath.count_tiles(w, s, e, n, args.zmin, args.zmax)
    hours = total / args.rate / 3600
    print(f"\ntotal {total:,} tiles  ~{total * AVG_TILE_KB / 1024:,.0f} MB  "
          f"~{hours:.1f} h at {args.rate}/s (upper bound: cached tiles are skipped)")

    if args.dry_run:
        return
    if total > CONFIRM_ABOVE and not args.yes:
        if input("Continue? [y/N] ").strip().lower() != "y":
            sys.exit(0)

    store = TileStore(args.cache_dir, args.upstream, args.rate, overzoom_levels=0)

    def jobs():
        for z in range(args.zmin, args.zmax + 1):
            x0, x1, y0, y1 = tilemath.tiles_in_bbox(w, s, e, n, z)
            for x in range(x0, x1 + 1):
                for y in range(y0, y1 + 1):
                    if intersects_nsw(z, x, y):
                        yield z, x, y

    def work(t):
        try:
            store._get_native(*t)
        except Exception as ex:
            print(f"\n  {t}: {ex}")

    start, done = time.monotonic(), 0
    # Threads overlap network waiting; the token bucket still caps the rate.
    with ThreadPoolExecutor(max_workers=4) as pool:
        for _ in pool.map(work, jobs()):
            done += 1
            if done % 50 == 0 or done == total:
                rate = done / max(1e-6, time.monotonic() - start)
                print(f"\r{done:,}/{total:,}  {rate:.1f} tiles/s  "
                      f"fetched {store.stats['upstream_requests']:,}  "
                      f"cached already {store.stats['hit']:,}", end="", flush=True)
    st = store.stats
    print(f"\ndone in {time.monotonic() - start:.0f}s: fetched {st['upstream_requests']:,}, "
          f"already cached {st['hit']:,}, errors {st['error']:,}")


if __name__ == "__main__":
    main()
