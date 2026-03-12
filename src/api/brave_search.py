"""
Brave Search API client for nonprofit prospect research.

Used for:
1. Finding nonprofit websites from org names
2. Finding foundation grant pages
3. General web search for funder discovery

Requires BRAVE_API_KEY environment variable.
Free tier: $5 monthly credits (~1000 queries).
"""

import os
import time
from typing import Optional

import requests

from src.core.cache import get_cache

BRAVE_API_URL = "https://api.search.brave.com/res/v1/web/search"


def is_available() -> bool:
    """Check if Brave Search API key is configured."""
    return bool(os.environ.get("BRAVE_API_KEY"))


def search(query: str, count: int = 5) -> list:
    """Run a Brave web search and return results.

    Args:
        query: Search query string
        count: Number of results (max 20)

    Returns:
        List of dicts with 'title', 'url', 'description' keys.
        Returns empty list if API key not set or search fails.
    """
    api_key = os.environ.get("BRAVE_API_KEY")
    if not api_key:
        return []

    cache = get_cache()
    cache_key = f"brave:{query}:{count}"

    # Check cache (24 hour TTL for search results)
    cached = cache.get_web_page(cache_key, max_age_hours=24)
    if cached:
        import json
        try:
            return json.loads(cached)
        except Exception:
            pass

    try:
        time.sleep(0.5)  # Rate limiting
        headers = {
            "X-Subscription-Token": api_key,
            "Accept": "application/json",
        }
        params = {
            "q": query,
            "count": min(count, 20),
        }

        r = requests.get(BRAVE_API_URL, headers=headers, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()

        results = []
        web_results = data.get("web", {}).get("results", [])
        for item in web_results:
            results.append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "description": item.get("description", ""),
            })

        # Cache results
        import json
        cache.set_web_page(cache_key, json.dumps(results))

        return results

    except Exception as e:
        print(f"    [Brave Search] Error: {e}")
        return []


def search_website(org_name: str, state: str = "") -> Optional[str]:
    """Find an organization's official website via web search.

    Args:
        org_name: Organization name
        state: State code for disambiguation

    Returns:
        Website URL or None
    """
    query = f"{org_name} official website"
    if state:
        query += f" {state}"

    results = search(query, count=5)

    if not results:
        return None

    # Look for the most likely official website
    # Skip social media, directories, and aggregator sites
    skip_domains = {
        "facebook.com", "twitter.com", "instagram.com", "linkedin.com",
        "youtube.com", "yelp.com", "bbb.org", "guidestar.org",
        "propublica.org", "charitynavigator.org", "candid.org",
        "greatnonprofits.org", "nonprofitexplorer.org",
        "wikipedia.org", "wikidata.org",
    }

    for result in results:
        url = result.get("url", "")
        if not url:
            continue

        from urllib.parse import urlparse
        domain = urlparse(url).netloc.lower()

        # Skip known aggregator/social sites
        if any(skip in domain for skip in skip_domains):
            continue

        # Skip .gov sites unless the org name contains government-related words
        if ".gov" in domain and "government" not in org_name.lower():
            continue

        # This is likely the org's actual website
        # Return just the base URL (scheme + domain)
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}"

    return None


def search_foundation_grants_page(foundation_name: str) -> Optional[str]:
    """Find a foundation's grants/grantees page via web search.

    Args:
        foundation_name: Foundation name

    Returns:
        URL of grants page or None
    """
    query = f"{foundation_name} grants grantees awarded"
    results = search(query, count=5)

    if not results:
        return None

    # Look for pages that are about grants/grantees
    grant_keywords = ["grant", "grantee", "awarded", "funded", "recipient",
                      "commitment", "investment", "annual-report"]

    for result in results:
        url = result.get("url", "").lower()
        title = result.get("title", "").lower()
        desc = result.get("description", "").lower()

        # Check if this result is about grants
        combined = url + " " + title + " " + desc
        if any(kw in combined for kw in grant_keywords):
            return result["url"]

    # Fallback: return first result if it's on the foundation's own site
    if results:
        return results[0]["url"]

    return None


def search_foundation_website(foundation_name: str) -> Optional[str]:
    """Find a foundation's official website via web search.

    Args:
        foundation_name: Foundation name

    Returns:
        Website URL or None
    """
    return search_website(foundation_name)
