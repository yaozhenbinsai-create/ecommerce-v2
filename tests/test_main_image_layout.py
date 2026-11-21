"""Current source inspection and historical-proof validation; no image composer."""
import hashlib
import tempfile
import unittest
from pathlib import Path

from PIL import Image,ImageDraw
from app.commerce import main_image_layout as layout,main_image_policy


class MainImageLayoutValidationTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup);self.root=Path(temporary.name)

    def source(self,image):
        path=self.root/'source.png';image.save(path);return path

    def test_source_inspection_never_rewrites_or_composes_the_original(self):
        source=self.source(Image.new('RGB',(1000,700),'gray'));before=hashlib.sha256(source.read_bytes()).hexdigest()
        result=layout.inspect_source(source)
        self.assertFalse(result['can_prepare']);self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),before)

    def test_every_visible_kit_component_remains_in_inspected_bounds(self):
        image=Image.new('RGBA',(1400,1000),(0,0,0,0));draw=ImageDraw.Draw(image)
        draw.rectangle((40,200,659,799),fill=(100,110,120,255));draw.rectangle((740,200,1359,799),fill=(160,170,180,255))
        result=layout.inspect_source(self.source(image))
        self.assertEqual(result['margin_crop']['content_bounds'],[40,200,1360,800])
        self.assertTrue(result['margin_crop']['all_detectable_content_preserved'])

    def test_near_white_product_does_not_acquire_guessed_crop(self):
        source=self.source(Image.new('RGB',(1200,1000),'white'))
        result=layout.inspect_source(source,main_image_policy.resolve({'product_type':'GENERAL'})|{'source_color':'白色'})
        self.assertFalse(result['margin_crop']['applied']);self.assertFalse(result['can_prepare'])

    def test_transparent_photo_bounds_use_visible_subject_and_actual_canvas(self):
        image=Image.new('RGBA',(1000,600),(0,0,0,0));ImageDraw.Draw(image).rectangle((50,0,949,599),fill=(90,100,110,128))
        frame=layout.frame_fill(image,(1000,1000))
        self.assertEqual(frame['bounds_within_photo'],[50,0,950,600]);self.assertEqual(frame['ratio'],.9)
        self.assertEqual(frame['status'],'eligible')

    def test_faint_edge_noise_does_not_make_an_empty_subject_eligible(self):
        image=Image.new('RGBA',(1000,600),(90,100,110,127));frame=layout.frame_fill(image,(1000,1000))
        self.assertIsNone(frame['bounds_within_photo']);self.assertEqual(frame['ratio'],0)

    def test_rectangular_canvas_cannot_use_a_short_axis_loophole(self):
        image=Image.new('RGBA',(848,448),(0,0,0,0));ImageDraw.Draw(image).rectangle((24,24,823,423),fill=(40,70,110,255))
        frame=layout.frame_fill(image,(884,1000));self.assertEqual(frame['ratio'],.8);self.assertEqual(frame['status'],'reject')

    def test_opaque_scene_needs_review_instead_of_an_invented_subject_box(self):
        frame=layout.frame_fill(Image.new('RGBA',(1200,800),(70,90,110,255)),(1200,1200))
        self.assertEqual(frame['status'],'requires_visual');self.assertIsNone(frame['ratio'])

    def test_historical_preservation_receipt_rejects_repaint_or_false_scale(self):
        proof={'main_layout_version':layout.VERSION,'protocol':'source-layout-v2','subject_repainted':False,
               'source_scale':1,'source_upscaled':False,'interpolated':False,'source_photo_cropped':False,'white_margin_crop':False}
        self.assertTrue(layout.preservation_valid(proof))
        for change in ({'subject_repainted':True},{'source_scale':1.26},{'source_upscaled':True},{'white_margin_crop':True}):
            self.assertFalse(layout.preservation_valid(proof|change))

    def test_historical_crop_receipt_must_bound_all_content(self):
        crop={'applied':True,'all_detectable_content_preserved':True,'method':'existing_alpha_all_visible_pixels',
              'content_bounds':[10,10,90,90],'source_crop_box':[5,5,95,95]}
        proof={'main_layout_version':layout.VERSION,'protocol':'source-layout-v2','subject_repainted':False,'source_scale':1,
               'source_upscaled':False,'interpolated':False,'source_photo_cropped':True,'white_margin_crop':True,
               'margin_crop':crop,'source_crop_box':crop['source_crop_box'],'source_content_bounds':crop['content_bounds']}
        self.assertTrue(layout.preservation_valid(proof))
        self.assertFalse(layout.preservation_valid(proof|{'source_crop_box':[20,20,80,80]}))
