"""
Layer 2: Find who funds a given organization.

Three strategies:
  A) Full-text search for org name in 990 filings (direct mentions)
  B) Sector-based search: find 990-PF filers in the same NTEE/state,
     parse their Schedule I grants for similar orgs (NTEE + keyword match)
  C) Sector-first BATCH search: find foundations in the sector, parse ALL
     their Schedule I grants, then match against ALL comp orgs at once.
     This is the most effective strategy — called once per pipeline run.
"""

import re

from src.api.propublica import ProPublicaClient, build_organization, parse_filing
from src.api.fulltext_search import FullTextSearchClient
from src.api.xml_parser import parse_schedule_i_grants, get_form_type
from src.core.models import Funder, Grant
from src.core.scoring import score_funder
from src.core.matching import fuzzy_match, fuzzy_match_score, normalize_name
from src.export.report import print_funders_table, print_funder_detail
from src.export.csv_export import export_funders_csv


# Words too generic to use as matching keywords
_STOP_WORDS = {
    "the", "of", "and", "for", "in", "a", "an", "to", "inc", "co", "org",
    "foundation", "fund", "trust", "association", "society", "corporation",
    "national", "american", "united", "international", "community",
}


def _extract_keywords(org_name: str) -> list:
    """Extract meaningful keywords from an org name for matching."""
    words = re.findall(r"[a-z]+", org_name.lower())
    return [w for w in words if len(w) >= 3 and w not in _STOP_WORDS]


def _ntee_major(ntee_code: str) -> str:
    """Extract the major NTEE category letter (e.g., 'B' from 'B20')."""
    if ntee_code:
        return ntee_code[0].upper()
    return ""


def _is_grantmaker(detail: dict) -> bool:
    """Check if an org is a grantmaker from its ProPublica detail."""
    filings = detail.get("filings_with_data", [])
    if not filings:
        return False
    form_type = filings[0].get("formtype", "")
    grants_paid = filings[0].get("totgrntsamt") or filings[0].get("grntundpay") or 0
    return "990PF" in str(form_type).replace("-", "").upper() or grants_paid > 0


def _get_foundation_grants(client: ProPublicaClient, ein: str, name: str,
                           detail: dict = None, diagnostics: list = None) -> tuple:
    """Download XML and parse Schedule I grants for a foundation.

    Returns (all_grants, filing) tuple. all_grants may be empty list.
    """
    diag = diagnostics if diagnostics is not None else []

    if not detail:
        detail = client.get_organization(ein)
    if not detail:
        diag.append(f"  [skip] {name} ({ein}): org not found on ProPublica")
        return [], None

    filings = detail.get("filings_with_data", [])
    if not filings:
        diag.append(f"  [skip] {name} ({ein}): no filings available")
        return [], None

    filing = parse_filing(filings[0])

    org_detail = detail.get("organization", {})
    object_id = org_detail.get("latest_object_id") or filings[0].get("object_id")
    if not object_id:
        diag.append(f"  [skip] {name} ({ein}): no XML object_id")
        return [], filing

    xml_bytes = client.download_xml(str(object_id))
    if not xml_bytes:
        diag.append(f"  [skip] {name} ({ein}): XML download failed")
        return [], filing

    all_grants = parse_schedule_i_grants(xml_bytes, funder_name=name, funder_ein=ein)
    if not all_grants:
        diag.append(f"  [skip] {name} ({ein}): XML parsed but 0 Schedule I grants")

    return all_grants, filing


# ---------------------------------------------------------------------------
# Strategy C: Sector-first batch search (NEW — most effective)
# ---------------------------------------------------------------------------

