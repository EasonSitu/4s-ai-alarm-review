"""Build docs/4S-API.pdf from the same Markdown used by /docs (requires ReportLab)."""
import os
import re
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle, XPreformatted

ROOT = Path(__file__).resolve().parents[1]
WIDTH = A4[0] - 90  # 39 pt margins + 6 pt frame padding per side.


def inline(value):
    value = escape(value)
    value = re.sub(r"`([^`]+)`", lambda m: '<font name="Courier">' + m[1] + '</font>'
                   if m[1].isascii() else m[1], value)
    return re.sub(r"\*\*(.+?)\*\*", r'<font name="DocHeading">\1</font>', value)


def para(value, size=10, leading=15, font="DocText", after=5):
    style = ParagraphStyle("api", fontName=font, fontSize=size, leading=leading,
                           spaceAfter=after, wordWrap="CJK", textColor=colors.HexColor("#202124"))
    return Paragraph(inline(value), style)


def field_table(block):
    rows = [[c.strip() for c in row.strip("|").split("|")] for row in block.splitlines()]
    header, cells = rows[0], rows[2:]
    if len(header) == 4:
        widths = (.19, .14, .09, .58)
    elif len(header) == 2:
        widths = (.19, .81)
    elif header[0] == "HTTP":
        widths = (.16, .37, .47)
    else:
        widths = (.32, .18, .50)
    data = []
    for number, row in enumerate([header] + cells):
        data.append([para(c, 9.1, 13, "DocHeading" if number == 0 else "DocText", 0) for c in row])
    table = Table(data, colWidths=[w * WIDTH for w in widths], repeatRows=1, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eff1f3")),
        ("GRID", (0, 0), (-1, -1), .45, colors.HexColor("#cdd0d4")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 7), ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return table


def render():
    pdfmetrics.registerFont(TTFont("DocText", os.getenv("FOUR_S_PDF_FONT", "/System/Library/Fonts/Supplemental/Arial Unicode.ttf")))
    pdfmetrics.registerFont(TTFont("DocHeading", os.getenv("FOUR_S_PDF_BOLD", "/System/Library/Fonts/STHeiti Medium.ttc")))
    md = (ROOT / "docs/4S-API.md").read_text(encoding="utf-8")
    target = ROOT / "docs/4S-API.pdf"
    staged = target.with_suffix(".pdf.new")
    title = md.splitlines()[0].removeprefix("# ")
    doc = SimpleDocTemplate(str(staged), pagesize=A4, title=title, author="4S",
                            leftMargin=39, rightMargin=39, topMargin=31, bottomMargin=41)
    story = []
    code = ParagraphStyle("json", fontName="Courier", fontSize=7.5, leading=11.3, leftIndent=9,
                          rightIndent=9, borderPadding=6, backColor=colors.HexColor("#f3f4f6"),
                          spaceBefore=4, spaceAfter=11)
    for block in md.strip().split("\n\n"):
        if block.startswith("# "):
            story.append(para(title, 21, 29, "DocHeading", 12))
        elif block.startswith("## "):
            section = block[3:]
            if section.split()[0] in {"2", "3", "4", "6", "7"}:
                story.append(PageBreak())
            story.append(Spacer(1, 5))
            story.append(para(section, 15, 22, "DocHeading", 6))
        elif block.startswith("### "):
            story.append(para(block[4:], 11, 17, "DocHeading", 4))
        elif block.startswith(("- ", "1. ")):
            story.extend(para(line.removeprefix("- "), after=3) for line in block.splitlines())
        elif block.startswith("|"):
            story.extend((field_table(block), Spacer(1, 7)))
        elif block.startswith("```"):
            body = escape("\n".join(block.splitlines()[1:-1]))
            body = re.sub(r"[^\x00-\x7f]+", lambda m: '<font name="DocText">'+m[0]+'</font>', body)
            story.append(XPreformatted(body, code))
        else:
            story.append(para(block))

    def footer(canvas, document):
        canvas.setStrokeColor(colors.HexColor("#cdd0d4"))
        canvas.line(44, 34, A4[0]-44, 34)
        canvas.setFont("DocText", 8)
        canvas.drawString(44, 21, title)
        canvas.drawRightString(A4[0]-44, 21, str(document.page))

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    staged.replace(target)


if __name__ == "__main__":
    render()
