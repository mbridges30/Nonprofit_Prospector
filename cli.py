#!/usr/bin/env python3
"""
Foundation Finder - CLI Tool
Finds mission-adjacent nonprofits and builds prioritized prospect lists
using ProPublica Nonprofit Explorer API and IRS 990 data.

Three-layer workflow:
  Layer 1: Find comparable orgs (who is similar to us?)
  Layer 2: Find funders of those orgs (who funds them?)
  Layer 3: List foundation grantees (what else do those funders support?)
"""

import argparse
import sys

from dotenv import load_dotenv
load_dotenv(override=True)  # Load .env file (for ANTHROPIC_API_KEY, etc.)

from src.api.propublica import ProPublicaClient
from src.core.cache import FilingCache


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="prospect-explorer",
        description="Foundation Finder - Find mission-adjacent nonprofits and their funders",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Layer 1: Find comparable orgs using a profile
  python cli.py search --profile math_agency

  # Layer 1: Quick keyword search
  python cli.py search --query "math education" --state WA

  # Look up a single org by EIN (with officers)
  python cli.py org --ein 91-1508191 --officers

  # Layer 2: Find who funds a specific org
  python cli.py funders --name "Eastside Pathways"
  python cli.py funders --ein 45-3005820

  # Layer 3: List all grantees of a foundation
  python cli.py grants --ein 56-2206524
  python cli.py grants --name "Ballmer" --sector education

  # Full pipeline: comps + funders + grant lists
  python cli.py prospect --profile math_agency --depth 2

  # Reverse query: foundations funding a sector in a state
  python cli.py sector --query "education" --state WA --min-grants 100000
        """,
    )

    # Global options
    parser.add_argument("--delay", type=float, default=0.5,
                        help="Delay between API calls in seconds (default: 0.5)")
    parser.add_argument("--no-cache", action="store_true",
                        help="Disable local cache")

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # --- search command (Layer 1) ---
    search_p = subparsers.add_parser("search", help="Layer 1: Find comparable orgs")
    search_p.add_argument("--profile", type=str, help="Use a JSON search profile")
    search_p.add_argument("--query", type=str, help="Search query (e.g. 'math education')")
    search_p.add_argument("--state", type=str, help="Filter by state (e.g. WA)")
    search_p.add_argument("--limit", type=int, default=25,
                          help="Max orgs to enrich (default: 25)")
    search_p.add_argument("--detail", type=int, default=0,
                          help="Show detailed view for top N results")
    search_p.add_argument("--officers", action="store_true",
                          help="Fetch officer names from XML filings")
    search_p.add_argument("--output", type=str, help="Export results to CSV file")

    # --- org command (single org lookup) ---
    org_p = subparsers.add_parser("org", help="Look up a single organization by EIN")
    org_p.add_argument("--ein", type=str, required=True, help="Organization EIN")
    org_p.add_argument("--officers", action="store_true",
                       help="Fetch officer names from XML filing")
    org_p.add_argument("--dump-fields", action="store_true", dest="dump_fields",
                       help="Show raw API field names (for debugging)")

    # --- funders command (Layer 2) ---
    funders_p = subparsers.add_parser("funders", help="Layer 2: Find who funds an org")
    funders_p.add_argument("--ein", type=str, help="Organization EIN")
    funders_p.add_argument("--name", type=str, help="Organization name to search for")
    funders_p.add_argument("--state", type=str, help="Target state for scoring")
    funders_p.add_argument("--ntee", type=str, help="NTEE code for sector-based search (e.g., B20, O, P)")
    funders_p.add_argument("--limit", type=int, default=25,
                           help="Max funders to check (default: 25)")
    funders_p.add_argument("--detail", type=int, default=0,
                           help="Show detailed view for top N funders")
    funders_p.add_argument("--output", type=str, help="Export results to CSV file")

    # --- grants command (Layer 3) ---
    grants_p = subparsers.add_parser("grants", help="Layer 3: List foundation grantees")
    grants_p.add_argument("--ein", type=str, help="Foundation EIN")
    grants_p.add_argument("--name", type=str, help="Foundation name to search for")
    grants_p.add_argument("--sector", type=str, help="Filter grants by sector keyword")
    grants_p.add_argument("--state", type=str, help="Filter grants by recipient state")
    grants_p.add_argument("--min-amount", type=float, dest="min_amount",
                          help="Minimum grant amount")
    grants_p.add_argument("--years", type=int, default=3,
                          help="Number of filing years to check (default: 3)")
    grants_p.add_argument("--output", type=str, help="Export results to CSV file")

    # --- prospect command (Full pipeline) ---
    prospect_p = subparsers.add_parser("prospect",
                                       help="Full pipeline: comps + funders + grants")
    prospect_p.add_argument("--profile", type=str, required=True,
                            help="JSON search profile (name or path)")
    prospect_p.add_argument("--depth", type=int, default=2, choices=[1, 2, 3],
                            help="Pipeline depth: 1=comps, 2=+funders, 3=+grants (default: 2)")
    prospect_p.add_argument("--limit", type=int, default=25,
                            help="Max comparable orgs (default: 25)")
    prospect_p.add_argument("--detail", type=int, default=0,
                            help="Show detailed view for top N results")
    prospect_p.add_argument("--officers", action="store_true",
                            help="Fetch officer names from XML filings")
    prospect_p.add_argument("--score", action="store_true",
                            help="Use AI to score mission alignment (requires ANTHROPIC_API_KEY)")
    prospect_p.add_argument("--output", type=str, help="Export results to CSV file")

    # --- sector command (Reverse query) ---
    sector_p = subparsers.add_parser("sector",
                                     help="Reverse query: foundations funding a sector")
    sector_p.add_argument("--query", type=str, help="Search keyword (e.g. 'education')")
    sector_p.add_argument("--ntee", type=str, help="NTEE code (e.g. B for Education)")
    sector_p.add_argument("--state", type=str, help="Filter by state")
    sector_p.add_argument("--min-grants", type=float, dest="min_grants",
                          help="Minimum grant amount")
    sector_p.add_argument("--limit", type=int, default=20,
                          help="Max foundations to check (default: 20)")
    sector_p.add_argument("--output", type=str, help="Export results to CSV file")

    # --- profile command (auto-generate profile from EIN) ---
    profile_p = subparsers.add_parser("profile",
                                       help="Auto-generate a search profile from an EIN")
    profile_p.add_argument("--ein", type=str, required=True, help="Organization EIN")
    profile_p.add_argument("--output", type=str, help="Output JSON file path")

    # --- draft command (grant application draft) ---
    draft_p = subparsers.add_parser("draft",
                                     help="Generate a grant application draft (requires ANTHROPIC_API_KEY)")
    draft_p.add_argument("--ein", type=str, required=True,
                          help="Target foundation EIN to apply to")
    draft_p.add_argument("--profile", type=str, required=True,
                          help="Your org's JSON profile (name or path)")
    draft_p.add_argument("--output", type=str, help="Output file for draft text")

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        print("\nTip: Try  python cli.py search --profile math_agency  to get started")
        sys.exit(1)

    # Set up client with optional cache
    cache = None if args.no_cache else FilingCache()
    client = ProPublicaClient(delay=args.delay, cache=cache)

    if cache:
        stats = cache.stats()
        if any(stats.values()):
            print(f"  Cache: {stats['organizations']} orgs, {stats['xml_filings']} XMLs, "
                  f"{stats['parsed_grants']} grant sets")

    # Dispatch to command handler
    if args.command == "search":
        from src.commands.search import cmd_search, cmd_profile
        if args.profile:
            cmd_profile(args, client)
        elif args.query:
            cmd_search(args, client)
        else:
            print("Please provide --profile or --query")

    elif args.command == "org":
        from src.commands.search import cmd_org
        cmd_org(args, client)

    elif args.command == "funders":
        from src.commands.funders import cmd_funders
        cmd_funders(args, client)

    elif args.command == "grants":
        from src.commands.grants import cmd_grants
        cmd_grants(args, client)

    elif args.command == "prospect":
        from src.commands.prospect import cmd_prospect
        cmd_prospect(args, client)

    elif args.command == "sector":
        from src.commands.grants import cmd_sector
        cmd_sector(args, client)

    elif args.command == "profile":
        from src.commands.profile import cmd_profile_gen
        cmd_profile_gen(args, client)

    elif args.command == "draft":
        from src.commands.draft import cmd_draft
        cmd_draft(args, client)


if __name__ == "__main__":
    main()
