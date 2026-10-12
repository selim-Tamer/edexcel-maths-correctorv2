import hashlib
import unittest
import pymupdf
from local_pdf import inspect_pages
from pdf_cases import make_pdf


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
            with pymupdf.open(stream=data, filetype='pdf') as source, pymupdf.open(stream=result['pdf'], filetype='pdf') as excerpt:
                for index, original in enumerate([2, 0]):
                    self.assertEqual(source[original].get_pixmap(annots=True).samples,
                                     excerpt[index].get_pixmap(annots=True).samples)
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
        with pymupdf.open(stream=make_pdf(), filetype='pdf') as doc:
            data = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw='synthetic-owner', user_pw='synthetic-password')
        with self.assertRaises(ValueError):
            inspect_pages(data, [1])