def find_funders_for_sector(client: ProPublicaClient, ft_client: FullTextSearchClient,
                            profile: dict, comp_orgs: list,
                            max_foundations: int = 50,
                            diagnostics: list = None) -> list:
    """Sector-first batch funder search.

    Instead of searching for funders per-comp-org, this:
    1) Finds foundation candidates in the sector (NTEE + keywords)
    2) Downloads & parses ALL their Schedule I grants
    3) Batch-matches grants against ALL comp org names at once

    Args:
        client: ProPublica API client
        ft_client: Full-text search client
        profile: Search profile with keywords, ntee_codes, search_states
        comp_orgs: List of comparable Organization objects from Layer 1
        max_foundations: Max foundation candidates to check
        diagnostics: Optional list to append diagnostic messages

    Returns:
        List of Funder objects with grants_to_target populated
    """
    diag = diagnostics if diagnostics is not None else []
    target_state = profile.get("state", "")
    ntee_codes = profile.get("ntee_codes", [])
    keywords = profile.get("keywords", [])
    profile_ein = profile.get("ein", "").replace("-", "")

    ntee_major = _ntee_major(ntee_codes[0]) if ntee_codes else ""

    # Build comp org lookup: normalized name -> org, plus EIN lookup
    comp_lookup = {}
    comp_ein_lookup = {}
    for org in comp_orgs:
        comp_lookup[normalize_name(org.name)] = org
        if org.ein:
            comp_ein_lookup[org.ein.replace("-", "")] = org

    print(f"\n  Strategy C: Sector-first batch search")
    print(f"    NTEE: {ntee_major or 'any'} | State: {target_state or 'any'}")
    print(f"    Keywords: {', '.join(keywords[:5])}")
    print(f"    Matching against {len(comp_orgs)} comp orgs")

    # --- Step 1: Build foundation candidate list ---
    all_candidates = {}  # ein -> ProPublica raw data

    # Search with profile keywords + foundation terms
    foundation_terms = ["foundation", "fund", "trust", "philanthropy"]
    search_queries = []

    for kw in keywords[:5]:
        for term in foundation_terms[:3]:
            search_queries.append(f"{kw} {term}")

    # Also search just the foundation terms with NTEE filter
    search_queries.extend(foundation_terms)

    # Also search keywords alone (may find foundations with keyword in name)
    search_queries.extend(keywords[:3])

    seen_queries = set()
    for query in search_queries:
        if query in seen_queries:
            continue
        seen_queries.add(query)

        # Search with NTEE + state
        orgs, _ = client.search(
            query, state=target_state if target_state else None,
            ntee=ntee_major if ntee_major else None,
        )
        for o in orgs:
            ein = str(o.get("ein", ""))
            if ein and ein != profile_ein:
                all_candidates[ein] = o

        # If NTEE search gave few results, also search without NTEE
        if len(orgs) < 5 and ntee_major:
            orgs2, _ = client.search(
                query, state=target_state if target_state else None,
            )
            for o in orgs2:
                ein = str(o.get("ein", ""))
                if ein and ein != profile_ein:
                    all_candidates.setdefault(ein, o)

    # Also try full-text search for each keyword (finds foundations that
    # mention the keyword in their filing text, e.g., "education" in mission)
    for kw in keywords[:3]:
        ft_results = ft_client.search(kw, state=target_state, search_type="filings")
        for r in ft_results[:10]:
            ein = r.get("ein", "")
            if ein and ein != profile_ein and ein not in all_candidates:
                all_candidates[ein] = r

    diag.append(f"Strategy C: {len(all_candidates)} foundation candidates from {len(seen_queries)} searches")
    print(f"    Found {len(all_candidates)} unique foundation candidates")

    # --- Step 2: Filter to grantmakers & parse Schedule I ---
    grant_pool = []  # List of (foundation_info, Grant)
    foundation_data = {}  # ein -> {detail, filing, all_grants, raw}
    checked = 0

    for ein, raw in list(all_candidates.items())[:max_foundations]:
        checked += 1
        name = raw.get("name", ein)

        if checked % 10 == 0:
            print(f"    Checking foundations... {checked}/{min(len(all_candidates), max_foundations)}")

        detail = client.get_organization(ein)
        if not detail:
            continue

        if not _is_grantmaker(detail):
            continue

        all_grants, filing = _get_foundation_grants(
            client, ein, name, detail=detail, diagnostics=diag
        )

        if not all_grants:
            continue

        foundation_data[ein] = {
            "name": name, "raw": raw, "detail": detail,
            "filing": filing, "all_grants": all_grants,
        }

        for g in all_grants:
            grant_pool.append((ein, g))

    diag.append(f"Strategy C: {len(foundation_data)} grantmakers with Schedule I data, {len(grant_pool)} total grants")
    print(f"    {len(foundation_data)} grantmakers with grants, {len(grant_pool)} total grants in pool")

    # --- Step 3: Batch fuzzy-match grants against comp orgs ---
    funder_matches = {}  # foundation_ein -> {grants_to_target, matched_comps}

    for fdn_ein, grant in grant_pool:
        recipient = grant.recipient_name or ""
        recipient_ein = (grant.recipient_ein or "").replace("-", "")

        # Check 1: EIN match (strongest signal)
        if recipient_ein and recipient_ein in comp_ein_lookup:
            matched_org = comp_ein_lookup[recipient_ein]
            funder_matches.setdefault(fdn_ein, {"grants": [], "comps": set()})
            funder_matches[fdn_ein]["grants"].append(grant)
            funder_matches[fdn_ein]["comps"].add(matched_org.name)
            continue

        # Check 2: Fuzzy name match against each comp org
        for norm_name, org in comp_lookup.items():
            # Use lower threshold (55) for sector-first since we cast a wider net
            if fuzzy_match(org.name, recipient, threshold=55):
                funder_matches.setdefault(fdn_ein, {"grants": [], "comps": set()})
                funder_matches[fdn_ein]["grants"].append(grant)
                funder_matches[fdn_ein]["comps"].add(org.name)
                break  # Don't double-count same grant

    diag.append(f"Strategy C: {len(funder_matches)} foundations matched grants to comp orgs")
    print(f"    Matched {len(funder_matches)} foundations to comp orgs")

    # --- Step 4: Build Funder objects ---
    funders = []
    for fdn_ein, match_data in funder_matches.items():
        fdn = foundation_data[fdn_ein]
        raw = fdn["raw"]

        funder = Funder(
            name=fdn["name"],
            ein=fdn_ein,
            city=raw.get("city", ""),
            state=raw.get("state", ""),
            total_giving=sum(g.amount or 0 for g in fdn["all_grants"]) or None,
            grants_to_target=match_data["grants"],
            all_grants=fdn["all_grants"],
            latest_filing=fdn["filing"],
            propublica_url=f"https://projects.propublica.org/nonprofits/organizations/{fdn_ein}",
        )
        score_funder(funder, target_state=target_state, sector_match=True)
        funders.append(funder)

        comps_str = ", ".join(sorted(match_data["comps"]))
        diag.append(f"  {fdn['name']}: {len(match_data['grants'])} grants → {comps_str}")

    funders.sort(key=lambda f: f.score, reverse=True)
    return funders


