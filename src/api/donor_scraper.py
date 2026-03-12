"""
Nonprofit donor/supporter page scraper.

For each comparable org, tries to find their website's donor page and
extract foundation/funder names. This is inherently fragile since every
nonprofit website is different, but it can surface data not in 990 filings.

Key strategies:
1. URL path guessing (/donors, /partners, /supporters, etc.)
2. Homepage link crawling (follow links containing donor-related keywords)
3. Logo/image extraction (alt text on sponsor logos, anchor hrefs)
4. Tier-based name extraction (heading tiers followed by name lists)
5. Comma-separated paragraph parsing
"""

import re
import time
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
from lxml import html

from src.core.cache import get_cache

# Common paths where nonprofits list their donors/supporters
_DONOR_PATHS = [
    "/donors", "/supporters", "/our-supporters", "/partners",
    "/funders", "/our-funders", "/our-donors", "/our-partners",
    "/sponsors", "/our-sponsors", "/corporate-sponsors",
    "/about/donors", "/about/supporters", "/about/partners",
    "/about/funders", "/about/sponsors",
    "/support/donors", "/giving/donors",
    "/annual-report", "/impact-report", "/financials",
    "/community-partners", "/funding-partners",
    "/who-supports-us", "/acknowledgements", "/thank-you",
]

# Words that indicate a section heading for donor tiers
_TIER_PATTERNS = [
    r'\$\s*[\d,]+\s*[\+\-\u2013\u2014]',  # $100,000+
    r'[\d,]+\s*[\+\-\u2013\u2014]\s*level',  # 100,000+ level
    r'platinum|gold|silver|bronze|diamond',
    r'leadership|founding|sustaining|major',
    r'champion|visionary|benefactor|patron',
    r'premier|presenting|title\s+sponsor',
]

_TIER_RE = re.compile('|'.join(_TIER_PATTERNS), re.IGNORECASE)

# Keywords that indicate a donor/supporter page (for page validation)
_DONOR_PAGE_KEYWORDS = [
    "donor", "supporter", "funder", "partner", "sponsor",
    "thank", "grateful", "acknowledge", "contributor",
    "underwriter", "benefactor", "philanthropi",
    "foundations", "corporations",
]

# Navigation words to skip
_NAV_WORDS = {
    "home", "about", "about us", "contact", "contact us", "donate",
    "blog", "news", "events", "programs", "services", "careers",
    "login", "sign in", "menu", "close", "search", "subscribe",
    "privacy", "terms", "sitemap", "back to top", "help",
    "shop", "store", "faq", "resources", "gallery", "media",
    "team", "staff", "board", "history", "mission", "vision",
    "press", "publications", "newsletter", "calendar", "map",
    "directions", "volunteer", "membership", "join", "register",
    "share", "print", "download", "apply", "submit", "learn more",
    "read more", "see more", "view all", "show more", "load more",
    "next", "previous", "back", "forward", "skip", "data",
    "locations", "music", "talk", "community portal",
}


def _fetch_page(url: str, cache_key: str = None) -> Optional[str]:
    """Fetch a web page with caching and error handling."""
    cache = get_cache()

    if cache_key:
        cached = cache.get_web_page(cache_key, max_age_hours=720)  # 30 days
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
        r = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
        r.raise_for_status()
        page_html = r.text

        if cache_key:
            cache.set_web_page(cache_key, page_html)

        return page_html
    except Exception:
        return None


def _find_donor_page(base_url: str, cache_key_prefix: str) -> Optional[tuple]:
    """Try to find the donor/supporter page on a nonprofit's website.

    Returns (page_html, page_url) or None.
    """
    # First, try common paths
    for path in _DONOR_PATHS:
        url = urljoin(base_url, path)
        page = _fetch_page(url, cache_key=f"{cache_key_prefix}:{path}")
        if page and len(page) > 500:
            # Verify it's actually about donors (not a 404 page)
            lower = page.lower()
            if any(word in lower for word in _DONOR_PAGE_KEYWORDS):
                return page, url

    # Try parsing the homepage for links to donor pages
    homepage = _fetch_page(base_url, cache_key=f"{cache_key_prefix}:home")
    if homepage:
        try:
            tree = html.fromstring(homepage)
            for link in tree.xpath("//a"):
                href = link.get("href", "")
                text = link.text_content().lower().strip()

                # Check link text OR href for donor-related keywords
                link_matches = any(
                    word in text for word in _DONOR_PAGE_KEYWORDS
                )
                href_matches = any(
                    word in href.lower() for word in [
                        "donor", "supporter", "funder", "partner",
                        "sponsor", "annual-report", "impact",
                    ]
                )

                if link_matches or href_matches:
                    full_url = urljoin(base_url, href)
                    # Only follow links on the same domain
                    if urlparse(full_url).netloc == urlparse(base_url).netloc:
                        page = _fetch_page(full_url, cache_key=f"{cache_key_prefix}:found:{href[:50]}")
                        if page and len(page) > 500:
                            return page, full_url
        except Exception:
            pass

    return None


