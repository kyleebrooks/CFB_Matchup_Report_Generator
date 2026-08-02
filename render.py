"""HTML assembly, PDF rendering, and the page watermark."""

import logging
import os
import re

import config

try:
    import markdown  # optional for Markdown -> HTML
except ImportError:
    markdown = None


# Design notes, learned the hard way:
# - wkhtmltopdf is WebKit circa 2015 and, as packaged by Ubuntu, the reduced build.
#   No CSS variables, no grid, unreliable flexbox — layout is tables and floats.
#   Header/footer options are silently ignored, so page numbers are stamped onto the
#   finished PDF with PyMuPDF instead (see _add_page_furniture).
# - The palette is editorial black/white/grey on purpose: every chart is drawn in the
#   two teams' own colours, and any brand colour here would eventually clash with one.
#   Ink and weight carry the hierarchy; the team colours get the page to themselves.
CSS = """
  body { font-family: "Helvetica Neue", Helvetica, Arial, "Liberation Sans",
                      "DejaVu Sans", sans-serif;
         line-height: 1.55; color: #16181d; font-size: 10.2pt; margin: 0; }
  h1, h2, h3 { margin: 0.35em 0 0.25em; }

  /* ---------- cover ---------- */
  .cover { page-break-after: always; }
  .cover-band { background: #101114; color: #ffffff; padding: 26pt 26pt 22pt; }
  .cover-kicker { font-size: 8.5pt; letter-spacing: 3.2pt; text-transform: uppercase;
                  color: #9aa0aa; margin: 0 0 14pt; font-weight: bold; }
  .cover-crests { width: 100%; border-collapse: collapse; margin: 0 0 16pt; }
  .cover-crests td { border: none; text-align: center; vertical-align: middle; }
  .cover-crests img { width: 86pt; height: 86pt; object-fit: contain; }
  .cover-vs { font-size: 13pt; font-weight: bold; color: #6d737e;
              letter-spacing: 2pt; width: 60pt; }
  .cover-title { font-size: 27pt; line-height: 1.08; font-weight: bold; margin: 0 0 6pt;
                 letter-spacing: 0.2pt; }
  .cover-sub { font-size: 11pt; color: #c3c7ce; margin: 0; }
  .cover-rule { border: none; border-top: 2.5pt solid #101114; margin: 0; }
  .cover-date { font-size: 9pt; color: #565b64; margin: 8pt 0 0;
                text-transform: uppercase; letter-spacing: 1.4pt; }

  .toc { margin-top: 22pt; }
  .toc-head { font-size: 9pt; letter-spacing: 2.6pt; text-transform: uppercase;
              font-weight: bold; color: #16181d; border-bottom: 1.5pt solid #101114;
              padding-bottom: 5pt; margin: 0 0 10pt; }
  .toc ol { margin: 0; padding: 0; list-style: none;
            column-count: 2; -webkit-column-count: 2; column-gap: 26pt;
            -webkit-column-gap: 26pt; }
  .toc li { font-size: 9.6pt; padding: 3.5pt 0; border-bottom: 0.75pt solid #e3e4e6;
            -webkit-column-break-inside: avoid; page-break-inside: avoid; }
  .toc a { color: #16181d; text-decoration: none; }
  .toc .toc-n { display: inline-block; width: 18pt; color: #8b9099; font-weight: bold;
                font-size: 8.5pt; }

  /* ---------- running content ---------- */
  .content { text-align: left; }
  .content h2 { font-size: 13.5pt; font-weight: bold; text-transform: uppercase;
                letter-spacing: 0.5pt; margin: 26pt 0 8pt; padding: 0 0 5pt;
                border-bottom: 1.5pt solid #101114; page-break-after: avoid; }
  .content h2 .sec-n { color: #9aa0aa; padding-right: 7pt; }
  .content h1 { font-size: 14pt; font-weight: bold; margin-top: 24pt;
                page-break-after: avoid; }
  .content h3 { font-size: 10.8pt; font-weight: bold; margin: 13pt 0 4pt;
                page-break-after: avoid; }
  .content p { margin: 0.55em 0; }
  .content ul, .content ol { margin: 0.4em 0 0.7em 1.35em; padding: 0; }
  .content li { margin: 0.28em 0; }
  .content blockquote { margin: 10pt 0 10pt 0; padding: 2pt 0 2pt 12pt;
                        border-left: 2.5pt solid #101114; color: #3c414a;
                        font-style: italic; }
  .content blockquote p { margin: 0.3em 0; }
  sup.cite { font-size: 6.8pt; line-height: 0; }
  sup.cite a { color: #6d737e; text-decoration: none; }

  /* Tables: black header band, hairline rows, zebra — no vertical grid. */
  .content table { border-collapse: collapse; width: 100%; margin: 0.8em 0;
                   font-size: 9pt; page-break-inside: auto; }
  .content tr { page-break-inside: avoid; }
  .content th { background: #101114; color: #ffffff; text-align: left;
                font-size: 7.8pt; text-transform: uppercase; letter-spacing: 0.8pt;
                padding: 5.5pt 8pt; border: none; }
  .content td { padding: 5pt 8pt; border: none; border-bottom: 0.75pt solid #e3e4e6; }
  .content tr:nth-child(even) td { background: #f6f6f4; }

  /* ---------- charts ---------- */
  .viz-intro { font-size: 9pt; color: #6d737e; margin: 0 0 12pt; }
  figure.chart { margin: 0 0 16pt; padding: 8pt 8pt 7pt; border: 0.75pt solid #d9dade;
                 page-break-inside: avoid; }
  figure.chart img { width: 100%; max-width: 100%; height: auto; display: block; }
  figure.chart figcaption { font-size: 8.6pt; color: #6d737e; margin-top: 6pt;
                            padding-top: 5pt; border-top: 0.75pt solid #e3e4e6; }
  figure.chart .chart-name { font-weight: bold; color: #16181d;
                             text-transform: uppercase; letter-spacing: 0.5pt;
                             font-size: 8.2pt; }

  /* A section heading pinned alone at the foot of a page reads as a mistake; keep it
     glued to its opening line. (wkhtmltopdf honours break-inside far more reliably
     than break-after.) */
  .keep { page-break-inside: avoid; }

  /* ---------- sources ---------- */
  /* Two columns via a table: this WebKit ignores CSS multi-column entirely. */
  .sources { margin-top: 26pt; }
  .sources h2 { font-size: 13.5pt; font-weight: bold; text-transform: uppercase;
                letter-spacing: 0.5pt; margin: 26pt 0 8pt; padding-bottom: 5pt;
                border-bottom: 1.5pt solid #101114; page-break-after: avoid; }
  .sources-cols { width: 100%; border-collapse: collapse; }
  .sources-cols td { width: 50%; vertical-align: top; border: none; padding: 0; }
  .sources-cols td + td { padding-left: 20pt; }
  .sources ol { margin: 6pt 0 0; padding-left: 16pt; }
  .sources li { font-size: 7.8pt; margin: 0 0 5pt; word-wrap: break-word;
                color: #3c414a; line-height: 1.4; }
  .sources .src-url { color: #8b9099; }

  /* ---------- generation details ---------- */
  .meta { margin-top: 20pt; background: #f6f6f4; border-top: 1.5pt solid #101114;
          padding: 8pt 12pt 9pt; font-size: 7.6pt; color: #6d737e; }
  .meta-head { text-transform: uppercase; letter-spacing: 1.6pt; font-weight: bold;
               color: #3c414a; margin: 0 0 4pt; font-size: 7.6pt; }
  .meta p { margin: 2pt 0; }
"""

