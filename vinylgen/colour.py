from __future__ import annotations

import numpy as np
from numba import njit, prange
from PIL import Image, ImageFilter

from vinylgen.parallel_guard import KERNEL_LOCK as _KERNEL_LOCK


# sRGB-linear to CIE XYZ (D65) and the D65 reference white.  Held at module scope
# so the fused Lab kernel receives them as arguments and its accumulation order
# matches numpy's `x @ M` element-for-element.
_LAB_M = np.array([
    [0.4124564, 0.2126729, 0.0193339],
    [0.3575761, 0.7151522, 0.1191920],
    [0.1804375, 0.0721750, 0.9503041],
])
_LAB_WHITE = np.array([0.95047, 1.0, 1.08883])


@njit(parallel=True, cache=True, fastmath=False)
def _srgb_to_lab_kernel(rgb, matrix, white):
    """Per-row sRGB->Lab, reproducing the numpy formulation bit-for-bit.

    Each channel is linearised, projected to XYZ in the same summation order as
    `x @ matrix` (`sum_k x[k] * matrix[k, j]`), normalised by the reference
    white, and passed through the CIE cube-root companding.  Every arithmetic
    step stays in double precision so the result matches the numpy path exactly.
    """
    n = rgb.shape[0]
    out = np.empty((n, 3), dtype=np.float64)
    epsilon = 216 / 24389
    kappa = 24389 / 27
    for i in prange(n):
        lin0 = rgb[i, 0] / 255.0
        lin1 = rgb[i, 1] / 255.0
        lin2 = rgb[i, 2] / 255.0
        lin0 = lin0 / 12.92 if lin0 <= 0.04045 else ((lin0 + 0.055) / 1.055) ** 2.4
        lin1 = lin1 / 12.92 if lin1 <= 0.04045 else ((lin1 + 0.055) / 1.055) ** 2.4
        lin2 = lin2 / 12.92 if lin2 <= 0.04045 else ((lin2 + 0.055) / 1.055) ** 2.4
        x = (lin0 * matrix[0, 0] + lin1 * matrix[1, 0] + lin2 * matrix[2, 0]) / white[0]
        y = (lin0 * matrix[0, 1] + lin1 * matrix[1, 1] + lin2 * matrix[2, 1]) / white[1]
        z = (lin0 * matrix[0, 2] + lin1 * matrix[1, 2] + lin2 * matrix[2, 2]) / white[2]
        fx = np.cbrt(x) if x > epsilon else (kappa * x + 16) / 116
        fy = np.cbrt(y) if y > epsilon else (kappa * y + 16) / 116
        fz = np.cbrt(z) if z > epsilon else (kappa * z + 16) / 116
        out[i, 0] = 116 * fy - 16
        out[i, 1] = 500 * (fx - fy)
        out[i, 2] = 200 * (fy - fz)
    return out


def srgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    rgb = np.ascontiguousarray(np.asarray(rgb, dtype=np.float64))
    with _KERNEL_LOCK:
        return _srgb_to_lab_kernel(rgb, _LAB_M, _LAB_WHITE)


@njit(parallel=True, cache=True, fastmath=False)
def _nearest_palette_labels_kernel(image, palette):
    """Index of the palette colour with the smallest squared RGB distance.

    Reproduces ``argmin`` of ``sum((image - palette) ** 2)`` bit-for-bit: each
    channel difference and its square accumulate in int64, and a strict ``<``
    keeps the first palette index on an exact tie, matching numpy's
    first-minimum ``argmin``.
    """
    height, width, _ = image.shape
    count = palette.shape[0]
    out = np.empty((height, width), dtype=np.int16)
    for y in prange(height):
        for x in range(width):
            r = np.int64(image[y, x, 0])
            g = np.int64(image[y, x, 1])
            b = np.int64(image[y, x, 2])
            best = np.int64(0)
            best_index = 0
            for index in range(count):
                dr = r - np.int64(palette[index, 0])
                dg = g - np.int64(palette[index, 1])
                db = b - np.int64(palette[index, 2])
                distance = dr * dr + dg * dg + db * db
                if index == 0 or distance < best:
                    best = distance
                    best_index = index
            out[y, x] = best_index
    return out