def _extract_donor_names(page_html: str) -> list:
    """Extract potential donor/funder names from a donor page.

    Uses multiple strategies to handle different page layouts:
    - List items (<li>)
    - Logo images with alt text
    - Linked logos (anchor href domains)
    - Heading-based tiers
    - Comma-separated paragraphs
    - Generic div/section content blocks

    Returns list of strings that look like organization names.
    """
    try:
        tree = html.fromstring(page_html)
    except Exception:
        return []

    names = []

    # Strategy 1: Logo-based extraction (most reliable for partner pages)
    # Many donor pages show logos as <a href="..."><img alt="Org Name"></a>
    names.extend(_extract_from_logos(tree))

    # Strategy 2: List items that seem like org names
    names.extend(_extract_from_lists(tree))

    # Strategy 3: Heading-based tiers ($100,000+ level, etc.)
    names.extend(_extract_from_tiers(tree))

    # Strategy 4: Comma-separated paragraphs
    names.extend(_extract_from_paragraphs(tree))

    # Strategy 5: Generic content blocks (div, section, article with short text items)
    names.extend(_extract_from_content_blocks(tree))

    # Deduplicate preserving order, clean up
    seen = set()
    unique = []
    for name in names:
        cleaned = _clean_name(name)
        if cleaned and len(cleaned) >= 4 and cleaned.lower() not in seen:
            seen.add(cleaned.lower())
            unique.append(cleaned)

    return unique


def _extract_from_logos(tree) -> list:
    """Extract donor names from logo images and their anchor links.

    Handles patterns like:
    - <a href="https://simonsfoundation.org"><img alt="Simons Foundation"></a>
    - <img alt="Norcliffe Foundation" src="...">
    - <a href="https://foundation.org">Foundation Name</a> (linked text)
    """
    names = []

    # Find all images on the page
    for img in tree.xpath("//img"):
        alt = (img.get("alt") or "").strip()
        title = (img.get("title") or "").strip()

        # Use alt text if it looks like an org name
        candidate = alt or title
        if candidate and 4 <= len(candidate) <= 150:
            # Skip generic alt text
            if candidate.lower() not in {"logo", "image", "photo", "banner",
                                          "icon", "placeholder", "loading"}:
                names.append(candidate)

        # Check parent anchor for domain info
        parent = img.getparent()
        if parent is not None and parent.tag == "a":
            href = parent.get("href", "")
            if href.startswith("http"):
                domain = urlparse(href).netloc.lower()
                # Skip social media and common non-donor links
                if not any(skip in domain for skip in [
                    "facebook", "twitter", "instagram", "linkedin",
                    "youtube", "google", "apple", "amazon",
                    "w3.org", "wordpress", "squarespace",
                ]):
                    # Extract org-like name from domain
                    domain_name = _domain_to_name(domain)
                    if domain_name and domain_name not in [n.lower() for n in names]:
                        names.append(domain_name)

    # Also check for linked text (non-image anchors in donor sections)
    for anchor in tree.xpath("//a[not(.//img)]"):
        href = anchor.get("href", "")
        text = anchor.text_content().strip()

        # Only consider external links with short text
        if (href.startswith("http") and 4 <= len(text) <= 100
                and text.lower() not in _NAV_WORDS):
            domain = urlparse(href).netloc.lower()
            page_domain = ""  # We don't know the page domain here
            if domain and domain != page_domain:
                # This is an external link with descriptive text - likely a donor
                if _looks_like_org_name(text) or _looks_like_donor_context(anchor):
                    names.append(text)

    return names


def _extract_from_lists(tree) -> list:
    """Extract donor names from <li> elements."""
    names = []

    for li in tree.xpath("//li"):
        text = li.text_content().strip()
        if len(text) < 4 or len(text) > 200:
            continue
        if text.count('\n') > 1 or len(text.split()) > 15:
            continue
        if text.lower() in _NAV_WORDS:
            continue

        # On a donor page, any clean list item is likely a donor name
        # We use a broader check than just org keywords
        if _looks_like_org_name(text) or _is_plausible_donor_name(text):
            names.append(text)

    return names


