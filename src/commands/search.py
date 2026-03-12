"""
Layer 1: Find comparable (mission-adjacent) organizations.
This is the enhanced version of the POC search functionality.
"""

import json
import os

from src.api.propublica import ProPublicaClient, build_organization, parse_filing
from src.api.xml_parser import parse_officers_from_xml
from src.core.scoring import score_organization
from src.export.report import print_summary_table, print_org_detail
from src.export.csv_export import export_prospects_csv


def run_prospect_search(client: ProPublicaClient, queries: list, states: list,
                        ntee_codes: list = None, max_per_query: int = 25) -> dict:
    """Run multiple keyword searches and collect unique organizations."""
    all_orgs = {}

    for query in queries:
        for state in states:
            state_label = state if state else "national"
            print(f"  Searching: \"{query}\" ({state_label})...")

            orgs, total = client.search(
                query, state=state,
                ntee=ntee_codes[0] if ntee_codes else None,
            )
            new_count = 0
            for o in orgs[:max_per_query]:
                ein = str(o.get("ein", ""))
                if ein and ein not in all_orgs:
                    all_orgs[ein] = o
                    new_count += 1
            print(f"    Found {len(orgs)} results, {new_count} new unique orgs")

    print(f"\n  Total unique organizations: {len(all_orgs)}")
    return all_orgs


def enrich_organizations(client: ProPublicaClient, raw_orgs: dict,
                         max_enrich: int = 25, priority_states: list = None,
                         fetch_officers: bool = True) -> list:
    """Fetch full filing data for top organizations.
    Optionally fetches XML to extract officers (Phase 1 enhancement)."""
    priority_states = set(priority_states or [])

    def sort_key(item):
        ein, raw = item
        state = raw.get("state", "")
        in_priority = 1 if state in priority_states else 0
        tot_rev = raw.get("totrevenue") or raw.get("score") or 0
        return (in_priority, tot_rev)

    sorted_orgs = sorted(raw_orgs.items(), key=sort_key, reverse=True)

    enriched = []
    count = 0

    for ein, raw in sorted_orgs:
        if count >= max_enrich:
            break
        count += 1
        name = raw.get("name", ein)
        print(f"  [{count}/{min(len(raw_orgs), max_enrich)}] Fetching 990 data for {name}...")

        detail = client.get_organization(ein)
        org = build_organization(raw, detail)
        score_organization(org)

        # Phase 1 enhancement: fetch officers from XML (with caching)
        if fetch_officers and detail:
            # Try cache first
            cached_officers = None
            if client.cache:
                cached_officers = client.cache.get_officers(ein)
            if cached_officers:
                from src.core.models import Officer
                org.officers = [Officer(**o) if isinstance(o, dict) else o for o in cached_officers]
                if org.officers:
                    print(f"    Found {len(org.officers)} officers/directors (cached)")
            else:
                org_data = detail.get("organization", {})
                object_id = org_data.get("latest_object_id")
                if not object_id:
                    filings = detail.get("filings_with_data", [])
                    if filings:
                        object_id = filings[0].get("object_id")
                if object_id:
                    xml_bytes = client.download_xml(str(object_id))
                    if xml_bytes:
                        org.officers = parse_officers_from_xml(xml_bytes)
                        if org.officers:
                            print(f"    Found {len(org.officers)} officers/directors")
                            # Cache the officers
                            if client.cache:
                                client.cache.store_officers(ein, org.officers)

        enriched.append(org)

    enriched.sort(key=lambda o: o.score, reverse=True)
    return enriched


def cmd_search(args, client: ProPublicaClient):
    """Run a single keyword search (CLI command)."""
    print(f"\nSearching for: \"{args.query}\"")
    if args.state:
        print(f"State filter: {args.state}")

    orgs, total = client.search(args.query, state=args.state)
    print(f"Found {total} total results\n")

    raw_dict = {str(o["ein"]): o for o in orgs}
    enriched = enrich_organizations(
        client, raw_dict, max_enrich=args.limit,
        priority_states=[args.state] if args.state else [],
        fetch_officers=args.officers,
    )

    print_summary_table(enriched)

    if args.detail:
        for org in enriched[:args.detail]:
            print_org_detail(org)

    if args.output:
        export_prospects_csv(enriched, args.output)

    return enriched


def cmd_org(args, client: ProPublicaClient):
    """Look up a single organization by EIN (CLI command)."""
    ein = args.ein.replace("-", "")
    print(f"\nLooking up EIN: {ein}")

    detail = client.get_organization(ein)
    if not detail:
        print("Organization not found.")
        return None

    if getattr(args, "dump_fields", False):
        filings = detail.get("filings_with_data", [])
        if filings:
            print(f"\n  RAW FILING FIELDS (latest filing, {len(filings[0])} fields):")
            for k, v in sorted(filings[0].items()):
                if v is not None and v != "" and v != 0:
                    print(f"    {k}: {v}")
        else:
            print("  No filings with data found.")
        return None

    raw = detail.get("organization", {"ein": ein, "name": "Unknown"})
    org = build_organization(raw, detail)
    score_organization(org)

    # Fetch officers from XML
    org_data = detail.get("organization", {})
    object_id = org_data.get("latest_object_id")
    if not object_id:
        filings = detail.get("filings_with_data", [])
        if filings:
            object_id = filings[0].get("object_id")
    if object_id:
        print("  Fetching officer data from XML filing...")
        xml_bytes = client.download_xml(str(object_id))
        if xml_bytes:
            org.officers = parse_officers_from_xml(xml_bytes)

    print_org_detail(org)
    return org


def cmd_profile(args, client: ProPublicaClient):
    """Run a full prospect search using a pre-built profile (CLI command)."""
    profile_path = args.profile
    # Check if it's a name that maps to a file in profiles/
    if not os.path.exists(profile_path):
        profile_path = os.path.join("profiles", f"{args.profile}.json")
    if not os.path.exists(profile_path):
        print(f"Profile not found: {args.profile}")
        print("Provide a path to a JSON profile file or a name matching profiles/<name>.json")
        return

    with open(profile_path, "r") as f:
        profile = json.load(f)

    print(f"\n{'=' * 70}")
    print(f"  990 PROSPECT EXPLORER")
    print(f"  Profile: {profile['name']}")
    print(f"  EIN: {profile['ein']} | {profile['city']}, {profile['state']}")
    print(f"  Mission: {profile['mission']}")
    print(f"{'=' * 70}")

    print(f"\nPhase 1: Searching for mission-adjacent organizations...")
    raw_orgs = run_prospect_search(
        client,
        queries=profile["keywords"],
        states=profile.get("search_states", [""]),
        ntee_codes=profile.get("ntee_codes"),
    )

    # Remove self from results
    own_ein = profile["ein"]
    raw_orgs.pop(own_ein, None)

    print(f"\nPhase 2: Enriching top prospects with 990 filing data...")
    enriched = enrich_organizations(
        client, raw_orgs, max_enrich=args.limit,
        priority_states=profile.get("search_states", []),
        fetch_officers=args.officers,
    )

    print_summary_table(enriched)

    if args.detail:
        detail_count = min(args.detail, len(enriched))
        print(f"\nShowing detail for top {detail_count} prospects:\n")
        for org in enriched[:detail_count]:
            print_org_detail(org)

    output_file = args.output or f"{os.path.splitext(os.path.basename(profile_path))[0]}_prospects.csv"
    export_prospects_csv(enriched, output_file)

    return enriched
