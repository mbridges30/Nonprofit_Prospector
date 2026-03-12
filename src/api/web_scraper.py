"""
Foundation grant page scraper.

Scrapes foundation websites for grant data that may not be in 990 filings.
Each foundation has a custom or generic parser. Results are cached to avoid
repeated requests.
"""

import json
import os
import re
import time
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
from lxml import html

from src.core.models import Grant
from src.core.cache import get_cache

# Cache TTL for web-scraped pages (7 days)
WEB_CACHE_TTL = 7 * 24 * 3600

# Path to foundation sources config
_SOURCES_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "data", "foundation_sources.json",
)


def _load_sources() -> list:
    """Load foundation grant sources from config file."""
    if not os.path.exists(_SOURCES_PATH):
        return []
    with open(_SOURCES_PATH, "r") as f:
        return json.load(f)


def _fetch_page(url: str, cache_key: str = None) -> Optional[str]:
    """Fetch a web page with caching."""
    cache = get_cache()

    # Check cache first
    if cache_key:
        cached = cache.get_web_page(cache_key)
        if cached:
            return cached

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml",
        }
        time.sleep(1.5)  # Be polite
        r = requests.get(url, headers=headers, timeout=20)
        r.raise_for_status()
        page_html = r.text

        # Cache the page
        if cache_key:
            cache.set_web_page(cache_key, page_html, ttl=WEB_CACHE_TTL)

        return page_html
    except Exception as e:
        print(f"    [web scraper] Error fetching {url}: {e}")
        return None


# ---------------------------------------------------------------------------
# Foundation-specific parsers
# ---------------------------------------------------------------------------

def _parse_ballmer(page_html: str, search_query: str = "") -> list:
    """Parse grants from Ballmer Group grants page.

    Structure:
      <article class="grant">
        <h2 class="entry-title"><a>Org Name</a></h2>
        <div class="grant-amount">$1,250,000 granted from 2021 - 2025</div>
        <div class="entry-content">Description...</div>
        <div class="entry-taxonomies">Region, Impact Area</div>
      </article>
    """
    grants = []
    try:
        tree = html.fromstring(page_html)
    except Exception:
        return []

    # Find all article.grant elements (each is one grant)
    articles = tree.xpath("//article[contains(@class, 'grant')]")

    # Fallback: find h2 headings if no articles found
    if not articles:
        articles = tree.xpath("//h2/parent::*")

    for article in articles:
        # Get org name from h2 > a
        h2s = article.xpath(".//h2")
        if not h2s:
            continue
        links = h2s[0].xpath(".//a")
        name = links[0].text_content().strip() if links else h2s[0].text_content().strip()
        if not name or len(name) < 3:
            continue

        # Get amount from div.grant-amount or following siblings
        amount = None
        year = None

        # Primary: look for div.grant-amount
        amount_divs = article.xpath(".//div[contains(@class, 'grant-amount')]")
        if amount_divs:
            text = amount_divs[0].text_content().strip()
            amount_match = re.search(r'\$([0-9,]+)', text)
            year_match = re.search(r'(\d{4})\s*[-\u2013]\s*(\d{4})', text)
            if not year_match:
                year_match = re.search(r'(\d{4})', text)

            if amount_match:
                try:
                    amount = float(amount_match.group(1).replace(",", ""))
                except ValueError:
                    pass
            if year_match:
                year = year_match.group(1)
        else:
            # Fallback: check following siblings of h2 for dollar amounts
            for elem in h2s[0].xpath("following-sibling::*")[:4]:
                text = elem.text_content().strip()
                amount_match = re.search(r'\$([0-9,]+)', text)
                if amount_match:
                    try:
                        amount = float(amount_match.group(1).replace(",", ""))
                    except ValueError:
                        pass
                    year_match = re.search(r'(\d{4})', text)
                    if year_match:
                        year = year_match.group(1)
                    break

        grants.append(Grant(
            funder_name="Ballmer Group",
            funder_ein="562206524",
            recipient_name=name,
            amount=amount,
            tax_year=year,
            purpose="(from ballmergroup.org/our-grants)",
        ))

    return grants


