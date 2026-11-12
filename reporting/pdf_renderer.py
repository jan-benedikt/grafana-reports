from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.async_api import Browser, BrowserContext, Playwright, TimeoutError as PWTimeoutError
from playwright.async_api import async_playwright

from .layout import MEASURE_HEIGHT_JS, PAGINATE_JS, PREPARE_PRINT_JS, height_from_grid_positions
from .models import RenderConfig
from .settings import Settings

logger = logging.getLogger(__name__)


class GrafanaPDFRenderer:
    """Renders Grafana dashboards to PDF using a shared Chromium instance."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._browser_lock = asyncio.Lock()
        self._storage_state: dict | None = None

    async def start(self) -> None:
        await self._ensure_browser()

    async def stop(self) -> None:
        async with self._browser_lock:
            if self._browser:
                try:
                    await self._browser.close()
                except Exception:
                    logger.debug("Error closing browser", exc_info=True)
                self._browser = None
            if self._playwright:
                try:
                    await self._playwright.stop()
                except Exception:
                    logger.debug("Error stopping playwright", exc_info=True)
                self._playwright = None

    async def _ensure_browser(self) -> Browser:
        async with self._browser_lock:
            if self._browser and self._browser.is_connected():
                return self._browser
            if self._playwright is None:
                self._playwright = await async_playwright().start()
            chromium_args = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
            logger.info("Launching Chromium (args=%s)", chromium_args)
            self._browser = await self._playwright.chromium.launch(args=chromium_args, headless=True)
            return self._browser

    async def render_url(
        self,
        url: str,
        output_pdf: Path,
        header_title: str,
        header_period: str,
        header_generated: str,
        render: RenderConfig | None = None,
    ) -> None:
        render = render or RenderConfig()
        width = render.width or self.settings.viewport_width
        scale = render.scale_factor if render.scale_factor is not None else self.settings.pdf_device_scale_factor
        size_mode = (render.page_mode or self.settings.pdf_size_mode or "paged").lower()
        page_height_px = render.page_height_px or self.settings.effective_page_height_px(width)

        browser = await self._ensure_browser()
        ctx: BrowserContext | None = None
        try:
            ctx = await browser.new_context(
                viewport={"width": width, "height": self.settings.viewport_height},
                device_scale_factor=scale,
                locale=self.settings.render_locale,
                timezone_id=self.settings.render_timezone,
                storage_state=self._storage_state,
            )

            if self.settings.pdf_fast_mode or self.settings.grafana_token:
                await ctx.route("**/*", self._make_route_handler())

            if self.settings.force_kiosk_css:
                await ctx.add_init_script(
                    """
                    (() => {
                      document.addEventListener('DOMContentLoaded', () => {
                        document.documentElement.classList.add('kiosk');
                        if (document.body) document.body.classList.add('kiosk');
                      });
                    })();
                    """
                )

            page = await ctx.new_page()

            if self.settings.debug_render:
                page.on("requestfailed", lambda req: self._log_request_failure(req))
                page.on("pageerror", lambda msg: logger.warning("PAGE ERROR: %s", msg))

            await self._ensure_session(ctx)

            logger.info("Navigating to dashboard URL")
            await page.goto(url, wait_until="domcontentloaded", timeout=120_000)
            network_timeout = self.settings.network_idle_timeout_ms if self.settings.pdf_fast_mode else 60_000
            if network_timeout > 0:
                try:
                    await page.wait_for_load_state("networkidle", timeout=network_timeout)
                except Exception:
                    logger.debug("Network idle not reached within %sms, continuing", network_timeout)

            if self.settings.wait_before_ready_ms > 0:
                await page.wait_for_timeout(self.settings.wait_before_ready_ms)

            if await page.locator('input[name="user"], form[action="/login"]').count() > 0:
                await self._handle_login(page, url)

            dashboard_json = await self._fetch_dashboard_json(page, url=url)
            await self._wait_for_queries_complete(page, url, dashboard_json)

            await self._wait_for_dashboard_grid(page)

            await self._inject_styles(page)
            await self._inject_banner(page, header_title, header_period, header_generated)

            media = self.settings.pdf_media if self.settings.pdf_media in {"screen", "print"} else "screen"
            await page.emulate_media(media=media)

            # Measure real dashboard height and expand viewport so all panels paint
            measured = await self._measure_height(page, dashboard_json)
            target_height = min(
                max(measured["totalHeight"], self.settings.viewport_height),
                self.settings.pdf_max_viewport_height,
            )
            logger.info(
                "Dashboard height measured via %s: content=%s total=%s -> viewport height=%s",
                measured.get("source"),
                measured.get("contentHeight"),
                measured.get("totalHeight"),
                target_height,
            )
            await page.set_viewport_size({"width": width, "height": target_height})
            await page.wait_for_timeout(500)

            ready = await self._wait_for_panels_ready(page)
            if not ready:
                logger.warning("Panels not fully ready by timeout, proceeding anyway (best effort)")

            extra_wait = 1000 if self.settings.pdf_fast_mode else self.settings.wait_extra_ms
            if extra_wait > 0:
                await page.wait_for_timeout(extra_wait)

            # Re-measure after resize/ready
            measured = await self._measure_height(page, dashboard_json)
            logger.info(
                "Dashboard height re-measured via %s: content=%s total=%s",
                measured.get("source"),
                measured.get("contentHeight"),
                measured.get("totalHeight"),
            )

            if self.settings.debug_screenshot and not page.is_closed():
                await page.screenshot(path=str(output_pdf.with_suffix(".png")), full_page=False)

            await page.evaluate(
                """(title) => { document.title = title || document.title; }""",
                header_title,
            )

            logger.info("Generating PDF with mode=%s", size_mode)
            if size_mode == "a4":
                await page.pdf(
                    path=str(output_pdf),
                    format=self.settings.pdf_paper_format,
                    landscape=self.settings.pdf_landscape,
                    print_background=True,
                    margin={"top": "0mm", "bottom": "0mm", "left": "0mm", "right": "0mm"},
                    display_header_footer=False,
                    prefer_css_page_size=False,
                )
            elif size_mode == "css":
                await page.pdf(
                    path=str(output_pdf),
                    landscape=self.settings.pdf_landscape,
                    print_background=True,
                    margin={"top": "0mm", "bottom": "0mm", "left": "0mm", "right": "0mm"},
                    display_header_footer=False,
                    prefer_css_page_size=True,
                )
            elif size_mode == "paged":
                await self._render_paged_pdf(page, output_pdf, width, page_height_px, measured)
            else:
                # auto: single page sized to content, fall back to paged if too tall
                width_in, height_in = await self._compute_pdf_dimensions(page, measured, width=width)
                if height_in > self.settings.pdf_max_inch or width_in > self.settings.pdf_max_inch:
                    logger.warning(
                        "auto mode exceeds PDF_MAX_INCH (%.1f x %.1f); falling back to paged",
                        width_in,
                        height_in,
                    )
                    await self._render_paged_pdf(page, output_pdf, width, page_height_px, measured)
                else:
                    await self._safe_eval(
                        page,
                        PREPARE_PRINT_JS,
                        arg=int(measured.get("totalHeight") or 0),
                        default=0,
                    )
                    await page.pdf(
                        path=str(output_pdf),
                        width=f"{width_in}in",
                        height=f"{height_in}in",
                        landscape=False,
                        print_background=True,
                        margin={"top": "0mm", "bottom": "0mm", "left": "0mm", "right": "0mm"},
                        display_header_footer=False,
                        prefer_css_page_size=False,
                        page_ranges="1",
                    )

            # Cache auth cookies for subsequent renders
            try:
                self._storage_state = await ctx.storage_state()
            except Exception:
                logger.debug("Could not persist storage_state", exc_info=True)

            logger.info("PDF generated at %s", output_pdf)
        finally:
            if ctx is not None:
                try:
                    await ctx.close()
                except Exception:
                    logger.debug("Error closing browser context", exc_info=True)

    async def _render_paged_pdf(
        self,
        page,
        output_pdf: Path,
        width: int,
        page_height_px: int,
        measured: dict,
    ) -> None:
        banner_h = int(measured.get("bannerHeight") or 0)
        result = await self._safe_eval(
            page,
            PAGINATE_JS,
            arg={"pageHeight": page_height_px, "bannerOffset": banner_h},
            default={"ok": False, "reason": "eval-failed"},
        )
        if not result.get("ok"):
            logger.warning("Pagination JS failed (%s); using single-page auto sizing", result.get("reason"))
            width_in, height_in = await self._compute_pdf_dimensions(page, measured, width=width)
            await self._safe_eval(page, PREPARE_PRINT_JS, arg=int(measured.get("totalHeight") or 0), default=0)
            await page.pdf(
                path=str(output_pdf),
                width=f"{width_in}in",
                height=f"{min(height_in, self.settings.pdf_max_inch)}in",
                landscape=False,
                print_background=True,
                margin={"top": "0mm", "bottom": "0mm", "left": "0mm", "right": "0mm"},
                display_header_footer=False,
                prefer_css_page_size=False,
            )
            return

        if result.get("tallPanels"):
            logger.warning(
                "%s panel(s) taller than page height (%spx); they may be split across pages",
                result["tallPanels"],
                page_height_px,
            )

        final_h = int(result.get("finalHeight") or page_height_px)
        logger.info(
            "Paged layout: %s page(s), page=%sx%spx, final_height=%s",
            result.get("pageCount"),
            width,
            page_height_px,
            final_h,
        )
        await page.pdf(
            path=str(output_pdf),
            width=f"{width}px",
            height=f"{page_height_px}px",
            landscape=False,
            print_background=True,
            margin={"top": "0mm", "bottom": "0mm", "left": "0mm", "right": "0mm"},
            display_header_footer=False,
            prefer_css_page_size=False,
        )

    def _make_route_handler(self):
        grafana_host = urlparse(self.settings.grafana_url).hostname
        token = self.settings.grafana_token
        fast = self.settings.pdf_fast_mode

        async def _handler(route, request):
            try:
                if fast:
                    rtype = request.resource_type
                    url = request.url
                    if rtype in ("font", "media", "websocket", "eventsource"):
                        return await route.abort()
                    if url.endswith((".woff", ".woff2", ".ttf")):
                        return await route.abort()
                if token:
                    host = urlparse(request.url).hostname
                    if host == grafana_host:
                        headers = {**request.headers, "Authorization": f"Bearer {token}"}
                        return await route.continue_(headers=headers)
                return await route.continue_()
            except Exception:
                try:
                    await route.abort()
                except Exception:
                    pass

        return _handler

    def _log_request_failure(self, req):
        try:
            if getattr(req, "resource_type", None) == "font":
                return
            failure = getattr(req, "failure", None)
            reason = failure if isinstance(failure, str) else (getattr(failure, "error_text", None) or "request failed")
            logger.debug("REQ FAILED %s -> %s", reason, req.url)
        except Exception as exc:
            logger.debug("REQ FAILED handler error: %s", exc)

    async def _ensure_session(self, ctx: BrowserContext) -> None:
        if self.settings.grafana_token:
            # Token auth is injected via route; still warm the session.
            try:
                check = await ctx.request.get(f"{self.settings.grafana_url}/api/user", timeout=30_000)
                if check.status == 200:
                    return
            except Exception:
                logger.debug("Token session check failed", exc_info=True)
            return

        if not self.settings.grafana_user or not self.settings.grafana_password:
            return

        try:
            check = await ctx.request.get(f"{self.settings.grafana_url}/api/user", timeout=30_000)
            if check.status == 200:
                return
        except Exception:
            pass

        try:
            resp = await ctx.request.post(
                f"{self.settings.grafana_url}/login",
                headers={"Content-Type": "application/json"},
                data={
                    "user": self.settings.grafana_user,
                    "password": self.settings.grafana_password,
                },
                timeout=60_000,
            )
            if resp.status == 200:
                ok = await ctx.request.get(f"{self.settings.grafana_url}/api/user", timeout=30_000)
                if ok.status == 200:
                    return
            else:
                txt = (await resp.text())[:200]
                logger.warning("JSON login returned HTTP %s: %s", resp.status, txt)
        except Exception as exc:
            logger.warning("JSON login failed: %s", exc)

        try:
            page = await ctx.new_page()
            await page.goto(f"{self.settings.grafana_url}/login", wait_until="domcontentloaded", timeout=120_000)
            if await page.locator('input[name="user"]').count() > 0:
                await page.fill('input[name="user"]', self.settings.grafana_user)
                await page.fill('input[name="password"]', self.settings.grafana_password)
                await page.click('button[type="submit"]')
                await page.wait_for_url(lambda url: "/login" not in url, timeout=120_000)
            await page.close()
        except PWTimeoutError:
            logger.warning("Interactive login timeout (maybe SSO). Continuing…")
        except Exception as exc:
            logger.warning("Interactive login failed: %s. Continuing…", exc)

    async def _handle_login(self, page, url: str) -> None:
        if self.settings.grafana_token:
            return
        if not (self.settings.grafana_user and self.settings.grafana_password):
            return
        await page.fill('input[name="user"]', self.settings.grafana_user)
        await page.fill('input[name="password"]', self.settings.grafana_password)
        await page.click('button[type="submit"]')
        await page.wait_for_url(lambda u: "/login" not in u, timeout=120_000)
        await page.goto(url, wait_until="domcontentloaded", timeout=120_000)
        if not self.settings.pdf_fast_mode:
            try:
                await page.wait_for_load_state("networkidle", timeout=60_000)
            except Exception:
                pass

    async def _safe_eval(self, page, script: str, default=None, arg=None):
        try:
            if page.is_closed():
                return default
            if arg is None:
                return await page.evaluate(script)
            return await page.evaluate(script, arg)
        except Exception:
            return default

    async def _wait_for_dashboard_grid(self, page, timeout_ms: int = 60_000) -> None:
        """Wait until Grafana has mounted at least one panel grid item."""
        try:
            await page.wait_for_selector(".react-grid-item", timeout=timeout_ms, state="attached")
        except Exception:
            logger.warning("Dashboard grid items not found within %sms", timeout_ms)

    @staticmethod
    def _dashboard_uid_from_url(url: str) -> str | None:
        try:
            path = urlparse(url).path or ""
        except Exception:
            return None
        # /d/<uid>/... or /d/<uid>
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 2 and parts[0] == "d":
            return parts[1]
        return None

    async def _fetch_dashboard_json(self, page, url: str | None = None) -> dict[str, Any] | None:
        script = """
        (async () => {
          const entry = performance.getEntriesByType("resource").find(r => {
            if (!['fetch','xmlhttprequest'].includes(r.initiatorType)) return false;
            const u = r.name;
            return /\\/api\\/dashboards\\/uid\\/[^/?#]+/.test(u) ||
              /\\/apis\\/dashboard\\.grafana\\.app\\/v1beta1\\/namespaces\\/[^/]+\\/dashboards\\/[^/]+\\/dto(?:\\?|$)/.test(u);
          });
          if (!entry) return null;
          try {
            const res = await fetch(entry.name, { credentials: 'include' });
            if (!res.ok) return null;
            return await res.json();
          } catch (e) {
            return null;
          }
        })()
        """
        result = await self._safe_eval(page, script, default=None)
        if isinstance(result, dict):
            return result

        # Fallback: fetch by UID via Grafana HTTP API (more reliable than performance entries).
        uid = self._dashboard_uid_from_url(url or getattr(page, "url", "") or "")
        if not uid:
            return None
        api_url = f"{self.settings.grafana_url.rstrip('/')}/api/dashboards/uid/{uid}"
        try:
            ctx = page.context
            resp = await ctx.request.get(api_url, timeout=30_000)
            if resp.status == 200:
                data = await resp.json()
                return data if isinstance(data, dict) else None
            logger.debug("Dashboard API fallback HTTP %s for %s", resp.status, uid)
        except Exception:
            logger.debug("Dashboard API fallback failed for %s", uid, exc_info=True)
        return None

    def _panels_from_dashboard_json(self, dashboard_json: dict | None) -> list:
        if not dashboard_json:
            return []
        root = (
            dashboard_json.get("spec")
            or dashboard_json.get("model")
            or dashboard_json.get("dashboard")
            or dashboard_json
        )
        panels = root.get("panels") if isinstance(root, dict) else None
        return panels if isinstance(panels, list) else []

    async def _measure_height(self, page, dashboard_json: dict | None) -> dict:
        measured = await self._safe_eval(page, MEASURE_HEIGHT_JS, default=None)
        panels = self._panels_from_dashboard_json(dashboard_json)
        estimated = height_from_grid_positions(panels)

        if isinstance(measured, dict) and measured.get("contentHeight"):
            # Prefer the larger of DOM measure vs gridPos estimate (DOM can still
            # under-count briefly while nested row layouts finish mounting).
            if estimated > 0 and int(measured.get("contentHeight") or 0) < int(estimated * 0.7):
                banner = int(measured.get("bannerHeight") or 60)
                offset = int(measured.get("offsetTop") or banner)
                logger.info(
                    "DOM height %s looks short vs gridPos estimate %s; using gridPos",
                    measured.get("contentHeight"),
                    estimated,
                )
                return {
                    "source": "gridPos",
                    "contentHeight": estimated,
                    "bannerHeight": banner,
                    "totalHeight": estimated + offset + 8,
                    "offsetTop": offset,
                }
            return measured

        if estimated > 0:
            banner = 60
            return {
                "source": "gridPos",
                "contentHeight": estimated,
                "bannerHeight": banner,
                "totalHeight": estimated + banner + 8,
                "offsetTop": banner,
            }

        return {
            "source": "viewport",
            "contentHeight": self.settings.viewport_height,
            "bannerHeight": 0,
            "totalHeight": self.settings.viewport_height,
            "offsetTop": 0,
        }

    async def _get_expected_query_count(self, page, dashboard_json: dict | None = None) -> int | None:
        if dashboard_json is None:
            dashboard_json = await self._fetch_dashboard_json(page)
        if not dashboard_json:
            return None
        root = (
            dashboard_json.get("spec")
            or dashboard_json.get("model")
            or dashboard_json.get("dashboard")
            or dashboard_json
        )
        if not isinstance(root, dict):
            return None
        panels = root.get("panels") if isinstance(root.get("panels"), list) else []
        templating = (root.get("templating") or {}).get("list") or []
        excluded = {"text"}

        def count_panels(lst: list) -> int:
            n = 0
            for p in lst:
                if not isinstance(p, dict) or p.get("type") in excluded:
                    continue
                if p.get("type") == "row" and isinstance(p.get("panels"), list):
                    n += count_panels(p["panels"])
                    continue
                has_ds = bool(p.get("datasource")) or any(
                    isinstance(t, dict) and t.get("datasource") for t in (p.get("targets") or [])
                )
                if has_ds:
                    n += 1
            return n

        valid = count_panels(panels)
        templating_queries = sum(1 for v in templating if isinstance(v, dict) and v.get("type") == "query")
        return valid + templating_queries

    async def _get_completed_query_count(self, page) -> int:
        script = """
        () => performance.getEntriesByType("resource")
          .filter(r => r.initiatorType === 'fetch' && (
            r.name.includes('/api/ds/query') || r.name.includes('/api/ds/query?')
          )).length
        """
        val = await self._safe_eval(page, script, default=0)
        return int(val or 0)

    async def _wait_for_queries_complete(self, page, url: str, dashboard_json: dict | None = None) -> None:
        if not self.settings.check_queries_to_complete:
            return
        if "viewPanel=" in url:
            logger.debug("Skipping query wait (single-panel view)")
            return
        logger.debug("Waiting for all queries to complete")
        await self._scroll_through(page)
        await page.wait_for_timeout(500)
        expected = await self._get_expected_query_count(page, dashboard_json)
        if expected is None or expected == 0:
            logger.debug("Could not get expected query count, skipping query wait")
            return
        panel_error_count = await self._safe_eval(
            page,
            """
            () => {
              const sel = '[data-testid="panel-error-container"], .panel-content .alert-error, .react-panel-error';
              const els = document.querySelectorAll(sel);
              const panels = new Set();
              els.forEach(el => {
                const p = el.closest('[data-testid="panel-container"], .react-grid-item, .panel-container');
                if (p) panels.add(p);
              });
              return panels.size;
            }
            """,
            default=0,
        )
        effective_expected = max(0, int(expected) - int(panel_error_count or 0))
        if effective_expected == 0:
            return
        timeout_ms = self.settings.check_queries_completion_timeout_ms
        stuck_ms = self.settings.check_queries_stuck_threshold_ms
        interval_ms = self.settings.check_queries_poll_interval_ms
        elapsed = 0
        last_count = -1
        stuck_since = 0
        while elapsed < timeout_ms and not page.is_closed():
            completed = await self._get_completed_query_count(page)
            if completed >= effective_expected:
                logger.debug("All queries complete (%s/%s)", completed, effective_expected)
                return
            if completed == last_count:
                stuck_since += interval_ms
                if stuck_since >= stuck_ms:
                    logger.warning(
                        "Query count unchanged at %s/%s for %sms, proceeding",
                        completed,
                        effective_expected,
                        stuck_ms,
                    )
                    return
            else:
                stuck_since = 0
            last_count = completed
            await page.wait_for_timeout(interval_ms)
            elapsed += interval_ms
        logger.warning(
            "Query wait timed out after %sms (last: %s/%s), proceeding",
            timeout_ms,
            last_count,
            effective_expected,
        )

    async def _compute_pdf_dimensions(
        self,
        page,
        measured: dict | None = None,
        width: int | None = None,
    ) -> tuple[float, float]:
        """Return PDF width/height in inches.

        Values are intentionally *not* capped to ``pdf_max_inch`` so callers can
        detect overflow and fall back to paged mode. Cap only when writing PDF.
        """
        if measured and measured.get("totalHeight"):
            scroll_w = width or self.settings.viewport_width
            scroll_h = int(measured["totalHeight"])
        else:
            vals = await self._safe_eval(
                page,
                """
                () => {
                  const doc = document.documentElement;
                  const body = document.body || {};
                  const scrollY = window.scrollY || window.pageYOffset || 0;
                  let maxBottom = 0;
                  document.querySelectorAll('.react-grid-item').forEach(el => {
                    const r = el.getBoundingClientRect();
                    const bottom = r.top + scrollY + r.height;
                    if (bottom > maxBottom) maxBottom = bottom;
                  });
                  const widthPx = Math.max(
                    doc.scrollWidth || 0,
                    body.scrollWidth || 0,
                    window.innerWidth || 0
                  );
                  const heightPx = Math.max(
                    doc.scrollHeight || 0,
                    body.scrollHeight || 0,
                    maxBottom
                  );
                  return [Math.ceil(widthPx), Math.ceil(heightPx)];
                }
                """,
                default=[width or self.settings.viewport_width, self.settings.viewport_height],
            )
            scroll_w, scroll_h = vals
        # Small padding so page_ranges="1" does not clip the last panel edge.
        pad_px = 48
        width_in = max(1.0, scroll_w / self.settings.pdf_css_px_per_in)
        height_in = max(1.0, (scroll_h + pad_px) / self.settings.pdf_css_px_per_in)
        return width_in, height_in


    async def _scroll_through(self, page) -> None:
        try:
            await self._safe_eval(
                page,
                """
                (async () => {
                  const sel = '.scroll-canvas, [data-testid="scroll-container"], .dashboard-container, .scrollbar-view';
                  const scrollableSection = document.querySelector(sel);
                  const target = scrollableSection || document.scrollingElement || document.documentElement;
                  const totalHeight = target.scrollHeight || document.documentElement.scrollHeight;
                  const viewportHeight = window.innerHeight;
                  let scrolled = 0;
                  while (scrolled < totalHeight) {
                    if (scrollableSection) scrollableSection.scrollBy(0, viewportHeight);
                    else window.scrollBy(0, viewportHeight);
                    scrolled += viewportHeight;
                    await new Promise(r => setTimeout(r, 120));
                  }
                  if (scrollableSection) scrollableSection.scrollTo(0, 0);
                  else window.scrollTo(0, 0);
                })()
                """,
            )
        except Exception:
            pass

    async def _wait_for_panels_ready(self, page, timeout_ms: int | None = None) -> bool:
        timeout_ms = timeout_ms or self.settings.ready_timeout_ms
        loop = asyncio.get_running_loop()
        deadline = loop.time() + (timeout_ms / 1000)

        panel_root_sel = (
            "[data-testid='VizPanel'], "
            "[data-testid='panel-container'], "
            "section[aria-label='Panel container'], "
            "div[aria-label='Panel'], "
            ".panel-container, .react-grid-item"
        )
        panel_content_sel = "canvas, svg, table, img, .table-panel, [data-testid='table']"

        async def expected_panels() -> int:
            val = await self._safe_eval(
                page,
                f"() => document.querySelectorAll(\"{panel_root_sel}\").length",
                default=0,
            )
            return int(val or 0)

        async def ready_panels() -> int:
            val = await self._safe_eval(
                page,
                f"""
                () => {{
                  const panels = Array.from(document.querySelectorAll("{panel_root_sel}"));
                  let ok = 0;
                  for (const p of panels) {{
                    const el = p.querySelector("{panel_content_sel}");
                    if (!el) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width > 2 && r.height > 2) ok++;
                  }}
                  return ok;
                }}
                """,
                default=0,
            )
            return int(val or 0)

        async def no_spinners() -> bool:
            return await self._safe_eval(
                page,
                """
                () => {
                  const sel = [
                    '[data-testid="Spinner"]', '.panel-loading', '.spinner', '[aria-label="Loading..."]'
                  ].join(',');
                  const els = Array.from(document.querySelectorAll(sel));
                  return els.every(e => e.offsetParent === null);
                }
                """,
                default=True,
            )

        async def tables_non_empty() -> bool:
            return await self._safe_eval(
                page,
                """
                () => {
                  const tables = Array.from(document.querySelectorAll('table'));
                  if (tables.length === 0) return true;
                  return tables.some(tb => {
                    const hasHeader = tb.querySelector('thead tr th');
                    const rows = tb.querySelectorAll('tbody tr');
                    return hasHeader || rows.length >= 1;
                  });
                }
                """,
                default=True,
            )

        exp = await expected_panels()
        if exp == 0:
            await page.wait_for_timeout(2000)
            return True

        if self.settings.pdf_fast_mode and exp <= 8:
            for _ in range(75):
                if await no_spinners() and await tables_non_empty():
                    rd = await ready_panels()
                    if rd >= max(1, int(exp * 0.6)):
                        return True
                await page.wait_for_timeout(200)

        consecutive_ready = 0
        last_ready_count = 0

        while loop.time() < deadline and not page.is_closed():
            ready = await ready_panels()
            exp = exp or await expected_panels()

            if ready > 0 and ready == last_ready_count:
                consecutive_ready += 1
            else:
                consecutive_ready = 0
                last_ready_count = ready

            if await no_spinners() and await tables_non_empty():
                if ready >= max(1, int((exp or 1) * 0.6)):
                    return True
                if consecutive_ready >= 3 and ready >= 1:
                    return True
                if ready >= 3:
                    return True

            await page.wait_for_timeout(250)

        return False

    async def _inject_styles(self, page) -> None:
        await page.add_style_tag(content=self._style_block())
        try:
            await page.evaluate(
                """
                (() => {
                    const isPdfExportLabel = (el) => {
                        const txt = (el.textContent || '').toLowerCase();
                        return txt.includes('pdf export');
                    };
                    const selectors = [
                        'a[href*="/pdf-export"]',
                        'button[href*="/pdf-export"]',
                    ];
                    selectors.forEach(sel => {
                        document.querySelectorAll(sel).forEach(el => el.remove());
                    });
                    document.querySelectorAll('a, button').forEach(el => {
                        if (isPdfExportLabel(el)) el.remove();
                    });
                })();
                """
            )
        except Exception:
            pass

    async def _inject_banner(self, page, title: str, period: str, generated: str) -> None:
        try:
            await page.evaluate(
                """({ title, period, generated }) => {
                        let banner = document.getElementById('report-banner');
                        if (!banner) {
                            banner = document.createElement('div');
                            banner.id = 'report-banner';
                            const body = document.body || document.documentElement;
                            if (body.firstChild) {
                                body.insertBefore(banner, body.firstChild);
                            } else {
                                body.appendChild(banner);
                            }
                        }
                        banner.replaceChildren();
                        const titleEl = document.createElement('div');
                        titleEl.className = 'rb-title';
                        titleEl.textContent = title;
                        const metaEl = document.createElement('div');
                        metaEl.className = 'rb-meta';
                        metaEl.textContent = 'Period: ' + period + ' • Generated: ' + generated;
                        banner.appendChild(titleEl);
                        banner.appendChild(metaEl);
                    }""",
                {"title": title, "period": period, "generated": generated},
            )
        except Exception as exc:
            logger.warning("Failed to inject banner: %s", exc)

        try:
            await page.evaluate(
                """
                () => {
                    const headers = document.querySelectorAll('[role="columnheader"], [role="row"][aria-label="table header"]');
                    headers.forEach(header => {
                        header.style.display = 'flex';
                        header.style.visibility = 'visible';
                        header.style.opacity = '1';
                        header.style.height = 'auto';
                        header.style.minHeight = '35px';
                        header.style.position = 'relative';
                        const children = header.querySelectorAll('*');
                        children.forEach(child => {
                            child.style.visibility = 'visible';
                            child.style.opacity = '1';
                        });
                    });
                    const separators = document.querySelectorAll('[role="separator"], [draggable="false"]');
                    separators.forEach(sep => {
                        if ((sep.getAttribute('role') === 'separator') ||
                            (sep.className && sep.className.toString().includes('resize')) ||
                            (sep.style && sep.style.cursor === 'col-resize')) {
                            sep.style.display = 'none';
                            sep.style.visibility = 'hidden';
                        }
                    });
                }
                """
            )
        except Exception as exc:
            logger.warning("Failed to force table headers visible: %s", exc)

    def _style_block(self) -> str:
        return """
            /* Hide Grafana chrome and actions */
            [data-testid="topnav"], [aria-label="Side menu"], [data-testid="sidemenu"],
            nav, aside, [role="banner"],
            [data-testid="TimeRangePicker"], [data-testid="DashboardHeader"],
            [data-testid="page-header"], [data-testid="breadcrumbs"], [aria-label="Breadcrumbs"],
            [data-testid="page-actions"], [data-testid="DataLinksButton"],
            [data-testid="RefreshPicker"], [aria-label="Refresh dashboard"],
            .css-12rf1df {
              display: none !important;
            }

            [data-testid="panel-header"] [data-testid="Panel menu"],
            [data-testid="panel-header"] button:not([role="columnheader"] button),
            [data-testid="panel-header"] [role="button"]:not([role="columnheader"] [role="button"]),
            .panel-header-corner,
            .panel-menu-container {
              display: none !important;
            }

            /* Keep panel legends visible (Grafana legends use buttons) */
            [data-testid="viz-legend"], [data-testid="legend"], .viz-legend, .legend-container, .graph-legend, .panel-legend {
                display: flex !important;
                visibility: visible !important;
                opacity: 1 !important;
                height: auto !important;
            }
            [data-testid="viz-legend"] button,
            [data-testid="legend"] button,
            .legend-container button,
            .panel-legend button,
            .graph-legend button,
            [data-testid="legend-item"] button,
            [data-testid="viz-legend-item"] button {
                display: inline-flex !important;
                visibility: visible !important;
                opacity: 1 !important;
            }

            .page, .page-content, .main-view, .dashboard-container {
              padding: 0 !important; margin: 0 !important;
            }

            * { animation: none !important; transition: none !important; }
            html, body {
              overflow: visible !important;
              -webkit-print-color-adjust: exact !important;
              print-color-adjust: exact !important;
            }
            table td, table th, .table-panel td, .table-panel th,
            [data-testid="table"] td, [data-testid="table"] th,
            [role="cell"], [role="gridcell"],
            [data-testid="table"] [role="row"] > div {
              -webkit-print-color-adjust: exact !important;
              print-color-adjust: exact !important;
            }

            body, html {
              margin: 0 !important;
              padding: 0 !important;
            }
            .dashboard-container, .scrollbar-view {
              padding-bottom: 0 !important;
              margin-bottom: 0 !important;
              overflow: visible !important;
              height: auto !important;
              max-height: none !important;
            }

            #report-banner {
              box-sizing: border-box;
              width: 100%;
              padding: 14px 18px;
              border-bottom: 1px solid #e5e7eb;
              background: #ffffff;
              font-family: system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
            }
            #report-banner .rb-title { font-size: 20px; font-weight: 600; line-height: 1.2; margin: 0 0 6px 0; }
            #report-banner .rb-meta  { font-size: 12px; opacity: 0.8; }

            table thead,
            table thead tr,
            table thead th,
            thead,
            .table-panel thead,
            [data-testid="table"] thead {
              display: table-header-group !important;
              visibility: visible !important;
              opacity: 1 !important;
              height: auto !important;
              max-height: none !important;
            }

            th, table th, thead th {
              display: table-cell !important;
              visibility: visible !important;
              opacity: 1 !important;
            }

            [role="rowgroup"]:has([role="columnheader"]) {
              display: block !important;
              visibility: visible !important;
              opacity: 1 !important;
              height: auto !important;
              overflow: visible !important;
            }

            [role="row"][aria-label="table header"] {
              display: flex !important;
              visibility: visible !important;
              opacity: 1 !important;
              height: auto !important;
              min-height: 35px !important;
              width: 100% !important;
              position: relative !important;
              overflow: visible !important;
            }

            [role="columnheader"],
            div[role="columnheader"] {
              display: flex !important;
              visibility: visible !important;
              opacity: 1 !important;
              height: auto !important;
              min-height: 35px !important;
              max-height: none !important;
              position: absolute !important;
              overflow: visible !important;
            }

            [role="columnheader"] [role="separator"],
            [role="columnheader"] [draggable],
            [role="separator"],
            div[role="separator"],
            .resizer,
            [class*="resize"],
            [class*="resizeHandle"] {
              display: none !important;
              visibility: hidden !important;
            }

            @media print {
              @page {
                margin: 0;
                size: auto;
              }
              html, body {
                -webkit-print-color-adjust: exact !important;
                print-color-adjust: exact !important;
              }
              body {
                padding: 0 !important;
                margin: 0 !important;
              }
              .react-grid-item, .panel-container, [data-testid="panel-container"] {
                break-inside: avoid !important;
                page-break-inside: avoid !important;
              }

              .toolbar button,
              nav button,
              [data-testid="topnav"] button,
              [data-testid="page-actions"] button,
              [data-testid="page-header"] button,
              [data-testid="DashboardHeader"] button,
              [data-testid="RefreshPicker"] button,
              .gf-form button {
                  display: none !important;
              }
              .toolbar, .gf-form {
                  display: none !important;
              }
            }
        """