def find_funders_by_sector(client: ProPublicaClient, org_name: str,
                           org_ein: str = "", org_ntee: str = "",
                           org_state: str = "", max_results: int = 15) -> list:
    """Strategy B: Find funders by searching for grant-making foundations
    in the same NTEE sector and state, then checking their Schedule I
    for grants to similar orgs.

    Returns list of Funder objects.
    """
    if not org_ntee and not org_state:
        return []

    ntee_major = _ntee_major(org_ntee)
    keywords = _extract_keywords(org_name)
    org_ein_clean = org_ein.replace("-", "")

    print(f"\n  Strategy B: Searching for grant-makers in sector/state...")
    if ntee_major:
        print(f"    NTEE sector: {ntee_major} ({org_ntee})")
    if org_state:
        print(f"    State: {org_state}")
    if keywords:
        print(f"    Keywords: {', '.join(keywords[:5])}")

    # Search for foundations using multiple queries
    search_queries = ["foundation", "fund", "trust"]
    all_candidates = {}

    for query in search_queries:
        orgs, total = client.search(
            query, state=org_state,
            ntee=ntee_major if ntee_major else None,
        )
        for o in orgs:
            ein = str(o.get("ein", ""))
            if ein and ein != org_ein_clean:
                all_candidates[ein] = o

    print(f"    Found {len(all_candidates)} candidate foundations")

    funders = []

    for i, (ein, raw) in enumerate(list(all_candidates.items())[:max_results]):
        name = raw.get("name", ein)
        print(f"    [{i+1}/{min(len(all_candidates), max_results)}] Checking {name}...")

        detail = client.get_organization(ein)
        if not detail:
            continue

        filings = detail.get("filings_with_data", [])
        if not filings:
            continue

        # Filter for grant-makers: 990-PF filers or orgs with grants paid
        form_type = filings[0].get("formtype", "")
        grants_paid = filings[0].get("totgrntsamt") or filings[0].get("grntundpay") or 0
        is_grantmaker = "990PF" in str(form_type).replace("-", "").upper() or grants_paid > 0

        if not is_grantmaker:
            continue

        filing = parse_filing(filings[0])

        # Download XML and parse Schedule I grants
        org_detail = detail.get("organization", {})
        object_id = org_detail.get("latest_object_id") or filings[0].get("object_id")
        if not object_id:
            continue

        xml_bytes = client.download_xml(str(object_id))
        if not xml_bytes:
            continue

        all_grants = parse_schedule_i_grants(xml_bytes, funder_name=name, funder_ein=ein)
        if not all_grants:
            continue

        # Match grants to similar orgs by NTEE + keywords
        grants_to_similar = []
        for g in all_grants:
            recipient_lower = (g.recipient_name or "").lower()
            purpose_lower = (g.purpose or "").lower()
            combined = recipient_lower + " " + purpose_lower

            # Check if any target keywords appear in grant recipient/purpose
            keyword_match = any(kw in combined for kw in keywords) if keywords else False

            if keyword_match:
                grants_to_similar.append(g)

        if not grants_to_similar:
            continue

        similar_total = sum(g.amount or 0 for g in grants_to_similar)
        print(f"      {len(grants_to_similar)} grants to similar orgs (${similar_total:,.0f})")

        funder = Funder(
            name=name,
            ein=ein,
            city=raw.get("city", ""),
            state=raw.get("state", ""),
            total_giving=sum(g.amount or 0 for g in all_grants) or None,
            grants_to_target=grants_to_similar,
            all_grants=all_grants,
            latest_filing=filing,
            propublica_url=f"https://projects.propublica.org/nonprofits/organizations/{ein}",
        )
        score_funder(funder, target_state=org_state, sector_match=True)
        funders.append(funder)

    funders.sort(key=lambda f: f.score, reverse=True)
    return funders


