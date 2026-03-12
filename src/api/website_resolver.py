"""
Website resolver for nonprofit organizations.

Discovers nonprofit website URLs using multiple strategies:
1. ProPublica detail data (org.website field)
2. IRS 990 XML filing (WebsiteAddressTxt element)
3. Brave Search API (web search for org name + website)
4. Cache results in SQLite for reuse
"""

from typing import Optional

from src.core.cache import get_cache


def discover_website(org_name: str, org_ein: str = "",
                     state: str = "", client=None,
                     xml_data: bytes = None) -> Optional[str]:
    """Try multiple strategies to find an org's website URL.

    Args:
        org_name: Organization name
        org_ein: EIN (for cache key and ProPublica lookup)
        state: State code (for search disambiguation)
        client: ProPublica client (optional, for detail lookup)
        xml_data: Raw XML filing bytes (optional, for WebsiteAddressTxt extraction)

    Returns:
        Website URL string or None
    """
    cache = get_cache()
    cache_key = f"website:{org_ein or org_name}"

    # Check cache first
    cached = cache.get_web_page(cache_key, max_age_hours=720)  # 30 days
    if cached:
        return cached if cached != "NONE" else None

    url = None

    # Strategy 1: ProPublica detail
    if client and org_ein:
        url = _try_propublica(client, org_ein)

    # Strategy 2: Extract from 990 XML filing
    if not url and xml_data:
        url = _extract_website_from_xml(xml_data)

    # Strategy 3: Brave Search API
    if not url:
        from src.api.brave_search import search_website, is_available
        if is_available():
            url = search_website(org_name, state=state)

    # Cache result (even failures to avoid repeated lookups)
    cache.set_web_page(cache_key, url or "NONE")

    return url


def _try_propublica(client, ein: str) -> Optional[str]:
    """Try to get website from ProPublica organization detail."""
    try:
        detail = client.get_organization(ein.replace("-", ""))
        if detail:
            org_data = detail.get("organization", {})
            website = org_data.get("website")
            if website:
                if not website.startswith("http"):
                    website = "https://" + website
                return website
    except Exception:
        pass
    return None


def _extract_website_from_xml(xml_data: bytes) -> Optional[str]:
    """Extract WebsiteAddressTxt from a 990 XML filing."""
    try:
        from lxml import etree
        root = etree.fromstring(xml_data)

        # Try multiple XPaths for the website field
        ns = {"irs": "http://www.irs.gov/efile"}

        xpaths = [
            "//irs:WebsiteAddressTxt",
            "//irs:WebsiteAddress",
            "//irs:WebSiteURL",
            "//irs:Filer/irs:WebsiteAddressTxt",
        ]

        for xpath in xpaths:
            elems = root.xpath(xpath, namespaces=ns)
            if elems and elems[0].text:
                url = elems[0].text.strip()
                if url and url.upper() != "N/A":
                    if not url.startswith("http"):
                        url = "https://" + url
                    return url
    except Exception:
        pass
    return None


def resolve_websites_batch(orgs: list, client=None) -> int:
    """Resolve websites for a batch of Organization objects.

    Modifies orgs in-place, setting org.website where found.
    Returns count of newly resolved websites.
    """
    resolved = 0

    for org in orgs:
        if org.website:
            continue

        # Try to get XML for WebsiteAddressTxt extraction
        xml_data = None
        if client and org.latest_filing and org.latest_filing.object_id:
            xml_data = client.download_xml(org.latest_filing.object_id)

        url = discover_website(
            org_name=org.name,
            org_ein=org.ein,
            state=org.state,
            client=client,
            xml_data=xml_data,
        )

        if url:
            org.website = url
            resolved += 1
            print(f"    {org.name}: {url}")

    return resolved