def _parse_gates(page_html: str, search_query: str = "") -> list:
    """Parse grants from Gates Foundation committed grants page.

    Note: Gates Foundation uses JavaScript rendering, so this may return
    limited results from the initial HTML. Falls back to generic parser.
    """
    return _parse_generic(page_html, funder_name="Bill & Melinda Gates Foundation",
                          funder_ein="562618866")


def _parse_generic(page_html: str, funder_name: str = "", funder_ein: str = "") -> list:
    """Generic parser — tries to extract grants from any foundation page.

    Looks for patterns like:
    - Tables with org names and dollar amounts
    - Lists with dollar amounts
    - Headings followed by dollar amounts
    """
    grants = []
    try:
        tree = html.fromstring(page_html)
    except Exception:
        return []

    # Strategy 1: Look for table rows with dollar amounts
    for row in tree.xpath("//tr"):
        cells = row.xpath(".//td|.//th")
        if len(cells) < 2:
            continue

        # Look for a cell with a dollar amount
        name_cell = None
        amount = None
        for cell in cells:
            text = cell.text_content().strip()
            amount_match = re.search(r'\$([0-9,]+(?:\.\d{2})?)', text)
            if amount_match and amount is None:
                try:
                    amount = float(amount_match.group(1).replace(",", ""))
                except ValueError:
                    pass
            elif len(text) > 3 and not text.startswith("$") and name_cell is None:
                name_cell = text

        if name_cell and amount:
            grants.append(Grant(
                funder_name=funder_name,
                funder_ein=funder_ein,
                recipient_name=name_cell[:200],
                amount=amount,
                purpose=f"(from {funder_name} website)",
            ))

    # Strategy 2: Look for list items with dollar amounts
    if not grants:
        for li in tree.xpath("//li"):
            text = li.text_content().strip()
            amount_match = re.search(r'\$([0-9,]+(?:\.\d{2})?)', text)
            if amount_match:
                # Try to extract org name (text before the dollar sign)
                parts = text.split("$")
                if parts[0].strip():
                    name = re.sub(r'[:\-–—]?\s*$', '', parts[0]).strip()
                    if len(name) > 3:
                        try:
                            amount = float(amount_match.group(1).replace(",", ""))
                        except ValueError:
                            continue
                        grants.append(Grant(
                            funder_name=funder_name,
                            funder_ein=funder_ein,
                            recipient_name=name[:200],
                            amount=amount,
                            purpose=f"(from {funder_name} website)",
                        ))

    return grants


# Parser registry
_PARSERS = {
    "ballmer": _parse_ballmer,
    "gates": _parse_gates,
    "generic": _parse_generic,
}


# ---------------------------------------------------------------------------
# Main scraper function
# ---------------------------------------------------------------------------

def scrape_foundation_grants(comp_org_names: list,
                             diagnostics: list = None) -> dict:
    """Scrape foundation grant pages for grants matching comp orgs.

    Args:
        comp_org_names: List of comparable org names to search for
        diagnostics: Optional list for diagnostic messages

    Returns:
        Dict of funder_ein -> list of matching Grant objects
    """
    diag = diagnostics if diagnostics is not None else []
    sources = _load_sources()

    if not sources:
        diag.append("Web scraper: no foundation sources configured")
        return {}

    print(f"\n  Web Scraper: Checking {len(sources)} foundation grant pages...")
    results = {}  # ein -> [Grant]

    for source in sources:
        name = source["name"]
        ein = source.get("ein", "")
        grants_url = source.get("grants_url", "")
        parser_name = source.get("parser", "generic")
        search_param = source.get("search_param")

        if not grants_url:
            continue

        parser = _PARSERS.get(parser_name, _parse_generic)

        # If the source supports search, search for each comp org
        all_grants = []

        if search_param:
            # Search for comp org names on the foundation's grants page
            for org_name in comp_org_names[:10]:  # Limit to avoid excessive requests
                # Extract key word(s) from org name for search
                search_terms = _extract_search_terms(org_name)
                for term in search_terms[:2]:
                    url = f"{grants_url}?{search_param}={requests.utils.quote(term)}"
                    cache_key = f"web:{ein}:search:{term}"

                    page_html = _fetch_page(url, cache_key=cache_key)
                    if not page_html:
                        continue

                    grants = parser(page_html, search_query=term)
                    all_grants.extend(grants)
        else:
            # Just fetch the main grants page
            cache_key = f"web:{ein}:main"
            page_html = _fetch_page(grants_url, cache_key=cache_key)
            if page_html:
                all_grants = parser(page_html)

        if all_grants:
            # Deduplicate by recipient name
            seen = set()
            unique = []
            for g in all_grants:
                key = (g.recipient_name.lower(), g.amount)
                if key not in seen:
                    seen.add(key)
                    unique.append(g)

            results[ein] = unique
            diag.append(f"Web scraper: {name} → {len(unique)} grants found")
            print(f"    {name}: {len(unique)} grants found")
        else:
            diag.append(f"Web scraper: {name} → 0 grants (page may use JS rendering)")
            print(f"    {name}: 0 grants found")

    return results