def nearest_palette_labels(image: np.ndarray, palette_rgb: np.ndarray) -> np.ndarray:
    """Label every pixel with its nearest palette colour by squared RGB distance.

    ``image`` is an ``(H, W, 3)`` RGB raster and ``palette_rgb`` an ``(N, 3)``
    palette. The result is bit-identical to a banded
    ``np.sum((rows[:, :, None, :] - palette[None, None]) ** 2, axis=3,
    dtype=np.int64).argmin(axis=2)`` cast to int16.
    """
    image = np.ascontiguousarray(np.asarray(image), dtype=np.uint8)
    palette = np.ascontiguousarray(np.asarray(palette_rgb), dtype=np.int64)
    with _KERNEL_LOCK:
        return _nearest_palette_labels_kernel(image, palette)


def lab_to_srgb(lab: np.ndarray) -> np.ndarray:
    fy = (lab[:, 0] + 16) / 116
    fx = fy + lab[:, 1] / 500
    fz = fy - lab[:, 2] / 200
    delta = 6 / 29

    def inverse(t: np.ndarray) -> np.ndarray:
        return np.where(t > delta, t**3, 3 * delta * delta * (t - 4 / 29))

    xyz = np.column_stack((inverse(fx), inverse(fy), inverse(fz)))
    xyz *= np.array([0.95047, 1.0, 1.08883])
    linear = xyz @ np.array([
        [3.2404542, -0.9692660, 0.0556434],
        [-1.5371385, 1.8760108, -0.2040259],
        [-0.4985314, 0.0415560, 1.0572252],
    ])
    rgb = np.where(
        linear <= 0.0031308,
        12.92 * linear,
        1.055 * np.maximum(linear, 0) ** (1 / 2.4) - 0.055,
    )
    return np.clip(np.rint(rgb * 255), 0, 255).astype(np.uint8)


def hex_colour(rgb: np.ndarray) -> str:
    return "#" + "".join(f"{int(channel):02X}" for channel in rgb)


COLOUR_REFERENCES = (
    ("Black", (0, 0, 0)), ("Charcoal", (54, 69, 79)),
    ("Deep Navy", (13, 40, 67)), ("Navy Blue", (25, 63, 103)),
    ("Slate Blue", (82, 104, 128)), ("Sky Blue", (91, 166, 219)),
    ("Teal", (30, 120, 120)), ("Forest Green", (34, 92, 60)),
    ("Sage Green", (151, 170, 139)), ("Mint Green", (170, 220, 190)),
    ("Cream", (246, 229, 180)), ("Golden Yellow", (235, 184, 65)),
    ("Burnt Orange", (205, 88, 40)), ("Terracotta", (181, 82, 57)),
    ("Crimson", (164, 42, 48)), ("Burgundy", (100, 35, 48)),
    ("Dusty Rose", (195, 125, 125)), ("Plum", (91, 49, 82)),
    ("Brown", (104, 70, 48)), ("Sand", (205, 179, 128)),
    ("Warm Grey", (135, 130, 120)), ("Cool Grey", (120, 132, 138)),
    ("White", (245, 245, 240)),
)


def colour_names(palette_rgb: np.ndarray) -> list[str]:
    references = np.asarray([rgb for _, rgb in COLOUR_REFERENCES], dtype=np.uint8)
    reference_lab = srgb_to_lab(references)
    palette_lab = srgb_to_lab(np.asarray(palette_rgb, dtype=np.uint8))
    base_names = [
        COLOUR_REFERENCES[int(np.argmin(np.linalg.norm(reference_lab - colour, axis=1)))][0]
        for colour in palette_lab
    ]
    totals: dict[str, int] = {}
    result = []
    for name in base_names:
        totals[name] = totals.get(name, 0) + 1
        result.append(name if totals[name] == 1 else f"{name} {totals[name]}")
    return result


def despeckle(image: Image.Image, radius: int) -> Image.Image:
    """Remove dither and pixel noise from a source before palette inference by
    replacing each channel with the median of its surrounding neighbourhood. A
    radius of zero returns the image untouched, so the default leaves the source
    exactly as supplied."""
    if radius <= 0:
        return image
    return image.filter(ImageFilter.MedianFilter(2 * radius + 1))


def analysis_image(image: Image.Image, maximum: int) -> Image.Image:
    result = image.copy()
    result.thumbnail((maximum, maximum), Image.Resampling.LANCZOS)
    return result


