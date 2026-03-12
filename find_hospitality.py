"""
Interactive AI Agent to find hospitality businesses (Restaurants & Hotels)
on Google Maps using Playwright and display results with tabulate.
"""

import sys
import time

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:
    sys.exit("Missing dependency: pip install playwright && python -m playwright install chromium")

try:
    from tabulate import tabulate
except ImportError:
    sys.exit("Missing dependency: pip install tabulate")


TARGET_COUNT = 20


def scroll_results_panel(page, panel_selector, target_count, max_scrolls=30):
    """Scroll the results panel until we have enough items or can't load more."""
    for _ in range(max_scrolls):
        items = page.locator(f"{panel_selector} a[href*='/maps/place/']").all()
        if len(items) >= target_count:
            return
        # Check for "end of list" marker
        if page.locator("p.fontBodyMedium span:has-text(\"end of the list\")").count():
            return
        # Scroll down inside the feed panel
        page.evaluate(
            """(sel) => {
                const el = document.querySelector(sel);
                if (el) el.scrollTop = el.scrollHeight;
            }""",
            panel_selector,
        )
        page.wait_for_timeout(1500)


def extract_results(page, panel_selector, target_count):
    """Extract business name, rating, and address from the visible result cards."""
    results = []
    seen = set()

    cards = page.locator(f"{panel_selector} div[jsaction] a[href*='/maps/place/']").all()

    for card in cards:
        if len(results) >= target_count:
            break

        # --- Name ---
        name = ""
        name_el = card.locator("div.fontHeadlineSmall")
        if name_el.count():
            name = name_el.first.inner_text().strip()
        if not name:
            aria = card.get_attribute("aria-label") or ""
            name = aria.strip()
        if not name or name in seen:
            continue
        seen.add(name)

        # --- Rating ---
        rating = "N/A"
        rating_el = card.locator("span[role='img']")
        if rating_el.count():
            aria_label = rating_el.first.get_attribute("aria-label") or ""
            # e.g. "4.5 stars 1,234 Reviews"
            parts = aria_label.split()
            if parts and parts[0].replace(".", "").isdigit():
                rating = parts[0]

        # --- Address ---
        address = "N/A"
        # Address usually appears after the category/price line
        text_spans = card.locator("div.fontBodyMedium > div > span[style*='color']").all()
        for span in text_spans:
            txt = span.inner_text().strip()
            if txt and len(txt) > 5 and not txt.startswith("$") and "·" not in txt:
                address = txt
                break

        # Fallback: grab second line of body text
        if address == "N/A":
            body_divs = card.locator("div.fontBodyMedium").all()
            for bd in body_divs:
                txt = bd.inner_text().strip()
                lines = [l.strip() for l in txt.split("\n") if l.strip()]
                for line in lines:
                    if len(line) > 8 and any(c.isdigit() for c in line):
                        address = line
                        break
                if address != "N/A":
                    break

        results.append({"Name": name, "Rating": rating, "Address": address})

    return results


def main():
    city = input("Enter a city or area name: ").strip()
    if not city:
        print("No city provided. Exiting.")
        return

    query = f"Restaurants and Hotels in {city}"
    maps_url = f"https://www.google.com/maps/search/{query.replace(' ', '+')}"

    print(f"\nSearching Google Maps for: {query}")
    print("Launching browser …\n")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        page = context.new_page()

        try:
            page.goto(maps_url, timeout=30000, wait_until="domcontentloaded")
        except PWTimeout:
            print("Error: Page failed to load. Check your internet connection.")
            browser.close()
            return
        except Exception as exc:
            print(f"Error loading page: {exc}")
            browser.close()
            return

        # Dismiss cookie/consent dialog if present
        try:
            accept_btn = page.locator("button:has-text('Accept all')")
            if accept_btn.count():
                accept_btn.first.click()
                page.wait_for_timeout(1000)
        except Exception:
            pass

        # Wait for results feed to appear
        panel_selector = "div[role='feed']"
        try:
            page.wait_for_selector(panel_selector, timeout=15000)
        except PWTimeout:
            print(f"No results found for '{city}'. The area may not exist or has no listings.")
            browser.close()
            return

        # Scroll to load enough results
        scroll_results_panel(page, panel_selector, TARGET_COUNT)

        # Extract data
        results = extract_results(page, panel_selector, TARGET_COUNT)

        browser.close()

    if not results:
        print(f"No hospitality businesses found for '{city}'.")
        return

    # Display results
    table_data = [
        [i + 1, r["Name"], r["Rating"], r["Address"]]
        for i, r in enumerate(results)
    ]
    print(tabulate(table_data, headers=["#", "Name", "Rating", "Address"], tablefmt="fancy_grid"))
    print(f"\nTotal results: {len(results)}")


if __name__ == "__main__":
    main()