def _extract_search_terms(org_name: str) -> list:
    """Extract useful search terms from an org name.

    Returns 1-2 terms that would likely match on a foundation grants page.
    """
    # Split into words, remove very short ones and stop words
    stop = {"the", "of", "and", "for", "in", "a", "an", "to", "inc", "org",
            "association", "corporation", "society"}
    words = re.findall(r"[a-z]+", org_name.lower())
    meaningful = [w for w in words if len(w) >= 3 and w not in stop]

    terms = []
    # First term: most distinctive word (longest)
    if meaningful:
        meaningful.sort(key=len, reverse=True)
        terms.append(meaningful[0])

    # Second term: first two words combined (if different)
    words_clean = [w for w in re.findall(r"[a-z]+", org_name.lower()) if len(w) >= 3 and w not in stop]
    if len(words_clean) >= 2:
        combined = f"{words_clean[0]} {words_clean[1]}"
        if combined != terms[0] if terms else "":
            terms.append(combined)

    return terms


# ---------------------------------------------------------------------------
# Two-hop bridge: donor name -> foundation grant page -> grantee matching
# ---------------------------------------------------------------------------

# Common grant page URL paths to try on foundation websites
_GRANT_PAGE_PATHS = [
    "/grants", "/our-grants", "/grantees", "/committed-grants",
    "/what-we-fund", "/our-grantees", "/grant-recipients",
    "/awarded-grants", "/grants-database", "/grants-awarded",
    "/impact/grants", "/programs/grants", "/giving/grants",
    "/annual-report", "/impact-report",
]


