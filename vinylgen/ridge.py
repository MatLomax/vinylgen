from __future__ import annotations

import math

import numpy as np
from numba import njit, prange

from vinylgen.parallel_guard import KERNEL_LOCK as _KERNEL_LOCK


# The eight normals sweep half a circle; the opposite sample is generated
# explicitly.  Radii 1, 2 and 3 (in that order) probe strokes of varying width.
_RADII = (1, 2, 3)
_ANGLES = np.linspace(0.0, np.pi, 8, endpoint=False)


def _offset_table() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Per (radius, angle) pixel offsets, radius outer and angle inner."""
    dx: list[float] = []
    dy: list[float] = []
    radius_of: list[int] = []
    for radius in _RADII:
        for angle in _ANGLES:
            dx.append(radius * math.cos(float(angle)))
            dy.append(radius * math.sin(float(angle)))
            radius_of.append(radius)
    return (
        np.asarray(dx, dtype=np.float64),
        np.asarray(dy, dtype=np.float64),
        np.asarray(radius_of, dtype=np.uint8),
    )


_DX, _DY, _RADIUS_OF = _offset_table()


@njit(cache=True, fastmath=False, inline="always")
def _sample_bilinear(image, coord_y, coord_x, height, width):
    """Order-1 map_coordinates with nearest-edge extension, in double precision.

    Reproduces scipy 1.11.4's accumulation exactly: the coordinate (already the
    double value of a float32 sample point) selects the lower knot by floor, the
    fractional part forms the two bilinear taps, out-of-range knot indices are
    clamped by nearest extension while the taps keep their unclamped weights, and
    the four taps accumulate in y-outer, x-inner order.
    """
    lower_y = int(math.floor(coord_y))
    lower_x = int(math.floor(coord_x))
    frac_y = coord_y - math.floor(coord_y)
    frac_x = coord_x - math.floor(coord_x)
    weight_y0 = 1.0 - frac_y
    weight_y1 = 1.0 - weight_y0
    weight_x0 = 1.0 - frac_x
    weight_x1 = 1.0 - weight_x0

    upper_y = lower_y + 1
    upper_x = lower_x + 1
    # Nearest-edge clamp of each knot index; identity away from the border.
    clamped_y0 = 0 if lower_y < 0 else (height - 1 if lower_y > height - 1 else lower_y)
    clamped_y1 = 0 if upper_y < 0 else (height - 1 if upper_y > height - 1 else upper_y)
    clamped_x0 = 0 if lower_x < 0 else (width - 1 if lower_x > width - 1 else lower_x)
    clamped_x1 = 0 if upper_x < 0 else (width - 1 if upper_x > width - 1 else upper_x)

    tap00 = float(image[clamped_y0, clamped_x0])
    tap01 = float(image[clamped_y0, clamped_x1])
    tap10 = float(image[clamped_y1, clamped_x0])
    tap11 = float(image[clamped_y1, clamped_x1])

    total = 0.0
    total += (tap00 * weight_y0) * weight_x0
    total += (tap01 * weight_y0) * weight_x1
    total += (tap10 * weight_y1) * weight_x0
    total += (tap11 * weight_y1) * weight_x1
    return total


@njit(parallel=True, cache=True, fastmath=False)
def _ridge_kernel(lightness, offsets_x, offsets_y, radius_of):
    height, width = lightness.shape
    response = np.zeros((height, width), dtype=np.float32)
    best_radius = np.ones((height, width), dtype=np.uint8)
    passes = offsets_x.shape[0]
    for y in prange(height):
        for x in range(width):
            centre = lightness[y, x]
            best = np.float32(0.0)
            best_r = np.uint8(1)
            for k in range(passes):
                delta_x = np.float32(offsets_x[k])
                delta_y = np.float32(offsets_y[k])
                base_y = np.float32(y)
                base_x = np.float32(x)
                # float32 coordinate arithmetic mirrors numpy's `yy + dy`.
                forward = _sample_bilinear(
                    lightness,
                    np.float64(np.float32(base_y + delta_y)),
                    np.float64(np.float32(base_x + delta_x)),
                    height,
                    width,
                )
                backward = _sample_bilinear(
                    lightness,
                    np.float64(np.float32(base_y - delta_y)),
                    np.float64(np.float32(base_x - delta_x)),
                    height,
                    width,
                )
                forward_gap = np.float32(forward) - centre
                backward_gap = np.float32(backward) - centre
                two_sided = forward_gap if forward_gap < backward_gap else backward_gap
                if two_sided > best:
                    best = two_sided
                    best_r = np.uint8(radius_of[k])
            response[y, x] = best
            best_radius[y, x] = best_r
    return response, best_radius


def two_sided_dark_ridge_response(
    lightness: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Strongest two-sided dark-ridge contrast and its winning radius per pixel.

    For every pixel the response is the largest value, over radii 1/2/3 and eight
    half-circle normals, of the minimum brightening seen on both sides of the
    pixel; ``best_radius`` records the radius that first strictly attained it
    (initialised to 1).  Results are bit-identical to the scipy order-1
    ``map_coordinates`` formulation with ``mode="nearest"``.
    """
    lightness = np.ascontiguousarray(lightness, dtype=np.float32)
    with _KERNEL_LOCK:
        return _ridge_kernel(lightness, _DX, _DY, _RADIUS_OF)
