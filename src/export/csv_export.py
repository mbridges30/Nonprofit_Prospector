"""
CSV export for prospect lists, funder lists, and grant lists.
"""

import csv
from src.core.models import Organization, Funder, Grant


def export_prospects_csv(orgs: list, filename: str):
    """Export prospect list (Layer 1 orgs) to CSV."""
    if not orgs:
        print("  No organizations to export.")
        return

    rows = []
    for org in orgs:
        f = org.latest_filing
        officers_str = "; ".join(
            f"{o.name} ({o.title})" for o in org.officers[:5]
        )
        funders_str = "; ".join(
            f"{fdr.name}" for fdr in org.funders[:5]
        )
        rows.append({
            "priority": org.score_label,
            "score": org.score,
            "name": org.name,
            "ein": org.ein,
            "city": org.city,
            "state": org.state,
            "ntee_code": org.ntee_code or "",
            "revenue": f.revenue if f else "",
            "contributions": f.contributions if f else "",
            "assets": f.assets if f else "",
            "expenses": f.expenses if f else "",
            "grants_paid": f.grants_paid if f else "",
            "net_assets": f.net_assets if f else "",
            "fiscal_year": f.tax_year if f else "",
            "key_people": officers_str,
            "known_funders": funders_str,
            "propublica_url": org.propublica_url or "",
        })

    _write_csv(rows, filename)


def export_funders_csv(funders: list, filename: str):
    """Export funder prospect list (Layer 2) to CSV."""
    if not funders:
        print("  No funders to export.")
        return

    rows = []
    for funder in funders:
        f = funder.latest_filing
        target_total = sum(g.amount or 0 for g in funder.grants_to_target)
        all_total = funder.total_giving or sum(g.amount or 0 for g in funder.all_grants)
        target_orgs = "; ".join(
            f"{g.recipient_name} ({g.amount or 0:,.0f})" for g in funder.grants_to_target[:10]
        )
        rows.append({
            "priority": funder.score_label,
            "score": funder.score,
            "funder_name": funder.name,
            "ein": funder.ein,
            "city": funder.city,
            "state": funder.state,
            "grants_to_similar_orgs": target_total,
            "total_giving": all_total,
            "num_grants": len(funder.all_grants),
            "revenue": f.revenue if f else "",
            "assets": f.assets if f else "",
            "grants_paid": f.grants_paid if f else "",
            "fiscal_year": f.tax_year if f else "",
            "funded_similar_orgs": target_orgs,
            "propublica_url": funder.propublica_url or "",
        })

    _write_csv(rows, filename)


def export_grants_csv(grants: list, filename: str):
    """Export grant list (Layer 3) to CSV."""
    if not grants:
        print("  No grants to export.")
        return

    rows = []
    for g in grants:
        rows.append({
            "funder_name": g.funder_name,
            "funder_ein": g.funder_ein,
            "recipient_name": g.recipient_name,
            "recipient_ein": g.recipient_ein or "",
            "amount": g.amount or "",
            "purpose": g.purpose or "",
            "tax_year": g.tax_year or "",
            "recipient_city": g.recipient_city or "",
            "recipient_state": g.recipient_state or "",
        })

    _write_csv(rows, filename)


def export_full_pipeline_csv(orgs: list, funders: list, filename: str):
    """Export the full pipeline result: orgs + their funders + evidence chain."""
    if not funders:
        print("  No pipeline data to export.")
        return

    rows = []
    for funder in funders:
        f = funder.latest_filing
        target_total = sum(g.amount or 0 for g in funder.grants_to_target)

        # Build evidence chain
        evidence_parts = []
        for g in funder.grants_to_target[:5]:
            evidence_parts.append(
                f"{g.recipient_name}: ${g.amount or 0:,.0f} ({g.tax_year})"
            )
        evidence = "; ".join(evidence_parts)

        rows.append({
            "priority": funder.score_label,
            "score": funder.score,
            "funder_name": funder.name,
            "funder_ein": funder.ein,
            "funder_city": funder.city,
            "funder_state": funder.state,
            "grants_to_similar_orgs": target_total,
            "evidence_chain": evidence,
            "total_giving": funder.total_giving or sum(g.amount or 0 for g in funder.all_grants),
            "num_total_grants": len(funder.all_grants),
            "assets": f.assets if f else "",
            "mission_score": getattr(funder, 'mission_score', "") or "",
            "mission_rationale": getattr(funder, 'mission_rationale', "") or "",
            "propublica_url": funder.propublica_url or "",
        })

    _write_csv(rows, filename)


def _write_csv(rows: list, filename: str):
    """Write rows to CSV file."""
    if not rows:
        return

    with open(filename, "w", newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nExported {len(rows)} rows to {filename}")