def discover_and_scrape_foundation(foundation_name: str,
                                   comp_org_names: list,
                                   client=None,
                                   diagnostics: list = None) -> dict:
    """Given a foundation name (from a donor page), find their grant page,
    scrape grantees, and match against comp orgs.

    This is the KEY bridge function implementing the second hop:
    donor_page("Simons Foundation") -> simonsfoundation.org/grantees -> match comp orgs

    Args:
        foundation_name: Name of the foundation found on a donor page
        comp_org_names: List of all comp org names to match against
        client: ProPublica client (optional)
        diagnostics: Optional diagnostics list

    Returns:
        Dict with keys:
            'grants': list of Grant objects matched to comp orgs
            'all_grants': list of all grants scraped from the page
            'funder_name': resolved name
            'funder_ein': resolved EIN (if found)
            'source_url': URL of the grant page
    """
    diag = diagnostics if diagnostics is not None else []
    result = {
        'grants': [],
        'all_grants': [],
        'funder_name': foundation_name,
        'funder_ein': '',
        'source_url': '',
    }

    # Step 1: Check foundation_sources.json for pre-configured info
    sources = _load_sources()
    pre_configured = None
    for source in sources:
        if _name_matches(foundation_name, source.get("name", "")):
            pre_configured = source
            break

    # Step 2: Try to resolve via ProPublica (get EIN, maybe website)
    funder_ein = ""
    funder_website = ""

    if pre_configured:
        funder_ein = pre_configured.get("ein", "")
        funder_website = pre_configured.get("website", "")
        result['funder_ein'] = funder_ein

    if client and not funder_ein:
        ein, website = _resolve_foundation_propublica(foundation_name, client)
        if ein:
            funder_ein = ein
            result['funder_ein'] = ein
        if website:
            funder_website = website

    # Step 3: Find the grant page URL
    grants_url = None

    # First: check pre-configured grants_url
    if pre_configured and pre_configured.get("grants_url"):
        grants_url = pre_configured["grants_url"]

    # Second: try to find grant page on foundation website
    if not grants_url and funder_website:
        grants_url = _discover_grant_page(funder_website, foundation_name)

    # Third: use Brave Search to find the foundation grant page directly
    if not grants_url:
        from src.api.brave_search import search_foundation_grants_page, \
            search_foundation_website, is_available as brave_available
        if brave_available():
            # Try searching directly for grants page
            grants_url = search_foundation_grants_page(foundation_name)

            # If that didn't work, find website first then crawl it
            if not grants_url and not funder_website:
                funder_website = search_foundation_website(foundation_name)
                if funder_website:
                    grants_url = _discover_grant_page(funder_website, foundation_name)

    if not grants_url:
        diag.append(f"Foundation bridge: {foundation_name} - no grant page found")
        return result

    result['source_url'] = grants_url

    # Step 4: Scrape the grant page
    cache_key = f"fdn_grants:{funder_ein or foundation_name}:page"
    page_html = _fetch_page(grants_url, cache_key=cache_key)
    if not page_html:
        diag.append(f"Foundation bridge: {foundation_name} - failed to fetch {grants_url}")
        return result

    # Use pre-configured parser or generic
    parser_name = pre_configured.get("parser", "generic") if pre_configured else "generic"
    parser = _PARSERS.get(parser_name, _parse_generic)

    all_grants = parser(page_html, funder_name=foundation_name, funder_ein=funder_ein) \
        if parser_name == "generic" else parser(page_html)

    # Also try generic extraction on the page if specific parser got nothing
    if not all_grants and parser_name != "generic":
        all_grants = _parse_generic(page_html, funder_name=foundation_name,
                                     funder_ein=funder_ein)

    # Additionally: extract org names from the page text even if no dollar amounts
    if not all_grants:
        all_grants = _extract_names_as_grants(page_html, foundation_name, funder_ein)

    result['all_grants'] = all_grants

    if not all_grants:
        diag.append(f"Foundation bridge: {foundation_name} - scraped {grants_url} but 0 grantees extracted")
        return result

    # Step 5: Match grantees against comp org names
    # Use threshold 75 to avoid false positives (e.g. individual researcher names
    # fuzzy-matching against org names like "Madhu Sudan" ~ "Mustang Math")
    from src.core.matching import fuzzy_match
    matched = []
    for grant in all_grants:
        for comp_name in comp_org_names:
            if fuzzy_match(comp_name, grant.recipient_name, threshold=75):
                matched.append(grant)
                break

    result['grants'] = matched
    diag.append(
        f"Foundation bridge: {foundation_name} - {len(all_grants)} grantees scraped, "
        f"{len(matched)} matched comp orgs from {grants_url}"
    )

    return result


