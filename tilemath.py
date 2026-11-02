"""
Slippy-map tile math (Web Mercator, EPSG:3857).

Everything a tile server needs comes down to two coordinate systems:

  * lon/lat in degrees (WGS84) - what humans and GPS use
  * tile (z, x, y)            - what the map asks for

and one intermediate: Web Mercator metres, which is what ArcGIS services
(like NSW's) describe their extents in.

The world at zoom z is a 2^z x 2^z grid of 256-pixel tiles.
x grows eastward from lon -180; y grows SOUTHWARD from lat ~85.05.
"""

import math

TILE_SIZE = 256
# Half the circumference of the Web Mercator "sphere" (radius 6378137 m).
# The square world spans [-ORIGIN_SHIFT, +ORIGIN_SHIFT] in both x and y.
EARTH_RADIUS = 6378137.0
ORIGIN_SHIFT = math.pi * EARTH_RADIUS  # 20037508.342789244
# Mercator blows up at the poles, so the projection is cut where the map is square.
MAX_LAT = 85.05112877980659


def lonlat_to_tile(lon: float, lat: float, zoom: int) -> tuple[int, int]:
    """Return the (x, y) tile containing this point at this zoom."""
    lat = max(-MAX_LAT, min(MAX_LAT, lat))
    n = 2 ** zoom
    # x: longitude maps linearly onto [0, 1)
    fx = (lon + 180.0) / 360.0
    # y: the Mercator formula ln(tan φ + sec φ) gives a value in [-π, π];
    # flip it (y grows south) and scale to [0, 1)
    phi = math.radians(lat)
    fy = (1.0 - math.log(math.tan(phi) + 1.0 / math.cos(phi)) / math.pi) / 2.0
    # clamp so lon=180 or lat=-MAX_LAT stays inside the grid
    x = min(n - 1, max(0, int(fx * n)))
    y = min(n - 1, max(0, int(fy * n)))
    return x, y


def tile_to_lonlat(x: float, y: float, zoom: int) -> tuple[float, float]:
    """Lon/lat of a tile's NORTH-WEST corner. Pass x+0.5, y+0.5 for its centre."""
    n = 2 ** zoom
    lon = x / n * 360.0 - 180.0
    # inverse of the formula above: the Gudermannian function
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    return lon, lat


def tile_bounds_mercator(x: int, y: int, zoom: int) -> tuple[float, float, float, float]:
    """(xmin, ymin, xmax, ymax) of a tile in Web Mercator metres."""
    size = 2 * ORIGIN_SHIFT / 2 ** zoom  # metres per tile edge
    xmin = -ORIGIN_SHIFT + x * size
    ymax = ORIGIN_SHIFT - y * size
    return xmin, ymax - size, xmin + size, ymax


def lonlat_to_mercator(lon: float, lat: float) -> tuple[float, float]:
    lat = max(-MAX_LAT, min(MAX_LAT, lat))
    mx = math.radians(lon) * EARTH_RADIUS
    my = math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)) * EARTH_RADIUS
    return mx, my


def meters_per_pixel(zoom: float, lat: float = 0.0) -> float:
    """TRUE ground distance covered by one pixel.

    Mercator stretches everything by 1/cos(lat) away from the equator, so a
    pixel near Sydney (lat -34) covers ~17% less ground than one at the equator.
    """
    return 2 * ORIGIN_SHIFT / (TILE_SIZE * 2 ** zoom) * math.cos(math.radians(lat))


def tiles_in_bbox(west, south, east, north, zoom):
    """Inclusive (xmin, xmax, ymin, ymax) range of tiles covering a lon/lat bbox."""
    x0, y0 = lonlat_to_tile(west, north, zoom)  # north-west corner -> smallest y
    x1, y1 = lonlat_to_tile(east, south, zoom)  # south-east corner -> largest y
    return x0, x1, y0, y1


def count_tiles(west, south, east, north, zmin, zmax) -> int:
    total = 0
    for z in range(zmin, zmax + 1):
        x0, x1, y0, y1 = tiles_in_bbox(west, south, east, north, z)
        total += (x1 - x0 + 1) * (y1 - y0 + 1)
    return total


def parent(z: int, x: int, y: int, levels: int = 1) -> tuple[int, int, int]:
    """The ancestor tile `levels` zooms up. Each level up halves x and y."""
    return z - levels, x >> levels, y >> levels


if __name__ == "__main__":
    # Sydney Opera House, the exercise from earlier
    lon, lat = 151.2153, -33.8568
    for z in (14, 18, 19):
        print(f"z{z}: tile {lonlat_to_tile(lon, lat, z)}  "
              f"{meters_per_pixel(z, lat):.3f} m/px at this latitude")
