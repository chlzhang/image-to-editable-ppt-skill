import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
import zipfile
from xml.etree import ElementTree as ET

from PIL import Image

RUNTIME = Path(__file__).resolve().parents[1] / 'skills/image-to-editable-ppt/cli/editppt/runtime'
sys.path.insert(0, str(RUNTIME))
from build_pptx_from_manifest import write_deck, write_pptx
from validate_pptx import NS


class DeckIntegrityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.entries = []
        for index, text in enumerate(('Revenue 100', 'Revenue 200'), 1):
            page = self.root / f'page_{index}'
            page.mkdir()
            Image.new('RGB', (8, 8), ('red', 'blue')[index - 1]).save(page / 'asset.png')
            model = {
                'source': {'width_px': 320, 'height_px': 180},
                'slide': {'width': 13.333, 'height': 7.5},
                'text_inventory': [text],
                'text_boxes': [{'text': text, 'box_px': [10, 20, 280, 50], 'font_size': 24}],
                'images': [{'path': 'asset.png', 'box_px': [10, 100, 20, 20]}],
                'visual_inventory': [], 'shapes': [],
                'background_strategy': {'mode': 'native-or-script'},
                'quality_checks': dict.fromkeys(['font_size_calibrated', 'visual_inventory_matched',
                    'background_strategy_checked', 'shape_corner_geometry_checked'], True),
            }
            path = page / 'manifest.json'
            path.write_text(json.dumps(model), encoding='utf-8')
            (page / 'validation.json').write_text('{"passed": true}', encoding='utf-8')
            self.entries.append({'manifest': model, 'manifest_path': path})
        self.meta = self.root / 'deck_manifest.json'
        self.meta.write_text(json.dumps({'page_count': 2, 'pages': [
            {'manifest': str(e['manifest_path']), 'validation': str(e['manifest_path'].parent / 'validation.json')}
            for e in self.entries]}), encoding='utf-8')
        self.pptx = self.root / 'deck.pptx'
        write_deck({}, self.entries, self.pptx, [])

    def mutate(self, transform):
        with zipfile.ZipFile(self.pptx) as z:
            parts = {n: z.read(n) for n in z.namelist()}
        with zipfile.ZipFile(self.pptx, 'w', zipfile.ZIP_STORED) as z:
            for name, data in parts.items():
                data = transform(name, data)
                if data is not None:
                    z.writestr(name, data)

    def validate(self, passed, page=False):
        result = subprocess.run([sys.executable, str(RUNTIME / 'validate_pptx.py'), str(self.pptx),
            '--manifest' if page else '--deck-manifest',
            str(self.entries[0]['manifest_path'] if page else self.meta)], capture_output=True, text=True)
        report = json.loads(result.stdout)
        self.assertEqual(passed, report['passed'], report)
        self.assertEqual(0 if passed else 1, result.returncode, result.stderr)
        return report

    def test_two_page_media_offsets(self):
        self.validate(True)

    def test_malformed_slide_fails(self):
        self.mutate(lambda n, d: b'<broken' if n == 'ppt/slides/slide1.xml' else d)
        self.validate(False)

    def test_changed_text_fails(self):
        self.mutate(lambda n, d: d.replace(b'Revenue 100', b'Revenue 999'))
        self.validate(False)

    def test_missing_slide_relationship_fails(self):
        self.mutate(lambda n, d: None if n == 'ppt/slides/_rels/slide1.xml.rels' else d)
        self.validate(False)

    def test_broken_relationship_target_fails(self):
        self.mutate(lambda n, d: d.replace(b'../media/image2.png', b'../media/missing.png') if n.endswith('.rels') else d)
        self.validate(False)

    def test_changed_media_fails(self):
        self.mutate(lambda n, d: b'changed image' if n == 'ppt/media/image2.png' else d)
        self.validate(False)

    def test_missing_media_fails(self):
        self.mutate(lambda n, d: None if n == 'ppt/media/image2.png' else d)
        self.validate(False)

    def test_rebound_media_fails(self):
        self.mutate(lambda n, d: d.replace(b'../media/image2.png', b'../media/image1.png')
                    if n == 'ppt/slides/_rels/slide2.xml.rels' else d)
        self.validate(False)

    def test_removed_picture_fails(self):
        def remove_picture(name, data):
            if name == 'ppt/slides/slide1.xml':
                root = ET.fromstring(data)
                tree = root.find('p:cSld/p:spTree', NS)
                tree.remove(tree.find('p:pic', NS))
                return ET.tostring(root)
            return data
        self.mutate(remove_picture)
        self.validate(False)

    def test_changed_picture_reference_fails(self):
        self.mutate(lambda n, d: d.replace(b'r:embed="rId2"', b'r:embed="rId1"')
                    if n == 'ppt/slides/slide1.xml' else d)
        self.validate(False)

    def test_page_parse_error_is_fatal_without_required_text(self):
        model = self.entries[0]['manifest']
        model['images'] = []
        model['text_inventory'] = []
        model['text_boxes'] = []
        self.entries[0]['manifest_path'].write_text(json.dumps(model), encoding='utf-8')
        write_pptx(model, self.pptx, self.entries[0]['manifest_path'])
        self.mutate(lambda n, d: b'<broken' if n == 'ppt/slides/slide1.xml' else d)
        self.validate(False, page=True)

    def test_swapped_page_text_fails(self):
        self.mutate(lambda n, d: d.replace(b'Revenue 100', b'Revenue 200') if n == 'ppt/slides/slide1.xml'
                    else d.replace(b'Revenue 200', b'Revenue 100') if n == 'ppt/slides/slide2.xml' else d)
        self.validate(False)

    def test_duplicate_text_and_empty_page_pass(self):
        for entry in self.entries:
            entry['manifest']['text_inventory'] = ['Repeated', 'Repeated']
            entry['manifest']['text_boxes'][0]['text'] = 'Repeated'
        self.entries[1]['manifest']['text_inventory'] = []
        self.entries[1]['manifest']['text_boxes'] = []
        for entry in self.entries:
            entry['manifest_path'].write_text(json.dumps(entry['manifest']), encoding='utf-8')
        write_deck({}, self.entries, self.pptx, [])
        self.validate(True)

    def test_malformed_root_xml_fails(self):
        self.mutate(lambda n, d: b'<broken' if n == 'ppt/presentation.xml' else d)
        self.validate(False)

    def test_crc_failure_fails(self):
        self.mutate(lambda n, d: d)
        data = bytearray(self.pptx.read_bytes())
        with zipfile.ZipFile(self.pptx) as z:
            item = z.getinfo('ppt/media/image2.png')
            offset = item.header_offset
            name_len, extra_len = struct.unpack_from('<HH', data, offset + 26)
            data[offset + 30 + name_len + extra_len] ^= 1
        self.pptx.write_bytes(data)
        self.validate(False)

    def test_malformed_notes_fails(self):
        write_deck({}, self.entries, self.pptx, [{'page_index': 1, 'text': 'Speaker notes'}])
        self.mutate(lambda n, d: b'<broken' if n == 'ppt/notesSlides/notesSlide1.xml' else d)
        self.validate(False)

    def test_missing_notes_relationships_fails(self):
        write_deck({}, self.entries, self.pptx, [{'page_index': 1, 'text': 'Speaker notes'}])
        self.mutate(lambda n, d: None if n == 'ppt/notesSlides/_rels/notesSlide1.xml.rels' else d)
        self.validate(False)


if __name__ == '__main__':
    unittest.main()
