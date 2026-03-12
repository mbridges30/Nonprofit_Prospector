"""
ProPublica full-text search across all 3M+ e-filed 990s.
This is critical for Layer 2: finding which foundations mention a given org.

The full-text search is on the ProPublica website (not the JSON API),
so we parse the HTML search results page.
"""

import re
import time
from typing import Optional

import requests
from lxml import html

SEARCH_URL = "https://projects.propublica.org/nonprofits/search"
DEFAULT_DELAY = 1.0  # Be polite to the website


class FullTextSearchClient:
    """Searches ProPublica's full-text index of 990 e-filings."""

    def __init__(self, delay: float = DEFAULT_DELAY):
        self.delay = delay
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml",
        })

    def search(self, query: str, state: str = None, ntee: str = None,
               c_code: str = None, page: int = 1, search_type: str = None) -> list:
        """Search ProPublica full-text index. Returns list of result dicts.

        Each result contains:
          - name: org name
          - ein: EIN
          - city: city
          - state: state
          - snippet: text snippet showing match context
          - url: ProPublica org URL
        """
        params = {"utf8": "✓", "q": query}
        if state:
            params["state[id]"] = state
        if ntee:
            params["ntee[id]"] = ntee
        if c_code:
            params["c_code[id]"] = c_code
        if search_type:
            params["search_type"] = search_type
        if page > 1:
            params["page"] = page

        time.sleep(self.delay)
        try:
            r = self.session.get(SEARCH_URL, params=params, timeout=20)
            r.raise_for_status()
        except requests.exceptions.RequestException as e:
            print(f"  [Full-text search error] {e}")
            return []

        return self._parse_results(r.text)

    def search_for_funders(self, org_name: str, max_pages: int = 3) -> list:
        """Search for foundations that mention an org in their 990 filings.

        This finds 990-PF filings that list the org as a grantee.
        Returns deduplicated list of potential funders.
        """
        all_results = []
        seen_eins = set()

        for page in range(1, max_pages + 1):
            results = self.search(org_name, page=page, search_type="filings")
            if not results:
                break

            new_count = 0
            for r in results:
                ein = r.get("ein", "")
                if ein and ein not in seen_eins:
                    seen_eins.add(ein)
                    all_results.append(r)
                    new_count += 1

            if new_count == 0:
                break

        return all_results

    def _parse_results(self, html_text: str) -> list:
        """Parse HTML search results into structured data."""
        results = []

        try:
            tree = html.fromstring(html_text)
        except Exception as e:
            print(f"  [HTML parse error] {e}")
            return []

        # ProPublica search results use .result-row containers
        result_blocks = tree.cssselect(".result-row")

        for block in result_blocks:
            result = self._parse_result_block(block)
            if result:
                results.append(result)

        # Fallback: parse links with /nonprofits/organizations/ pattern
        if not results:
            results = self._parse_from_links(tree)

        return results

    def _parse_result_block(self, block) -> Optional[dict]:
        """Parse a single search result block.

        Actual ProPublica DOM structure (as of 2026):
          .result-row
            .result-item
              .result-item__row
                .result-item__hed > a[href="/nonprofits/organizations/{EIN}"]
                .text-sub  →  "City, ST • Category"
              .metrics-wrapper  →  revenue info
        """
        # Find org link
        links = block.cssselect("a[href*='/organizations/']")
        if not links:
            return None

        link = links[0]
        # Name may include a span with DBA name — get just the main text
        name = link.text_content().strip()
        # Clean up whitespace from multi-line HTML
        name = re.sub(r'\s+', ' ', name).strip()
        # Remove "— DBA Name" suffix if present
        if ' — ' in name:
            name = name.split(' — ')[0].strip()

        href = link.get("href", "")

        # Extract EIN from URL
        ein_match = re.search(r"/organizations/(\d+)", href)
        ein = ein_match.group(1) if ein_match else ""

        # Location from .text-sub div
        city = ""
        state = ""
        text_sub = block.cssselect(".text-sub")
        if text_sub:
            sub_text = text_sub[0].text_content().strip()
            # Format is "City, ST" or "City, ST • Category"
            location_match = re.search(r"^([A-Za-z\s.]+),\s*([A-Z]{2})", sub_text)
            if location_match:
                city = location_match.group(1).strip()
                state = location_match.group(2)

        # Snippet: use full block text for context
        snippet = block.text_content()
        snippet = re.sub(r'\s+', ' ', snippet).strip()[:200]

        return {
            "name": name,
            "ein": ein,
            "city": city,
            "state": state,
            "snippet": snippet,
            "url": f"https://projects.propublica.org{href}" if href.startswith("/") else href,
        }

    def _parse_from_links(self, tree) -> list:
        """Fallback parser: extract results from org links."""
        results = []
        seen = set()

        for link in tree.xpath("//a[contains(@href, '/nonprofits/organizations/')]"):
            href = link.get("href", "")
            ein_match = re.search(r"/organizations/(\d+)", href)
            if not ein_match:
                continue

            ein = ein_match.group(1)
            if ein in seen:
                continue
            seen.add(ein)

            name = link.text_content().strip()
            if not name or len(name) < 2:
                continue

            # Get parent text for context
            parent = link.getparent()
            snippet = parent.text_content().strip()[:200] if parent is not None else ""

            results.append({
                "name": name,
                "ein": ein,
                "city": "",
                "state": "",
                "snippet": snippet,
                "url": f"https://projects.propublica.org{href}" if href.startswith("/") else href,
            })

        return results
