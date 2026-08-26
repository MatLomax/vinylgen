from __future__ import annotations

import numpy as np
from PIL import Image, ImageFilter


def srgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    x = np.asarray(rgb, dtype=np.float64) / 255.0
    x = np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4)
    xyz = x @ np.array([
        [0.4124564, 0.2126729, 0.0193339],
        [0.3575761, 0.7151522, 0.1191920],
        [0.1804375, 0.0721750, 0.9503041],
    ])
    xyz /= np.array([0.95047, 1.0, 1.08883])
    epsilon = 216 / 24389
    kappa = 24389 / 27
    f = np.where(xyz > epsilon, np.cbrt(xyz), (kappa * xyz + 16) / 116)
    return np.column_stack((
        116 * f[:, 1] - 16,
        500 * (f[:, 0] - f[:, 1]),
        200 * (f[:, 1] - f[:, 2]),
    ))


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


def palette_costs(
    pixels: np.ndarray, centres: np.ndarray, chunk: int = 200_000
) -> np.ndarray:
    """Return every pixel's perceptual cost to every palette colour.

    The result is float32 while it is being used.  The segmentation contract
    stores it as float16: that preserves substantially more useful boundary
    evidence than a single confidence number without making the handoff huge.
    """
    costs = np.empty((len(pixels), len(centres)), dtype=np.float32)
    for start in range(0, len(pixels), chunk):
        part = pixels[start:start + chunk]
        costs[start:start + chunk] = np.linalg.norm(
            part[:, None, :] - centres[None, :, :], axis=2
        )
    return costs