def discover_and_scrape_foundations_batch(donor_names_by_org: dict,
                                          comp_org_names: list,
                                          client=None,
                                          diagnostics: list = None,
                                          comp_org_ein_to_name: dict = None) -> list:
    """Process all donor names found across comp org donor pages.

    Args:
        donor_names_by_org: Dict of org_ein -> [donor_name_strings] from donor scraper
        comp_org_names: List of all comp org names
        client: ProPublica client
        diagnostics: Optional diagnostics list
        comp_org_ein_to_name: Dict of org_ein -> org_name for comp orgs

    Returns:
        List of Funder objects created from web-discovered foundations
    """
    from src.core.models import Funder
    from src.core.scoring import score_funder

    diag = diagnostics if diagnostics is not None else []

    # Collect unique foundation names with which comp orgs listed them
    all_donor_names = set()
    # Track which comp orgs listed each foundation on their donor page
    fdn_to_comp_orgs = {}  # foundation_name -> set of comp_org_eins
    for org_ein, names in donor_names_by_org.items():
        for name in names:
            all_donor_names.add(name)
            fdn_to_comp_orgs.setdefault(name, set()).add(org_ein)

    # Filter to likely foundations (not individual donors)
    foundation_names = [
        name for name in all_donor_names
        if _is_likely_foundation(name)
    ]

    print(f"\n  Foundation Bridge: {len(foundation_names)} potential foundations "
          f"from {len(all_donor_names)} total donor names")

    # Build EIN -> org name lookup for comp orgs
    comp_org_lookup = {}  # ein -> name
    for name in comp_org_names:
        comp_org_lookup[name] = name

    funders = []

    for i, fdn_name in enumerate(foundation_names[:30]):  # Limit
        print(f"    [{i+1}/{min(len(foundation_names), 30)}] Investigating {fdn_name}...")

        result = discover_and_scrape_foundation(
            foundation_name=fdn_name,
            comp_org_names=comp_org_names,
            client=client,
            diagnostics=diag,
        )

        if result['grants'] or result['all_grants']:
            # If foundation had 0 grant page matches but was listed on a comp org's
            # donor page, add donor-page-confirmed grants as evidence
            grants_to_target = list(result['grants'])
            if not grants_to_target and fdn_name in fdn_to_comp_orgs:
                # This foundation was listed on a comp org's donor/sponsor page
                # That's evidence they fund orgs in this space
                ein_to_name = comp_org_ein_to_name or {}
                for org_ein in fdn_to_comp_orgs[fdn_name]:
                    comp_name = ein_to_name.get(org_ein, "")
                    if comp_name:
                        grants_to_target.append(Grant(
                            funder_name=result['funder_name'],
                            funder_ein=result['funder_ein'],
                            recipient_name=comp_name,
                            amount=None,
                            purpose="Listed on donor/sponsor page",
                            tax_year=None,
                        ))

            funder = Funder(
                name=result['funder_name'],
                ein=result['funder_ein'],
                grants_to_target=grants_to_target,
                all_grants=result['all_grants'],
                total_giving=sum(g.amount or 0 for g in result['all_grants']) or None,
            )
            score_funder(funder)
            funders.append(funder)
            print(f"      -> {len(result['grants'])} matched grants, "
                  f"{len(result['all_grants'])} total from {result['source_url']}")

    print(f"  Foundation Bridge: {len(funders)} funders created from web discovery")
    return funders


def _resolve_foundation_propublica(name: str, client) -> tuple:
    """Try to find a foundation on ProPublica by name.

    Returns (ein, website) tuple.
    """
    try:
        orgs, _ = client.search(name, c_code="3")
        if not orgs:
            orgs, _ = client.search(name, c_code="")

        for org in orgs[:5]:
            org_name = org.get("name", "")
            # Check if this looks like the right foundation
            if _name_matches(name, org_name):
                ein = str(org.get("ein", ""))
                # Get detail for website
                detail = client.get_organization(ein)
                website = ""
                if detail:
                    org_data = detail.get("organization", {})
                    website = org_data.get("website", "") or ""
                    if website and not website.startswith("http"):
                        website = "https://" + website
                return ein, website
    except Exception:
        pass
    return "", ""


def _discover_grant_page(base_url: str, foundation_name: str) -> Optional[str]:
    """Try to find a grants/grantees page on a foundation website."""
    cache = get_cache()

    for path in _GRANT_PAGE_PATHS:
        url = urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
        cache_key = f"fdn_discover:{foundation_name}:{path}"

        page = _fetch_page(url, cache_key=cache_key)
        if page and len(page) > 500:
            lower = page.lower()
            # Check if the page contains grant-related content
            grant_keywords = [
                "grant", "grantee", "recipient", "awarded",
                "funded", "investment", "commitment",
            ]
            if any(kw in lower for kw in grant_keywords):
                return url

    # Also try sitemap if available
    try:
        robots_url = urljoin(base_url, "/robots.txt")
        robots = _fetch_page(robots_url, cache_key=f"robots:{foundation_name}")
        if robots:
            for line in robots.split("\n"):
                if line.strip().lower().startswith("sitemap:"):
                    sitemap_url = line.split(":", 1)[1].strip()
                    # Could parse sitemap for grant pages, but this is expensive
                    # For now just note it in diagnostics
                    break
    except Exception:
        pass

    return None