_HEADING_RE = re.compile(r"<h[1-3][^>]*>", re.IGNORECASE)

# A trailing "Sources"/"References" heading the model wrote anyway, despite being told not
# to. We render the authoritative list ourselves from the registry, so drop the duplicate.
_MODEL_SOURCES_RE = re.compile(
    r"\n#{1,6}\s*\**\s*(sources?|references?|citations?|works cited)\b\s*\**\s*\n.*\Z",
    re.IGNORECASE | re.DOTALL,
)


def strip_model_sources(text: str) -> str:
    """Remove a model-authored trailing sources section, keeping inline [n] markers."""
    cleaned = _MODEL_SOURCES_RE.sub("\n", text or "")
    if cleaned != text:
        logging.info("Stripped a model-authored sources section; using the registry list.")
    return cleaned.rstrip()


def markdown_to_html(text: str) -> str:
    if markdown:
        return markdown.markdown(text, extensions=["tables", "sane_lists"])
    return "<br>\n".join(text.split("\n"))


_H2_RE = re.compile(r"<h2([^>]*)>(.*?)</h2>", re.IGNORECASE | re.DOTALL)
_CITE_RE = re.compile(r"\[(\d{1,3})\]")
_TAG_RE = re.compile(r"<[^>]+>")


def link_citations(body_html: str) -> str:
    """Turn the model's inline [n] markers into superscript links to the source list.

    Bracketed citations as body text read like a draft; superscripts read like a
    published report. The target ids already exist — sources_block() writes one per
    entry — so the marker becomes navigation instead of clutter.
    """
    return _CITE_RE.sub(r'<sup class="cite"><a href="#src-\1">\1</a></sup>', body_html)


