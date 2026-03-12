"""
Full pipeline: Chains Layers 1-3 together.
Finds comparable orgs -> their funders -> funder grant lists.
Produces a prioritized prospect list with evidence chains.
"""

import json
import os

from src.api.propublica import ProPublicaClient
from src.api.fulltext_search import FullTextSearchClient
from src.commands.search import run_prospect_search, enrich_organizations
from src.commands.funders import find_funders_for_org, find_funders_for_sector
from src.commands.grants import list_foundation_grants
from src.core.models import Funder, Grant, ProspectResult
from src.core.scoring import score_funder
from src.core.matching import fuzzy_match
from src.core.crossref import find_shared_board_members, format_shared_members
from src.core.ai_scoring import batch_score_funders, is_available as ai_available
from src.export.report import (
    print_summary_table, print_org_detail, print_funders_table,
    print_funder_detail, print_prospect_chain, fmt_dollar, fmt_short
)
from src.export.csv_export import export_full_pipeline_csv
from src.api.web_scraper import scrape_foundation_grants, discover_and_scrape_foundations_batch
from src.api.donor_scraper import scrape_donor_pages
from src.api.website_resolver import resolve_websites_batch


def run_full_pipeline(client: ProPublicaClient, profile: dict,
                      max_comps: int = 25, max_funders_per_comp: int = 10,
                      depth: int = 2, fetch_officers: bool = True,
                      use_ai_scoring: bool = False) -> tuple:
    """Run the full three-layer prospect pipeline.

    Args:
        client: ProPublica API client
        profile: Search profile dict with keywords, states, etc.
        max_comps: Max comparable orgs to find (Layer 1)
        max_funders_per_comp: Max funders per comp org (Layer 2)
        depth: Pipeline depth (1=comps only, 2=comps+funders, 3=full)
        fetch_officers: Whether to fetch officers from XML

    Returns:
        (enriched_orgs, all_funders, shared_board_members, diagnostics) tuple
    """
    ft_client = FullTextSearchClient(delay=client.delay * 2)
    target_state = profile.get("state", "")
    target_name = profile.get("name", "")

    # -----------------------------------------------------------------------
    # Layer 1: Find comparable organizations
    # -----------------------------------------------------------------------
    print(f"\n{'=' * 70}")
    print(f"  LAYER 1: Finding mission-adjacent organizations")
    print(f"{'=' * 70}")

    raw_orgs = run_prospect_search(
        client,
        queries=profile["keywords"],
        states=profile.get("search_states", [""]),
        ntee_codes=profile.get("ntee_codes"),
    )

    # Remove self
    raw_orgs.pop(profile.get("ein", ""), None)

    print(f"\n  Enriching top {max_comps} prospects...")
    enriched = enrich_organizations(
        client, raw_orgs, max_enrich=max_comps,
        priority_states=profile.get("search_states", []),
        fetch_officers=fetch_officers,
    )

    print_summary_table(enriched)

    # Board member cross-referencing
    shared = {}
    if fetch_officers:
        shared = find_shared_board_members(enriched)
        if shared:
            print(f"\n  Board Member Cross-References ({len(shared)} shared):")
            print(format_shared_members(shared))

    if depth < 2:
        return enriched, [], shared, []

    # -----------------------------------------------------------------------
    # Layer 2: Find funders of each comparable org
    # -----------------------------------------------------------------------
    print(f"\n{'=' * 70}")
    print(f"  LAYER 2: Identifying funders of comparable organizations")
    print(f"{'=' * 70}")

    all_funders = {}  # ein -> Funder (deduplicated across comp orgs)
    diagnostics = []

    # --- Strategy C: Sector-first batch search (most effective) ---
    sector_funders = find_funders_for_sector(
        client, ft_client,
        profile=profile,
        comp_orgs=enriched,
        max_foundations=50,
        diagnostics=diagnostics,
    )

    for funder in sector_funders:
        all_funders[funder.ein] = funder
        # Tag which comp orgs this funder supports
        for org in enriched:
            for g in funder.grants_to_target:
                if fuzzy_match(org.name, g.recipient_name, threshold=55):
                    if not org.funders:
                        org.funders = []
                    if not any(f.ein == funder.ein for f in org.funders):
                        org.funders.append(funder)

    print(f"\n  Sector-first search found {len(sector_funders)} funders")

    # --- Strategy A: Per-org full-text search (additional specificity) ---
    comp_count = 0
    top_comps = [o for o in enriched if o.score >= 2][:15]

    for org in top_comps:
        comp_count += 1
        print(f"\n  --- Comp Org [{comp_count}/{len(top_comps)}]: {org.name} ---")

        funders = find_funders_for_org(
            client, ft_client,
            org_name=org.name,
            org_ein=org.ein,
            max_results=max_funders_per_comp,
            target_state=target_state,
        )

        if not org.funders:
            org.funders = []
        for f in funders:
            if not any(existing.ein == f.ein for existing in org.funders):
                org.funders.append(f)

        for funder in funders:
            if funder.ein in all_funders:
                # Merge grants from this comp org into existing funder
                existing = all_funders[funder.ein]
                existing.grants_to_target.extend(funder.grants_to_target)

                # Merge all_grants (deduplicate)
                existing_keys = {
                    (g.recipient_name.lower(), g.tax_year, g.amount)
                    for g in existing.all_grants
                }
                for g in funder.all_grants:
                    key = (g.recipient_name.lower(), g.tax_year, g.amount)
                    if key not in existing_keys:
                        existing.all_grants.append(g)
                        existing_keys.add(key)

                # Re-score with merged data
                score_funder(existing, target_state=target_state)
            else:
                all_funders[funder.ein] = funder

    # -----------------------------------------------------------------------
    # Web enrichment: Two-hop web discovery pipeline
    # -----------------------------------------------------------------------
    print(f"\n{'=' * 70}")
    print(f"  WEB DISCOVERY: Two-hop funder identification")
    print(f"{'=' * 70}")

    comp_names = [org.name for org in enriched]

    # --- Step 1: Discover websites for comp orgs ---
    print(f"\n  Step 1: Resolving websites for comp orgs...")
    try:
        resolved_count = resolve_websites_batch(enriched[:20], client=client)
        diagnostics.append(f"Website resolver: found {resolved_count} websites for comp orgs")
        print(f"    Resolved {resolved_count} websites")
    except Exception as e:
        diagnostics.append(f"Website resolver error: {e}")
        print(f"    Website resolver error: {e}")

    # --- Step 2: Scrape donor pages from comp org websites ---
    print(f"\n  Step 2: Scraping donor pages from comp org websites...")
    donor_data = {}
    try:
        donor_data = scrape_donor_pages(enriched, client=client, diagnostics=diagnostics)
    except Exception as e:
        diagnostics.append(f"Donor page scraping error: {e}")
        print(f"    Donor page scraping error: {e}")

    # --- Step 3: Bridge — discover foundation grant pages from donor names ---
    if donor_data:
        print(f"\n  Step 3: Investigating {sum(len(v) for v in donor_data.values())} "
              f"donor names from {len(donor_data)} comp orgs...")
        try:
            ein_to_name = {org.ein: org.name for org in enriched}
            web_funders = discover_and_scrape_foundations_batch(
                donor_names_by_org=donor_data,
                comp_org_names=comp_names,
                client=client,
                diagnostics=diagnostics,
                comp_org_ein_to_name=ein_to_name,
            )

            # Merge web-discovered funders into all_funders
            for funder in web_funders:
                funder_key = funder.ein or funder.name.lower()
                if funder_key in all_funders:
                    existing = all_funders[funder_key]
                    # Merge grants
                    existing_names = {g.recipient_name.lower() for g in existing.grants_to_target}
                    for g in funder.grants_to_target:
                        if g.recipient_name.lower() not in existing_names:
                            existing.grants_to_target.append(g)
                    score_funder(existing, target_state=target_state)
                else:
                    score_funder(funder, target_state=target_state)
                    all_funders[funder_key] = funder
                    diagnostics.append(f"Web discovery: NEW funder {funder.name}")

        except Exception as e:
            diagnostics.append(f"Foundation bridge error: {e}")
            print(f"    Foundation bridge error: {e}")
    else:
        diagnostics.append("Step 3 skipped: no donor names found in Step 2")
        print(f"\n  Step 3: Skipped (no donor names found)")

    # --- Step 4: Also cross-reference donor names against existing funders ---
    for org_ein, donor_names in donor_data.items():
        for donor_name in donor_names:
            for fdn_key, funder in all_funders.items():
                if fuzzy_match(donor_name, funder.name, threshold=60):
                    diagnostics.append(
                        f"Donor page confirms: {donor_name} -> {funder.name}"
                    )
                    break

    # --- Step 5: Foundation grant page scraping (pre-configured sources) ---
    print(f"\n  Step 5: Checking pre-configured foundation grant pages...")
    try:
        web_grants = scrape_foundation_grants(comp_names, diagnostics=diagnostics)

        for fdn_ein, grants in web_grants.items():
            matched_grants = []
            for g in grants:
                for org in enriched:
                    if fuzzy_match(org.name, g.recipient_name, threshold=55):
                        matched_grants.append(g)
                        break

            if matched_grants:
                if fdn_ein in all_funders:
                    existing = all_funders[fdn_ein]
                    existing_names = {g.recipient_name.lower() for g in existing.grants_to_target}
                    for g in matched_grants:
                        if g.recipient_name.lower() not in existing_names:
                            existing.grants_to_target.append(g)
                    score_funder(existing, target_state=target_state)
                else:
                    funder = Funder(
                        name=grants[0].funder_name if grants else f"Foundation {fdn_ein}",
                        ein=fdn_ein,
                        grants_to_target=matched_grants,
                        all_grants=grants,
                        total_giving=sum(g.amount or 0 for g in grants) or None,
                    )
                    score_funder(funder, target_state=target_state)
                    all_funders[fdn_ein] = funder
                    diagnostics.append(f"Grant page: NEW funder {funder.name}")
    except Exception as e:
        diagnostics.append(f"Foundation grant page scraping error: {e}")
        print(f"    Foundation page scraping error: {e}")

    funder_list = sorted(all_funders.values(), key=lambda f: f.score, reverse=True)

    # -----------------------------------------------------------------------
    # Foundation Enrichment: fetch website + officers for top funders
    # -----------------------------------------------------------------------
    enrich_targets = [f for f in funder_list if f.score >= 4][:10]
    if enrich_targets:
        print(f"\n  Enriching top {len(enrich_targets)} funders (website + officers)...")
        for i, funder in enumerate(enrich_targets):
            try:
                if funder.ein:
                    detail = client.get_organization(funder.ein)
                    if detail:
                        org_data = detail.get("organization", {})
                        website = org_data.get("website", "") or ""
                        if website and not website.startswith("http"):
                            website = "https://" + website
                        funder.website = website or None
                        funder.propublica_url = (
                            f"https://projects.propublica.org/nonprofits/organizations/{funder.ein}"
                        )

                        # Fetch officers for high-scoring funders
                        if funder.score >= 7:
                            object_id = org_data.get("latest_object_id")
                            if not object_id:
                                filings = detail.get("filings_with_data", [])
                                if filings:
                                    object_id = filings[0].get("object_id")
                            if object_id:
                                from src.api.xml_parser import parse_officers_from_xml
                                xml_bytes = client.download_xml(str(object_id))
                                if xml_bytes:
                                    funder.officers = parse_officers_from_xml(xml_bytes)

                        # Also grab filing data if missing
                        if not funder.latest_filing:
                            from src.api.propublica import parse_filing
                            filings = detail.get("filings_with_data", [])
                            if filings:
                                funder.latest_filing = parse_filing(filings[0])
            except Exception as e:
                diagnostics.append(f"Funder enrichment error for {funder.name}: {e}")

    if funder_list:
        print_funders_table(funder_list)

    # AI mission scoring (if requested)
    if use_ai_scoring and funder_list:
        target_mission = profile.get("mission", "")
        if target_mission and ai_available():
            print(f"\n  AI Mission Scoring (using Claude)...")
            scored = batch_score_funders(
                target_name=target_name,
                target_mission=target_mission,
                funders=funder_list,
                max_score=min(20, len(funder_list)),
            )
            print(f"  Scored {scored} funders")
        elif not target_mission:
            print("  Skipping AI scoring: no mission statement in profile")
        else:
            print("  Skipping AI scoring: ANTHROPIC_API_KEY not set")

    if depth < 3 or not funder_list:
        return enriched, funder_list, shared, diagnostics

    # -----------------------------------------------------------------------
    # Layer 3: Get full grant lists from top funders
    # -----------------------------------------------------------------------
    print(f"\n{'=' * 70}")
    print(f"  LAYER 3: Fetching complete grant lists from top funders")
    print(f"{'=' * 70}")

    top_funders = [f for f in funder_list if f.score >= 4][:10]

    for i, funder in enumerate(top_funders):
        if funder.all_grants:
            # Already have grants from Layer 2
            continue

        print(f"\n  [{i+1}/{len(top_funders)}] Fetching grants for {funder.name}...")

        grants = list_foundation_grants(
            client, funder.ein,
            funder_name=funder.name,
            max_years=2,
        )
        if grants:
            funder.all_grants = grants
            funder.total_giving = sum(g.amount or 0 for g in grants)
            score_funder(funder, target_state=target_state)

    # Re-sort after Layer 3 enrichment
    funder_list = sorted(all_funders.values(), key=lambda f: f.score, reverse=True)

    return enriched, funder_list, shared, diagnostics