def _extract_names_as_grants(page_html: str, funder_name: str,
                              funder_ein: str) -> list:
    """Extract organization names from a page as Grant objects.

    Used when the page lists grantees as text (not in tables with amounts).
    Common for annual report pages that just list supported organizations.
    """
    try:
        tree = html.fromstring(page_html)
    except Exception:
        return []

    grants = []
    seen = set()

    # Strategy 1: List items
    for li in tree.xpath("//li"):
        text = li.text_content().strip()
        if 8 <= len(text) <= 200 and text.count('\n') <= 1:
            # Remove dollar amounts if present
            clean = re.sub(r'\$[\d,]+(?:\.\d{2})?', '', text).strip()
            clean = re.sub(r'^\s*[\-:]\s*', '', clean).strip()
            if clean and len(clean) >= 8 and clean.lower() not in seen:
                # Filter out nav elements and junk
                if not _looks_like_org_or_grantee(clean):
                    continue
                # Extract amount if present
                amount = None
                amt_match = re.search(r'\$([0-9,]+(?:\.\d{2})?)', text)
                if amt_match:
                    try:
                        amount = float(amt_match.group(1).replace(",", ""))
                    except ValueError:
                        pass
                seen.add(clean.lower())
                grants.append(Grant(
                    funder_name=funder_name,
                    funder_ein=funder_ein,
                    recipient_name=clean[:200],
                    amount=amount,
                    purpose=f"(from {funder_name} website)",
                ))

    # Strategy 2: Table rows
    for row in tree.xpath("//tr"):
        cells = row.xpath(".//td")
        if cells:
            first_cell = cells[0].text_content().strip()
            if 8 <= len(first_cell) <= 200 and first_cell.lower() not in seen:
                if not _looks_like_org_or_grantee(first_cell):
                    continue
                amount = None
                for cell in cells[1:]:
                    amt_match = re.search(r'\$([0-9,]+(?:\.\d{2})?)',
                                          cell.text_content())
                    if amt_match:
                        try:
                            amount = float(amt_match.group(1).replace(",", ""))
                        except ValueError:
                            pass
                        break
                seen.add(first_cell.lower())
                grants.append(Grant(
                    funder_name=funder_name,
                    funder_ein=funder_ein,
                    recipient_name=first_cell[:200],
                    amount=amount,
                    purpose=f"(from {funder_name} website)",
                ))

    # Strategy 3: Paragraph tags in list-like containers (e.g. Simons Foundation)
    # Some pages list grantees as <p> tags inside divs
    for p in tree.xpath("//div[contains(@class, 'list') or contains(@class, 'grantee') "
                        "or contains(@class, 'recipient')]//p"):
        text = p.text_content().strip()
        # Remove trailing asterisks and other markers
        text = re.sub(r'[*†‡§]+$', '', text).strip()
        if 8 <= len(text) <= 200 and text.lower() not in seen:
            if not _looks_like_org_or_grantee(text):
                continue
            seen.add(text.lower())
            grants.append(Grant(
                funder_name=funder_name,
                funder_ein=funder_ein,
                recipient_name=text[:200],
                amount=None,
                purpose=f"(from {funder_name} website)",
            ))

    return grants


def _looks_like_org_or_grantee(text: str) -> bool:
    """Check if text looks like an organization name (not a nav element or junk)."""
    lower = text.lower().strip()

    # Reject very short text (likely nav elements)
    if len(lower) < 8:
        return False

    # Reject single words (nav elements like "Help", "Shop", "Music")
    if " " not in lower:
        return False

    # Reject common nav/UI elements
    nav_words = {
        "help", "shop", "store", "menu", "home", "about", "contact",
        "search", "login", "sign in", "subscribe", "donate", "apply",
        "news", "blog", "events", "careers", "press", "media",
        "privacy", "terms", "faq", "resources", "gallery",
        "locations", "music", "talk", "data", "team", "mission",
        "history", "read", "community portal", "advanced search",
        "grants & funding", "hhs agencies", "foia", "leadership",
        "museum map", "about the museum", "student scholarships",
        "story times", "science and health",
    }
    if lower in nav_words:
        return False

    # Reject text starting with common non-org words
    first_word = lower.split()[0]
    if first_word in {"help", "shop", "store", "menu", "home", "about",
                       "contact", "search", "login", "subscribe", "our",
                       "the", "a", "an", "all", "see", "view", "show",
                       "more", "less", "back", "next", "previous",
                       "skip", "jump", "go", "click", "tap",
                       "share", "print", "download", "save",
                       "international", "office"}:
        return False

    # Reject sentences
    sentence_markers = [
        " is ", " are ", " was ", " were ", " has ", " have ",
        " will ", " can ", " the ", " your ", " our ", " their ",
        " we ", " you ", " they ", " it ", "! ", "? ",
    ]
    if any(m in lower for m in sentence_markers):
        return False

    # Must start with a capital letter (proper noun)
    if text[0].islower():
        return False

    # At least 2 words with the majority capitalized
    words = text.split()
    if len(words) < 2:
        return False
    capitalized = sum(1 for w in words if w[0].isupper())
    if capitalized < len(words) * 0.5:
        return False

    return True