def number_sections(body_html: str) -> tuple[str, list[tuple[int, str]]]:
    """Give every section heading a number and an anchor; return the contents list.

    The reports run twelve-plus sections over eight-plus pages with nothing to
    navigate by. Numbering the headings and collecting them for a linked contents
    page on the cover is what turns that from a scroll into a document.
    """
    sections: list[tuple[int, str]] = []

    def stamp(match):
        n = len(sections) + 1
        attrs, inner = match.group(1), match.group(2)
        title = _TAG_RE.sub("", inner).strip()
        sections.append((n, title))
        return (f'<h2 id="sec-{n}"{attrs}>'
                f'<span class="sec-n">{n:02d}</span>{inner}</h2>')

    numbered = _H2_RE.sub(stamp, body_html)
    # Glue each heading to its opening paragraph so a section title is never left
    # stranded as the last line of a page.
    numbered = re.sub(r"(<h2\b.*?</h2>)\s*(<p>.*?</p>)",
                      r'<div class="keep">\1\2</div>', numbered,
                      flags=re.IGNORECASE | re.DOTALL)
    return numbered, sections


def toc_block(sections: list[tuple[int, str]]) -> str:
    if not sections:
        return ""
    items = "".join(
        f'<li><a href="#sec-{n}"><span class="toc-n">{n:02d}</span>{title}</a></li>'
        for n, title in sections
    )
    return (f'<div class="toc"><p class="toc-head">In this report</p>'
            f'<ol>{items}</ol></div>')


def _charts_block(charts: list[dict]) -> str:
    figures = []
    for c in charts:
        figures.append(
            f'<figure class="chart">'
            f'<img src="{c["img"]}" alt="{c["title"]}">'
            f'<figcaption><span class="chart-name">{c["title"]}</span> &middot; '
            f'{c["caption"]}</figcaption>'
            f'</figure>'
        )
    return (
        '<div class="viz">'
        '<h2>Visual Analytics</h2>'
        '<p class="viz-intro">Generated directly from the CollegeFootballData feeds for this '
        'matchup. The same visuals appear on every report, so numbers can be compared '
        'like-for-like from one game to the next.</p>'
        + "".join(figures) +
        '</div>'
    )


def inject_charts(body_html: str, charts: list[dict]) -> str:
    """Place the visual dashboard immediately after the opening section.

    Anchoring to the second heading keeps the position identical on every report while
    letting the Matchup Overview set the stage first. If the model produced no second
    heading, the dashboard simply leads.
    """
    if not charts:
        return body_html
    block = _charts_block(charts)
    matches = list(_HEADING_RE.finditer(body_html))
    if len(matches) >= 2:
        cut = matches[1].start()
        return body_html[:cut] + block + body_html[cut:]
    return block + body_html


def sources_block(registry) -> str:
    entries = registry.entries()
    if not entries:
        return ""
    items = []
    for e in entries:
        label = e["title"] or e["publisher"] or e["url"]
        publisher = f' <em>({e["publisher"]})</em>' if e["publisher"] and e["title"] else ""
        items.append(
            f'<li id="src-{e["index"]}">{label}{publisher}<br>'
            f'<span class="src-url">{e["url"]}</span></li>'
        )
    # Split into two balanced columns, numbering continuing down then across.
    half = (len(items) + 1) // 2
    first, second = items[:half], items[half:]
    right = (f'<ol start="{half + 1}">' + "".join(second) + '</ol>') if second else ''
    return (
        '<div class="sources"><h2>Sources</h2>'
        '<table class="sources-cols"><tr>'
        f'<td><ol>{"".join(first)}</ol></td>'
        f'<td>{right}</td>'
        '</tr></table></div>'
    )


