"""
Formatted terminal output for prospect explorer results.
"""

import sys
import io

from src.core.models import Organization, Funder, Grant

# Fix Windows console encoding for Unicode characters
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


def fmt_dollar(n) -> str:
    if n is None:
        return "N/A"
    return f"${n:,.0f}"


def fmt_short(n) -> str:
    if n is None:
        return "--"
    if n >= 1e9:
        return f"${n/1e9:.1f}B"
    if n >= 1e6:
        return f"${n/1e6:.1f}M"
    if n >= 1e3:
        return f"${n/1e3:.0f}K"
    return f"${n:,.0f}"


def print_summary_table(orgs: list):
    """Print a formatted summary table of prospects."""
    print("\n" + "=" * 105)
    print("PROSPECT SUMMARY")
    print("=" * 105)
    print(
        f"{'#':<4} {'Priority':<14} {'Organization':<35} "
        f"{'Revenue':<12} {'Contributions':<14} {'State':<6} {'FY':<6}"
    )
    print("-" * 105)

    for i, org in enumerate(orgs, 1):
        f = org.latest_filing
        print(
            f"{i:<4} "
            f"{org.score_label:<14} "
            f"{org.name[:34]:<35} "
            f"{fmt_short(f.revenue if f else None):<12} "
            f"{fmt_short(f.contributions if f else None):<14} "
            f"{org.state:<6} "
            f"{f.tax_year if f else '--':<6}"
        )

    print("-" * 105)
    print(f"Total: {len(orgs)} organizations\n")


def print_org_detail(org: Organization):
    """Print detailed view of a single organization."""
    print(f"\n{'─' * 70}")
    print(f"  {org.name}")
    print(f"  {org.city}, {org.state} | EIN: {org.ein}")
    print(f"  Priority: {org.score_label} (score: {org.score})")
    if org.propublica_url:
        print(f"  ProPublica: {org.propublica_url}")

    f = org.latest_filing
    if f:
        print(f"\n  FINANCIALS (FY {f.tax_year})")
        print(f"    Revenue:        {fmt_dollar(f.revenue)}")
        print(f"    Contributions:  {fmt_dollar(f.contributions)}")
        print(f"    Total Assets:   {fmt_dollar(f.assets)}")
        print(f"    Total Expenses: {fmt_dollar(f.expenses)}")
        print(f"    Grants Paid:    {fmt_dollar(f.grants_paid)}")
        print(f"    Net Assets:     {fmt_dollar(f.net_assets)}")

    if org.officers:
        print(f"\n  KEY PEOPLE ({len(org.officers)})")
        for o in org.officers[:10]:
            parts = [f"    {o.name}"]
            if o.title:
                parts.append(f" - {o.title}")
            if o.compensation and o.compensation > 0:
                parts.append(f"  ({fmt_dollar(o.compensation)})")
            if o.hours_per_week:
                parts.append(f"  [{o.hours_per_week}h/wk]")
            print("".join(parts))

    if len(org.filings) > 1:
        print(f"\n  REVENUE TREND")
        max_rev = max((fl.revenue or 0) for fl in org.filings) or 1
        for fl in org.filings:
            bar_len = int((fl.revenue or 0) / max_rev * 30)
            bar = "\u2588" * max(bar_len, 1)
            print(f"    FY {fl.tax_year}: {fmt_short(fl.revenue):<10} {bar}")

    if org.funders:
        print(f"\n  KNOWN FUNDERS ({len(org.funders)})")
        for funder in org.funders[:10]:
            grants_str = ", ".join(
                f"{fmt_short(g.amount)} ({g.tax_year})" for g in funder.grants_to_target[:3]
            )
            print(f"    {funder.name} | {grants_str}")

    print(f"{'─' * 70}")


