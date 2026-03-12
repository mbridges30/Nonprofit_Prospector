"""
Layer 3: List all grantees of a foundation.
Given a foundation EIN, fetch their 990-PF XML and parse Schedule I
to extract every grant with recipient, amount, and purpose.
"""

from src.api.propublica import ProPublicaClient, parse_filing
from src.api.xml_parser import parse_schedule_i_grants
from src.core.models import Grant
from src.export.report import print_grants_table, fmt_dollar
from src.export.csv_export import export_grants_csv


def list_foundation_grants(client: ProPublicaClient, ein: str,
                           funder_name: str = "",
                           sector_filter: str = None,
                           state_filter: str = None,
                           min_amount: float = None,
                           max_years: int = 3) -> list:
    """Fetch and parse all grants from a foundation's 990-PF filings.

    Args:
        client: ProPublica API client
        ein: Foundation EIN
        funder_name: Foundation name (for display)
        sector_filter: Filter grants by keyword in purpose/name
        state_filter: Filter grants by recipient state
        min_amount: Minimum grant amount to include
        max_years: Number of filing years to check

    Returns list of Grant objects.
    """
    ein = ein.replace("-", "")
    print(f"\n  Fetching filing data for EIN {ein}...")

    detail = client.get_organization(ein)
    if not detail:
        print("  Foundation not found.")
        return []

    org_info = detail.get("organization", {})
    if not funder_name:
        funder_name = org_info.get("name", "Unknown Foundation")

    filings = detail.get("filings_with_data", [])
    if not filings:
        print("  No filings with data found.")
        return []

    print(f"  Foundation: {funder_name}")
    print(f"  Found {len(filings)} filing years")

    all_grants = []
    seen_grants = set()  # Deduplicate across years

    # Get latest_object_id from org data for the first filing
    org_latest_object_id = detail.get("organization", {}).get("latest_object_id")

    for idx, filing_data in enumerate(filings[:max_years]):
        object_id = filing_data.get("object_id")
        if not object_id and idx == 0:
            object_id = org_latest_object_id
        tax_year = filing_data.get("tax_prd_yr", "")

        if not object_id:
            continue

        print(f"  Parsing grants from FY {tax_year} filing...")
        xml_bytes = client.download_xml(str(object_id))
        if not xml_bytes:
            continue

        grants = parse_schedule_i_grants(
            xml_bytes, funder_name=funder_name, funder_ein=ein
        )

        for g in grants:
            # Deduplicate by recipient+year
            key = (g.recipient_name.lower(), g.tax_year, g.amount)
            if key in seen_grants:
                continue
            seen_grants.add(key)

            # Apply filters
            if min_amount and (g.amount or 0) < min_amount:
                continue
            if state_filter and g.recipient_state and g.recipient_state.upper() != state_filter.upper():
                continue
            if sector_filter and sector_filter.lower() not in (
                (g.purpose or "") + " " + (g.recipient_name or "")
            ).lower():
                continue

            all_grants.append(g)

        print(f"    Found {len(grants)} grants in FY {tax_year}")

    # Sort by amount descending
    all_grants.sort(key=lambda g: g.amount or 0, reverse=True)
    return all_grants


def cmd_grants(args, client: ProPublicaClient):
    """List all grantees of a foundation (CLI command)."""
    ein = ""
    funder_name = args.name or ""

    if args.ein:
        ein = args.ein.replace("-", "")
    elif args.name:
        # Search for the foundation by name
        print(f"\nSearching for foundation: \"{args.name}\"")
        orgs, total = client.search(args.name)
        if orgs:
            # Try to find a match, preferring foundations (990-PF filers)
            for o in orgs:
                if args.name.lower() in o.get("name", "").lower():
                    ein = str(o.get("ein", ""))
                    funder_name = o.get("name", args.name)
                    break
            if not ein:
                ein = str(orgs[0].get("ein", ""))
                funder_name = orgs[0].get("name", args.name)
            print(f"  Found: {funder_name} (EIN: {ein})")
        else:
            print("  No results found.")
            return
    else:
        print("Please provide --ein or --name to identify the foundation.")
        return

    print(f"\n{'=' * 70}")
    print(f"  LAYER 3: FOUNDATION GRANT LIST")
    print(f"  Foundation: {funder_name}")
    print(f"  EIN: {ein}")
    if args.sector:
        print(f"  Sector filter: {args.sector}")
    if args.state:
        print(f"  State filter: {args.state}")
    print(f"{'=' * 70}")

    grants = list_foundation_grants(
        client, ein,
        funder_name=funder_name,
        sector_filter=args.sector,
        state_filter=args.state,
        min_amount=args.min_amount,
        max_years=args.years,
    )

    if not grants:
        print("\n  No grants found. The foundation may not file Schedule I,")
        print("  or grants may not match your filters.")
        return

    total = sum(g.amount or 0 for g in grants)
    print(f"\n  Found {len(grants)} grants totaling {fmt_dollar(total)}")

    print_grants_table(grants, funder_name=funder_name)

    if args.output:
        export_grants_csv(grants, args.output)

    return grants


def cmd_sector(args, client: ProPublicaClient):
    """Reverse query: find foundations funding a sector (CLI command).

    Searches for foundations that gave grants matching sector keywords
    in a specific state.
    """
    print(f"\n{'=' * 70}")
    print(f"  SECTOR FUNDING MAP")
    print(f"  Sector: NTEE {args.ntee}" if args.ntee else "  Sector: (keyword search)")
    if args.state:
        print(f"  State: {args.state}")
    if args.min_grants:
        print(f"  Min grant amount: {fmt_dollar(args.min_grants)}")
    print(f"{'=' * 70}")

    # Search for foundations in the sector
    query = args.query or "foundation"
    print(f"\n  Searching for foundations: \"{query}\"...")

    all_foundations = {}
    orgs, total = client.search(query, state=args.state, ntee=args.ntee)
    for o in orgs:
        ein = str(o.get("ein", ""))
        if ein:
            all_foundations[ein] = o

    # Also search with "grant" keyword
    orgs2, _ = client.search(f"{query} grant", state=args.state)
    for o in orgs2:
        ein = str(o.get("ein", ""))
        if ein and ein not in all_foundations:
            all_foundations[ein] = o

    print(f"  Found {len(all_foundations)} potential foundations")

    # For each foundation, get their grants
    all_grants = []
    count = 0

    for ein, raw in list(all_foundations.items())[:args.limit]:
        count += 1
        name = raw.get("name", ein)
        print(f"  [{count}/{min(len(all_foundations), args.limit)}] Checking {name}...")

        grants = list_foundation_grants(
            client, ein,
            funder_name=name,
            min_amount=args.min_grants,
            max_years=1,
        )
        all_grants.extend(grants)

    if all_grants:
        # Deduplicate and sort
        all_grants.sort(key=lambda g: g.amount or 0, reverse=True)
        total = sum(g.amount or 0 for g in all_grants)
        print(f"\n  Total: {len(all_grants)} grants, {fmt_dollar(total)}")
        print_grants_table(all_grants[:50])

        if args.output:
            export_grants_csv(all_grants, args.output)
    else:
        print("\n  No grants found matching criteria.")

    return all_grants
