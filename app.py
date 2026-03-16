"""
Flask-based Lead Generator – Business Finder.
Uses headless Playwright to scrape Google Maps and streams
real-time progress to the browser via Server-Sent Events.
CSV is served in-memory; never written to disk.
"""

import csv
import io
import json
import queue
import threading
from datetime import date
from urllib.parse import quote_plus, unquote_plus

from flask import Flask, render_template, request, jsonify, Response
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

app = Flask(__name__)

# ── Global state ─────────────────────────────────────────────────────
progress_queues: dict[str, queue.Queue] = {}
results_store: dict[str, list] = {}
status_store: dict[str, str] = {}   # idle | running | done | error
meta_store: dict[str, dict] = {}

TARGET_COUNT = 50
SCROLL_PAUSE_MS = 1800


# ── Scraper helpers ──────────────────────────────────────────────────

def _scroll_and_collect_urls(page, panel, target, max_scrolls=80):
    """Scroll the results panel and collect unique place URLs."""
    seen_urls: set[str] = set()
    stale = 0

    for _ in range(max_scrolls):
        links = page.locator(f"{panel} a[href*='/maps/place/']").all()
        for link in links:
            href = link.get_attribute("href") or ""
            if "/maps/place/" in href and href not in seen_urls:
                seen_urls.add(href)

        if len(seen_urls) >= target:
            break

        if page.locator('p.fontBodyMedium span:has-text("end of the list")').count():
            break

        prev = len(seen_urls)
        page.evaluate(
            "(sel) => { const el = document.querySelector(sel); if (el) el.scrollTop = el.scrollHeight; }",
            panel,
        )
        page.wait_for_timeout(SCROLL_PAUSE_MS)

        new_count = len(seen_urls)
        # Re-evaluate after scroll
        links = page.locator(f"{panel} a[href*='/maps/place/']").all()
        for link in links:
            href = link.get_attribute("href") or ""
            if "/maps/place/" in href and href not in seen_urls:
                seen_urls.add(href)

        if len(seen_urls) == prev:
            stale += 1
            if stale >= 6:
                break
        else:
            stale = 0

    return list(seen_urls)[:target]


def _get_name_from_url(url: str) -> str:
    """Extract a readable business name from the Google Maps URL."""
    try:
        # URL pattern: /maps/place/Business+Name/
        part = url.split("/maps/place/")[1].split("/")[0]
        return unquote_plus(part).strip()
    except Exception:
        return ""


def _scrape_detail_page(page, url: str):
    """Navigate directly to a place URL and scrape its details."""
    phone, website, address = "", "", ""
    name = ""

    try:
        page.goto(url, timeout=30000, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)

        # Wait for the detail panel to load
        try:
            page.wait_for_selector("h1.fontHeadlineLarge, button[data-item-id*='phone'], button[data-item-id='address']", timeout=8000)
        except PWTimeout:
            pass

        # Business name from h1
        h1 = page.locator("h1.fontHeadlineLarge")
        if h1.count():
            name = h1.first.inner_text().strip()

        # Address
        a = page.locator("button[data-item-id='address']")
        if a.count():
            aria = a.first.get_attribute("aria-label") or ""
            address = aria.replace("Address:", "").replace("Address", "").strip().lstrip(":")

        # Phone
        p = page.locator("button[data-item-id*='phone']")
        if p.count():
            aria = p.first.get_attribute("aria-label") or ""
            phone = aria.replace("Phone:", "").replace("Phone", "").strip().lstrip(":")
            # Remove leading zero if present
            if phone.startswith("0"):
                phone = phone.lstrip("0")

        # Website – prefer the anchor tag
        w = page.locator("a[data-item-id='authority']")
        if w.count():
            website = (w.first.get_attribute("href") or "").strip()
        if not website:
            w2 = page.locator("button[data-item-id='authority']")
            if w2.count():
                aria = w2.first.get_attribute("aria-label") or ""
                website = aria.replace("Website:", "").replace("Website", "").strip().lstrip(":")

    except Exception:
        pass

    return name, phone, website, address


# ── Background scraper thread ────────────────────────────────────────