def _name_matches(name1: str, name2: str) -> bool:
    """Quick check if two foundation names likely refer to the same entity."""
    n1 = re.sub(r'[^\w\s]', '', name1.lower()).strip()
    n2 = re.sub(r'[^\w\s]', '', name2.lower()).strip()

    # Exact match after normalization
    if n1 == n2:
        return True

    # One contains the other
    if n1 in n2 or n2 in n1:
        return True

    # Check if key words match (drop common stop words)
    stop = {"the", "a", "an", "of", "for", "and", "in", "inc", "org"}
    words1 = {w for w in n1.split() if w not in stop and len(w) > 2}
    words2 = {w for w in n2.split() if w not in stop and len(w) > 2}

    if words1 and words2:
        overlap = len(words1 & words2)
        if overlap >= min(len(words1), len(words2)) * 0.7:
            return True

    return False


def _is_likely_foundation(name: str) -> bool:
    """Check if a donor name is likely a foundation (vs individual person or junk text).

    Must be strict to avoid treating page content, UI elements, statistics,
    and other garbage text as foundation names.
    """
    lower = name.lower()

    # Reject obvious non-foundation text
    if len(name) < 5 or len(name) > 100:
        return False

    # Reject text with colons followed by numbers (data labels)
    if re.search(r':\s*[\d.]', name):
        return False

    # Reject text with decimal numbers (statistics)
    if re.search(r'\d+\.\d+', name):
        return False

    # Reject social media / UI text
    junk_patterns = [
        "share on", "share by", "share via", "follow us",
        "sign up", "click here", "learn more", "read more",
        "see more", "view all", "join us", "be the first",
        "no sponsors", "coming soon", "percent", " gni",
        " gdp", "honor roll", "estimated ", "mean score",
        "board of directors", "privacy policy", "terms of",
        "cookie", "copyright", "all rights reserved",
        "powered by", "built with", "designed by",
        "back to top", "scroll to", "skip to",
        "loading", "please wait", "subscribe",
        "faq", "menu", "navigation",
    ]
    if any(pat in lower for pat in junk_patterns):
        return False

    # Reject sentences (contain common verbs/articles)
    sentence_words = [
        " is ", " are ", " was ", " were ", " has ", " have ",
        " will ", " can ", " the ", " this ", " that ",
        " your ", " our ", " their ", " we ", " you ",
        " they ", " it ", " its ", " do ", " does ",
        " guide ", " help ", " make ", " take ",
        "! ", "? ",
    ]
    if any(w in lower for w in sentence_words):
        return False

    # Reject text starting with lowercase (not a proper noun)
    if name[0].islower():
        return False

    # Strong indicators of GRANT-MAKING institutions specifically
    # Note: "school", "museum", "library", "center" are NOT included because
    # those are typically grant RECIPIENTS, not grant MAKERS
    funder_indicators = [
        "foundation", "fund", "trust", "endowment", "charitable",
        "philanthropi", "group", "association", "institute",
        "society", "council", "corporation", "inc.", "llc",
        "ltd", "company", "corp",
        "community foundation", "united way", "rotary",
    ]
    if any(ind in lower for ind in funder_indicators):
        return True

    # Reject anything else — we need positive funder signals
    return False


