from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
from pathlib import Path

from vinylgen.contracts import PaletteSettings
from vinylgen.palette import infer_palette


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", type=Path, help="PNG/JPEG inputs or directories")
    parser.add_argument("--output", type=Path, default=Path("outputs"))
    parser.add_argument("--initial-colours", type=int, default=PaletteSettings.initial_colours)
    parser.add_argument("--final-colours", type=int, default=PaletteSettings.final_colours,
                        help="require an exact final palette size; automatic when omitted")
    parser.add_argument("--similar-delta-e", type=float, default=PaletteSettings.similar_delta_e)
    parser.add_argument("--small-share", type=float, default=PaletteSettings.small_share)
    parser.add_argument("--small-delta-e", type=float, default=PaletteSettings.small_delta_e)
    parser.add_argument(
        "--min-region-pixels",
        type=int,
        default=PaletteSettings.min_region_pixels,
        help="replace connected regions smaller than this many pixels; 0 disables cleanup",
    )
    parser.add_argument("--intentional-line-min-contrast", type=float,
                        default=PaletteSettings.intentional_line_min_contrast)
    parser.add_argument("--intentional-line-min-length", type=int,
                        default=PaletteSettings.intentional_line_min_length)
    parser.add_argument("--aa-ambiguity-margin", type=float,
                        default=PaletteSettings.aa_ambiguity_margin)
    parser.add_argument("--aa-smoothness", type=float,
                        default=PaletteSettings.aa_smoothness)
    parser.add_argument(
        "--flatten-upscale",
        type=int,
        default=PaletteSettings.flatten_upscale,
        help="nearest-neighbour upscale applied to the flattened output and segmentation grid",
    )
    parser.add_argument(
        "--flatten-smooth-edges",
        action=argparse.BooleanOptionalAction,
        default=PaletteSettings.flatten_smooth_edges,
        help="Scale2x edge smoothing of the upscaled steps, using only palette colours",
    )
    parser.add_argument(
        "--protect-component-at",
        action="append",
        default=[],
        metavar="X,Y",
        help="preserve the four-connected flattened colour component containing this source-pixel point",
    )
    parser.add_argument(
        "--preserve-colour-at",
        action="append",
        default=[],
        metavar="X,Y",
        help="exempt the inferred palette colour at this source-pixel point from the global usage cutoff",
    )
    parser.add_argument(
        "--keep-colours-distinct-at",
        action="append",
        default=[],
        metavar="X1,Y1:X2,Y2",
        help="prevent the two source-pixel colour families from being merged",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=0,
        help="images to process concurrently; 0 selects a conservative automatic value",
    )
    parser.add_argument(
        "--min-colour-share",
        type=float,
        default=PaletteSettings.min_colour_share,
        help="fold colours below this image share into the nearest colour; 0 disables",
    )
    parser.add_argument(
        "--dither-radius",
        type=int,
        default=PaletteSettings.dither_radius,
        help="neighbourhood radius for dither and pixel-noise removal before flattening; "
             "0 disables it. Only speckle that a despeckled copy and the local majority "
             "both agree on is reassigned, so isolated real detail is kept",
    )
    return parser.parse_args()


def input_files(inputs: list[Path]) -> list[Path]:
    extensions = {".png", ".jpg", ".jpeg", ".webp"}
    files: list[Path] = []
    for item in inputs:
        if item.is_dir():
            files.extend(path for path in sorted(item.iterdir()) if path.suffix.lower() in extensions)
        elif item.suffix.lower() in extensions:
            files.append(item)
    by_stem: dict[str, list[Path]] = {}
    for path in files:
        by_stem.setdefault(path.stem, []).append(path)
    collisions = {stem: paths for stem, paths in by_stem.items() if len(paths) > 1}
    if collisions:
        detail = "; ".join(
            f"{stem!r}: {', '.join(str(path) for path in paths)}"
            for stem, paths in sorted(collisions.items())
        )
        raise ValueError(
            f"inputs share an output name and would overwrite each other: {detail}"
        )
    return files


def component_points(values: list[str]) -> tuple[tuple[int, int], ...]:
    points = []
    for value in values:
        try:
            x_text, y_text = value.split(",", 1)
            point = (int(x_text), int(y_text))
        except ValueError as error:
            raise ValueError(f"invalid protected component point {value!r}; expected X,Y") from error
        if point[0] < 0 or point[1] < 0:
            raise ValueError(f"protected component point must be non-negative: {value!r}")
        points.append(point)
    return tuple(points)


def distinct_point_pairs(
    values: list[str],
) -> tuple[tuple[tuple[int, int], tuple[int, int]], ...]:
    pairs = []
    for value in values:
        try:
            first, second = value.split(":", 1)
        except ValueError as error:
            raise ValueError(
                f"invalid distinct-colour pair {value!r}; expected X1,Y1:X2,Y2"
            ) from error
        points = component_points([first, second])
        pairs.append((points[0], points[1]))
    return tuple(pairs)


def main() -> None:
    args = parse_args()
    settings = PaletteSettings(
        initial_colours=args.initial_colours,
        final_colours=args.final_colours,
        similar_delta_e=args.similar_delta_e,
        small_share=args.small_share,
        small_delta_e=args.small_delta_e,
        min_region_pixels=args.min_region_pixels,
        min_colour_share=args.min_colour_share,
        dither_radius=args.dither_radius,
        intentional_line_min_contrast=args.intentional_line_min_contrast,
        intentional_line_min_length=args.intentional_line_min_length,
        aa_ambiguity_margin=args.aa_ambiguity_margin,
        aa_smoothness=args.aa_smoothness,
        flatten_upscale=args.flatten_upscale,
        flatten_smooth_edges=args.flatten_smooth_edges,
        protected_component_points=component_points(args.protect_component_at),
        preserved_colour_points=component_points(args.preserve_colour_at),
        distinct_colour_point_pairs=distinct_point_pairs(args.keep_colours_distinct_at),
    )
    files = input_files(args.inputs)
    jobs = args.jobs or min(4, len(files), os.cpu_count() or 1)
    if jobs <= 1:
        reports = [infer_palette(path, args.output, settings) for path in files]
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as executor:
            reports = list(
                executor.map(
                    lambda path: infer_palette(path, args.output, settings),
                    files,
                )
            )
    summary = {
        Path(report["source"]).name: report["inferred_colour_count"] for report in reports
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
