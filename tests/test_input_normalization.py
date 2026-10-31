import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import zipfile
from xml.etree import ElementTree as ET

from PIL import Image

RUNTIME = Path(__file__).resolve().parents[1] / 'skills/image-to-editable-ppt/cli/editppt/runtime'
sys.path.insert(0, str(RUNTIME))
import _input_normalization as normalization
from build_pptx_from_manifest import write_deck

NS = normalization.NS


class InputNormalizationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.entries = []
        for i, color in enumerate(('red', 'blue'), 1):
            asset = self.root / f'asset{i}.png'
            Image.new('RGB', (320, 180), color).save(asset)
            self.entries.append({'manifest_path': self.root / f'manifest{i}.json', 'manifest': {
                'source': {'width_px': 320, 'height_px': 180},
                'slide': {'width': 13.333, 'height': 7.5},
                'content_box': {'left': 0, 'top': 0, 'width': 13.333, 'height': 7.5},
                'images': [{'path': str(asset), 'box_px': [0, 0, 320, 180]}],
                'text_boxes': [], 'shapes': [],
            }})
        self.pptx = self.root / 'source.pptx'
        write_deck({}, self.entries, self.pptx, [{'page_index': 2, 'text': 'Keep original notes'}])

    def mutate_slide(self, transform):
        with zipfile.ZipFile(self.pptx) as z:
            parts = {name: z.read(name) for name in z.namelist()}
        root = ET.fromstring(parts['ppt/slides/slide1.xml'])
        transform(root)
        parts['ppt/slides/slide1.xml'] = ET.tostring(root)
        with zipfile.ZipFile(self.pptx, 'w', zipfile.ZIP_DEFLATED) as z:
            for name, data in parts.items():
                z.writestr(name, data)

    def assert_unsupported(self):
        with self.assertRaisesRegex(SystemExit, 'Convert native/complex PPTX slides to PDF or page images first'):
            normalization.normalize_inputs([self.pptx], job_dir=self.root / 'run')
        self.assertFalse((self.root / 'run/deck_manifest.json').exists())

    def test_ppt_forwards_dpi_after_conversion(self):
        source = self.root / 'source.ppt'
        source.write_bytes(b'synthetic legacy PPT')
        pdf = self.root / 'converted.pdf'
        def render(path, pages_dir, dpi):
            self.assertEqual(pdf, path)
            self.assertEqual(144, dpi)
            results = []
            for i in (1, 2):
                directory = pages_dir / f'page_{i:03d}'
                directory.mkdir(parents=True)
                out = directory / 'source.png'
                Image.new('RGB', (320, 180)).save(out)
                results.append(out)
            return results
        with mock.patch.object(normalization, 'convert_ppt_to_pptx', return_value=self.pptx), \
             mock.patch.object(normalization, 'convert_office_to_pdf', return_value=pdf), \
             mock.patch.object(normalization, 'render_pdf_pages', side_effect=render) as renderer:
            manifest = normalization.normalize_inputs([source], job_dir=self.root / 'run', dpi=144)
        renderer.assert_called_once()
        deck = json.loads(manifest.read_text(encoding='utf-8'))
        self.assertEqual([1, 2], [page['source_page'] for page in deck['pages']])
        notes = json.loads((manifest.parent / 'notes_manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(2, notes['notes'][0]['page_index'])
        self.assertEqual('Keep original notes', notes['notes'][0]['text'])

    def test_missing_office_converter_is_clear(self):
        source = self.root / 'source.ppt'
        source.write_bytes(b'synthetic legacy PPT')
        with mock.patch.object(normalization, 'find_soffice', return_value=None), \
             self.assertRaisesRegex(RuntimeError, 'No local Office converter'):
            normalization.normalize_inputs([source], job_dir=self.root / 'run')

    def test_single_full_slide_pictures_and_notes_are_preserved(self):
        manifest = normalization.normalize_inputs([self.pptx], job_dir=self.root / 'run')
        deck = json.loads(manifest.read_text(encoding='utf-8'))
        self.assertEqual(2, deck['page_count'])
        for i, page in enumerate(deck['pages'], 1):
            with Image.open(manifest.parent / page['source_image']) as actual, \
                 Image.open(self.root / f'asset{i}.png') as expected:
                self.assertEqual(expected.tobytes(), actual.tobytes())
        notes = json.loads((manifest.parent / 'notes_manifest.json').read_text(encoding='utf-8'))
        self.assertEqual('Keep original notes', notes['notes'][0]['text'])
        self.assertEqual(2, notes['notes'][0]['page_index'])
        self.assertEqual(normalization.sha256_text('Keep original notes'), notes['notes'][0]['text_sha256'])

    def test_native_shape_overlay_is_rejected(self):
        self.entries[0]['manifest']['shapes'] = [{'type': 'rect', 'box_px': [0, 0, 100, 100], 'fill': '#0000ff', 'z_index': 500}]
        write_deck({}, self.entries, self.pptx, [])
        self.assert_unsupported()

    def test_connector_overlay_is_rejected(self):
        self.mutate_slide(lambda root: ET.SubElement(root.find('p:cSld/p:spTree', NS), '{%s}cxnSp' % NS['p']))
        self.assert_unsupported()

    def test_table_overlay_is_rejected(self):
        self.entries[0]['manifest']['tables'] = [{'box_px': [10, 20, 100, 40], 'cells': [['']], 'style': {'fill': '#0000ff'}}]
        write_deck({}, self.entries, self.pptx, [])
        self.assert_unsupported()

    def test_grouped_picture_is_rejected(self):
        def group(root):
            tree = root.find('p:cSld/p:spTree', NS)
            picture = tree.find('p:pic', NS)
            tree.remove(picture)
            ET.SubElement(tree, '{%s}grpSp' % NS['p']).append(picture)
        self.mutate_slide(group)
        self.assert_unsupported()

    def test_cropped_picture_is_rejected(self):
        self.mutate_slide(lambda root: ET.SubElement(root.find('.//p:blipFill', NS), '{%s}srcRect' % NS['a'], {'l': '10000'}))
        self.assert_unsupported()

    def test_rotated_picture_is_rejected(self):
        self.mutate_slide(lambda root: root.find('.//p:pic/p:spPr/a:xfrm', NS).set('rot', '5400000'))
        self.assert_unsupported()

    def test_flipped_picture_is_rejected(self):
        self.mutate_slide(lambda root: root.find('.//p:pic/p:spPr/a:xfrm', NS).set('flipH', '1'))
        self.assert_unsupported()

    def test_tiled_picture_is_rejected(self):
        def tile(root):
            fill = root.find('.//p:blipFill', NS)
            fill.remove(fill.find('a:stretch', NS))
            ET.SubElement(fill, '{%s}tile' % NS['a'])
        self.mutate_slide(tile)
        self.assert_unsupported()

    def test_picture_with_color_effect_is_rejected(self):
        self.mutate_slide(lambda root: ET.SubElement(root.find('.//a:blip', NS), '{%s}grayscl' % NS['a']))
        self.assert_unsupported()

    def test_picture_with_border_is_rejected(self):
        self.mutate_slide(lambda root: ET.SubElement(root.find('.//p:pic/p:spPr', NS), '{%s}ln' % NS['a'], {'w': '12700'}))
        self.assert_unsupported()

    def test_clipped_geometry_is_rejected(self):
        self.mutate_slide(lambda root: root.find('.//p:pic/p:spPr/a:prstGeom', NS).set('prst', 'ellipse'))
        self.assert_unsupported()

    def test_hidden_picture_is_rejected(self):
        self.mutate_slide(lambda root: root.find('.//p:pic/p:nvPicPr/p:cNvPr', NS).set('hidden', '1'))
        self.assert_unsupported()

    def test_theme_picture_style_is_rejected(self):
        def style(root):
            style = ET.SubElement(root.find('.//p:pic', NS), '{%s}style' % NS['p'])
            reference = ET.SubElement(style, '{%s}lnRef' % NS['a'], {'idx': '1'})
            ET.SubElement(reference, '{%s}srgbClr' % NS['a'], {'val': '0000FF'})
        self.mutate_slide(style)
        self.assert_unsupported()

    def test_transparent_picture_is_rejected(self):
        asset = self.root / 'asset1.png'
        Image.new('RGBA', (320, 180), (255, 0, 0, 0)).save(asset)
        write_deck({}, self.entries, self.pptx, [])
        self.assert_unsupported()

    def test_palette_transparency_is_rejected(self):
        asset = self.root / 'asset1.png'
        image = Image.new('P', (320, 180), 0)
        image.putpalette([255, 0, 0] * 256)
        image.save(asset, transparency=0)
        write_deck({}, self.entries, self.pptx, [])
        self.assert_unsupported()

    def test_explicit_no_outline_is_accepted(self):
        def no_outline(root):
            line = ET.SubElement(root.find('.//p:pic/p:spPr', NS), '{%s}ln' % NS['a'], {'w': '12700'})
            ET.SubElement(line, '{%s}noFill' % NS['a'])
        self.mutate_slide(no_outline)
        self.assertEqual(2, len(normalization.extract_image_based_pptx_pages(self.pptx, self.root / 'pages')))

    def test_benign_local_dpi_metadata_is_accepted(self):
        def metadata(root):
            extensions = ET.SubElement(root.find('.//a:blip', NS), '{%s}extLst' % NS['a'])
            extension = ET.SubElement(extensions, '{%s}ext' % NS['a'], {'uri': '{28A0092B-C50C-407E-A947-70E740481C1C}'})
            ET.SubElement(extension, '{http://schemas.microsoft.com/office/drawing/2010/main}useLocalDpi', {'val': '0'})
        self.mutate_slide(metadata)
        outputs = normalization.extract_image_based_pptx_pages(self.pptx, self.root / 'pages')
        with Image.open(outputs[0]) as actual:
            self.assertEqual((255, 0, 0), actual.getpixel((0, 0)))


if __name__ == '__main__':
    unittest.main()