def _run_scraper(city: str, referred_by: str, category: str, job_id: str):
    q = progress_queues[job_id]

    def emit(event, data):
        q.put({"event": event, "data": data})

    query = f"{category} in {city}"
    maps_url = f"https://www.google.com/maps/search/{quote_plus(query)}"

    emit("log", f"Searching Google Maps for: {query}")
    emit("log", "Launching headless browser...")

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(
                viewport={"width": 1280, "height": 900},
                locale="en-US",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
            )
            page = ctx.new_page()

            # ── Load search results ──────────────────────────────
            try:
                page.goto(maps_url, timeout=60000, wait_until="domcontentloaded")
            except PWTimeout:
                emit("error", "Page failed to load. Check your internet connection.")
                status_store[job_id] = "error"
                browser.close()
                return
            except Exception as exc:
                emit("error", f"Error loading page: {exc}")
                status_store[job_id] = "error"
                browser.close()
                return

            # Consent banner
            try:
                btn = page.locator("button:has-text('Accept all')")
                if btn.count():
                    btn.first.click()
                    page.wait_for_timeout(1000)
            except Exception:
                pass

            panel = "div[role='feed']"
            try:
                page.wait_for_selector(panel, timeout=15000)
            except PWTimeout:
                emit("error", f"No results feed found for '{city}'.")
                status_store[job_id] = "error"
                browser.close()
                return

            emit("log", "Scrolling to collect all place links...")
            emit("phase", "scrolling")

            place_urls = _scroll_and_collect_urls(page, panel, TARGET_COUNT)
            total = len(place_urls)

            emit("log", f"Collected {total} place links. Extracting details...")
            emit("total", str(total))
            emit("phase", "extracting")

            if total == 0:
                emit("error", f"No businesses found for '{category}' in '{city}'.")
                status_store[job_id] = "error"
                browser.close()
                return

            today_str = date.today().strftime("%m/%d/%Y")
            results = []

            for seq, place_url in enumerate(place_urls, start=1):
                # Show progress with URL-derived name while loading
                preview_name = _get_name_from_url(place_url) or f"Business {seq}"
                emit("progress", json.dumps({"current": seq, "total": total, "name": preview_name}))

                name, phone, website, address = _scrape_detail_page(page, place_url)

                # Use URL-derived name as fallback if page h1 not found
                if not name:
                    name = preview_name

                row = {
                    "Business Name":  name,
                    "Client Name":    name,
                    "Location":       city,
                    "Phone":          phone,
                    "Date":           today_str,
                    "Referred By":    referred_by,
                    "Service":        "",
                    "Status":         "",
                    "Amount ₹":       "",
                    "Category":       category,
                    "Website":        website,
                    "Real Location":  address,
                    "Maps Link":      place_url,
                }
                results.append(row)
                emit("row", json.dumps(row))

            browser.close()

        # Sort by website availability first, then by phone availability.
        # Within each website group, entries without a phone appear first.
        def has_value(val):
            txt = (val or "").strip().lower()
            return txt not in ("", "n/a", "na", "none", "null", "-")

        results.sort(
            key=lambda r: (
                0 if has_value(r.get("Website")) else 1,
                0 if not has_value(r.get("Phone")) else 1,
            )
        )

        results_store[job_id] = results
        status_store[job_id] = "done"
        emit("done", json.dumps({"count": len(results)}))

    except Exception as exc:
        emit("error", str(exc))
        status_store[job_id] = "error"


# ── Flask routes ─────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/search", methods=["POST"])
def search():
    data = request.get_json(force=True)
    city        = (data.get("city") or "").strip()
    referred_by = (data.get("referred_by") or "").strip()
    category    = (data.get("category") or "").strip()

    if not city:
        return jsonify(error="City name is required."), 400
    if not referred_by:
        return jsonify(error="Referred By name is required."), 400
    if not category:
        return jsonify(error="Category is required."), 400

    job_id = f"{city}-{category}-{date.today().isoformat()}"

    if status_store.get(job_id) == "running":
        return jsonify(error="A search for this city & category is already running."), 409

    progress_queues[job_id] = queue.Queue()
    results_store[job_id] = []
    status_store[job_id] = "running"
    meta_store[job_id] = {"city": city, "category": category}

    t = threading.Thread(
        target=_run_scraper, args=(city, referred_by, category, job_id), daemon=True
    )
    t.start()

    return jsonify(job_id=job_id)


@app.route("/stream/<job_id>")
def stream(job_id):
    def generate():
        q = progress_queues.get(job_id)
        if q is None:
            yield "event: error\ndata: Unknown job.\n\n"
            return
        while True:
            try:
                msg = q.get(timeout=120)
            except queue.Empty:
                yield "event: error\ndata: Timeout waiting for scraper.\n\n"
                return
            yield f"event: {msg['event']}\ndata: {msg['data']}\n\n"
            if msg["event"] in ("done", "error"):
                return

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/download/<job_id>")
def download(job_id):
    results = results_store.get(job_id)
    if not results:
        return "No results available for this job.", 404

    meta = meta_store.get(job_id, {})
    city_slug = meta.get("city", "leads").lower().replace(" ", "_")
    today_str = date.today().strftime("%m-%d-%Y")
    filename = f"{city_slug}_leads_{today_str}.csv"

    cols = [
        "Business Name", "Client Name", "Location", "Phone",
        "Date", "Referred By", "Service", "Status", "Amount ₹",
        "Category", "Website", "Real Location", "Maps Link",
    ]

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=cols)
    writer.writeheader()
    writer.writerows(results)

    return Response(
        buf.getvalue().encode("utf-8-sig"),
        mimetype="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": "text/csv; charset=utf-8-sig",
        },
    )


if __name__ == "__main__":
    app.run(debug=False, port=5000)
