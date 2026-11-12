from __future__ import annotations

from reporting.layout import (
    MEASURE_HEIGHT_JS,
    PAGINATE_JS,
    PREPARE_PRINT_JS,
    PanelRect,
    compute_page_breaks,
    compute_shift_plan,
    height_from_grid_positions,
    panels_taller_than_page,
)


def test_height_from_grid_positions():
    panels = [
        {"type": "timeseries", "gridPos": {"x": 0, "y": 0, "w": 12, "h": 8}},
        {"type": "timeseries", "gridPos": {"x": 0, "y": 8, "w": 12, "h": 8}},
        {
            "type": "row",
            "panels": [
                {"type": "stat", "gridPos": {"x": 0, "y": 16, "w": 6, "h": 4}},
            ],
        },
    ]
    # max y+h = 20; 20*(30+8)-8 = 752
    assert height_from_grid_positions(panels) == 20 * 38 - 8


def test_height_from_empty_panels():
    assert height_from_grid_positions([]) == 0
    assert height_from_grid_positions([{"type": "text"}]) == 0


def test_page_breaks_align_to_panel_tops():
    panels = [
        PanelRect(0, 400),
        PanelRect(400, 800),
        PanelRect(800, 1200),
        PanelRect(1200, 1600),
    ]
    breaks = compute_page_breaks(panels, page_height=1000, content_height=1600)
    assert breaks[0] == 0.0
    # First cut should be at 800 (safe top within first page)
    assert 800.0 in breaks
    assert breaks[-1] < 1600


def test_cut_avoids_straddling_panel():
    # Page height 500; panel from 300-700 straddles natural cut at 500
    panels = [
        PanelRect(0, 300),
        PanelRect(300, 700),
        PanelRect(700, 1000),
    ]
    breaks = compute_page_breaks(panels, page_height=500, content_height=1000)
    # Safe cut inside first page is at 300 (top of second panel)
    assert 300.0 in breaks


def test_shift_plan_increases_height():
    panels = [
        PanelRect(0, 400),
        PanelRect(400, 900),
        PanelRect(900, 1300),
    ]
    shifts, final_h = compute_shift_plan(panels, page_height=1000, content_height=1300)
    assert final_h % 1000 == 0
    assert final_h >= 1300
    # Shifts push content so cuts land on page boundaries
    for threshold, delta in shifts:
        assert delta > 0
        assert threshold >= 0


def test_single_page_no_shifts():
    panels = [PanelRect(0, 400), PanelRect(400, 800)]
    shifts, final_h = compute_shift_plan(panels, page_height=1000, content_height=800)
    assert shifts == []
    assert final_h == 1000  # rounded up to one full page


def test_tall_panel_detection():
    panels = [PanelRect(0, 500), PanelRect(500, 2000)]
    tall = panels_taller_than_page(panels, 1000)
    assert len(tall) == 1
    assert tall[0].top == 500


def test_compute_page_breaks_rejects_nonpositive_height():
    import pytest

    with pytest.raises(ValueError):
        compute_page_breaks([], page_height=0, content_height=100)


def test_measure_height_js_ignores_percent_and_uses_all_items():
    # Nested Grafana rows use style.height="100%" on wrapper grids; parseFloat("100%")===100
    # would under-measure. The script must require an explicit px unit and scan all items.
    assert "/px$/i.test" in MEASURE_HEIGHT_JS or "/px$/i.test(styleH.trim())" in MEASURE_HEIGHT_JS
    assert "react-grid-item" in MEASURE_HEIGHT_JS
    assert 'source: \'grid\'' in MEASURE_HEIGHT_JS or 'source: "grid"' in MEASURE_HEIGHT_JS


def test_paginate_js_uses_all_grid_items():
    assert "document.querySelectorAll('.react-grid-item')" in PAGINATE_JS
    # Must not only measure the first nested row grid.
    assert "const grid = document.querySelector('.react-grid-layout')" not in PAGINATE_JS


def test_prepare_print_js_expands_document():
    assert "document.documentElement.style.height" in PREPARE_PRINT_JS
    assert "scrollbar-view" in PREPARE_PRINT_JS
