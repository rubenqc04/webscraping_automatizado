"""Tests del triaje de calidad de la capa de texto de PDFs.

Se construyen PDFs sintéticos en memoria con PyMuPDF: uno con texto
normal, uno con "texto" corrupto (simula OCR upstream malo) y uno sin
texto (escaneado clásico).
"""

import random
import unittest

import pymupdf as fitz

from webharvest.extractors.pdf_extractor import analyze_ocr_need, _page_wordy_ratio

NORMAL = ("El desarrollo económico de la región muestra avances sostenidos "
          "durante el periodo analizado según las cifras oficiales. ") * 20

def _garbage(n_tokens=200, seed=7):
    rng = random.Random(seed)
    chars = "abcdefghij©ª^~|1230"
    return " ".join("".join(rng.choice(chars) for _ in range(rng.randint(1, 6)))
                    for _ in range(n_tokens))


def build_pdf(pages_text: list) -> fitz.Document:
    doc = fitz.open()
    for text in pages_text:
        page = doc.new_page()
        if text:
            page.insert_textbox(fitz.Rect(50, 50, 550, 800), text, fontsize=10)
    return doc


class TestTextQuality(unittest.TestCase):
    def test_wordy_ratio_separates_normal_from_garbage(self):
        self.assertGreater(_page_wordy_ratio(NORMAL), 0.6)
        self.assertLess(_page_wordy_ratio(_garbage()), 0.45)

    def test_clean_pdf_not_queued(self):
        doc = build_pdf([NORMAL] * 4)
        s = analyze_ocr_need(doc)
        doc.close()
        self.assertFalse(s.needs_ocr)
        self.assertLess(s.bad_text_page_ratio, 0.2)

    def test_corrupt_text_layer_is_queued(self):
        doc = build_pdf([_garbage(seed=i) for i in range(4)])
        s = analyze_ocr_need(doc)
        doc.close()
        self.assertTrue(s.needs_ocr)
        self.assertIn("corrupta", s.reason)

    def test_scanned_no_text_is_queued(self):
        doc = build_pdf(["", "", ""])
        s = analyze_ocr_need(doc)
        doc.close()
        self.assertTrue(s.needs_ocr)


if __name__ == "__main__":
    unittest.main()