def find_funders_for_org(client: ProPublicaClient, ft_client: FullTextSearchClient,
                         org_name: str, org_ein: str = "",
                         max_results: int = 25, target_state: str = "",
                         org_ntee: str = "") -> list:
    """Find foundations that have funded a given org.

    Two strategies combined:
      A) Full-text search for org name across 990 filings (direct mentions)
      B) Sector search: find grant-makers in same NTEE/state with similar grantees

    Returns list of Funder objects.
    """
    # Look up NTEE code if we have an EIN but no NTEE
    org_ntee_code = org_ntee
    if not org_ntee_code and org_ein:
        detail = client.get_organization(org_ein.replace("-", ""))
        if detail:
            org_info = detail.get("organization", {})
            org_ntee_code = org_info.get("ntee_code", "")
            if not target_state:
                target_state = org_info.get("state", "")

    # --- Strategy A: Full-text search ---
    print(f"\n  Strategy A: Searching filings that mention \"{org_name}\"...")
    search_results = ft_client.search_for_funders(org_name, max_pages=3)
    print(f"  Found {len(search_results)} results from full-text search")

    # Filter out the org itself
    org_ein_clean = org_ein.replace("-", "")
    candidates = [
        r for r in search_results
        if r.get("ein", "") != org_ein_clean
    ]
    print(f"  {len(candidates)} potential funders (excluding self)")

    funders = []
    seen_eins = set()

    for i, candidate in enumerate(candidates[:max_results]):
        ein = candidate.get("ein", "")
        if not ein or ein in seen_eins:
            continue
        seen_eins.add(ein)

        name = candidate.get("name", "Unknown")
        print(f"  [{i+1}/{min(len(candidates), max_results)}] Checking {name}...")

        # Fetch org detail to get filing data
        detail = client.get_organization(ein)
        if not detail:
            continue

        filings = detail.get("filings_with_data", [])
        if not filings:
            continue

        # Check if this is a foundation (990-PF filer) or has grant-making
        form_type = filings[0].get("formtype", "")
        grants_paid = filings[0].get("totgrntsamt") or filings[0].get("grntundpay") or 0

        # Parse filing for basic financials
        filing = parse_filing(filings[0])

        # Try to get Schedule I grants from XML
        grants_to_target = []
        all_grants = []
        org_detail = detail.get("organization", {})
        object_id = org_detail.get("latest_object_id") or filings[0].get("object_id")

        if object_id:
            xml_bytes = client.download_xml(str(object_id))
            if xml_bytes:
                all_grants = parse_schedule_i_grants(
                    xml_bytes, funder_name=name, funder_ein=ein
                )

                # Find grants that match our target org (fuzzy matching)
                for g in all_grants:
                    if fuzzy_match(org_name, g.recipient_name, threshold=55):
                        grants_to_target.append(g)

                if all_grants:
                    print(f"    Found {len(all_grants)} grants, {len(grants_to_target)} to target")

        # Also check if the search snippet mentions the org (even without Schedule I)
        snippet = candidate.get("snippet", "")
        if not grants_to_target and fuzzy_match(org_name, snippet, threshold=60):
            # Create a placeholder grant from the search evidence
            grants_to_target.append(Grant(
                funder_name=name,
                funder_ein=ein,
                recipient_name=org_name,
                recipient_ein=org_ein,
                amount=None,
                purpose="(mentioned in filing - amount not confirmed)",
                tax_year=filing.tax_year if filing else "",
            ))

        if not grants_to_target:
            continue

        funder = Funder(
            name=name,
            ein=ein,
            city=candidate.get("city", ""),
            state=candidate.get("state", ""),
            total_giving=sum(g.amount or 0 for g in all_grants) or None,
            grants_to_target=grants_to_target,
            all_grants=all_grants,
            latest_filing=filing,
            propublica_url=f"https://projects.propublica.org/nonprofits/organizations/{ein}",
        )
        score_funder(funder, target_state=target_state)
        funders.append(funder)

    # --- Strategy B: Sector-based search ---
    sector_funders = find_funders_by_sector(
        client,
        org_name=org_name,
        org_ein=org_ein,
        org_ntee=org_ntee_code,
        org_state=target_state,
        max_results=max(10, max_results // 2),
    )

    # Merge results, deduplicating by EIN
    for sf in sector_funders:
        if sf.ein not in seen_eins:
            seen_eins.add(sf.ein)
            funders.append(sf)

    funders.sort(key=lambda f: f.score, reverse=True)
    return funders


def cmd_funders(args, client: ProPublicaClient):
    """Find funders of a specific org (CLI command)."""
    ft_client = FullTextSearchClient(delay=client.delay * 2)

    org_name = args.name
    org_ein = ""
    target_state = args.state or ""
    org_ntee = getattr(args, "ntee", "") or ""

    # If EIN provided, look up the org name
    if args.ein:
        org_ein = args.ein.replace("-", "")
        print(f"\nLooking up EIN: {org_ein}")
        detail = client.get_organization(org_ein)
        if detail:
            org_info = detail.get("organization", {})
            org_name = org_info.get("name", org_name or "Unknown")
            target_state = target_state or org_info.get("state", "")
            org_ntee = org_ntee or org_info.get("ntee_code", "")
            print(f"  Organization: {org_name}")
            if org_ntee:
                print(f"  NTEE: {org_ntee}")
        elif not org_name:
            print("Organization not found and no --name provided.")
            return

    if not org_name:
        print("Please provide --name or --ein to identify the organization.")
        return

    print(f"\n{'=' * 70}")
    print(f"  LAYER 2: FUNDER IDENTIFICATION")
    print(f"  Target: {org_name}")
    if org_ein:
        print(f"  EIN: {org_ein}")
    print(f"{'=' * 70}")

    funders = find_funders_for_org(
        client, ft_client,
        org_name=org_name,
        org_ein=org_ein,
        max_results=args.limit,
        target_state=target_state,
        org_ntee=org_ntee,
    )

    if not funders:
        print("\n  No funders found. Try a different org name or broader search.")
        return

    print_funders_table(funders)

    if args.detail:
        detail_count = min(args.detail, len(funders))
        print(f"\nShowing detail for top {detail_count} funders:\n")
        for funder in funders[:detail_count]:
            print_funder_detail(funder)

    if args.output:
        export_funders_csv(funders, args.output)

    return funders
