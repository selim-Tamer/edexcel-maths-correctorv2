"""Synthetic PDFs using permissive dependencies; no student data or OCR."""
from contextlib import closing
from io import BytesIO
from reportlab.pdfgen.canvas import Canvas
from reportlab.lib.utils import ImageReader
from pypdf import PdfWriter
from pypdf.generic import (ArrayObject, DictionaryObject, FloatObject, NameObject,
                           NumberObject, DecodedStreamObject)
import pypdfium2 as pdfium


def render_page(data, number=0, scale=1):
    with pdfium.PdfDocument(data) as doc:
        doc.init_forms()
        with closing(doc[number]) as page, closing(page.render(scale=scale, draw_annots=True)) as bitmap:
            return bitmap.to_pil().copy()


def make_pdf(scanned=False, pages=6, forms=False):
    buffer = BytesIO()
    canvas = Canvas(buffer, pagesize=(595, 842), invariant=True, pageCompression=0)
    for n in range(pages):
        canvas.setFont('Helvetica', 11)
        canvas.drawString(40, 792, f'Question {n + 1}: Find the mean. [2 marks]')
        canvas.drawString(40, 762, 'Digital reference: (67 + 1) / 2 = 34')
        canvas.setStrokeColorRGB(0, 0, 0.5)
        canvas.setLineWidth(1.2)
        path = canvas.beginPath()
        for x in range(45, 180, 19):
            path.moveTo(x, 717)
            for xx, yy in [(x+9, 725), (x+13, 712), (x+2, 698), (x+15, 697)]:
                path.lineTo(xx, yy)
        canvas.drawPath(path)
        canvas.line(40, 687, 180, 687)
        canvas.circle(290, 692, 40)
        canvas.line(250, 692, 330, 692)
        if forms:
            canvas.acroForm.textfield(name=f'answer{n+1}', value='34', x=40, y=500, width=100, height=24)
        canvas.showPage()
    canvas.save()
    writer = PdfWriter(clone_from=BytesIO(buffer.getvalue()))
    for page in writer.pages:
        # Real ink annotation and deterministic appearance, not a flattened drawing.
        appearance = DecodedStreamObject()
        appearance.set_data(b'1 0 0 RG 1 w 0 5 m 35 24 l 65 0 l S')
        appearance.update({NameObject('/Type'): NameObject('/XObject'),
                           NameObject('/Subtype'): NameObject('/Form'),
                           NameObject('/BBox'): ArrayObject(map(NumberObject, [0, 0, 65, 24])),
                           NameObject('/Resources'): DictionaryObject()})
        annotation = DictionaryObject({
            NameObject('/Type'): NameObject('/Annot'), NameObject('/Subtype'): NameObject('/Ink'),
            NameObject('/Rect'): ArrayObject(map(NumberObject, [40, 617, 105, 641])),
            NameObject('/InkList'): ArrayObject([ArrayObject(map(NumberObject, [40, 622, 75, 641, 105, 617]))]),
            NameObject('/C'): ArrayObject(map(FloatObject, [1, 0, 0])), NameObject('/F'): NumberObject(4),
            NameObject('/AP'): DictionaryObject({NameObject('/N'): writer._add_object(appearance)}),
        })
        page.setdefault(NameObject('/Annots'), ArrayObject()).append(writer._add_object(annotation))
    out = BytesIO()
    writer.write(out)
    writer.close()
    digital = out.getvalue()
    if not scanned:
        return digital
    scan = BytesIO()
    canvas = Canvas(scan, pagesize=(595, 842), invariant=True)
    for n in range(pages):
        with render_page(digital, n) as image:
            canvas.drawImage(ImageReader(image), 0, 0, 595, 842)
        canvas.showPage()
    canvas.save()
    return scan.getvalue()
