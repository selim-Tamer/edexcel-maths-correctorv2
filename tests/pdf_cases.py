"""Synthetic digital text, vector pen strokes, diagram and image-only scan.
No real student handwriting or personal data. Generated in memory only.
"""
def make_pdf(scanned=False, pages=6):
    import pymupdf as pdf
    with pdf.open() as doc:
        for n in range(pages):
            page = doc.new_page(width=595, height=842)
            page.insert_text((40, 50), f'Question {n + 1}: Find the mean. [2 marks]')
            page.insert_text((40, 80), 'Digital reference: (67 + 1) / 2 = 34')
            shape = page.new_shape()
            # Synthetic pen strokes with a fraction bar and a small diagram.
            for x in range(45, 180, 19):
                shape.draw_polyline([(x, 125), (x+9, 117), (x+13, 130), (x+2, 144), (x+15, 145)])
            shape.draw_line((40, 155), (180, 155))
            shape.draw_circle((290, 150), 40)
            shape.draw_line((250, 150), (330, 150))
            shape.finish(color=(0, 0, 0.5), width=1.2)
            shape.commit()
            page.add_ink_annot([[(40, 220), (75, 201), (105, 225)]])
        if not scanned:
            return doc.tobytes()
        with pdf.open() as scan:
            for page in doc:
                target = scan.new_page(width=595, height=842)
                target.insert_image(target.rect, pixmap=page.get_pixmap(alpha=False, annots=True))
            return scan.tobytes()