def _extract_from_tiers(tree) -> list:
    """Extract names from tier-based donor sections.

    Looks for headings like "$100,000+" or "Platinum Sponsors"
    followed by lists of names.
    """
    names = []

    headings = tree.xpath("//h1|//h2|//h3|//h4|//h5|//h6|//strong|//b")
    for heading in headings:
        heading_text = heading.text_content().strip()
        if _TIER_RE.search(heading_text):
            parent = heading.getparent()
            if parent is not None:
                following = heading.xpath("following-sibling::*")
                for elem in following[:15]:
                    # Check list items
                    for li in elem.xpath(".//li") or [elem]:
                        text = li.text_content().strip()
                        if 4 <= len(text) <= 200 and text.count('\n') <= 1:
                            names.append(text)
                    # Stop if we hit another tier heading
                    if _TIER_RE.search(elem.text_content().strip()):
                        break

    return names


def _extract_from_paragraphs(tree) -> list:
    """Extract donor names from comma-separated paragraphs."""
    names = []

    for p in tree.xpath("//p"):
        text = p.text_content().strip()
        if len(text) > 50 and text.count(",") >= 3:
            parts = [part.strip() for part in text.split(",")]
            for part in parts:
                if 4 <= len(part) <= 100:
                    # On a donor page, comma-separated items are likely donor names
                    if _looks_like_org_name(part) or _is_plausible_donor_name(part):
                        names.append(part)

    return names


def _extract_from_content_blocks(tree) -> list:
    """Extract donor names from div/section content blocks.

    Some pages list donors in <div> elements or <p> tags
    without using <li> or table structures.
    """
    names = []

    # Look for sections/divs with donor-related class names or IDs
    for container in tree.xpath(
        "//*[contains(@class, 'donor') or contains(@class, 'sponsor') "
        "or contains(@class, 'partner') or contains(@class, 'funder') "
        "or contains(@class, 'supporter') or contains(@id, 'donor') "
        "or contains(@id, 'sponsor') or contains(@id, 'partner')]"
    ):
        # Extract text from child elements
        for child in container.xpath(".//p|.//div|.//span|.//h3|.//h4"):
            text = child.text_content().strip()
            if 4 <= len(text) <= 150 and text.count('\n') <= 1:
                if text.lower() not in _NAV_WORDS:
                    names.append(text)

    return names


def _looks_like_org_name(text: str) -> bool:
    """Heuristic: does this text look like an organization name?"""
    lower = text.lower()
    org_indicators = [
        "foundation", "fund", "trust", "group", "association",
        "institute", "society", "council", "corporation", "inc",
        "llc", "ltd", "philanthropi", "endowment", "charitable",
        "community", "united way", "rotary", "kiwanis", "lions",
        "family", "memorial", "charitable trust",
        # Additional indicators for foundations
        "grant", "giving", "initiative", "program",
        "arts", "science", "education", "health",
    ]
    return any(ind in lower for ind in org_indicators)


def _is_plausible_donor_name(text: str) -> bool:
    """Broader check: could this text be a donor name on a donor page?

    On a confirmed donor page, we can be more lenient about what
    constitutes a name. Multi-word text that looks like a proper noun
    is likely a donor name.
    """
    # Must be multi-word and title-cased or all-caps
    words = text.split()
    if len(words) < 2 or len(words) > 8:
        return False

    # Must be short enough to be a name (not a paragraph)
    if len(text) > 80:
        return False

    lower = text.lower()

    # Skip sentences (contain common verbs/articles mid-text)
    sentence_indicators = [
        " is ", " are ", " was ", " were ", " has ", " have ",
        " will ", " can ", " the ", " this ", " that ",
        " with ", " from ", " your ", " our ", " their ",
        " we ", " you ", " they ", " it ", " its ",
        " be ", " been ", " being ", " do ", " does ",
        ". ", "! ", "? ",
    ]
    if any(ind in lower for ind in sentence_indicators):
        return False

    # Skip social media / UI action text
    ui_patterns = [
        "share on ", "share by ", "share via ",
        "follow us", "connect with", "sign up",
        "click here", "learn more", "read more",
        "see more", "view all", "no sponsors",
        "be the first", "join us", "coming soon",
        "estimated ", "mean score", "honor roll",
        "top 5%", "top 10%",
    ]
    if any(pat in lower for pat in ui_patterns):
        return False

    # Skip statistics / data labels (contain colons followed by numbers)
    if re.search(r':\s*\d', text):
        return False

    # Skip text with "percent" or "GNI" (data labels from charts)
    if any(word in lower for word in ["percent", "gni", "gdp", "avg", "median"]):
        return False

    # Skip text that looks like a heading/label with numbers
    if re.search(r'\d+\.\d+', text):
        return False

    # Skip text starting with common non-name words
    first_word = lower.split()[0] if words else ""
    if first_word in {"no", "be", "join", "estimated", "mean", "our",
                       "all", "see", "view", "show", "more", "also",
                       "please", "click", "visit", "check"}:
        return False

    # Check if it looks like a proper noun (most words capitalized)
    capitalized = sum(1 for w in words if w[0].isupper() or w[0].isdigit())
    return capitalized >= len(words) * 0.6


