"""
ProPublica Nonprofit Explorer API v2 client.
Handles search, org detail, XML filing download, and pagination.
"""

import time
from typing import Optional

import requests

from src.core.models import Filing, Officer, Organization

PROPUBLICA_BASE = "https://projects.propublica.org/nonprofits/api/v2"
DEFAULT_DELAY = 0.5


class ProPublicaClient:
    """Wrapper for ProPublica Nonprofit Explorer API v2."""

    def __init__(self, delay: float = DEFAULT_DELAY, cache=None):
        self.base = PROPUBLICA_BASE
        self.delay = delay
        self.cache = cache
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "990-Prospect-Explorer/2.0 (nonprofit research tool)"
        })

    def _get(self, path: str, params: dict = None) -> Optional[dict]:
        url = f"{self.base}{path}"
        time.sleep(self.delay)
        try:
            r = self.session.get(url, params=params, timeout=15)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.RequestException as e:
            print(f"  [API error] {e}")
            return None

    def search(self, query: str, state: str = None, ntee: str = None,
               c_code: str = "3", page: int = 0) -> tuple:
        """Search for nonprofits. Default filters to 501(c)(3).
        Retries without ntee if 404."""
        params = {"q": query, "page": page}
        if state:
            params["state[id]"] = state
        if ntee:
            params["ntee[id]"] = ntee
        if c_code:
            params["c_code[id]"] = c_code

        data = self._get("/search.json", params)
        # Retry cascade: some filter combos return 404
        if not data and ntee:
            params.pop("ntee[id]", None)
            data = self._get("/search.json", params)
        if not data and state:
            params.pop("state[id]", None)
            data = self._get("/search.json", params)
        if not data:
            return [], 0

        orgs = data.get("organizations", [])
        total = data.get("total_results", 0)
        return orgs, total

    def search_all_pages(self, query: str, state: str = None, ntee: str = None,
                         max_pages: int = 3) -> tuple:
        """Search across multiple pages and deduplicate."""
        all_orgs = {}
        for page in range(max_pages):
            orgs, total = self.search(query, state=state, ntee=ntee, page=page)
            if not orgs:
                break
            for o in orgs:
                ein = str(o.get("ein", ""))
                if ein and ein not in all_orgs:
                    all_orgs[ein] = o
            if len(all_orgs) >= total or len(orgs) < 25:
                break
        return list(all_orgs.values()), len(all_orgs)

    def get_organization(self, ein: str) -> Optional[dict]:
        """Get full organization detail including filings."""
        ein = ein.replace("-", "")

        if self.cache:
            cached = self.cache.get_org(ein)
            if cached:
                return cached

        data = self._get(f"/organizations/{ein}.json")
        if data and self.cache:
            self.cache.store_org(ein, data)
        return data

    def get_xml_url(self, filing_data: dict) -> Optional[str]:
        """Extract the XML download URL from a filing object."""
        object_id = filing_data.get("object_id")
        if not object_id:
            return None
        return f"https://projects.propublica.org/nonprofits/download-xml?object_id={object_id}"

    def download_xml(self, object_id: str) -> Optional[bytes]:
        """Download a 990 XML filing by object_id."""
        if self.cache:
            cached = self.cache.get_xml(object_id)
            if cached:
                return cached

        url = f"https://projects.propublica.org/nonprofits/download-xml?object_id={object_id}"
        time.sleep(self.delay)
        try:
            r = self.session.get(url, timeout=30)
            r.raise_for_status()
            xml_bytes = r.content
            if self.cache:
                self.cache.store_xml(object_id, xml_bytes)
            return xml_bytes
        except requests.exceptions.RequestException as e:
            print(f"  [XML download error] {e}")
            return None


# ---------------------------------------------------------------------------
# Parsing helpers (moved from POC, enhanced)
# ---------------------------------------------------------------------------

def parse_filing(f: dict) -> Filing:
    """Parse a ProPublica filing object into a Filing dataclass.
    Handles field name variations across Form 990, 990-EZ, and 990-PF."""
    contributions = (
        f.get("totcntrbgfts")
        or f.get("totcntriam")
        or f.get("totcntrbs")
        or f.get("gftgrntrcvd170")
        or f.get("totgftgrntrcvd170")
        or f.get("grssrcptspublcuse509")
        or f.get("totsupp170")
    )

    grants_paid = (
        f.get("grntundpay")
        or f.get("grntstogovt")
        or f.get("grntaam")
        or f.get("totgrntsamt")
        or f.get("grntstoindiv")
        or f.get("grntstofrngovt")
    )

    return Filing(
        tax_year=str(f.get("tax_prd_yr", "")),
        revenue=f.get("totrevenue"),
        contributions=contributions,
        assets=f.get("totassetsend"),
        expenses=f.get("totfuncexpns"),
        grants_paid=grants_paid,
        net_assets=f.get("totnetassetend"),
        form_type=f.get("formtype"),
        pdf_url=f.get("pdf_url"),
        object_id=str(f.get("object_id", "")) if f.get("object_id") else None,
    )


def build_organization(raw_search: dict, detail: dict = None) -> Organization:
    """Build an Organization from search result + optional detail data."""
    ein = str(raw_search.get("ein", ""))
    org = Organization(
        name=raw_search.get("name", "Unknown"),
        ein=ein,
        city=raw_search.get("city", ""),
        state=raw_search.get("state", ""),
        ntee_code=raw_search.get("ntee_code"),
        propublica_url=f"https://projects.propublica.org/nonprofits/organizations/{ein}",
    )

    if detail:
        filings_with_data = detail.get("filings_with_data", [])
        if filings_with_data:
            org.latest_filing = parse_filing(filings_with_data[0])
            org.filings = [parse_filing(f) for f in filings_with_data[:5]]

        # Store the latest_object_id from the organization data (needed for XML download)
        org_data = detail.get("organization", {})
        latest_obj_id = org_data.get("latest_object_id")
        if latest_obj_id and org.latest_filing:
            org.latest_filing.object_id = str(latest_obj_id)

        # Try to get website from ProPublica organization detail
        website = org_data.get("website")
        if website:
            if not website.startswith("http"):
                website = "https://" + website
            org.website = website

    return org