def print_funder_detail(funder: Funder):
    """Print detailed view of a funder/foundation."""
    print(f"\n{'━' * 70}")
    print(f"  {funder.name}")
    print(f"  {funder.city}, {funder.state} | EIN: {funder.ein}")
    print(f"  Prospect Score: {funder.score_label} ({funder.score})")
    if funder.propublica_url:
        print(f"  ProPublica: {funder.propublica_url}")

    f = funder.latest_filing
    if f:
        print(f"\n  FOUNDATION FINANCIALS (FY {f.tax_year})")
        print(f"    Revenue:        {fmt_dollar(f.revenue)}")
        print(f"    Total Assets:   {fmt_dollar(f.assets)}")
        print(f"    Grants Paid:    {fmt_dollar(f.grants_paid)}")

    if funder.grants_to_target:
        print(f"\n  GRANTS TO TARGET/SIMILAR ORGS ({len(funder.grants_to_target)})")
        for g in funder.grants_to_target:
            purpose_str = f" - {g.purpose[:60]}" if g.purpose else ""
            print(f"    {g.recipient_name}: {fmt_dollar(g.amount)} ({g.tax_year}){purpose_str}")

    if funder.all_grants:
        total = sum(g.amount or 0 for g in funder.all_grants)
        print(f"\n  ALL GRANTS ({len(funder.all_grants)} total, {fmt_dollar(total)})")
        # Show top 10 by amount
        sorted_grants = sorted(funder.all_grants, key=lambda g: g.amount or 0, reverse=True)
        for g in sorted_grants[:10]:
            loc = f" ({g.recipient_state})" if g.recipient_state else ""
            print(f"    {g.recipient_name}{loc}: {fmt_dollar(g.amount)}")
        if len(funder.all_grants) > 10:
            print(f"    ... and {len(funder.all_grants) - 10} more grants")

    print(f"{'━' * 70}")


def print_funders_table(funders: list):
    """Print summary table of funders."""
    print("\n" + "=" * 110)
    print("FUNDER PROSPECT LIST")
    print("=" * 110)
    print(
        f"{'#':<4} {'Priority':<14} {'Foundation':<35} "
        f"{'Grants to Similar':<18} {'Total Giving':<14} {'State':<6} {'# Grants':<8}"
    )
    print("-" * 110)

    for i, funder in enumerate(funders, 1):
        target_total = sum(g.amount or 0 for g in funder.grants_to_target)
        all_total = funder.total_giving or sum(g.amount or 0 for g in funder.all_grants)
        # Show grantee count instead of $0 when amounts aren't available
        target_display = fmt_short(target_total) if target_total else \
            (f"{len(funder.grants_to_target)} matches" if funder.grants_to_target else "--")
        all_display = fmt_short(all_total) if all_total else \
            (f"{len(funder.all_grants)} grantees" if funder.all_grants else "--")
        print(
            f"{i:<4} "
            f"{funder.score_label:<14} "
            f"{funder.name[:34]:<35} "
            f"{target_display:<18} "
            f"{all_display:<14} "
            f"{funder.state:<6} "
            f"{len(funder.all_grants):<8}"
        )

    print("-" * 110)
    print(f"Total: {len(funders)} funders identified\n")


def print_grants_table(grants: list, funder_name: str = ""):
    """Print table of grants from a foundation."""
    if funder_name:
        print(f"\nGrants by {funder_name}")
    print("=" * 100)
    print(
        f"{'#':<4} {'Recipient':<40} {'Amount':<14} "
        f"{'State':<6} {'Year':<6} {'Purpose':<28}"
    )
    print("-" * 100)

    for i, g in enumerate(grants, 1):
        purpose = (g.purpose or "")[:27]
        print(
            f"{i:<4} "
            f"{g.recipient_name[:39]:<40} "
            f"{fmt_short(g.amount):<14} "
            f"{(g.recipient_state or ''):<6} "
            f"{(g.tax_year or ''):<6} "
            f"{purpose:<28}"
        )

    total = sum(g.amount or 0 for g in grants)
    print("-" * 100)
    print(f"Total: {len(grants)} grants, {fmt_dollar(total)}\n")


def print_prospect_chain(org: Organization, funder: Funder, target_name: str = ""):
    """Print the chain of evidence: Target -> Comp Org -> Funder."""
    grants_str = ", ".join(
        f"{fmt_short(g.amount)} ({g.tax_year})" for g in funder.grants_to_target[:3]
    )
    print(
        f"  {funder.name} gave {grants_str} to {org.name}"
    )
    if org.similarity_note:
        print(f"    {org.name} is similar to {target_name}: {org.similarity_note}")
