"""Table paint is simplified before print so PDFs stay cheap to open."""

from __future__ import annotations

import pytest

from reporting.pdf_renderer import GrafanaPDFRenderer
from reporting.settings import Settings


def _pdf_counts(data: bytes) -> dict[str, int]:
    return {
        "shading": data.count(b"/ShadingType"),
        "smask": data.count(b"/SMask"),
        "images": data.count(b"/Subtype /Image") + data.count(b"/Subtype/Image"),
        "groups": data.count(b"/S /Transparency") + data.count(b"/S/Transparency"),
    }


def _sheen_table(rows: int = 16, cols: int = 6) -> str:
    cells: list[str] = []
    for r in range(rows):
        for c in range(cols):
            extra = "opacity:0.5;" if r == 0 and c == 0 else ""
            ident = " id='faded'" if r == 0 and c == 0 else ""
            cells.append(
                f"<div role='gridcell'{ident} style='width:120px;height:28px;{extra}"
                "box-shadow:0 1px 3px rgba(0,0,0,.35);"
                "background:linear-gradient(120deg, rgb(46,140,70), rgb(115,191,105));'>"
                f"host-{r}-{c}.example</div>"
            )
    return (
        "<!doctype html><body style='margin:0;background:#fff'>"
        "<div id='chart' style='width:80px;height:40px;background:linear-gradient(90deg, red, blue)'>chart</div>"
        "<div class='rdg' role='grid'>"
        + "".join(cells)
        + "<div class='rdg-frozen-column-shadow' style='width:10px;height:80px;"
        "background:linear-gradient(to right, rgba(0,0,0,.2), transparent)'></div>"
        "<div id='hidden-row' style='opacity:0'>HIDDEN-MARKER</div>"
        "</div></body>"
    )


def _gauge_table(n: int = 30) -> str:
    cells: list[str] = []
    for i in range(n):
        pct = 20 + (i * 13) % 70
        cells.append(
            f"<div role='gridcell' style='width:180px;height:28px;"
            f"background:linear-gradient(90deg, #73BF69 {pct}%, rgba(255,255,255,0) {pct}%)'>"
            f"{pct}%</div>"
        )
    return (
        "<!doctype html><body style='margin:0;background:#fff'>"
        "<div class='rdg' role='grid'>" + "".join(cells) + "</div></body>"
    )


def test_simplify_tables_defaults_on(monkeypatch, tmp_path):
    monkeypatch.delenv("PDF_SIMPLIFY_TABLES", raising=False)
    assert Settings(pdf_outdir=tmp_path).pdf_simplify_tables is True


@pytest.mark.asyncio
async def test_colored_cells_become_solid_fills_and_gauges_one_image(tmp_path):
    pytest.importorskip("playwright")
    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    try:
        try:
            browser = await pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
        except Exception as exc:
            pytest.skip(f"Chromium unavailable: {exc}")
        try:
            page = await browser.new_page(viewport={"width": 1000, "height": 3200})
            renderer = GrafanaPDFRenderer(Settings(pdf_outdir=tmp_path, pdf_simplify_tables=True))

            await page.set_content(_sheen_table())
            raw = await page.pdf(width="1000px", height="3200px", print_background=True)
            raw_counts = _pdf_counts(raw)
            assert raw_counts["shading"] > 40 or raw_counts["images"] > 40
            assert raw_counts["smask"] > 40

            await renderer._optimize_tables_for_pdf(page)
            faded = await page.eval_on_selector("#faded", "el => getComputedStyle(el).opacity")
            hidden = await page.eval_on_selector("#hidden-row", "el => getComputedStyle(el).opacity")
            bg = await page.eval_on_selector("#faded", "el => getComputedStyle(el).backgroundColor")
            image = await page.eval_on_selector("#faded", "el => getComputedStyle(el).backgroundImage")
            chart = await page.eval_on_selector("#chart", "el => getComputedStyle(el).backgroundImage")
            text = await page.inner_text("#faded")
            assert faded == "1"
            assert hidden == "0"
            assert bg == "rgb(115, 191, 105)"
            assert image == "none"
            assert "linear-gradient" in chart
            assert "host-0-0.example" in text
            assert await page.locator(".rdg img").count() == 0

            simple = await page.pdf(width="1000px", height="3200px", print_background=True)
            simple_counts = _pdf_counts(simple)
            assert simple_counts["shading"] <= 2
            assert simple_counts["images"] == 0
            assert simple_counts["smask"] <= 2
            assert simple_counts["groups"] <= 2
            assert len(simple) < len(raw) / 2

            await page.set_content(_sheen_table())
            off = GrafanaPDFRenderer(Settings(pdf_outdir=tmp_path, pdf_simplify_tables=False))
            await off._optimize_tables_for_pdf(page)
            kept = await page.eval_on_selector("#faded", "el => getComputedStyle(el).backgroundImage")
            assert "linear-gradient" in kept

            await page.set_content(_gauge_table())
            gauge_raw = await page.pdf(width="400px", height="1200px", print_background=True)
            gauge_raw_counts = _pdf_counts(gauge_raw)
            assert gauge_raw_counts["smask"] > 20

            await renderer._optimize_tables_for_pdf(page)
            assert await page.locator(".rdg img").count() == 1
            assert await page.locator(".rdg [role=gridcell]").count() == 0
            gauge = await page.pdf(width="400px", height="1200px", print_background=True)
            gauge_counts = _pdf_counts(gauge)
            assert gauge_counts["images"] >= 1
            assert gauge_counts["images"] < 10
            assert gauge_counts["smask"] <= 3
            assert gauge_counts["shading"] <= 3
        finally:
            await browser.close()
    finally:
        await pw.stop()
