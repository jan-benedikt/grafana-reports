from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict

from playwright.async_api import TimeoutError as PWTimeoutError
from playwright.async_api import async_playwright

from .settings import Settings


class GrafanaPDFRenderer:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def render_url(
        self,
        url: str,
        output_pdf: Path,
        header_title: str,
        header_period: str,
        header_generated: str,
    ) -> None:
        print(f"[DEBUG] render_url: Starting Playwright for {header_title}")
        async with async_playwright() as p:
            chromium_args = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
            print(f"[DEBUG] render_url: Launching browser with args: {chromium_args}")
            browser = await p.chromium.launch(args=chromium_args, headless=True)
            print(f"[DEBUG] render_url: Browser launched successfully")
            print(f"[DEBUG] render_url: Creating browser context")
            ctx = await browser.new_context(
                viewport={"width": self.settings.viewport_width, "height": self.settings.viewport_height},
                device_scale_factor=1,
                locale=self.settings.render_locale,
                timezone_id=self.settings.render_timezone,
            )
            print(f"[DEBUG] render_url: Browser context created")

            if self.settings.pdf_fast_mode:
                print(f"[DEBUG] render_url: Setting up fast mode route filter")
                await ctx.route("**/*", self._route_filter)

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

            print(f"[DEBUG] render_url: Creating new page")
            page = await ctx.new_page()
            print(f"[DEBUG] render_url: Page created")

            if self.settings.debug_render:
                page.on("requestfailed", lambda req: self._log_request_failure(req))
                page.on("pageerror", lambda msg: print(f"[PAGE ERROR] {msg}"))

            print(f"[DEBUG] render_url: Ensuring session cookie")
            await self._ensure_session_cookie(ctx)
            print(f"[DEBUG] render_url: Session cookie ensured")

            print(f"[DEBUG] render_url: Navigating to {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=120_000)
            print(f"[DEBUG] render_url: Page loaded (domcontentloaded)")
            network_timeout = self.settings.network_idle_timeout_ms if self.settings.pdf_fast_mode else 60_000
            if network_timeout > 0:
                try:
                    await page.wait_for_load_state("networkidle", timeout=network_timeout)
                    print(f"[DEBUG] render_url: Network idle (timeout={network_timeout}ms)")
                except Exception:
                    print(f"[DEBUG] render_url: Network idle not reached within {network_timeout}ms, continuing")
            if self.settings.wait_before_ready_ms > 0:
                print(f"[DEBUG] render_url: Waiting {self.settings.wait_before_ready_ms}ms before panel checks")
                await page.wait_for_timeout(self.settings.wait_before_ready_ms)

            if await page.locator('input[name="user"], form[action="/login"]').count() > 0:
                await self._handle_login(page, url)

            await self._wait_for_queries_complete(page, url)

            print(f"[DEBUG] render_url: Injecting styles and banner")
            await self._inject_styles(page)
            await self._inject_banner(page, header_title, header_period, header_generated)
            media = self.settings.pdf_media if getattr(self.settings, "pdf_media", None) in {"print", "screen"} else "print"
            await page.emulate_media(media=media)
            print(f"[DEBUG] render_url: Styles and banner injected (media={media})")

            print(f"[DEBUG] render_url: Waiting for panels to be ready")
            ready = await self._wait_for_panels_ready(page)
            if not ready:
                print("[WARN] Panels not fully ready by timeout, proceeding anyway (best effort).")
            else:
                print(f"[DEBUG] render_url: All panels ready")

            extra_wait = 3000 if self.settings.pdf_fast_mode else self.settings.wait_extra_ms
            print(f"[DEBUG] render_url: Extra wait of {extra_wait}ms")
            await page.wait_for_timeout(extra_wait)

            print(f"[DEBUG] render_url: Scrolling and trimming page")
            await self._gentle_scroll(page)
            await self._trim_page_height(page)
            print(f"[DEBUG] render_url: Page prepared")

            if self.settings.debug_screenshot and not page.is_closed():
                print(f"[DEBUG] render_url: Taking debug screenshot")
                await page.screenshot(path=str(output_pdf.with_suffix(".png")), full_page=False)

            print(f"[DEBUG] render_url: Generating PDF with mode={self.settings.pdf_size_mode}")
            if self.settings.pdf_size_mode == "a4":
                await page.pdf(
                    path=str(output_pdf),
                    format=self.settings.pdf_paper_format,
                    landscape=self.settings.pdf_landscape,
                    print_background=True,
                    margin={"top": "0mm", "bottom": "0mm", "left": "0mm", "right": "0mm"},
                    display_header_footer=False,
                    prefer_css_page_size=False,
                )
            elif self.settings.pdf_size_mode == "css":
                await page.pdf(
                    path=str(output_pdf),
                    landscape=self.settings.pdf_landscape,
                    print_background=True,
                    margin={"top": "0mm", "bottom": "0mm", "left": "0mm", "right": "0mm"},
                    display_header_footer=False,
                    prefer_css_page_size=True,
                )
            else:
                width_in, height_in = await self._compute_pdf_dimensions(page)
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
            print(f"[DEBUG] render_url: PDF generated at {output_pdf}")

            print(f"[DEBUG] render_url: Closing browser context and browser")
            await ctx.close()
            await browser.close()
            print(f"[DEBUG] render_url: Browser cleanup complete")

    async def _route_filter(self, route, request):
        try:
            rtype = request.resource_type
            url = request.url
            if rtype in ("font", "media", "websocket", "eventsource"):
                return await route.abort()
            if url.endswith((".woff", ".woff2", ".ttf")):
                return await route.abort()
            return await route.continue_()
        except Exception:
            try:
                await route.abort()
            except Exception:
                pass

    def _log_request_failure(self, req):
        try:
            if getattr(req, "resource_type", None) == "font":
                return
            failure = getattr(req, "failure", None)
            reason = failure if isinstance(failure, str) else (getattr(failure, "error_text", None) or "request failed")
            print(f"[REQ FAILED] {reason} -> {req.url}")
        except Exception as exc:
            print(f"[REQ FAILED] (handler error: {exc})")

    async def _ensure_session_cookie(self, ctx):
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
                data=f'{{"user":"{self.settings.grafana_user}","password":"{self.settings.grafana_password}"}}',
                timeout=60_000,
            )
            if resp.status == 200:
                ok = await ctx.request.get(f"{self.settings.grafana_url}/api/user", timeout=30_000)
                if ok.status == 200:
                    return
            else:
                txt = await resp.text()
                print(f"[WARN] JSON login returned HTTP {resp.status}: {txt}")
        except Exception as exc:
            print(f"[WARN] JSON login failed: {exc}")

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
            print("[WARN] Interactive login timeout (maybe SSO). Continuing…")
        except Exception as exc:
            print(f"[WARN] Interactive login failed: {exc}. Continuing…")

    async def _handle_login(self, page, url: str) -> None:
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

    async def _safe_eval(self, page, script: str, default=None):
        try:
            if page.is_closed():
                return default
            return await page.evaluate(script)
        except Exception:
            return default

    async def _get_expected_query_count(self, page) -> int | None:
        """Get expected number of data queries from dashboard JSON (from browser performance entries)."""
        script = """
        (async () => {
          const excludedTypes = ['text'];
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
            const json = await res.json();
            const root = json?.spec ?? json?.model ?? json?.dashboard ?? json;
            const panels = Array.isArray(root?.panels) ? root.panels : [];
            const templating = root?.templating?.list ?? [];
            function countPanels(list) {
              let n = 0;
              for (const p of list) {
                if (!p || excludedTypes.includes(p.type)) continue;
                if (p.type === 'row' && Array.isArray(p.panels)) { n += countPanels(p.panels); continue; }
                const hasDs = !!p.datasource || (Array.isArray(p.targets) && p.targets.some(t => t?.datasource));
                if (hasDs) n++;
              }
              return n;
            }
            const valid = countPanels(panels);
            const templatingQueries = templating.filter(v => v?.type === 'query').length;
            return valid + templatingQueries;
          } catch (e) {
            return null;
          }
        })()
        """
        result = await self._safe_eval(page, script, default=None)
        if result is None:
            return None
        return int(result) if isinstance(result, (int, float)) else None

    async def _get_completed_query_count(self, page) -> int:
        """Count completed Grafana query requests in the page's performance entries."""
        script = """
        () => performance.getEntriesByType("resource")
          .filter(r => r.initiatorType === 'fetch' && r.name.includes('query')).length
        """
        val = await self._safe_eval(page, script, default=0)
        return int(val or 0)

    async def _wait_for_queries_complete(self, page, url: str) -> None:
        """
        Wait for Grafana data queries to complete by polling performance entries.
        Skips when CHECK_QUERIES_TO_COMPLETE is off or URL is single-panel (viewPanel=).
        """
        if not self.settings.check_queries_to_complete:
            return
        if "viewPanel=" in url:
            print("[DEBUG] render_url: Skipping query wait (single-panel view)")
            return
        print("[DEBUG] render_url: Waiting for all queries to complete (CHECK_QUERIES_TO_COMPLETE)")
        # Scroll to trigger lazy-loaded panels
        await self._safe_eval(
            page,
            """
            (async () => {
              const sel = '.scroll-canvas, [data-testid="scroll-container"], .dashboard-container';
              const scrollableSection = document.querySelector(sel);
              if (scrollableSection) {
                const totalHeight = scrollableSection.scrollHeight;
                const viewportHeight = window.innerHeight;
                let scrolled = 0;
                while (scrolled < totalHeight) {
                  scrollableSection.scrollBy(0, viewportHeight);
                  scrolled += viewportHeight;
                  await new Promise(r => setTimeout(r, 500));
                }
              }
            })()
            """,
        )
        await page.wait_for_timeout(500)
        expected = await self._get_expected_query_count(page)
        if expected is None or expected == 0:
            print("[DEBUG] render_url: Could not get expected query count, skipping query wait")
            return
        # Optional: subtract panels with errors so we don't wait forever for failed panels
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
        panel_error_count = int(panel_error_count or 0)
        effective_expected = max(0, expected - panel_error_count)
        if effective_expected == 0:
            print("[DEBUG] render_url: No effective queries to wait for")
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
                print(f"[DEBUG] render_url: All queries complete ({completed}/{effective_expected})")
                return
            if completed == last_count:
                stuck_since += interval_ms
                if stuck_since >= stuck_ms:
                    print(f"[WARN] Query count unchanged at {completed}/{effective_expected} for {stuck_ms}ms, proceeding")
                    return
            else:
                stuck_since = 0
            last_count = completed
            await page.wait_for_timeout(interval_ms)
            elapsed += interval_ms
            if elapsed % 10000 < interval_ms:
                print(f"[DEBUG] render_url: Queries {completed}/{effective_expected}, elapsed {elapsed}ms")
        print(f"[WARN] Query wait timed out after {timeout_ms}ms (last: {last_count}/{effective_expected}), proceeding")

    async def _compute_pdf_dimensions(self, page) -> tuple[float, float]:
        vals = await self._safe_eval(
            page,
            """
            () => {
              const doc = document.documentElement;
              const body = document.body || {};
              const widthCandidates = [
                doc.scrollWidth,
                doc.offsetWidth,
                body.scrollWidth || 0,
                body.offsetWidth || 0
              ].filter(Boolean);

              const heightCandidates = [
                doc.scrollHeight,
                doc.offsetHeight,
                body.scrollHeight || 0,
                body.offsetHeight || 0
              ].filter(Boolean);

              let maxBottom = 0;
              const nodes = document.body ? document.body.querySelectorAll('*') : [];
              nodes.forEach(el => {
                const rect = el.getBoundingClientRect();
                const bottom = rect.bottom + window.scrollY;
                if (Number.isFinite(bottom) && bottom > maxBottom) {
                  maxBottom = bottom;
                }
              });

              const widthPx = Math.max(...(widthCandidates.length ? widthCandidates : [0]));
              const heightPx = Math.max(maxBottom, ...(heightCandidates.length ? heightCandidates : [0]));
              return [Math.ceil(widthPx), Math.ceil(heightPx)];
            }
            """,
            default=[self.settings.viewport_width, self.settings.viewport_height],
        )
        scroll_w, scroll_h = vals
        width_in = min(max(1.0, scroll_w / self.settings.pdf_css_px_per_in), self.settings.pdf_max_inch)
        height_in = min(max(1.0, (scroll_h + 5) / self.settings.pdf_css_px_per_in), self.settings.pdf_max_inch)
        return width_in, height_in

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
        panel_root_sel = "".join(panel_root_sel)
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

        async def gentle_scroll_if_needed():
            total = await self._safe_eval(page, "() => document.documentElement.scrollHeight", default=self.settings.viewport_height)
            if total and total <= self.settings.viewport_height * 1.5:
                return
            step = 800
            y = 0
            while y < total and not page.is_closed():
                await page.evaluate("(y) => window.scrollTo(0, y)", y)
                await page.wait_for_timeout(120)
                y += step
            if not page.is_closed():
                await page.evaluate("() => window.scrollTo(0, 0)")
                await page.wait_for_timeout(120)

        exp = await expected_panels()
        if exp == 0:
            await page.wait_for_timeout(2000)
            return True

        if self.settings.pdf_fast_mode and exp <= 8:
            for _ in range(75):  # up to ~15s (75 * 200ms) so slow panels can render
                if await no_spinners() and await tables_non_empty():
                    rd = await ready_panels()
                    if rd >= max(1, int(exp * 0.6)):
                        return True
                await page.wait_for_timeout(200)
        else:
            await gentle_scroll_if_needed()

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
            if page.is_closed():
                break
            await page.evaluate("() => window.scrollBy(0, 300)")

        return False

    async def _gentle_scroll(self, page) -> None:
        try:
            total = await self._safe_eval(page, "() => document.documentElement.scrollHeight", default=self.settings.viewport_height)
            step = max(600, self.settings.viewport_height // 2)
            y = 0
            while y < min(total, self.settings.viewport_height * 4) and not page.is_closed():
                await page.evaluate("(y) => window.scrollTo(0, y)", y)
                await page.wait_for_timeout(120)
                y += step
            if not page.is_closed():
                await page.evaluate("() => window.scrollTo(0, 0)")
                await page.wait_for_timeout(120)
        except Exception:
            pass

    async def _trim_page_height(self, page) -> None:
        try:
            await page.evaluate(
                """
                () => {
                    const body = document.body;
                    if (!body) return;
                    const allElements = Array.from(document.querySelectorAll('*'));
                    let maxBottom = 0;
                    allElements.forEach(el => {
                        const rect = el.getBoundingClientRect();
                        const bottom = rect.top + rect.height;
                        if (bottom > maxBottom) {
                            maxBottom = bottom;
                        }
                    });
                    if (maxBottom > 0) {
                        body.style.height = Math.ceil(maxBottom + 20) + 'px';
                        body.style.minHeight = body.style.height;
                        body.style.maxHeight = body.style.height;
                    }
                }
                """
            )
        except Exception as exc:
            print(f"[WARN] Failed to trim page height: {exc}")

    async def _inject_styles(self, page) -> None:
        await page.add_style_tag(content=self._style_block())
        # Remove any visible "PDF Export" buttons/links to keep the PDF clean
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
                        banner.innerHTML = `
                            <div class="rb-title">${title}</div>
                            <div class="rb-meta">Period: ${period} • Generated: ${generated}</div>
                        `;
                    }""",
                {"title": title, "period": period, "generated": generated},
            )
        except Exception as exc:
            print(f"[WARN] Failed to inject banner: {exc}")

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
                    const rowGroups = document.querySelectorAll('[role="rowgroup"]');
                    rowGroups.forEach(group => {
                        if (group.querySelector('[role="columnheader"]')) {
                            group.style.display = 'block';
                            group.style.visibility = 'visible';
                            group.style.opacity = '1';
                        }
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
            print(f"[WARN] Failed to force table headers visible: {exc}")

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
              background: #ffffff !important;
              overflow: visible !important;
              -webkit-print-color-adjust: exact !important;
              print-color-adjust: exact !important;
            }
            /* Preserve Grafana threshold/conditional formatting colors in table cells (avoid green turning pink in PDF) */
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
            table thead td,
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

            [role="columnheader"] button,
            [role="columnheader"] svg,
            [role="columnheader"] div {
              visibility: visible !important;
              opacity: 1 !important;
              display: inline-flex !important;
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
              table td, table th, .table-panel td, .table-panel th,
              [data-testid="table"] td, [data-testid="table"] th,
              [role="cell"], [role="gridcell"],
              [data-testid="table"] [role="row"] > div {
                -webkit-print-color-adjust: exact !important;
                print-color-adjust: exact !important;
              }
              body {
                padding: 0 !important;
                margin: 0 !important;
              }
              * {
                break-before: avoid !important;
              }
              .dashboard-container, .react-grid-layout, .panel-container {
                break-inside: avoid !important;
              }

                            /* Hide general UI controls but keep legends */
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

              table thead,
              thead,
              .table-panel thead,
              [data-testid="table"] thead {
                display: table-header-group !important;
                page-break-inside: avoid !important;
                visibility: visible !important;
              }
              table thead tr,
              table thead th,
              table thead td,
              th {
                display: table-cell !important;
                visibility: visible !important;
                opacity: 1 !important;
              }

              [role="rowgroup"]:has([role="columnheader"]) {
                display: block !important;
                visibility: visible !important;
                opacity: 1 !important;
                page-break-inside: avoid !important;
                overflow: visible !important;
              }

              [role="row"][aria-label="table header"] {
                display: flex !important;
                visibility: visible !important;
                opacity: 1 !important;
                page-break-inside: avoid !important;
                min-height: 35px !important;
                position: relative !important;
                overflow: visible !important;
              }

              [role="columnheader"],
              div[role="columnheader"] {
                display: flex !important;
                visibility: visible !important;
                opacity: 1 !important;
                page-break-inside: avoid !important;
                position: absolute !important;
                overflow: visible !important;
              }
            }
        """