def cmd_prospect(args, client: ProPublicaClient):
    """Run the full pipeline (CLI command)."""
    profile_path = args.profile
    if not os.path.exists(profile_path):
        profile_path = os.path.join("profiles", f"{args.profile}.json")
    if not os.path.exists(profile_path):
        print(f"Profile not found: {args.profile}")
        return

    with open(profile_path, "r") as f:
        profile = json.load(f)

    print(f"\n{'=' * 70}")
    print(f"  990 PROSPECT EXPLORER - FULL PIPELINE")
    print(f"  Profile: {profile['name']}")
    print(f"  EIN: {profile['ein']} | {profile['city']}, {profile['state']}")
    print(f"  Mission: {profile['mission']}")
    print(f"  Pipeline depth: {args.depth}")
    print(f"{'=' * 70}")

    enriched, funders, _shared, _diag = run_full_pipeline(
        client, profile,
        max_comps=args.limit,
        depth=args.depth,
        fetch_officers=args.officers,
        use_ai_scoring=getattr(args, 'score', False),
    )

    # Print evidence chains
    if funders:
        print(f"\n{'=' * 70}")
        print(f"  PROSPECT EVIDENCE CHAINS")
        print(f"  \"Why should {profile['name']} approach these funders?\"")
        print(f"{'=' * 70}")

        for funder in funders[:20]:
            print(f"\n  {funder.score_label}: {funder.name}")
            for g in funder.grants_to_target[:5]:
                if g.purpose and g.purpose.startswith("Listed on"):
                    print(f"    -> {g.purpose}: {g.recipient_name}")
                else:
                    amt = fmt_short(g.amount) if g.amount else "grant"
                    yr = f" ({g.tax_year})" if g.tax_year else ""
                    print(f"    -> Gave {amt} to {g.recipient_name}{yr}")
            if funder.all_grants:
                total = sum(g.amount or 0 for g in funder.all_grants)
                has_amounts = any(g.amount for g in funder.all_grants)
                if has_amounts:
                    print(f"    Total giving: {fmt_dollar(total)} across {len(funder.all_grants)} grants")
                else:
                    print(f"    Portfolio: {len(funder.all_grants)} grantees on file")

    # Show detail for top funders
    if args.detail and funders:
        detail_count = min(args.detail, len(funders))
        print(f"\nShowing detail for top {detail_count} funder prospects:\n")
        for funder in funders[:detail_count]:
            print_funder_detail(funder)

    # Export
    output_file = args.output or f"{os.path.splitext(os.path.basename(profile_path))[0]}_pipeline.csv"
    export_full_pipeline_csv(enriched, funders, output_file)

    return enriched, funders