def _looks_like_donor_context(elem) -> bool:
    """Check if an element's parent/ancestors suggest donor context."""
    for ancestor in elem.iterancestors():
        cls = (ancestor.get("class") or "").lower()
        id_attr = (ancestor.get("id") or "").lower()
        combined = cls + " " + id_attr
        if any(kw in combined for kw in [
            "donor", "sponsor", "partner", "funder", "supporter",
            "corporate", "foundation",
        ]):
            return True
        # Stop traversing after a few levels
        if ancestor.tag in ("body", "html"):
            break
    return False


def _domain_to_name(domain: str) -> Optional[str]:
    """Convert a domain name to an org-like name.

    Examples:
        "simonsfoundation.org" -> "Simons Foundation"
        "www.norcliffe.org" -> "Norcliffe"
    """
    # Remove www prefix and TLD
    domain = domain.lower()
    if domain.startswith("www."):
        domain = domain[4:]

    # Remove TLD
    parts = domain.rsplit(".", 1)
    if len(parts) < 2:
        return None
    name_part = parts[0]

    # Skip very short or numeric domains
    if len(name_part) < 3 or name_part.isdigit():
        return None

    # Try to split camelCase or compound words
    # "simonsfoundation" -> "simons foundation"
    # Insert space before common org suffixes
    suffixes = ["foundation", "trust", "fund", "group", "institute",
                "society", "council", "charitable"]
    for suffix in suffixes:
        if name_part.endswith(suffix) and len(name_part) > len(suffix):
            prefix = name_part[:-len(suffix)]
            return f"{prefix.title()} {suffix.title()}"

    # Just title-case the domain name
    return name_part.replace("-", " ").title()


def _clean_name(text: str) -> str:
    """Clean up extracted name text."""
    # Remove leading bullets, numbers, asterisks
    text = re.sub(r'^[\s\-*\u2022\u00b7\u25ba\u25aa\u25b8\u2192\d.)\]]+', '', text)
    # Remove trailing whitespace and punctuation
    text = text.strip().rstrip(':;,.')
    # Collapse whitespace
    text = re.sub(r'\s+', ' ', text)
    # Remove very common prefixes that aren't part of the name
    text = re.sub(r'^(?:Sponsored by|Funded by|Supported by|Thanks to)\s*:?\s*',
                  '', text, flags=re.IGNORECASE)
    return text


def scrape_donor_pages(comp_orgs: list, client=None,
                       diagnostics: list = None) -> dict:
    """Scrape donor pages from comparable org websites.

    Args:
        comp_orgs: List of Organization objects from Layer 1
        client: ProPublica client (for fetching org details with website URLs)
        diagnostics: Optional list for diagnostic messages

    Returns:
        Dict of comp_org_ein -> list of donor name strings found
    """
    from src.api.website_resolver import discover_website

    diag = diagnostics if diagnostics is not None else []
    results = {}  # ein -> [donor_name_strings]

    print(f"\n  Donor Page Scraper: Checking {len(comp_orgs)} org websites...")

    checked = 0
    found = 0

    for org in comp_orgs[:20]:  # Check top 20 comp orgs
        checked += 1

        # Try to get the org's website (multiple strategies)
        website = org.website if hasattr(org, 'website') and org.website else None

        # Fall back to website resolver (ProPublica detail + 990 XML + domain guessing)
        if not website:
            website = discover_website(
                org_name=org.name,
                org_ein=org.ein,
                state=org.state,
                client=client,
            )
            # Store it on the org for future use
            if website and hasattr(org, 'website'):
                org.website = website

        if not website:
            diag.append(f"Donor scraper: {org.name} - no website found")
            continue

        if not website.startswith("http"):
            website = "https://" + website

        # Ensure we're not scraping government or very large sites
        domain = urlparse(website).netloc.lower()
        if any(skip in domain for skip in [".gov", "facebook.com", "twitter.com",
                                            "linkedin.com", "youtube.com"]):
            continue

        cache_prefix = f"donor:{org.ein}"

        # Find the donor page
        donor_result = _find_donor_page(website, cache_prefix)
        if not donor_result:
            diag.append(f"Donor scraper: {org.name} ({website}) - no donor page found")
            continue

        page_html, page_url = donor_result

        # Extract donor names
        donor_names = _extract_donor_names(page_html)
        if donor_names:
            results[org.ein] = donor_names
            found += 1
            diag.append(f"Donor scraper: {org.name} -> {len(donor_names)} donors on {page_url}")
            print(f"    {org.name}: {len(donor_names)} donors found on {page_url}")
        else:
            diag.append(f"Donor scraper: {org.name} -> donor page found but 0 names extracted ({page_url})")
            print(f"    {org.name}: donor page found but 0 names extracted")

    print(f"    Checked {checked} orgs, found donor pages for {found}")
    return results
