"""Report generation: PDF + PNG image, branded with the Balanzo logo."""
import io
import os

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from PIL import Image, ImageDraw, ImageFont

RS = "Rs "  # the report fonts lack a rupee glyph; use a safe prefix

LOGO_PATH = os.path.join(os.path.dirname(__file__), "logo.png")
try:
    _LOGO = Image.open(LOGO_PATH).convert("RGBA")
except Exception:
    _LOGO = None

# section order + display title (no emoji: report fonts can't render them)
SECTIONS = [
    ("income", "INCOME"),
    ("expense", "EXPENSE"),
    ("lent", "LENT"),
    ("borrow", "BORROW"),
]


def cash_in_hand(totals):
    return totals["income"] - totals["expense"] - totals["lent"] + totals["borrow"]


# -------------------------
# LOGO HELPERS
# -------------------------
def _circular_logo(size):
    """Logo cropped to a circle (transparent corners), at the given px size."""
    logo = _LOGO.resize((size, size), Image.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(logo, (0, 0), mask)
    return out


def _logo_on_white(size, opacity=1.0):
    """Circular logo flattened onto white (for PDF, which we draw as RGB)."""
    c = _circular_logo(size)
    bg = Image.new("RGBA", (size, size), (255, 255, 255, 255))
    bg.alpha_composite(c)
    rgb = bg.convert("RGB")
    if opacity < 1.0:
        white = Image.new("RGB", (size, size), (255, 255, 255))
        rgb = Image.blend(white, rgb, opacity)
    return rgb


# -------------------------
# PDF
# -------------------------
def build_pdf(label, totals, entries, generated=None):
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4

    # --- watermark: faint logo, centered ---
    if _LOGO is not None:
        wm = _logo_on_white(420, opacity=0.06)
        size = 95 * mm
        c.drawImage(ImageReader(wm), (width - size) / 2, (height - size) / 2,
                    size, size)

    # --- header ---
    x = 25 * mm
    y = height - 28 * mm
    if _LOGO is not None:
        logo = _circular_logo(220)
        c.drawImage(ImageReader(logo), x, y - 6, 16 * mm, 16 * mm, mask="auto")
        tx = x + 19 * mm
    else:
        tx = x
    c.setFillColor(colors.HexColor("#2e7d32"))
    c.setFont("Helvetica-Bold", 22)
    c.drawString(tx, y + 4, "Balanzo")
    c.setFont("Helvetica", 10)
    c.setFillColor(colors.grey)
    c.drawString(tx, y - 9, f"Finance Report  •  {label}")
    if generated:
        c.setFont("Helvetica", 8)
        c.drawString(tx, y - 20, f"Generated {generated}")
    y -= 20 * mm
    c.setStrokeColor(colors.lightgrey)
    c.line(x, y, width - 25 * mm, y)
    y -= 24

    right = width - 25 * mm

    # --- sections: each entry on its own dated line ---
    c.setFillColor(colors.black)
    for key, title in SECTIONS:
        rows = entries.get(key, [])
        c.setFont("Helvetica-Bold", 13)
        c.drawString(x, y, title)
        c.setFont("Helvetica", 11)
        c.drawRightString(right, y, f"{RS}{totals[key]:,.2f}")
        y -= 16
        if not rows:
            c.setFont("Helvetica-Oblique", 9)
            c.setFillColor(colors.grey)
            c.drawString(x + 12, y, "no entries")
            c.setFillColor(colors.black)
            y -= 14
        for date, name, amt in rows:
            c.setFont("Helvetica", 9)
            c.setFillColor(colors.grey)
            c.drawString(x + 12, y, str(date))
            c.setFont("Helvetica", 10)
            c.setFillColor(colors.dimgrey)
            c.drawString(x + 80, y, str(name)[:52])
            c.drawRightString(right, y, f"{RS}{amt:,.2f}")
            c.setFillColor(colors.black)
            y -= 14
            if y < 40 * mm:
                c.showPage()
                y = height - 30 * mm
        y -= 10
        if y < 50 * mm:
            c.showPage()
            y = height - 30 * mm

    # --- balance ---
    y -= 6
    c.setStrokeColor(colors.lightgrey)
    c.line(x, y, width - 25 * mm, y)
    y -= 22
    c.setFont("Helvetica-Bold", 15)
    c.setFillColor(colors.HexColor("#1565c0"))
    c.drawString(x, y, "Cash in hand")
    c.drawRightString(width - 25 * mm, y, f"{RS}{cash_in_hand(totals):,.2f}")

    c.save()
    buf.seek(0)
    return buf


# -------------------------
# IMAGE (PNG)
# -------------------------
def _font(size, bold=False):
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold
        else "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def build_image(label, totals, entries, generated=None):
    W = 760
    line_count = sum(1 + max(1, len(entries.get(k, []))) for k, _ in SECTIONS)
    H = 210 + line_count * 30 + 90

    img = Image.new("RGB", (W, H), "white")

    # --- watermark: faint circular logo, centered ---
    if _LOGO is not None:
        wsize = 420
        wm = _circular_logo(wsize)
        faint = wm.split()[3].point(lambda a: int(a * 0.07))
        wm.putalpha(faint)
        img.paste(wm, ((W - wsize) // 2, (H - wsize) // 2), wm)

    d = ImageDraw.Draw(img)

    # --- header ---
    x = 40
    tx = x
    if _LOGO is not None:
        logo = _circular_logo(76)
        img.paste(logo, (x, 30), logo)
        tx = x + 92
    d.text((tx, 32), "Balanzo", font=_font(38, bold=True), fill="#2e7d32")
    d.text((tx, 80), f"Finance Report  •  {label}", font=_font(17), fill="#777777")
    if generated:
        d.text((tx, 102), f"Generated {generated}", font=_font(13), fill="#999999")
    d.line([(x, 130), (W - 40, 130)], fill="#dddddd", width=2)

    y = 158
    for key, title in SECTIONS:
        rows = entries.get(key, [])
        d.text((x, y), title, font=_font(22, bold=True), fill="#111111")
        d.text((W - 40, y), f"{RS}{totals[key]:,.2f}", font=_font(20, bold=True),
               fill="#111111", anchor="ra")
        y += 32
        if not rows:
            d.text((x + 22, y), "no entries", font=_font(15), fill="#999999")
            y += 28
        for date, name, amt in rows:
            d.text((x + 22, y), str(date), font=_font(14), fill="#999999")
            d.text((x + 120, y), str(name)[:42], font=_font(16), fill="#555555")
            d.text((W - 40, y), f"{RS}{amt:,.2f}", font=_font(16),
                   fill="#555555", anchor="ra")
            y += 28
        y += 12

    d.line([(x, y), (W - 40, y)], fill="#dddddd", width=2)
    y += 18
    d.text((x, y), "Cash in hand", font=_font(24, bold=True), fill="#1565c0")
    d.text((W - 40, y), f"{RS}{cash_in_hand(totals):,.2f}", font=_font(24, bold=True),
           fill="#1565c0", anchor="ra")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


# -------------------------
# GROUP / TRIP SETTLEMENT REPORTS
# -------------------------
def build_group_pdf(group_name, settlement):
    """Trip settlement report: per-person paid/share/net + minimized transfers."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4

    if _LOGO is not None:
        wm = _logo_on_white(420, opacity=0.06)
        size = 95 * mm
        c.drawImage(ImageReader(wm), (width - size) / 2, (height - size) / 2,
                    size, size)

    x = 25 * mm
    y = height - 28 * mm
    if _LOGO is not None:
        logo = _circular_logo(220)
        c.drawImage(ImageReader(logo), x, y - 6, 16 * mm, 16 * mm, mask="auto")
        tx = x + 19 * mm
    else:
        tx = x
    c.setFillColor(colors.HexColor("#2e7d32"))
    c.setFont("Helvetica-Bold", 22)
    c.drawString(tx, y + 4, "Balanzo")
    c.setFont("Helvetica", 10)
    c.setFillColor(colors.grey)
    c.drawString(tx, y - 9, f"Trip Settlement  -  {group_name}")
    y -= 20 * mm
    c.setStrokeColor(colors.lightgrey)
    c.line(x, y, width - 25 * mm, y)
    y -= 24

    right = width - 25 * mm

    # per-person
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 13)
    c.drawString(x, y, "PER PERSON")
    y -= 18
    c.setFont("Helvetica", 9)
    c.setFillColor(colors.grey)
    c.drawString(x + 12, y, "Name")
    c.drawRightString(right - 150, y, "Paid")
    c.drawRightString(right - 70, y, "Share")
    c.drawRightString(right, y, "Net")
    y -= 14
    c.setFillColor(colors.black)
    for m in settlement["per_member"]:
        c.setFont("Helvetica", 10)
        c.drawString(x + 12, y, str(m["name"]))
        c.drawRightString(right - 150, y, f"{RS}{m['paid']:,.2f}")
        c.drawRightString(right - 70, y, f"{RS}{m['owed']:,.2f}")
        net = m["net"]
        c.setFillColor(colors.HexColor("#2e7d32") if net >= 0 else colors.HexColor("#c62828"))
        sign = "+" if net >= 0 else "-"
        c.drawRightString(right, y, f"{sign}{RS}{abs(net):,.2f}")
        c.setFillColor(colors.black)
        y -= 14
        if y < 60 * mm:
            c.showPage()
            y = height - 30 * mm

    # settlements
    y -= 12
    c.setStrokeColor(colors.lightgrey)
    c.line(x, y, right, y)
    y -= 20
    c.setFont("Helvetica-Bold", 13)
    c.drawString(x, y, "WHO PAYS WHOM")
    y -= 18
    if not settlement["transfers"]:
        c.setFont("Helvetica", 10)
        c.setFillColor(colors.dimgrey)
        c.drawString(x + 12, y, "All settled up - nothing to pay.")
        c.setFillColor(colors.black)
        y -= 14
    for t in settlement["transfers"]:
        c.setFont("Helvetica", 10)
        c.drawString(x + 12, y, f"{t['from_name']}  ->  {t['to_name']}")
        c.drawRightString(right, y, f"{RS}{t['amount']:,.2f}")
        y -= 14
        if y < 50 * mm:
            c.showPage()
            y = height - 30 * mm

    # total
    y -= 8
    c.setStrokeColor(colors.lightgrey)
    c.line(x, y, right, y)
    y -= 22
    c.setFont("Helvetica-Bold", 15)
    c.setFillColor(colors.HexColor("#1565c0"))
    c.drawString(x, y, "Total trip expense")
    c.drawRightString(right, y, f"{RS}{settlement['total']:,.2f}")

    c.save()
    buf.seek(0)
    return buf


def build_group_image(group_name, settlement):
    W = 760
    rows = len(settlement["per_member"]) + max(1, len(settlement["transfers"]))
    H = 260 + rows * 30 + 90

    img = Image.new("RGB", (W, H), "white")
    if _LOGO is not None:
        wsize = 420
        wm = _circular_logo(wsize)
        faint = wm.split()[3].point(lambda a: int(a * 0.07))
        wm.putalpha(faint)
        img.paste(wm, ((W - wsize) // 2, (H - wsize) // 2), wm)

    d = ImageDraw.Draw(img)
    x = 40
    tx = x
    if _LOGO is not None:
        logo = _circular_logo(76)
        img.paste(logo, (x, 30), logo)
        tx = x + 92
    d.text((tx, 32), "Balanzo", font=_font(38, bold=True), fill="#2e7d32")
    d.text((tx, 80), f"Trip Settlement  •  {group_name}", font=_font(17), fill="#777777")
    d.line([(x, 120), (W - 40, 120)], fill="#dddddd", width=2)

    y = 150
    d.text((x, y), "PER PERSON", font=_font(22, bold=True), fill="#111111")
    y += 34
    for m in settlement["per_member"]:
        d.text((x + 12, y), str(m["name"]), font=_font(17), fill="#333333")
        d.text((W - 250, y), f"paid {RS}{m['paid']:,.0f}", font=_font(15),
               fill="#777777", anchor="ra")
        net = m["net"]
        color = "#2e7d32" if net >= 0 else "#c62828"
        sign = "+" if net >= 0 else "-"
        d.text((W - 40, y), f"{sign}{RS}{abs(net):,.2f}", font=_font(17, bold=True),
               fill=color, anchor="ra")
        y += 28
    y += 12
    d.line([(x, y), (W - 40, y)], fill="#dddddd", width=2)
    y += 18

    d.text((x, y), "WHO PAYS WHOM", font=_font(22, bold=True), fill="#111111")
    y += 34
    if not settlement["transfers"]:
        d.text((x + 12, y), "All settled up — nothing to pay.", font=_font(17),
               fill="#555555")
        y += 28
    for t in settlement["transfers"]:
        d.text((x + 12, y), f"{t['from_name']}  →  {t['to_name']}", font=_font(17),
               fill="#333333")
        d.text((W - 40, y), f"{RS}{t['amount']:,.2f}", font=_font(17),
               fill="#c62828", anchor="ra")
        y += 28
    y += 12
    d.line([(x, y), (W - 40, y)], fill="#dddddd", width=2)
    y += 18
    d.text((x, y), "Total trip expense", font=_font(24, bold=True), fill="#1565c0")
    d.text((W - 40, y), f"{RS}{settlement['total']:,.2f}", font=_font(24, bold=True),
           fill="#1565c0", anchor="ra")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf
