import unittest

from vinylgen.preview import PREVIEW_DPI, PREVIEW_MAX_SIDE, preview_plan


def _svg(width: str, height: str, view_box: str | None = "0 0 1000 700") -> bytes:
    attrs = f'width="{width}" height="{height}"'
    if view_box is not None:
        attrs += f' viewBox="{view_box}"'
    return f'<svg xmlns="http://www.w3.org/2000/svg" {attrs}></svg>'.encode()


class PreviewPlanTests(unittest.TestCase):
    def test_small_pixel_document_keeps_full_dpi(self) -> None:
        plan = preview_plan(_svg("200", "150", "0 0 200 150"))
        self.assertEqual(plan["arguments"], [f"--export-dpi={PREVIEW_DPI}"])
        self.assertEqual((plan["width"], plan["height"]), (1600, 1200))
        self.assertFalse(plan["capped"])

    def test_large_pixel_document_caps_longer_side_at_maximum(self) -> None:
        plan = preview_plan(_svg("1496", "1051", "0 0 1496 1051"))
        self.assertEqual(plan["arguments"], [f"--export-width={PREVIEW_MAX_SIDE}"])
        self.assertEqual(plan["width"], PREVIEW_MAX_SIDE)
        self.assertEqual(plan["height"], round(1051 * PREVIEW_MAX_SIDE / 1496))
        self.assertTrue(plan["capped"])
        self.assertLess(plan["dpi"], PREVIEW_DPI)

    def test_source_larger_than_maximum_is_capped_below_its_own_size(self) -> None:
        plan = preview_plan(_svg("5980", "4208", "0 0 5980 4208"))
        self.assertEqual(plan["arguments"], [f"--export-width={PREVIEW_MAX_SIDE}"])
        self.assertEqual((plan["width"], plan["height"]),
                         (PREVIEW_MAX_SIDE, round(4208 * PREVIEW_MAX_SIDE / 5980)))
        self.assertEqual(plan["max_side"], PREVIEW_MAX_SIDE)

    def test_portrait_document_caps_height(self) -> None:
        plan = preview_plan(_svg("1051", "1496", "0 0 1051 1496"))
        self.assertEqual(plan["arguments"], [f"--export-height={PREVIEW_MAX_SIDE}"])

    def test_physical_size_uses_millimetres_for_dpi(self) -> None:
        # 150 mm at 768 DPI is 4535 px, above the cap for a 1496 px source.
        plan = preview_plan(_svg("150mm", "105.4mm", "0 0 1496 1051"))
        self.assertEqual(plan["arguments"], [f"--export-width={PREVIEW_MAX_SIDE}"])
        small = preview_plan(_svg("60mm", "42mm", "0 0 1000 700"))
        self.assertEqual(small["arguments"], [f"--export-dpi={PREVIEW_DPI}"])
        self.assertEqual(small["width"], round(60 / 25.4 * PREVIEW_DPI))

    def test_units_are_case_insensitive(self) -> None:
        self.assertEqual(preview_plan(_svg("5IN", "2In", "0 0 500 200"))["width"],
                         preview_plan(_svg("5in", "2in", "0 0 500 200"))["width"])

    def test_missing_physical_size_falls_back_to_view_box(self) -> None:
        plan = preview_plan(b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 100"></svg>')
        self.assertEqual((plan["width"], plan["height"]), (1600, 800))

    def test_unsized_document_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            preview_plan(b'<svg xmlns="http://www.w3.org/2000/svg"></svg>')
        with self.assertRaises(ValueError):
            preview_plan(_svg("0", "0", "0 0 0 0"))


if __name__ == "__main__":
    unittest.main()
