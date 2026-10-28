"""PDF generation for invoices and rehab programmes."""

from datetime import date
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .config import Settings
from .db import pence_to_str

BRAND = colors.HexColor("#1f4e5a")


def _styles():
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("title", parent=base["Title"], textColor=BRAND, alignment=0, fontSize=20),
        "h2": ParagraphStyle("h2", parent=base["Heading2"], textColor=BRAND),
        "body": ParagraphStyle("body", parent=base["BodyText"], fontSize=10, leading=14),
        "small": ParagraphStyle("small", parent=base["BodyText"], fontSize=9, leading=12,
                                textColor=colors.HexColor("#444444")),
    }


def _esc(text: str | None) -> str:
    text = text or ""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br/>")


def _fmt_date(iso: str) -> str:
    return date.fromisoformat(iso[:10]).strftime("%-d %B %Y")


def render_invoice(invoice: dict, settings: Settings, out_path: Path) -> Path:
    """Create a UK-style invoice PDF (not VAT registered, so no VAT lines)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    s = _styles()
    doc = SimpleDocTemplate(str(out_path), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                            topMargin=16 * mm, bottomMargin=16 * mm,
                            title=f"Invoice {invoice['number']}", author=settings.business_name)
    story = []

    from_lines = [f"<b>{_esc(settings.business_name)}</b>"]
    if settings.owner_name:
        from_lines.append(_esc(settings.owner_name))
    if settings.business_address:
        from_lines.append(_esc(settings.business_address))
    for extra in (settings.business_phone, settings.business_email):
        if extra:
            from_lines.append(_esc(extra))

    to_lines = [f"<b>{_esc(invoice['owner_name'])}</b>"]
    if invoice.get("owner_address"):
        to_lines.append(_esc(invoice["owner_address"]))

    header = Table(
        [[Paragraph("INVOICE", s["title"]), Paragraph("<br/>".join(from_lines), s["body"])]],
        colWidths=[85 * mm, 89 * mm],
    )
    header.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story += [header, Spacer(1, 8 * mm)]

    meta = Table(
        [[Paragraph("<b>Bill to</b><br/>" + "<br/>".join(to_lines), s["body"]),
          Paragraph(f"<b>Invoice number:</b> {_esc(invoice['number'])}<br/>"
                    f"<b>Invoice date:</b> {_fmt_date(invoice['issue_date'])}<br/>"
                    f"<b>Payment due:</b> {_fmt_date(invoice['due_date'])}", s["body"])]],
        colWidths=[87 * mm, 87 * mm],
    )
    meta.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story += [meta, Spacer(1, 8 * mm)]

    rows = [["Description", "Amount"]]
    for item in invoice["items"]:
        desc = item["description"]
        if item.get("horse_name") and item["horse_name"].lower() not in desc.lower():
            desc = f"{desc} ({item['horse_name']})"
        rows.append([Paragraph(_esc(desc), s["body"]), pence_to_str(item["amount_pence"])])
    rows.append([Paragraph("<b>Total due</b>", s["body"]), pence_to_str(invoice["total_pence"])])

    table = Table(rows, colWidths=[134 * mm, 40 * mm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), BRAND),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LINEBELOW", (0, 1), (-1, -2), 0.25, colors.HexColor("#cccccc")),
        ("LINEABOVE", (0, -1), (-1, -1), 1, BRAND),
        ("FONTNAME", (1, -1), (1, -1), "Helvetica-Bold"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story += [table, Spacer(1, 10 * mm)]

    pay = ["<b>How to pay</b>"]
    if settings.bank_account_number:
        pay.append(f"Bank transfer to: {_esc(settings.bank_account_name or settings.business_name)}")
        pay.append(f"Sort code: {_esc(settings.bank_sort_code)} &nbsp;&nbsp; "
                   f"Account number: {_esc(settings.bank_account_number)}")
        pay.append(f"Please use <b>{_esc(invoice['number'])}</b> as the payment reference.")
    else:
        pay.append(f"Please quote {_esc(invoice['number'])} when paying.")
    story.append(Paragraph("<br/>".join(pay), s["body"]))
    if invoice.get("notes"):
        story += [Spacer(1, 6 * mm), Paragraph(_esc(invoice["notes"]), s["small"])]
    story += [Spacer(1, 10 * mm), Paragraph("Thank you for your custom.", s["small"])]

    doc.build(story)
    return out_path


def render_rehab(title: str, horse_name: str, owner_name: str, content: str,
                 settings: Settings, out_path: Path, issued: date | None = None) -> Path:
    """A simple, printable rehab programme. `content` is plain text; lines starting
    with '#' become headings and lines starting with '-' or '*' become bullet points."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    s = _styles()
    doc = SimpleDocTemplate(str(out_path), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                            topMargin=16 * mm, bottomMargin=16 * mm, title=title,
                            author=settings.business_name)
    issued = issued or date.today()
    story = [
        Paragraph(_esc(title), s["title"]),
        Paragraph(f"<b>Horse:</b> {_esc(horse_name)} &nbsp;&nbsp; <b>Owner:</b> {_esc(owner_name)} "
                  f"&nbsp;&nbsp; <b>Date:</b> {issued.strftime('%-d %B %Y')}", s["body"]),
        Spacer(1, 6 * mm),
    ]
    bullet = ParagraphStyle("bullet", parent=s["body"], leftIndent=12, bulletIndent=2)
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            story.append(Spacer(1, 3 * mm))
        elif stripped.startswith("#"):
            story.append(Paragraph(_esc(stripped.lstrip("#").strip()), s["h2"]))
        elif stripped[:1] in "-*•":
            story.append(Paragraph(_esc(stripped[1:].strip()), bullet, bulletText="•"))
        else:
            story.append(Paragraph(_esc(stripped), s["body"]))
    contact = settings.business_phone or settings.business_email
    footer = (f"If you have any concerns, or your horse's condition changes, stop the programme and "
              f"contact {_esc(settings.owner_name or settings.business_name)}"
              + (f" on {_esc(contact)}" if contact else "") + " or your vet.")
    story += [Spacer(1, 8 * mm), Paragraph(footer, s["small"]),
              Paragraph(_esc(settings.business_name), s["small"])]
    doc.build(story)
    return out_path
