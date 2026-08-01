"""HTML assembly, PDF rendering, and the page watermark."""

import logging
import os
import re

import config

try:
    import markdown  # optional for Markdown -> HTML
except ImportError:
    markdown = None


CSS = """
  @page { margin: 16mm 12mm; }
  body { font-family: Arial, Helvetica, sans-serif; line-height: 1.5; color: #1f2933; font-size: 11pt; }
  h1, h2, h3 { margin: 0.35em 0 0.25em; }
  .hdr { display:flex; justify-content:space-between; align-items:center; margin-bottom:18px;
         background-color:#333; padding:18px; color:white; position:relative; z-index:1; }
  .hdr img { width:100px; height:100px; object-fit:contain; }
  .hdr h1 { font-size: 19pt; margin: 0 0 4px; }
  .hdr h2 { font-size: 13pt; margin: 0 0 6px; font-weight: normal; }
  .content { text-align:left; position:relative; z-index:1; }

  /* Section headings: larger, bold, underlined - per report spec. */
  .content h2 { font-size: 15pt; font-weight: bold; text-decoration: underline;
                margin-top: 22px; padding-top: 6px; page-break-after: avoid; }
  .content h1 { font-size: 16pt; font-weight: bold; text-decoration: underline;
                margin-top: 22px; page-break-after: avoid; }
  .content h3 { font-size: 12pt; font-weight: bold; margin-top: 14px; page-break-after: avoid; }
  .content p { margin: 0.55em 0; }
  .content ul, .content ol { margin: 0.4em 0 0.7em 1.2em; }
  .content li { margin: 0.2em 0; }
  .content table { border-collapse: collapse; width: 100%; margin: 0.6em 0; font-size: 9.5pt; }
  .content th, .content td { border: 1px solid #d9dde3; padding: 5px 7px; text-align: left; }
  .content th { background: #f2f4f7; }

  .viz { margin: 18px 0 6px; }
  .viz-intro { font-size: 9.5pt; color: #6b7280; margin: 0 0 12px; }
  figure.chart { margin: 0 0 20px; padding: 0; page-break-inside: avoid; }
  figure.chart img { width: 100%; max-width: 100%; height: auto; display: block; }
  figure.chart figcaption { font-size: 9pt; color: #6b7280; margin-top: 5px;
                            border-left: 3px solid #d9dde3; padding-left: 8px; }
  figure.chart .chart-name { font-weight: bold; color: #1f2933; }

  .sources { margin-top: 26px; page-break-before: auto; }
  .sources ol { margin: 0.4em 0 0 1.4em; padding: 0; }
  .sources li { font-size: 9pt; margin: 3px 0; word-wrap: break-word; }
  .sources .src-url { color: #55606e; }

  .meta { margin-top: 22px; border-top: 1px solid #d9dde3; padding-top: 8px;
          font-size: 8.5pt; color: #6b7280; }
  .meta p { margin: 2px 0; }
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


def _charts_block(charts: list[dict]) -> str:
    figures = []
    for c in charts:
        figures.append(
            f'<figure class="chart">'
            f'<img src="{c["img"]}" alt="{c["title"]}">'
            f'<figcaption><span class="chart-name">{c["title"]}.</span> {c["caption"]}</figcaption>'
            f'</figure>'
        )
    return (
        '<div class="viz">'
        '<h2>Visual Analytics</h2>'
        '<p class="viz-intro">Generated directly from the CollegeFootballData feeds for this '
        'matchup. The same eight visuals appear on every report, so numbers can be compared '
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
    return (
        '<div class="sources"><h2>Sources</h2><ol>' + "".join(items) + '</ol></div>'
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
) -> str:
    """Assemble the printable HTML.

    Single-team reports pass an empty away_full/away_logo; the header then centres on
    one crest instead of rendering an "X vs (blank)" line and an empty <img>.
    """
    body = inject_charts(markdown_to_html(strip_model_sources(report_markdown)), charts)
    meta_html = "".join(f"<p>{line}</p>" for line in meta_lines)

    subject = f"{home_full} vs {away_full} ({year})" if away_full else f"{home_full} ({year})"
    doc_title = title or (f"{home_full} vs {away_full}" if away_full else home_full)
    away_img = f'<img src="{away_logo}" alt="{away_full} logo">' if away_full and away_logo else ""

    return f"""<html>
<head>
  <meta charset="utf-8" />
  <title>{doc_title}</title>
  <style>{CSS}</style>
</head>
<body>
  <div class="hdr">
    <img src="{home_logo}" alt="{home_full} logo">
    <div style="text-align:center; flex-grow:1;">
        <h1>{banner}</h1>
        <h2>{subject}</h2>
        <p style="margin:0;">Report created on: {report_created}</p>
    </div>
    {away_img}
  </div>
  <div class="content">{body}</div>
  {sources_block(registry)}
  <div class="meta">{meta_html}</div>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------
def write_pdf(html_content: str, filepath: str) -> None:
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

    img = ImageReader(image_path)
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