def reduced_histogram(rgb: np.ndarray, bits: int) -> tuple[np.ndarray, np.ndarray]:
    shift = 8 - bits
    bins = (rgb.astype(np.uint32) >> shift).reshape(-1, 3)
    packed = (bins[:, 0] << (2 * bits)) | (bins[:, 1] << bits) | bins[:, 2]
    values, counts = np.unique(packed, return_counts=True)
    mask = (1 << bits) - 1
    decoded = np.column_stack(
        ((values >> (2 * bits)) & mask, (values >> bits) & mask, values & mask)
    )
    scale = 255 / mask
    return decoded.astype(np.float64) * scale, counts.astype(np.float64)


def weighted_kmeans(
    points: np.ndarray, weights: np.ndarray, k: int, iterations: int = 60
) -> tuple[np.ndarray, np.ndarray]:
    k = min(k, len(points))
    centres = [points[int(np.argmax(weights))]]
    nearest = ((points - centres[0]) ** 2).sum(axis=1)
    for _ in range(1, k):
        index = int(np.argmax(nearest * np.sqrt(weights)))
        centres.append(points[index])
        nearest = np.minimum(nearest, ((points - points[index]) ** 2).sum(axis=1))
    centres = np.asarray(centres)
    labels = np.zeros(len(points), dtype=np.int32)
    for _ in range(iterations):
        distances = ((points[:, None, :] - centres[None, :, :]) ** 2).sum(axis=2)
        new_labels = distances.argmin(axis=1)
        new_centres = centres.copy()
        for index in range(k):
            members = new_labels == index
            if members.any():
                new_centres[index] = np.average(points[members], axis=0, weights=weights[members])
        if np.array_equal(labels, new_labels) and np.allclose(centres, new_centres):
            centres, labels = new_centres, new_labels
            break
        centres, labels = new_centres, new_labels
    return centres, labels


def assign(pixels: np.ndarray, centres: np.ndarray, chunk: int = 200_000) -> np.ndarray:
    labels = np.empty(len(pixels), dtype=np.int32)
    for start in range(0, len(pixels), chunk):
        part = pixels[start:start + chunk]
        distances = ((part[:, None, :] - centres[None, :, :]) ** 2).sum(axis=2)
        labels[start:start + chunk] = distances.argmin(axis=1)
    return labels


def assignment_margin(
    pixels: np.ndarray, centres: np.ndarray, labels: np.ndarray, chunk: int = 200_000
) -> np.ndarray:
    margins = np.empty(len(pixels), dtype=np.float32)
    for start in range(0, len(pixels), chunk):
        part = pixels[start:start + chunk]
        distances = np.linalg.norm(part[:, None, :] - centres[None, :, :], axis=2)
        selected = labels[start:start + chunk]
        best = distances[np.arange(len(part)), selected]
        distances[np.arange(len(part)), selected] = np.inf
        margins[start:start + chunk] = distances.min(axis=1) - best
    return margins


@njit(parallel=True, cache=True, fastmath=False)
def _palette_costs_kernel(pixels, centres):
    """Per-pixel Euclidean Lab distance to every centre.

    The squared differences accumulate in double precision, exactly as
    `np.linalg.norm(..., axis=2)` reduces the three Lab channels, and each result
    is rounded to float32 on store so the array is bit-identical to the numpy
    formulation.
    """
    n = pixels.shape[0]
    k = centres.shape[0]
    costs = np.empty((n, k), dtype=np.float32)
    for i in prange(n):
        p0 = pixels[i, 0]
        p1 = pixels[i, 1]
        p2 = pixels[i, 2]
        for j in range(k):
            d0 = p0 - centres[j, 0]
            d1 = p1 - centres[j, 1]
            d2 = p2 - centres[j, 2]
            costs[i, j] = np.float32(np.sqrt(d0 * d0 + d1 * d1 + d2 * d2))
    return costs


def palette_costs(
    pixels: np.ndarray, centres: np.ndarray, chunk: int = 200_000
) -> np.ndarray:
    """Return every pixel's perceptual cost to every palette colour.

    The result is float32 while it is being used.  The segmentation contract
    stores it as float16: that preserves substantially more useful boundary
    evidence than a single confidence number without making the handoff huge.

    The ``chunk`` parameter is retained for call-site compatibility; the fused
    parallel kernel materialises the whole array directly and does not chunk.
    """
    pixels = np.ascontiguousarray(pixels, dtype=np.float64)
    centres = np.ascontiguousarray(centres, dtype=np.float64)
    with _KERNEL_LOCK:
        return _palette_costs_kernel(pixels, centres)
