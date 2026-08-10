"""OpenStreetMap static maps without an API key, an account, or an image library.

A slippy-map tile is a fixed 256x256 PNG addressed by (zoom, x, y). To show an
arbitrary point centred in an arbitrary viewport we work out the smallest
rectangle of tiles that covers the viewport, lay them out in a grid, and shift
the grid by CSS so the point lands in the middle. The pin is then just an
absolutely positioned dot at the viewport centre.

That keeps the whole thing to <img> tags and two offsets -- no compositing, so
no Pillow, so no dependencies.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass

TILE = 256
TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
ATTRIBUTION = "© OpenStreetMap contributors"

# OSM serves its "418 Access blocked" notice as a normal PNG with HTTP 200, so
# nothing upstream can tell it from a map. Recognising it by content is the only
# reliable defence -- without this a report renders 60 identical "blocked"
# squares and reports zero failures.
BLOCKED_TILE_SHA256 = {
    "99465c86e84cdbcdf88f207e48c2e8ce70b144d12e8f029a5b15bbee0b34df4a",
}


def is_placeholder_tile(data: bytes) -> bool:
    """True if `data` is a known provider error/placeholder image."""
    if not data:
        return True
    return hashlib.sha256(data).hexdigest() in BLOCKED_TILE_SHA256


@dataclass
class TileWindow:
    z: int
    x0: int
    y0: int
    nx: int
    ny: int
    offset_x: float   # CSS left for the mosaic, relative to the viewport
    offset_y: float
    width: int
    height: int

    def tiles(self):
        """Yield (col, row, x, y) for each tile in the mosaic."""
        span = 2 ** self.z
        for row in range(self.ny):
            for col in range(self.nx):
                y = self.y0 + row
                if not (0 <= y < span):
                    continue
                yield col, row, (self.x0 + col) % span, y

    def urls(self):
        return [TILE_URL.format(z=self.z, x=x, y=y) for _, _, x, y in self.tiles()]


def project(lat: float, lon: float, z: int) -> tuple[float, float]:
    """Web-Mercator world pixel coordinates at zoom `z`."""
    span = 2 ** z
    x = (lon + 180.0) / 360.0 * span
    rad = math.radians(max(min(lat, 85.05112878), -85.05112878))
    y = (1.0 - math.log(math.tan(rad) + 1.0 / math.cos(rad)) / math.pi) / 2.0 * span
    return x * TILE, y * TILE


def window(lat: float, lon: float, width: int, height: int, z: int = 15) -> TileWindow:
    wx, wy = project(lat, lon, z)
    left, top = wx - width / 2.0, wy - height / 2.0

    x0 = math.floor(left / TILE)
    y0 = math.floor(top / TILE)
    x1 = math.floor((left + width - 1) / TILE)
    y1 = math.floor((top + height - 1) / TILE)

    return TileWindow(
        z=z, x0=x0, y0=y0, nx=x1 - x0 + 1, ny=y1 - y0 + 1,
        offset_x=-(left - x0 * TILE), offset_y=-(top - y0 * TILE),
        width=width, height=height,
    )
