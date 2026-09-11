"""仅测试本地模板/排版：python -m unittest discover -s tests -p test_templates.py -v"""
from copy import deepcopy
from io import BytesIO
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageFont

from app import compose, fonts, templates


class TemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.font_path = fonts.resolve()
        except fonts.FontError as exc:
            raise unittest.SkipTest(str(exc))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def config(self, **layer):
        return {
            "canvas": {"width": 400, "height": 320},
            "vars": {"company": "测试公司"},
            "templates": {"sample": {
                "layers": [{"type": "paragraph", "text": "你好 {name}",
                            "font": self.font_path, "size": 36, "xy": [25, 25],
                            "max_width": 350, "color": "#123456", **layer}],
            }},
        }

    def layout(self, **layer):
        cfg = templates.validate_config(self.config(**layer))
        entry = cfg["templates"]["sample"]["layers"][0]
        with Image.new("RGBA", (400, 320)) as image:
            return compose._layout_text(ImageDraw.Draw(image), entry, entry["text"], image.size)

    def test_merge_is_independent_and_preserves_design(self):
        cfg = self.config(shadow={"x": 2, "y": 4, "blur": 1, "color": "#00000044"})
        cfg["templates"]["sample"]["layers"].append({"text": "署名", "xy": [30, 200], "font": self.font_path})
        before = deepcopy(cfg)
        edits = {"templates": {"sample": {"layers": [{"size": 28, "shadow": {"x": 7}}]}},
                 "vars": {"company": "新公司"}}
        updated = templates.merge_config(cfg, edits)
        self.assertEqual(cfg, before)
        self.assertEqual(updated["templates"]["sample"]["layers"][1], before["templates"]["sample"]["layers"][1])
        layer = updated["templates"]["sample"]["layers"][0]
        self.assertEqual(layer["size"], 28)
        self.assertEqual(layer["shadow"]["x"], 7)
        self.assertEqual(layer["shadow"]["color"], "#00000044")
        updated["templates"]["sample"]["layers"][1]["text"] = "changed"
        self.assertEqual(cfg, before)
        self.assertFalse(updated["templates"]["sample"]["ai"]["enabled"])

    def test_sparse_merge_and_invalid_edit(self):
        cfg = self.config()
        merged = templates.merge_config(cfg, {"templates": {"sample": {"layers": [None]}}})
        self.assertEqual(merged["templates"]["sample"]["layers"], cfg["templates"]["sample"]["layers"])
        for edits in ({"name": "员工"}, {"templates": {"sample": {"layers": [{}, {}]}}},
                      {"templates": {"sample": {"layers": [12]}}}):
            with self.subTest(edits=edits), self.assertRaises(templates.TemplateError):
                templates.merge_config(cfg, edits)
        with self.assertRaises(templates.TemplateError):
            templates.merge_config(cfg, {"templates": {"../escape": {"layers": []}}})

    def test_default_ai_disabled_and_memory_render_never_loads_file(self):
        cfg = self.config(text="统一设计")
        before = deepcopy(cfg)
        with patch.object(compose, "load_config", side_effect=AssertionError("global read")), \
                patch.object(templates, "_atomic_write", side_effect=AssertionError("global write")):
            raw = compose.render_png("sample", {}, ai_image_path="nonexistent-ai.png", cfg=cfg)
        with Image.open(BytesIO(raw)) as result:
            self.assertEqual(result.size, (400, 320))
            self.assertEqual(result.mode, "RGB")
        self.assertEqual(cfg, before)

    def test_explicit_empty_config_does_not_load_global(self):
        with patch.object(compose, "load_config", side_effect=AssertionError("global read")):
            with self.assertRaises(templates.TemplateError):
                compose.render_png("sample", {}, cfg={})

    def test_placeholder_validation(self):
        bad = ["{unknown}", "{name.__class__}", "{name[0]}", "{name!r}",
               "{name:>100}", "{name:{years}}", "{", "}", "{}", "{0}"]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(templates.TemplateError):
                templates.validate_config(self.config(text=value))
        cfg = self.config(text="{{祝福}} {name} {slogan} {department} {event_date} {employee_id}")
        cfg["vars"]["slogan"] = "同行"
        templates.validate_config(cfg)
        self.assertEqual(templates.format_text("{{姓名}} {name}", {"name": "李四"}), "{姓名} 李四")
        with self.assertRaisesRegex(templates.TemplateError, "缺少.*name"):
            compose.render_png("sample", {}, cfg=self.config())

    def test_invalid_geometry_colors_and_font_paths(self):
        cases = [{"xy": [-1, 0]}, {"xy": [1]}, {"xy": [0, float("nan")]},
                 {"max_width": 0}, {"max_width": 400}, {"max_height": -1},
                 {"size": 0}, {"size": True}, {"size": 2.5}, {"min_size": 99},
                 {"spacing": -1}, {"line_gap": 0}, {"anchor": "xx"}, {"halign": "center"},
                 {"color": "#XYZ123"}, {"color": [256, 0, 0]}, {"color": [False, 0, 0]},
                 {"shadow": {"blur": -1}}, {"stroke": {"width": 2.5}},
                 {"font": "../../secret.ttf"}, {"font": "not-a-font.txt"},
                 {"font": "missing-brand-font.ttf"}, {"flow": "false"}, {"wrap": 1}]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(templates.TemplateError):
                templates.validate_config(self.config(**case))
        for kind in ("rect", "image"):
            cfg = self.config(type=kind, rect=[390, 0, 20, 20], path="dummy.png")
            with self.assertRaisesRegex(templates.TemplateError, "超出"):
                templates.validate_config(cfg, check_assets=False)

    def test_font_resolution_is_cwd_independent_and_missing_is_explicit(self):
        local = self.root / "project/fonts"
        local.mkdir(parents=True)
        copied = local / "Brand.TTC"
        shutil.copyfile(self.font_path, copied)
        with patch.object(fonts, "FONT_DIR", local), patch.object(fonts, "BASE_DIR", self.root):
            self.assertEqual(Path(fonts.resolve("Brand.TTC")), copied)
            self.assertEqual(Path(fonts.resolve("project\\fonts\\Brand.TTC")), copied)
            with self.assertRaises(fonts.FontError):
                fonts.resolve("Missing.ttf")
            bad = local / "broken.ttf"
            bad.write_bytes(b"invalid")
            with self.assertRaises(fonts.FontError):
                fonts.resolve("broken.ttf")

    def test_auto_skips_corrupt_fonts_and_list_values_resolve(self):
        (self.root / "broken.ttf").write_bytes(b"invalid")
        with patch.object(fonts, "FONT_DIR", self.root), \
                patch.object(fonts, "CANDIDATES", [self.font_path]), \
                patch.object(fonts, "_system_dirs", return_value=[]):
            self.assertEqual(fonts.resolve(), self.font_path)
            options = templates.font_options()
            self.assertEqual(options[0]["value"], "auto")
            self.assertGreater(len(options), 1)
            for option in options:
                self.assertTrue({"value", "label", "source"}.issubset(option))
                fonts.resolve(option["value"])

    @unittest.skipUnless(os.name == "nt", "Windows 字体集成")
    def test_windows_regular_and_bold_are_loadable(self):
        with patch.object(fonts, "FONT_DIR", self.root):
            for bold in (False, True):
                path = fonts.resolve("auto", bold)
                loaded = ImageFont.truetype(path, 24, index=fonts.font_index(path, bold))
                self.assertGreater(loaded.getlength("生日快乐"), 0)

    def test_atomic_save_success_and_failure_preserve_original(self):
        target = self.root / "templates.json"
        saved = templates.save_config(self.config(), target)
        self.assertEqual(templates.load_config(target), saved)
        before = target.read_bytes()
        with patch.object(templates.os, "replace", side_effect=OSError("locked")):
            with self.assertRaises(OSError):
                templates.save_config(self.config(text="新的文案"), target)
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(list(self.root.iterdir()), [target])
        with self.assertRaises(templates.TemplateError):
            templates.save_config(self.config(size=-1), target)
        self.assertEqual(target.read_bytes(), before)

    def test_upload_returns_metadata_without_saving_config(self):
        stream = BytesIO()
        Image.new("RGB", (120, 80), "red").save(stream, "JPEG")
        with patch.object(templates, "save_config", side_effect=AssertionError("config write")):
            info = templates.store_background(stream.getvalue(), "../../unsafe.jpg", directory=self.root)
            other = templates.store_background(BytesIO(stream.getvalue()), "../../unsafe.jpg", directory=self.root)
        self.assertEqual(set(info), {"base_image", "width", "height"})
        self.assertEqual((info["width"], info["height"]), (120, 80))
        path = Path(info["base_image"])
        self.assertEqual(path.parent, self.root)
        self.assertNotEqual(info["base_image"], other["base_image"])
        with Image.open(path) as im:
            self.assertEqual(im.format, "PNG")
        for data in (b"", b"not png"):
            with self.assertRaises(templates.TemplateError):
                templates.store_background(data, "fake.png", directory=self.root)
        with self.assertRaises(templates.TemplateError):
            templates.store_background(stream.getvalue(), max_bytes=10, directory=self.root)
        self.assertEqual(len(list(self.root.iterdir())), 2)

    def test_upload_applies_exif_orientation(self):
        stream = BytesIO()
        with Image.new("RGB", (30, 60)) as im:
            exif = Image.Exif()
            exif[274] = 6
            im.save(stream, "JPEG", exif=exif)
        info = templates.store_background(stream.getvalue(), directory=self.root)
        self.assertEqual((info["width"], info["height"]), (60, 30))

    def test_replacement_background_fits_canvas_without_stretching(self):
        stream = BytesIO()
        with Image.new("RGB", (100, 100), "red") as image:
            image.save(stream, "PNG")
        for fit, corner in (("cover", (255, 0, 0, 255)), ("contain", (255, 255, 255, 255))):
            with self.subTest(fit=fit):
                info = templates.save_base_image(stream.getvalue(), directory=self.root, size=(100, 200), fit=fit)
                with Image.open(info["base_image"]) as image:
                    self.assertEqual(image.size, (100, 200))
                    self.assertEqual(image.getpixel((0, 0)), corner)
                    self.assertEqual(image.getpixel((50, 100)), (255, 0, 0, 255))

    def test_upload_pixel_limit_and_failed_replace_leave_no_files(self):
        stream = BytesIO()
        Image.new("RGB", (20, 20)).save(stream, "PNG")
        with patch.object(templates, "MAX_PIXELS", 100):
            with self.assertRaises(templates.TemplateError):
                templates.store_background(stream.getvalue(), directory=self.root)
        with patch.object(templates.os, "replace", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                templates.store_background(stream.getvalue(), directory=self.root)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_paragraph_wraps_shrinks_and_preserves_all_characters(self):
        text = "很长的部门名称与祝福MixedEnglish words " * 4 + "结尾不能丢失"
        layout = self.layout(text=text, max_width=180, max_height=145, size=44, min_size=10)
        self.assertLess(layout.size, 44)
        self.assertGreater(len(layout.lines), 1)
        self.assertEqual("".join(line.text for line in layout.lines), text)
        self.assertLessEqual(layout.bbox[3] - layout.bbox[1], 145)
        self.assertLessEqual(layout.bbox[2] - layout.bbox[0], 180)

    def test_explicit_newlines_and_combining_characters_are_preserved(self):
        text = "第一行\n\nCafe\u0301 最后一行\n"
        layout = self.layout(text=text)
        self.assertEqual([line.text for line in layout.lines], text.split("\n"))
        self.assertIn("e\u0301", compose._clusters(text))

    def test_spacing_measurement_matches_painted_extent(self):
        base = {"text": "甲乙丙", "size": 38, "min_size": 38}
        normal = self.layout(**base, spacing=0)
        spaced = self.layout(**base, spacing=9)
        self.assertAlmostEqual(spaced.lines[0].width - normal.lines[0].width, 18, delta=2)
        painted_widths = []
        for amount, layout in ((0, normal), (9, spaced)):
            layer = self.config(**base, spacing=amount)["templates"]["sample"]["layers"][0]
            with Image.new("RGBA", (400, 320)) as im:
                compose._paint_layout(im, layer, layout)
                bbox = im.getbbox()
                painted_widths.append(bbox[2] - bbox[0])
                self.assertGreaterEqual(bbox[0], layout.bbox[0] - 1)
                self.assertLessEqual(bbox[2], layout.bbox[2] + 1)
        self.assertAlmostEqual(painted_widths[1] - painted_widths[0], 18, delta=2)

    def test_horizontal_and_vertical_alignment(self):
        for anchor, xy in (("lt", [40, 40]), ("mm", [200, 160]), ("rb", [370, 280])):
            layout = self.layout(type="text", text="Ag中文", anchor=anchor, xy=xy, max_width=150)
            left, top, right, bottom = layout.bbox
            if anchor == "lt":
                self.assertEqual((left, top), tuple(xy))
            elif anchor == "mm":
                self.assertEqual(((left + right) / 2, (top + bottom) / 2), tuple(xy))
            else:
                self.assertEqual((right, bottom), tuple(xy))

    def test_text_can_opt_into_wrapping_and_canvas_height_shrinks(self):
        single = self.layout(type="text", text="姓名很长" * 3, size=45, max_width=150)
        wrapped = self.layout(type="text", wrap=True, text="姓名很长" * 3, size=45, max_width=150)
        self.assertGreater(wrapped.size, single.size)
        self.assertGreater(len(wrapped.lines), 1)
        near_bottom = self.layout(text="边界适配测试", xy=[25, 293], size=45)
        self.assertLess(near_bottom.size, 45)
        self.assertLessEqual(near_bottom.bbox[3], 320)

    def test_overflow_raises_with_layer_and_does_not_overwrite_output(self):
        target = self.root / "preview.png"
        target.write_bytes(b"previous image")
        cases = [{"text": "太长" * 80, "max_height": 15, "min_size": 24},
                 {"text": "汉", "max_width": 1, "min_size": 12},
                 {"text": "阴影", "xy": [0, 0], "shadow": {"blur": 5}},
                 {"text": "不能截字", "xy": [25, 318], "min_size": 20}]
        for case in cases:
            with self.subTest(case=case), self.assertRaisesRegex(compose.LayoutError, r"sample.layers\[0\].*min_size"):
                compose.render("sample", {}, out_path=target, cfg=self.config(**case))
            self.assertEqual(target.read_bytes(), b"previous image")

    def test_stroke_shadow_are_inside_reported_bounds(self):
        layer = self.config(text="中文 Ag", shadow={"x": -4, "y": 5, "blur": 3},
                            stroke={"width": 3}, xy=[50, 50])["templates"]["sample"]["layers"][0]
        with Image.new("RGBA", (400, 320)) as im:
            layout = compose._layout_text(ImageDraw.Draw(im), layer, layer["text"], im.size)
            compose._paint_layout(im, layer, layout)
            actual = im.getbbox()
        self.assertGreaterEqual(actual[0], layout.bbox[0] - 1)
        self.assertGreaterEqual(actual[1], layout.bbox[1] - 1)
        self.assertLessEqual(actual[2], layout.bbox[2] + 1)
        self.assertLessEqual(actual[3], layout.bbox[3] + 1)

    def test_flow_uses_fitted_bounds_and_empty_layer_does_not_advance(self):
        cfg = self.config(type="text", text="长姓名需要缩字号" * 2, size=50, flow=True, max_width=230,
                          shadow={"blur": 1, "x": 1, "y": 1}, stroke={"width": 1})
        first = cfg["templates"]["sample"]["layers"][0]
        cfg["templates"]["sample"]["flow_gap"] = 12
        cfg["templates"]["sample"]["layers"] += [
            {**first, "text": ""}, {**first, "text": "第二段", "size": 25},
        ]
        recorded = []
        actual = compose._draw_text

        def record(image, layer, ctx, **kwargs):
            layout = actual(image, layer, ctx, **kwargs)
            if layout:
                recorded.append(layout)
            return layout

        with patch.object(compose, "_draw_text", side_effect=record):
            compose.render_png("sample", {}, cfg=cfg)
        self.assertEqual(len(recorded), 2)
        self.assertLess(recorded[0].size, 50)
        self.assertEqual(recorded[1].bbox[1], recorded[0].bbox[3] + 12)

    def test_missing_base_or_image_is_error_and_base_size_is_authoritative(self):
        cfg = self.config(text="底图")
        cfg["templates"]["sample"]["base_image"] = str(self.root / "missing.png")
        with self.assertRaises(templates.TemplateError):
            compose.render_png("sample", {}, cfg=cfg)
        base = self.root / "base.png"
        Image.new("RGB", (500, 350), "#abcdef").save(base)
        cfg["templates"]["sample"]["base_image"] = str(base)
        with compose.render_image("sample", {}, cfg=cfg) as im:
            self.assertEqual(im.size, (500, 350))
            self.assertEqual(im.getpixel((0, 0)), (171, 205, 239))
        cfg["templates"]["sample"]["layers"] = [{"type": "image", "path": str(self.root / "{name}.png")}]
        with self.assertRaisesRegex(templates.TemplateError, "layers"):
            compose.render_png("sample", {"name": "missing"}, cfg=cfg)

    def test_alpha_color_blends_and_file_output_is_png(self):
        cfg = self.config()
        cfg["templates"]["sample"]["layers"] = [{"type": "rect", "rect": [10, 10, 20, 20], "color": "#FF000080"}]
        path = compose.render("sample", {}, out_path=self.root / "card.png", cfg=cfg)
        with Image.open(path) as im:
            self.assertEqual(im.format, "PNG")
            self.assertEqual(im.getpixel((10, 10)), (255, 127, 127))
            self.assertEqual(im.getpixel((30, 30)), (255, 255, 255))

    def test_existing_templates_render_with_auto_without_changing_config(self):
        path = Path(templates.TEMPLATE_CONFIG)
        before = path.read_bytes()
        cfg = templates.load_config()
        ctx = {"name": "预览员工", "years": 3, "department": "研发部门", "event_date": "2026-09-10"}
        for key, tpl in cfg["templates"].items():
            self.assertFalse(tpl["ai"]["enabled"])
            self.assertTrue(all(layer.get("font") == "auto" for layer in tpl["layers"]
                                if layer.get("type", "text") in ("text", "paragraph")))
            with compose.render_image(key, ctx, cfg=cfg) as im:
                self.assertEqual(im.size, (1080, 2340))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(ctx["years"], 3)


if __name__ == "__main__":
    unittest.main()
