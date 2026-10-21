"""Build a PDF of a meeting's summary and transcript (kept in memory, never stored)."""
import io
import os
from xml.sax.saxutils import escape

from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
import tfsfmt
from reportlab.lib import colors as rl
from reportlab.platypus import Image, PageBreak, Table, TableStyle, KeepTogether, ListFlowable, ListItem, Paragraph, SimpleDocTemplate, Spacer

FD = "/usr/share/fonts/truetype/dejavu/"
REG, BOLD = "Helvetica", "Helvetica-Bold"
if os.path.exists(FD + "DejaVuSans.ttf") and os.path.exists(FD + "DejaVuSans-Bold.ttf"):
    pdfmetrics.registerFont(TTFont("DejaVu", FD + "DejaVuSans.ttf"))
    pdfmetrics.registerFont(TTFont("DejaVu-Bold", FD + "DejaVuSans-Bold.ttf"))
    REG, BOLD = "DejaVu", "DejaVu-Bold"

H1 = ParagraphStyle("h1", fontName=BOLD, fontSize=18, leading=22, spaceAfter=6)
H2 = ParagraphStyle("h2", fontName=BOLD, fontSize=13, leading=17, spaceBefore=12, spaceAfter=4)
P = ParagraphStyle("p", fontName=REG, fontSize=10, leading=14, spaceAfter=6)
NOTE = ParagraphStyle("n", parent=P, fontSize=8.5, leading=12, textColor="#555555")
T = ParagraphStyle("t", parent=P, fontSize=9, leading=12, spaceAfter=3)


GREEN, TERRA, INK, MUT = "#4a5e3d", "#c0562b", "#2e2118", "#6b5a4b"
LOGO = "/opt/tfs-review/static/tfs-logo.png"
H1 = ParagraphStyle("h1", fontName=BOLD, fontSize=22, leading=26, spaceAfter=4, textColor=INK)
H2 = ParagraphStyle("h2", fontName=BOLD, fontSize=14, leading=18, spaceBefore=14, spaceAfter=6, textColor=GREEN)
P = ParagraphStyle("p", fontName=REG, fontSize=10.5, leading=15, spaceAfter=6, textColor=INK)
NOTE = ParagraphStyle("n", parent=P, fontSize=9, leading=13, textColor=MUT)
TT = ParagraphStyle("tt", parent=P, fontSize=10, leading=14.5, spaceAfter=2)


def _page(canvas, doc):
    canvas.saveState()
    canvas.setFillColor(GREEN)
    canvas.rect(0, letter[1] - 0.28 * inch, letter[0], 0.28 * inch, stroke=0, fill=1)
    canvas.setFillColor(TERRA)
    canvas.rect(0, letter[1] - 0.31 * inch, letter[0], 0.03 * inch, stroke=0, fill=1)
    canvas.setFont(REG, 8)
    canvas.setFillColor(MUT)
    canvas.drawString(0.9 * inch, 0.45 * inch, "Example Org  |  example.org  |  Private: please do not share")
    canvas.drawRightString(letter[0] - 0.9 * inch, 0.45 * inch, "Page " + str(doc.page))
    canvas.restoreState()


def make_pdf(title: str, when: str, summary: str, transcript: str, people=None) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=0.9 * inch, rightMargin=0.9 * inch,
                            topMargin=0.85 * inch, bottomMargin=0.8 * inch, title=title,
                            author="Example Org")
    s = []
    if os.path.exists(LOGO):
        try:
            s.append(Image(LOGO, width=1.1 * inch, height=1.1 * inch, kind="proportional", hAlign="LEFT"))
            s.append(Spacer(1, 6))
        except Exception:
            pass
    s += [Paragraph(escape(title), H1),
          Paragraph("Example Org meeting recorded " + escape(when) + ".", NOTE), Spacer(1, 6)]
    items = []

    def flush():
        nonlocal items
        if items:
            s.append(ListFlowable([ListItem(Paragraph(escape(i), P), leftIndent=14) for i in items],
                                  bulletType="bullet", start="•", leftIndent=14))
            items = []

    for line in summary.splitlines():
        t = line.strip()
        if t.startswith("- ") or t.startswith("* "):
            items.append(t[2:])
            continue
        flush()
        if t.startswith("## "):
            s.append(Paragraph(escape(t[3:]), H2))
        elif t:
            s.append(Paragraph(escape(t), P))
    flush()
    if people:
        s.append(Paragraph("People", H2))
        s.append(ListFlowable([ListItem(Paragraph(escape(i), P), leftIndent=14) for i in people],
                              bulletType="bullet", start="•", leftIndent=14))
    s.append(PageBreak())
    s.append(Paragraph("Full transcript", H2))
    turns = tfsfmt.parse(transcript)
    col = tfsfmt.colors(turns)
    for t in turns:
        who = ""
        if t["who"]:
            who = '<font name="' + BOLD + '" color="' + col[t["who"]] + '">' + escape(t["who"]) + "</font>"
        if t["time"]:
            who += ' <font size="8" color="' + MUT + '">' + escape(t["time"]) + "</font>"
        row = Table([[Paragraph((who + "<br/>" if who else "") + escape(t["text"]), TT)]], colWidths=[doc.width - 10])
        row.setStyle(TableStyle([("LINEBEFORE", (0, 0), (0, 0), 3, col.get(t["who"], MUT)),
                                 ("LEFTPADDING", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (-1, -1), 3),
                                 ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
        s.append(row)
        s.append(Spacer(1, 4))
    doc.build(s, onFirstPage=_page, onLaterPages=_page)
    return buf.getvalue()
