"""Dashboard layout measurement and panel-aware PDF pagination."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

# Grafana grid constants (see grafana/public/app/core/constants.ts)
GRID_CELL_HEIGHT = 30
GRID_CELL_VMARGIN = 8


@dataclass(frozen=True, slots=True)
class PanelRect:
    top: float
    bottom: float

    @property
    def height(self) -> float:
        return self.bottom - self.top


def _max_grid_bottom(panels: Iterable[dict]) -> int:
    """Return max(gridPos.y + gridPos.h) across panels, including collapsed rows."""
    max_bottom = 0
    for panel in panels:
        if not isinstance(panel, dict):
            continue
        if panel.get("type") == "row" and isinstance(panel.get("panels"), list):
            max_bottom = max(max_bottom, _max_grid_bottom(panel["panels"]))
        gp = panel.get("gridPos") or {}
        try:
            y = int(gp.get("y", 0))
            h = int(gp.get("h", 0))
        except (TypeError, ValueError):
            continue
        max_bottom = max(max_bottom, y + h)
    return max_bottom


def height_from_grid_positions(panels: Iterable[dict]) -> int:
    """Estimate dashboard height in CSS px from Grafana panel gridPos values."""
    max_bottom = _max_grid_bottom(panels)
    if max_bottom <= 0:
        return 0
    return max_bottom * (GRID_CELL_HEIGHT + GRID_CELL_VMARGIN) - GRID_CELL_VMARGIN


def clamp_viewport_height(
    desired_height: int,
    *,
    width: int,
    scale: float,
    max_height: int,
    pixel_budget: int,
    min_height: int = 720,
) -> int:
    """Clamp the expanded viewport height to hard and memory-budget limits.

    ``pixel_budget`` bounds the rasterized surface in device pixels
    (``width*scale * height*scale``); huge viewports at high device scale
    factors were measured at ~1 GB Chromium RSS and can OOM the container.
    A value <= 0 disables the budget.
    """
    height = min(desired_height, max_height)
    if pixel_budget > 0 and width > 0 and scale > 0:
        budget_height = int(pixel_budget / (width * scale * scale))
        if height > budget_height:
            height = max(min_height, budget_height)
    return max(min_height, height)


def compute_page_breaks(
    panels: Sequence[PanelRect],
    page_height: float,
    content_height: float,
) -> list[float]:
    """Return Y offsets (relative to content top) where PDF pages should start.

    Breaks are placed on panel top edges so no panel straddles a page boundary
    when possible. A panel taller than ``page_height`` is allowed to overflow
    (caller should log a warning).
    """
    if page_height <= 0:
        raise ValueError("page_height must be positive")
    if content_height <= 0:
        return [0.0]

    sorted_panels = sorted(panels, key=lambda p: (p.top, p.bottom))
    tops = sorted({p.top for p in sorted_panels if p.top > 0})

    starts: list[float] = [0.0]
    page_start = 0.0

    while page_start + page_height < content_height - 0.5:
        limit = page_start + page_height
        candidates = [y for y in tops if page_start < y <= limit]

        chosen: float | None = None
        for y in reversed(candidates):
            if _cut_is_safe(sorted_panels, y):
                chosen = y
                break

        if chosen is None:
            # No safe cut: advance by a full page (tall panel may be split)
            next_start = limit
        else:
            # Push remaining content so the cut aligns with the page boundary
            next_start = chosen
            # After shifting, the next page starts at page_start + page_height
            # in the final laid-out document; we track content-space y here.
            next_start = chosen

        if next_start <= page_start + 0.5:
            next_start = page_start + page_height

        starts.append(next_start)
        page_start = next_start

        # Safety against infinite loops
        if len(starts) > 500:
            break

    return starts


def compute_shift_plan(
    panels: Sequence[PanelRect],
    page_height: float,
    content_height: float,
) -> tuple[list[tuple[float, float]], float]:
    """Compute (threshold_y, shift_delta) pairs and final content height.

    Panels with ``top >= threshold_y`` should be shifted down by ``shift_delta``
    so that ``threshold_y`` lands exactly on a page boundary.
    """
    breaks = compute_page_breaks(panels, page_height, content_height)
    shifts: list[tuple[float, float]] = []
    cumulative = 0.0

    for i, cut_y in enumerate(breaks[1:], start=1):
        page_start = breaks[i - 1]
        # Desired page end in the *already shifted* coordinate space
        desired_end = page_start + page_height + cumulative
        # cut_y is in original content coordinates; after previous shifts it is at cut_y + cumulative
        current_cut = cut_y + cumulative
        delta = desired_end - current_cut
        if delta > 0.5:
            shifts.append((cut_y, delta))
            cumulative += delta

    final_height = content_height + cumulative
    # Round up to whole pages so the last page is fully sized
    pages = max(1, int((final_height + page_height - 1) // page_height))
    final_height = pages * page_height
    return shifts, final_height


def _cut_is_safe(panels: Sequence[PanelRect], y: float) -> bool:
    """True if no panel straddles the horizontal line at y (exclusive of edges)."""
    for p in panels:
        if p.top + 0.5 < y < p.bottom - 0.5:
            return False
    return True


def panels_taller_than_page(panels: Sequence[PanelRect], page_height: float) -> list[PanelRect]:
    return [p for p in panels if p.height > page_height + 0.5]


# JavaScript executed in the browser to apply the same pagination algorithm.
PAGINATE_JS = """
({ pageHeight, bannerOffset }) => {
  // Use every panel across nested row grids (Grafana nests .react-grid-layout per row).
  const items = Array.from(document.querySelectorAll('.react-grid-item'));
  if (!items.length) {
    return { ok: false, reason: 'no-grid', pageCount: 1, finalHeight: 0, shifts: [] };
  }

  const scrollY = window.scrollY || window.pageYOffset || 0;
  let contentOrigin = Infinity;
  items.forEach(el => {
    const r = el.getBoundingClientRect();
    const top = r.top + scrollY;
    if (top < contentOrigin) contentOrigin = top;
  });
  if (!Number.isFinite(contentOrigin)) contentOrigin = 0;

  const panels = items.map(el => {
    const r = el.getBoundingClientRect();
    const top = (r.top + scrollY) - contentOrigin;
    return { el, top, bottom: top + r.height, height: r.height };
  }).filter(p => Number.isFinite(p.top) && p.height > 0);

  if (!panels.length) {
    return { ok: false, reason: 'no-panels', pageCount: 1, finalHeight: 0, shifts: [] };
  }

  const contentHeight = Math.max(...panels.map(p => p.bottom));

  function cutSafe(y) {
    return panels.every(p => !(p.top + 0.5 < y && y < p.bottom - 0.5));
  }

  const tops = [...new Set(panels.map(p => p.top).filter(t => t > 0))].sort((a, b) => a - b);
  const breaks = [0];
  let pageStart = 0;
  while (pageStart + pageHeight < contentHeight - 0.5 && breaks.length < 500) {
    const limit = pageStart + pageHeight;
    const candidates = tops.filter(y => y > pageStart && y <= limit);
    let chosen = null;
    for (let i = candidates.length - 1; i >= 0; i--) {
      if (cutSafe(candidates[i])) { chosen = candidates[i]; break; }
    }
    let next = chosen === null ? limit : chosen;
    if (next <= pageStart + 0.5) next = pageStart + pageHeight;
    breaks.push(next);
    pageStart = next;
  }

  let cumulative = 0;
  const shiftPlan = [];
  for (let i = 1; i < breaks.length; i++) {
    const cutY = breaks[i];
    const prev = breaks[i - 1];
    const desiredEnd = prev + pageHeight + cumulative;
    const currentCut = cutY + cumulative;
    const delta = desiredEnd - currentCut;
    if (delta > 0.5) {
      shiftPlan.push({ threshold: cutY, delta });
      cumulative += delta;
    }
  }

  // Apply shifts from bottom to top so earlier thresholds stay valid
  for (let i = shiftPlan.length - 1; i >= 0; i--) {
    const { threshold, delta } = shiftPlan[i];
    panels.forEach(p => {
      if (p.top >= threshold - 0.5) {
        const style = p.el.style;
        const transform = style.transform || window.getComputedStyle(p.el).transform;
        // Prefer adjusting translateY inside existing transform
        if (transform && transform !== 'none' && transform.includes('translate')) {
          const m = transform.match(/translate\\(([^,]+),\\s*([^)]+)\\)/);
          if (m) {
            const x = m[1].trim();
            const y = parseFloat(m[2]) + delta;
            style.transform = transform.replace(m[0], `translate(${x}, ${y}px)`);
            return;
          }
          const m3 = transform.match(/translate3d\\(([^,]+),\\s*([^,]+),\\s*([^)]+)\\)/);
          if (m3) {
            const x = m3[1].trim();
            const z = m3[3].trim();
            const y = parseFloat(m3[2]) + delta;
            style.transform = transform.replace(m3[0], `translate3d(${x}, ${y}px, ${z})`);
            return;
          }
        }
        const top = parseFloat(style.top || '0') || 0;
        style.top = (top + delta) + 'px';
      }
    });
  }

  const pages = Math.max(1, Math.ceil((contentHeight + cumulative) / pageHeight));
  const finalGridHeight = pages * pageHeight;

  // Expand every nested layout so shifted panels remain visible.
  document.querySelectorAll('.react-grid-layout').forEach(grid => {
    grid.style.height = finalGridHeight + 'px';
    grid.style.minHeight = finalGridHeight + 'px';
  });

  // Expand body so Chromium paginates cleanly
  const totalDocHeight = finalGridHeight + (bannerOffset || 0) + 8;
  document.documentElement.style.height = totalDocHeight + 'px';
  document.documentElement.style.minHeight = totalDocHeight + 'px';
  document.documentElement.style.overflow = 'visible';
  if (document.body) {
    document.body.style.height = totalDocHeight + 'px';
    document.body.style.overflow = 'visible';
    document.body.style.minHeight = totalDocHeight + 'px';
  }

  // Kill internal scroll containers so print uses document flow
  document.querySelectorAll(
    '.scrollbar-view, .scroll-canvas, [data-testid="scrollbar-view"], [data-testid="scroll-container"]'
  ).forEach(el => {
    el.style.overflow = 'visible';
    el.style.height = 'auto';
    el.style.maxHeight = 'none';
  });

  const tall = panels.filter(p => p.height > pageHeight + 0.5).length;
  return {
    ok: true,
    pageCount: pages,
    finalHeight: totalDocHeight,
    pageHeight,
    shifts: shiftPlan,
    tallPanels: tall,
  };
}
"""

MEASURE_HEIGHT_JS = """
() => {
  const banner = document.getElementById('report-banner');
  const bannerH = banner ? banner.getBoundingClientRect().height : 0;
  const scrollY = window.scrollY || window.pageYOffset || 0;

  // Grafana nests a .react-grid-layout per row. The first match often has
  // style.height="100%" (parseFloat → 100) and only covers one section, so
  // measure every panel in document coordinates and take the max bottom.
  let maxDocBottom = 0;
  let minDocTop = Infinity;
  document.querySelectorAll('.react-grid-item').forEach(el => {
    const r = el.getBoundingClientRect();
    if (r.width <= 0 && r.height <= 0) return;
    const top = r.top + scrollY;
    const bottom = top + r.height;
    if (top < minDocTop) minDocTop = top;
    if (bottom > maxDocBottom) maxDocBottom = bottom;
  });

  document.querySelectorAll('.react-grid-layout').forEach(grid => {
    const r = grid.getBoundingClientRect();
    const top = r.top + scrollY;
    let gridBottom = top + Math.max(r.height, grid.scrollHeight || 0);
    const styleH = grid.style && grid.style.height;
    // Only trust explicit pixel heights — "100%" must not become 100px.
    if (styleH && /px$/i.test(styleH.trim())) {
      const px = parseFloat(styleH);
      if (Number.isFinite(px)) gridBottom = Math.max(gridBottom, top + px);
    }
    if (gridBottom > maxDocBottom) maxDocBottom = gridBottom;
  });

  const doc = document.documentElement;
  const body = document.body || {};
  const scrollH = Math.max(
    doc.scrollHeight || 0,
    doc.offsetHeight || 0,
    body.scrollHeight || 0,
    body.offsetHeight || 0
  );

  if (maxDocBottom > 0) {
    const offsetTop = Number.isFinite(minDocTop) ? minDocTop : 0;
    const contentHeight = Math.max(0, maxDocBottom - offsetTop);
    const totalHeight = Math.max(maxDocBottom, scrollH) + 8;
    return {
      source: 'grid',
      contentHeight: Math.ceil(contentHeight),
      bannerHeight: Math.ceil(bannerH),
      totalHeight: Math.ceil(totalHeight),
      offsetTop: Math.ceil(offsetTop),
    };
  }

  return {
    source: 'document',
    contentHeight: Math.ceil(scrollH),
    bannerHeight: Math.ceil(bannerH),
    totalHeight: Math.ceil(scrollH),
    offsetTop: 0,
  };
}
"""

# Expand document + kill Grafana scroll containers so Chromium prints full height.
PREPARE_PRINT_JS = """
(totalHeight) => {
  const h = Math.ceil(Number(totalHeight) || 0);
  if (h > 0) {
    document.documentElement.style.height = h + 'px';
    document.documentElement.style.minHeight = h + 'px';
    document.documentElement.style.overflow = 'visible';
    if (document.body) {
      document.body.style.height = h + 'px';
      document.body.style.minHeight = h + 'px';
      document.body.style.overflow = 'visible';
    }
  }
  document.querySelectorAll(
    '.scrollbar-view, .scroll-canvas, [data-testid="scrollbar-view"], [data-testid="scroll-container"]'
  ).forEach(el => {
    el.style.overflow = 'visible';
    el.style.height = 'auto';
    el.style.maxHeight = 'none';
  });
  return h;
}
"""

# Grafana's default colored-background cell is a CSS linear-gradient, and
# react-data-grid adds box-shadows plus translucent gauge and frozen-column
# gradients. Chromium prints each of those as its own PDF shading or
# soft-masked image, so Acrobat paints cells one by one and can hang.
# Full-cell sheens become a single opaque fill (text stays vector). Tables
# that still contain many bar gradients are left for the renderer to snapshot.
OPTIMIZE_TABLES_FOR_PDF_JS = r"""
() => {
  const RASTER_MIN = 24;
  const rootSel = '.rdg, table, [role="grid"], [role="table"]';
  const all = Array.from(document.querySelectorAll(rootSel));
  const roots = all.filter(el => !all.some(other => other !== el && other.contains(el)));
  if (!roots.length) {
    return { flattened: 0, opacityFixed: 0, rasterize: [] };
  }

  if (!document.getElementById('pdf-table-optimize')) {
    const style = document.createElement('style');
    style.id = 'pdf-table-optimize';
    style.textContent = `
      .rdg-frozen-column-shadow, [class*="frozen-column-shadow"] {
        display: none !important;
        background: none !important;
      }
      .rdg, .rdg *, table, table *, [role="grid"], [role="grid"] *, [role="table"], [role="table"] * {
        box-shadow: none !important;
        filter: none !important;
        -webkit-filter: none !important;
        backdrop-filter: none !important;
        -webkit-backdrop-filter: none !important;
        text-shadow: none !important;
        mix-blend-mode: normal !important;
      }
    `;
    (document.head || document.documentElement).appendChild(style);
  }

  const canvas = document.createElement('canvas');
  canvas.width = 1;
  canvas.height = 1;
  const ctx = canvas.getContext('2d', { willReadFrequently: true });
  if (!ctx) {
    return { flattened: 0, opacityFixed: 0, rasterize: [], error: 'no-canvas' };
  }

  const COLOR_RE = /rgba?\([^)]+\)|hsla?\([^)]+\)|#[0-9a-fA-F]{3,8}|\btransparent\b/g;

  function alphaOf(color) {
    if (!color || color === 'transparent') return 0;
    ctx.clearRect(0, 0, 1, 1);
    ctx.globalCompositeOperation = 'copy';
    ctx.fillStyle = '#000';
    ctx.fillStyle = color;
    ctx.fillRect(0, 0, 1, 1);
    ctx.globalCompositeOperation = 'source-over';
    return ctx.getImageData(0, 0, 1, 1).data[3];
  }

  function bake(color, under) {
    ctx.clearRect(0, 0, 1, 1);
    ctx.globalCompositeOperation = 'source-over';
    ctx.fillStyle = under || '#ffffff';
    ctx.fillRect(0, 0, 1, 1);
    ctx.fillStyle = color;
    ctx.fillRect(0, 0, 1, 1);
    const d = ctx.getImageData(0, 0, 1, 1).data;
    return 'rgb(' + d[0] + ', ' + d[1] + ', ' + d[2] + ')';
  }

  function opaqueUnder(el) {
    let node = el.parentElement;
    while (node) {
      const bg = getComputedStyle(node).backgroundColor;
      if (bg && bg !== 'transparent' && bg !== 'rgba(0, 0, 0, 0)') {
        const a = alphaOf(bg);
        if (a >= 250) return bg;
        if (a > 0) return bake(bg, '#ffffff');
      }
      node = node.parentElement;
    }
    return '#ffffff';
  }

  // Full-cell sheen: linear-gradient(120deg, darker, color) with no stops.
  // Bar gauges use percentage stops (color 40%, transparent 40%) and must
  // not be flattened to a single color.
  function classify(bgImage) {
    if (!bgImage || bgImage === 'none' || !bgImage.includes('gradient')) return null;
    const grads = bgImage.match(/[a-z-]*gradient\(/g) || [];
    if (grads.length !== 1 || bgImage.includes('repeating-')) return 'complex';
    if (!bgImage.startsWith('linear-gradient(')) return 'complex';
    if (bgImage.includes('%')) return 'complex';
    const colors = bgImage.match(COLOR_RE) || [];
    if (colors.length < 2) return 'complex';
    return { colors: colors };
  }

  let flattened = 0;
  let opacityFixed = 0;
  const rasterize = [];
  let rasterSeq = 0;

  roots.forEach(root => {
    const nodes = [root, ...root.querySelectorAll('*')];
    const sheens = [];
    let complex = 0;
    nodes.forEach(el => {
      if (!(el instanceof HTMLElement)) return;
      const cs = getComputedStyle(el);
      const op = parseFloat(cs.opacity);
      if (op > 0.02 && op < 0.999) {
        el.style.setProperty('opacity', '1', 'important');
        opacityFixed++;
      }
      const kind = classify(cs.backgroundImage);
      if (kind === 'complex') {
        complex++;
        return;
      }
      if (kind && kind.colors) {
        sheens.push({ el: el, colors: kind.colors });
        return;
      }
      const bg = cs.backgroundColor;
      if (bg && bg.startsWith('rgba')) {
        const a = alphaOf(bg);
        if (a > 8 && a < 250) {
          sheens.push({ el: el, solid: bake(bg, opaqueUnder(el)) });
        }
      }
    });

    if (complex >= RASTER_MIN) {
      const rect = root.getBoundingClientRect();
      const id = String(rasterSeq++);
      root.setAttribute('data-pdf-table-id', id);
      rasterize.push({ id: id, complex: complex, width: rect.width, height: rect.height });
      return;
    }

    sheens.forEach(item => {
      const el = item.el;
      if (item.solid) {
        el.style.setProperty('background-image', 'none', 'important');
        el.style.setProperty('background-color', item.solid, 'important');
        flattened++;
        return;
      }
      const last = item.colors[item.colors.length - 1];
      const lastAlpha = alphaOf(last);
      if (lastAlpha > 20) {
        el.style.setProperty('background', bake(last, opaqueUnder(el)), 'important');
      } else {
        el.style.setProperty('background-image', 'none', 'important');
        const label = (el.className && el.className.toString()) || '';
        const text = (el.textContent || '').trim();
        if (!text && (el.clientWidth <= 24 || el.clientHeight <= 24 || /shadow/i.test(label))) {
          el.style.setProperty('display', 'none', 'important');
        }
      }
      flattened++;
    });
  });

  return { flattened: flattened, opacityFixed: opacityFixed, rasterize: rasterize };
}
"""