def build_html(
    *,
    home_full: str,
    away_full: str,
    year: int,
    home_logo: str,
    away_logo: str,
    report_created: str,
    report_markdown: str,
    charts: list[dict],
    registry,
    meta_lines: list[str],
    title: str | None = None,
    banner: str = "AFPLNA College Football Matchup Report",
    include_sources: bool = True,
    include_generation_details: bool = True,
) -> str:
    """Assemble the printable HTML.

    Single-team reports pass an empty away_full/away_logo; the header then centres on
    one crest instead of rendering an "X vs (blank)" line and an empty <img>.
    """
    # With sources off, [n] markers would point at a list that is not there — strip
    # them entirely rather than leaving dead superscripts in the text.
    prose = strip_model_sources(report_markdown)
    if not include_sources:
        prose = _CITE_RE.sub("", prose)
        prose = re.sub(r"[ \t]+([.,;:!?])", r"\1", prose)   # no space left before punctuation
    body = markdown_to_html(prose)
    if include_sources:
        body = link_citations(body)
    body = inject_charts(body, charts)
    body, sections = number_sections(body)
    meta_html = "".join(f"<p>{line}</p>" for line in meta_lines)
    meta_block = (f'<div class="meta"><p class="meta-head">Generation details</p>'
                  f'{meta_html}</div>') if include_generation_details else ''

    subject = f"{home_full} vs {away_full} ({year})" if away_full else f"{home_full} ({year})"
    doc_title = title or (f"{home_full} vs {away_full}" if away_full else home_full)

    # The cover: one crest centred for a team report, two either side of a VS mark for
    # a matchup. Laid out with a table because this renders through wkhtmltopdf.
    home_img = (f'<img src="{home_logo}" alt="{home_full} logo">'
                if home_logo else "")
    if away_full:
        away_img = (f'<img src="{away_logo}" alt="{away_full} logo">'
                    if away_logo else "")
        crests = (f'<table class="cover-crests"><tr>'
                  f'<td style="width:45%">{home_img}</td>'
                  f'<td class="cover-vs">VS</td>'
                  f'<td style="width:45%">{away_img}</td>'
                  f'</tr></table>')
        cover_title = f"{home_full}<br>vs {away_full}"
    else:
        crests = (f'<table class="cover-crests"><tr><td>{home_img}</td></tr></table>'
                  if home_img else "")
        cover_title = home_full

    return f"""<html>
<head>
  <meta charset="utf-8" />
  <title>{doc_title}</title>
  <style>{CSS}</style>
</head>
<body>
  <div class="cover">
    <div class="cover-band">
      <p class="cover-kicker">{banner}</p>
      {crests}
      <p class="cover-title">{cover_title}</p>
      <p class="cover-sub">{year} Season</p>
    </div>
    <p class="cover-date">Generated {report_created}</p>
    {toc_block(sections)}
  </div>
  <div class="content">{body}</div>
  {sources_block(registry) if include_sources else ''}
  {meta_block}
</body>
</html>
"""


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------
def _add_page_furniture(filepath: str, subject: str, brand: str) -> None:
    """Stamp a running footer — subject left, page numbers right — onto every page.

    wkhtmltopdf's own header/footer options require the patched-Qt build, and the one
    apt installs is the reduced build that silently ignores them. PyMuPDF is already a
    dependency for the watermark, and stamping after the fact works on any build.

    The cover is left clean; numbering starts at 2 on the first content page, the way
    any printed report numbers its front matter.
    """
    import fitz

    ink = (0.42, 0.44, 0.48)
    hairline = (0.85, 0.85, 0.86)
    inset = 34                       # matches the 12mm side margins
    doc = fitz.open(filepath)
    try:
        total = doc.page_count
        for index in range(1, total):
            page = doc[index]
            width, height = page.rect.width, page.rect.height
            y = height - 26
            page.draw_line(fitz.Point(inset, y - 10), fitz.Point(width - inset, y - 10),
                           color=hairline, width=0.7)
            left = f"{subject}  ·  {brand}" if brand else subject
            page.insert_text(fitz.Point(inset, y), left, fontsize=7.2,
                             fontname="helv", color=ink)
            label = f"Page {index + 1} of {total}"
            text_w = fitz.get_text_length(label, fontname="helv", fontsize=7.2)
            page.insert_text(fitz.Point(width - inset - text_w, y), label,
                             fontsize=7.2, fontname="helv", color=ink)
        doc.saveIncr()
    finally:
        doc.close()


