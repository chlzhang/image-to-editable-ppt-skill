import copy
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from PIL import Image, ImageChops

RUNTIME = Path(__file__).resolve().parents[1] / 'skills/image-to-editable-ppt/cli/editppt/runtime'
sys.path.insert(0, str(RUNTIME))
import build_pptx_from_manifest as builder


class PreviewPortabilityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.model = {'source': {'width_px': 800, 'height_px': 200},
                      'slide': {'width': 8, 'height': 2}, 'preview_scale': 100,
                      'content_box': {'left': 0, 'top': 0, 'width': 8, 'height': 2},
                      'text_boxes': [{'text': 'Size Test', 'box_px': [10, 10, 700, 170],
                                      'font_size': 12, 'fit_text': False, 'color': '#000000'}]}

    def render(self, model=None, name='preview.png'):
        output = self.root / name
        builder.render_preview(model or self.model, self.root / 'manifest.json', output)
        with Image.open(output) as image:
            return image.convert('RGB')

    def test_windows_font_discovery(self):
        expected = Path('/test-windows/Fonts/msyh.ttc')
        with mock.patch.dict(os.environ, {'WINDIR': '/test-windows'}), \
             mock.patch.object(Path, 'is_file', lambda p: p == expected):
            self.assertEqual(expected, Path(builder.choose_preview_font(None) or 'missing'))

    def test_linux_font_discovery(self):
        expected = Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc')
        with mock.patch.object(Path, 'is_file', lambda p: p == expected):
            self.assertEqual(expected, Path(builder.choose_preview_font(None) or 'missing'))

    def test_explicit_font_precedes_defaults(self):
        expected = self.root / 'explicit.ttf'
        expected.touch()
        self.assertEqual(expected, Path(builder.choose_preview_font(str(expected))))

    def test_requested_font_sizes_change_actual_ink_bounds(self):
        small = ImageChops.invert(self.render()).getbbox()
        self.model['text_boxes'][0]['font_size'] = 48
        large = ImageChops.invert(self.render()).getbbox()
        self.assertGreater(large[3] - large[1], 2 * (small[3] - small[1]))
        self.assertGreater(large[2] - large[0], 2 * (small[2] - small[0]))

    def test_mixed_runs_keep_their_own_font_sizes(self):
        self.model['text_boxes'][0].pop('text')
        self.model['text_boxes'][0]['runs'] = [{'text': 'A', 'font_size': 12},
                                              {'text': 'A', 'font_size': 48, 'color': '#FF0000'}]
        image = self.render()
        mask_small = Image.new('1', image.size)
        mask_large = Image.new('1', image.size)
        for y in range(image.height):
            for x in range(image.width):
                r, g, b = image.getpixel((x, y))
                if max(r, g, b) < 100:
                    mask_small.putpixel((x, y), 1)
                if r > 150 and max(g, b) < 100:
                    mask_large.putpixel((x, y), 1)
        small, large = mask_small.getbbox(), mask_large.getbbox()
        self.assertGreater(large[3] - large[1], 2 * (small[3] - small[1]))

    def test_cjk_characters_do_not_become_identical_missing_glyph_boxes(self):
        self.model['text_boxes'][0].update(text='中', font_size=40)
        first = self.render()
        self.model['text_boxes'][0]['text'] = '文'
        second = self.render()
        self.assertIsNotNone(ImageChops.difference(first, second).getbbox())

    def test_no_font_is_an_explicit_failure(self):
        with mock.patch.object(builder, 'choose_preview_font', return_value=None), \
             self.assertRaisesRegex(ValueError, 'font'):
            self.render()
        self.assertFalse((self.root / 'preview.png').exists())

    def test_invalid_requested_font_is_an_explicit_failure(self):
        bad = self.root / 'bad.ttf'
        bad.write_bytes(b'not a font')
        self.model['text_boxes'][0]['preview_font'] = str(bad)
        with self.assertRaisesRegex(ValueError, 'font'):
            self.render()

    def test_small_supported_text_keeps_requested_size(self):
        font = builder.load_preview_font(None, 1, 'A')
        self.assertEqual(1, font.size)
        self.model['preview_scale'] = 6
        self.model['text_boxes'][0].update(text='A', font_size=12)
        self.render()

    def test_missing_character_still_fails(self):
        with self.assertRaisesRegex(ValueError, 'lacks glyph'):
            builder.load_preview_font(None, 12, '\U0010ffff')

    def svg_model(self):
        source = self.root / 'asset.svg'
        source.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8"><rect width="8" height="8" fill="red"/></svg>')
        model = copy.deepcopy(self.model)
        model['text_boxes'] = []
        model['images'] = [{'path': str(source), 'box_px': [10, 10, 100, 100]}]
        return model

    def test_svg_uses_path_converter_with_closed_output_file(self):
        model = self.svg_model()
        def convert(command, **kwargs):
            self.assertEqual('test-magick', command[0])
            Image.new('RGBA', (8, 8), 'red').save(command[-1])
            return subprocess.CompletedProcess(command, 0)
        with mock.patch('shutil.which', return_value='test-magick'), mock.patch.object(builder.subprocess, 'run', convert):
            self.assertEqual((255, 0, 0), self.render(model).getpixel((20, 20)))

    def test_missing_svg_converter_fails_instead_of_omitting_image(self):
        with mock.patch('shutil.which', return_value=None), self.assertRaisesRegex(RuntimeError, 'ImageMagick'):
            self.render(self.svg_model())
        self.assertFalse((self.root / 'preview.png').exists())


if __name__ == '__main__':
    unittest.main()
