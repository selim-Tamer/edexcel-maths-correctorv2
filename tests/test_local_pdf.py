import hashlib
import unittest
from contextlib import closing
from io import BytesIO
from pypdf import PdfReader, PdfWriter
from local_pdf import inspect_pages
from pdf_cases import make_pdf, render_page


class LocalPDFTests(unittest.TestCase):
    def test_digital_and_scanned_original_pages_preserve_pixels(self):
        for scanned in (False, True):
            data = make_pdf(scanned)
            before = hashlib.sha256(data).digest()
            result = inspect_pages(data, [3, 1])
            self.assertEqual(result['pages'], [3, 1])
            self.assertEqual(result['page_count'], 6)
            self.assertTrue(result['preview'].startswith(b'\x89PNG'))
            self.assertEqual(hashlib.sha256(data).digest(), before)
            for index, original in enumerate([2, 0]):
                with render_page(data, original) as before_image, render_page(result['pdf'], index) as after_image:
                    self.assertEqual(before_image.size, after_image.size)
                    self.assertEqual(before_image.tobytes(), after_image.tobytes())
            if scanned:
                self.assertTrue(all(not item['text'].strip() for item in result['text']))
            else:
                self.assertIn('(67 + 1) / 2 = 34', result['text'][0]['text'])

    def test_bad_input_bounds_and_safe_errors(self):
        data = make_pdf()
        for pages in ([], [0], [7], [1, 1], [True], list(range(1, 12))):
            with self.subTest(pages=pages), self.assertRaises(ValueError):
                inspect_pages(data, pages)
        for invalid in (b'private-student-synthetic-invalid', b'x' * (50 * 1024 * 1024 + 1)):
            with self.assertRaises(ValueError) as error:
                inspect_pages(invalid, [1])
            self.assertNotIn('private-student', str(error.exception))

    def test_no_cross_document_cache(self):
        first = inspect_pages(make_pdf(), [1])
        second = inspect_pages(make_pdf(scanned=True), [1])
        self.assertNotEqual(first['text'], second['text'])

    def test_password_protected_pdf_fails_safely(self):
        with closing(PdfWriter(clone_from=BytesIO(make_pdf()))) as doc, BytesIO() as output:
            doc.encrypt('synthetic-password')
            doc.write(output)
            data = output.getvalue()
        with self.assertRaises(ValueError):
            inspect_pages(data, [1])

    def test_form_fields_ink_rotation_and_crop_survive_extraction(self):
        with closing(PdfWriter(clone_from=BytesIO(make_pdf(forms=True)))) as writer, BytesIO() as output:
            writer.pages[2].rotate(90)
            writer.pages[2].cropbox.lower_left = (20, 20)
            writer.write(output)
            original = output.getvalue()
        result = inspect_pages(original, [3, 1])
        reader = PdfReader(BytesIO(result['pdf']))
        self.assertEqual(set(reader.get_fields()), {'answer1', 'answer3'})
        self.assertTrue(all(f['/V'] == '34' for f in reader.get_fields().values()))
        self.assertEqual(reader.pages[0].rotation, 90)
        self.assertEqual(reader.pages[0].cropbox.lower_left, (20, 20))
        for n, old in enumerate([2, 0]):
            subtypes = {a.get_object()['/Subtype'] for a in reader.pages[n]['/Annots']}
            self.assertTrue({'/Ink', '/Widget'}.issubset(subtypes))
            with render_page(original, old) as before, render_page(result['pdf'], n) as after:
                self.assertEqual(before.size, after.size)
                self.assertEqual(before.tobytes(), after.tobytes())

    def test_preview_dimensions_and_text_limits(self):
        from PIL import Image
        result = inspect_pages(make_pdf(), [3])
        with Image.open(BytesIO(result['preview'])) as image:
            self.assertLessEqual(max(image.size), 1201)
        self.assertLessEqual(len(result['text'][0]['text']), 20000)
        self.assertIn('Question 3', result['text'][0]['text'])