def write_pdf(html_content: str, filepath: str, *, footer_subject: str = "",
              footer_brand: str = "") -> None:
    """Render HTML to PDF, tolerating wkhtmltopdf's non-zero exit on non-fatal warnings."""
    import pdfkit

    pdfkit_config = (
        pdfkit.configuration(wkhtmltopdf=config.WKHTMLTOPDF_PATH)
        if config.WKHTMLTOPDF_PATH else None
    )
    pdf_options = {
        "enable-local-file-access": None,
        "background": None,
        # A missing/slow remote resource (e.g. a team-logo URL) makes wkhtmltopdf exit
        # non-zero even though it writes a valid PDF; tell it to ignore load errors.
        "load-error-handling": "ignore",
        "load-media-error-handling": "ignore",
        "encoding": "UTF-8",
        # wkhtmltopdf ignores @page CSS margins; they only exist as options. The wide
        # bottom margin reserves the strip the stamped running footer sits in.
        "margin-top": "14mm",
        "margin-bottom": "16mm",
        "margin-left": "12mm",
        "margin-right": "12mm",
    }
    try:
        pdfkit.from_string(html_content, filepath, configuration=pdfkit_config, options=pdf_options)
    except Exception as e:
        # pdfkit raises whenever wkhtmltopdf returns a non-zero exit code, which it does for
        # non-fatal warnings even after writing a perfectly good PDF. Only treat this as a
        # real failure when no usable PDF actually landed on disk.
        pdf_ok = os.path.exists(filepath) and os.path.getsize(filepath) > 1024
        if pdf_ok:
            try:
                import fitz
                doc = fitz.open(filepath)
                pdf_ok = doc.page_count > 0
                doc.close()
            except Exception:
                pdf_ok = False
        if not pdf_ok:
            raise
        logging.warning(f"wkhtmltopdf exited non-zero but produced a valid PDF; continuing: {e}")

    if footer_subject or footer_brand:
        try:
            _add_page_furniture(filepath, footer_subject, footer_brand)
        except Exception as e:
            # A report without page numbers still ships; one that failed does not.
            logging.warning(f"Could not stamp the page footer: {e}")



def watermark_preview(image_path: str, out_path: str, opacity: float = 0.09,
                      scale: float = 0.92, pages: int = 1) -> str:
    """Stamp a sample page so a watermark can be judged without building a report.

    A real report costs several minutes and real money per attempt, which is a poor way
    to discover that an image is too strong, too pale, or carrying a background.
    """
    import io
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import letter

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    width, height = letter
    for n in range(max(1, pages)):
        c.setFont('Helvetica-Bold', 16)
        c.drawString(64, height - 72, 'Watermark preview')
        c.setFont('Helvetica', 10)
        c.drawString(64, height - 92,
                     f'opacity={opacity}  scale={scale}  source={os.path.basename(image_path)}')
        c.setFont('Helvetica', 10.5)
        y = height - 130
        body = (
            "This paragraph exists so the watermark can be judged against real body text. "
            "If any word below is hard to read, the mark is too strong: lower the opacity. "
            "If the mark is invisible, raise it. A background box, a grey grid or a hard "
            "rectangular edge behind this text means the source image is carrying a "
            "background that should have been transparent."
        )
        for i in range(28):
            c.drawString(64, y, body[:96] if i % 2 == 0 else body[96:192] or body[:96])
            y -= 15
        c.showPage()
    c.save()

    with open(out_path, 'wb') as fh:
        fh.write(buf.getvalue())
    add_pdf_watermark(out_path, image_path, opacity=opacity, scale=scale)
    return out_path


