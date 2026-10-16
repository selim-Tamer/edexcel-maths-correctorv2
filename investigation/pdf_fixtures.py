"""Reproducible original synthetic PDFs, without reading external documents."""
import io

# Deliberately simple pen-stroke glyphs, not a claim to simulate real handwriting.
GLYPHS = {
    '2': [[(0,8),(1,10),(5,10),(6,8),(0,0),(6,0)]],
    '4': [[(5,10),(0,3),(6,3)],[(5,10),(5,0)]],
    '8': [[(1,5),(0,8),(1,10),(5,10),(6,8),(5,5),(1,5),(0,2),(1,0),(5,0),(6,2),(5,5),(1,5)]],
    'x': [[(0,7),(5,0)],[(0,0),(5,7)]],
    '=': [[(0,6),(6,6)],[(0,3),(6,3)]],
    ' ': [],
}


def pdf_bytes(kind):
    if kind not in ('digital', 'strokes', 'scheme'):
        raise ValueError('Unknown synthetic fixture')
    from reportlab.pdfgen import canvas
    output = io.BytesIO()
    pdf = canvas.Canvas(output, pagesize=(420, 300), invariant=1, pageCompression=0)
    pdf.setTitle('Synthetic diagnostic fixture')
    pdf.setAuthor('AI Maths Marker synthetic tests')
    pdf.setFont('Helvetica', 12)
    pdf.drawString(25, 270, 'SYNTHETIC TEST - NO STUDENT DATA')
    pdf.drawString(25, 238, 'Question 1: Solve 2x + 3 = 11.')
    if kind == 'scheme':
        pdf.drawString(25, 195, 'M1: subtract 3 correctly to obtain 2x = 8.')
        pdf.drawString(25, 165, 'A1: divide by 2 correctly to obtain x = 4.')
        pdf.drawString(25, 125, 'A1 depends on valid method. Maximum: 2 marks.')
    elif kind == 'digital':
        pdf.drawString(25, 195, 'Student working:')
        pdf.drawString(40, 160, '2x = 8')
        pdf.drawString(40, 120, 'x = 4')
    else:
        pdf.drawString(25, 195, 'Synthetic pen-stroke working (not real handwriting):')
        pdf.setLineWidth(1.6)
        for row, text in enumerate(('2x = 8', 'x = 4')):
            for col, char in enumerate(text):
                for stroke in GLYPHS[char]:
                    path = pdf.beginPath()
                    for index, (x, y) in enumerate(stroke):
                        point = (40 + col*18 + x*1.7 + y*.035, 145-row*45 + y*1.7)
                        (path.moveTo if index == 0 else path.lineTo)(*point)
                    pdf.drawPath(path)
    pdf.showPage(); pdf.save()
    return output.getvalue()


if __name__ == '__main__':
    from pathlib import Path
    target = Path('output/pdf')
    target.mkdir(parents=True, exist_ok=True)
    for kind in ('digital','strokes','scheme'):
        (target / (kind+'.pdf')).write_bytes(pdf_bytes(kind))
    print('Wrote three synthetic PDFs to output/pdf; no network requests.')