def prepare_watermark(image_path: str):
    """Turn any supplied image into something usable as a page watermark.

    Customers send whatever they have. Two shapes arrive repeatedly and both stamp
    badly if used as-is:

      - a logo on an opaque WHITE background, which lays a white rectangle over the
        page and boxes in the text;
      - an export where the checkerboard that a graphics editor draws to *indicate*
        transparency has been flattened into real pixels, so the mark arrives wearing
        a grey grid.

    Both are fixed by the same rule: build the alpha channel from darkness. Ink stays,
    paper disappears, and the mid-grey of a checkerboard falls away to almost nothing.
    An image that already carries real transparency keeps it — that alpha is multiplied
    by the darkness ramp rather than replaced, so a genuine transparent PNG is not
    second-guessed.

    Returns a BytesIO of a PNG, ready for ImageReader.
    """
    import io
    from PIL import Image

    with Image.open(image_path) as raw:
        src = raw.convert('RGBA')

    grey = src.convert('L')
    # alpha = how dark the pixel is. White -> 0, black -> 255, and the ~90% grey of a
    # transparency checkerboard -> single digits.
    darkness = grey.point(lambda v: 255 - v)

    existing = src.getchannel('A')
    if existing.getextrema()[0] < 255:
        # The image really is transparent somewhere; respect it and layer darkness on top.
        darkness = Image.composite(darkness, Image.new('L', src.size, 0), existing)

    # A source that has already been faded to near-white has almost no darkness left,
    # so the derived alpha is ~0 and the stamp comes out invisible. Say so: the symptom
    # is a report with no watermark and nothing anywhere explaining why.
    peak = darkness.getextrema()[1]
    if peak < 40:
        logging.warning(
            f"Watermark {os.path.basename(image_path)} is almost entirely light "
            f"(peak ink {peak}/255). It will stamp close to invisible. Supply the "
            f"full-contrast version of the artwork and let watermark_opacity do the "
            f"fading."
        )

    out = Image.merge('RGBA', (*src.convert('RGB').split(), darkness))
    buf = io.BytesIO()
    out.save(buf, format='PNG', optimize=True)
    buf.seek(0)
    return buf


def watermark_ink(image_path: str) -> dict:
    """How much usable ink a candidate watermark has. For the preview command."""
    from PIL import Image
    with Image.open(image_path) as raw:
        grey = raw.convert('RGBA').convert('L')
    lo, hi = grey.getextrema()
    px = list(grey.getdata())
    inked = sum(1 for v in px if v < 200)
    return {'peak_ink': 255 - lo, 'lightest': hi,
            'inked_pct': inked / len(px) * 100,
            'usable': (255 - lo) >= 40}


def add_pdf_watermark(pdf_path: str, image_path: str, opacity: float = 0.09, scale: float = 0.92) -> None:
    """Stamp a centered, faint watermark on every page of a PDF.

    wkhtmltopdf reuses a single shared resource dictionary across all pages, which makes a
    PyPDF2 per-page merge render the mark on only one page. Instead we build one faint
    overlay per page size with reportlab (opacity baked in via fill-alpha) and composite it
    onto every page with PyMuPDF's show_pdf_page, which isolates per-page resources
    correctly. The mark is drawn on top at low opacity, so it shows on every page while the
    report text stays fully readable.
    """
    import io
    import fitz  # PyMuPDF
    from reportlab.pdfgen import canvas
    from reportlab.lib.utils import ImageReader

    img = ImageReader(prepare_watermark(image_path))
    iw, ih = img.getSize()

    def _overlay(pw, ph):
        ratio = min((pw * scale) / iw, (ph * scale) / ih)   # fill the page, keep aspect ratio
        dw, dh = iw * ratio, ih * ratio
        buf = io.BytesIO()
        c = canvas.Canvas(buf, pagesize=(pw, ph))
        c.setFillAlpha(opacity)                             # faint enough to read text through
        c.drawImage(img, (pw - dw) / 2.0, (ph - dh) / 2.0, width=dw, height=dh, mask="auto")
        c.showPage()
        c.save()
        return buf.getvalue()

    doc = fitz.open(pdf_path)
    overlays: dict = {}
    for page in doc:
        rect = page.rect
        key = (round(rect.width, 1), round(rect.height, 1))
        if key not in overlays:
            overlays[key] = _overlay(rect.width, rect.height)
        ov = fitz.open("pdf", overlays[key])
        page.show_pdf_page(rect, ov, 0, overlay=True)       # composite per page -> every page gets it
        ov.close()

    tmp = pdf_path + ".wm.tmp"
    doc.save(tmp, garbage=3, deflate=True)                  # PyMuPDF cannot save over the open file
    doc.close()
    os.replace(tmp, pdf_path)
