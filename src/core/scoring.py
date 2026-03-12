"""
Prospect scoring and ranking logic.
Scores organizations and funders based on financial indicators and relevance.
"""

from src.core.models import Organization, Funder


def score_organization(org: Organization):
    """Score an org based on financial indicators for prospect quality.
    Sets org.score (0-10) and org.score_label."""
    f = org.latest_filing
    if not f:
        org.score = 0
        org.score_label = "No Data"
        return

    s = 0.0

    # Revenue score (0-3)
    rev = f.revenue or 0
    if rev > 10_000_000:
        s += 3
    elif rev > 1_000_000:
        s += 2
    elif rev > 200_000:
        s += 1

    # Contributions score (0-3)
    contrib = f.contributions or 0
    if contrib > 5_000_000:
        s += 3
    elif contrib > 500_000:
        s += 2
    elif contrib > 50_000:
        s += 1

    # Assets score (0-2)
    assets = f.assets or 0
    if assets > 10_000_000:
        s += 2
    elif assets > 1_000_000:
        s += 1

    # Grants paid score (0-2) - indicates grant-making capacity
    grants = f.grants_paid or 0
    if grants > 500_000:
        s += 2
    elif grants > 50_000:
        s += 1

    org.score = s
    if s >= 7:
        org.score_label = "HIGH PRIORITY"
    elif s >= 4:
        org.score_label = "STRONG"
    elif s >= 2:
        org.score_label = "MODERATE"
    else:
        org.score_label = "SMALL"


def score_funder(funder: Funder, target_state: str = "", sector_match: bool = False):
    """Score a funder based on giving patterns and relevance.
    Sets funder.score (0-22) and funder.score_label.

    Args:
        sector_match: True if this funder was found via sector search
                      (grants_to_target are to similar orgs, not the target itself)
    """
    s = 0.0

    # Total giving to target/similar orgs (0-4)
    target_total = sum(g.amount or 0 for g in funder.grants_to_target)
    if target_total > 1_000_000:
        s += 4
    elif target_total > 500_000:
        s += 3
    elif target_total > 100_000:
        s += 2
    elif target_total > 10_000:
        s += 1

    # Number of grants to target/similar orgs (0-3)
    n_grants = len(funder.grants_to_target)
    if n_grants >= 5:
        s += 3
    elif n_grants >= 3:
        s += 2
    elif n_grants >= 1:
        s += 1

    # Web evidence bonus (0-4): confirmed comp org match from grant page
    # Web-scraped grants typically have amount=None, so dollar-based scoring
    # misses them. This bonus ensures web-confirmed matches are valued.
    web_grants = [g for g in funder.grants_to_target if g.amount is None]
    if web_grants:
        # Each web-confirmed match is strong evidence (grant page listed them)
        if len(web_grants) >= 3:
            s += 4  # Multiple confirmed matches = very strong
        elif len(web_grants) >= 2:
            s += 3
        else:
            s += 2  # Even one confirmed web match is significant

    # Sector match bonus (0-3): funds orgs in the same sector
    if sector_match and n_grants >= 1:
        if n_grants >= 5:
            s += 3
        elif n_grants >= 2:
            s += 2
        else:
            s += 1

    # Overall giving capacity from filing (0-3)
    f = funder.latest_filing
    if f:
        total = f.grants_paid or f.expenses or 0
        if total > 10_000_000:
            s += 3
        elif total > 1_000_000:
            s += 2
        elif total > 100_000:
            s += 1

    # Total giving across all grantees (0-3)
    all_total = funder.total_giving or sum(g.amount or 0 for g in funder.all_grants)
    if all_total > 50_000_000:
        s += 3
    elif all_total > 10_000_000:
        s += 2
    elif all_total > 1_000_000:
        s += 1

    # Grantee portfolio size as capacity proxy (0-2)
    # When financial data is unavailable, the number of grantees
    # on a foundation's grant page signals giving capacity.
    n_all = len(funder.all_grants)
    if n_all >= 500:
        s += 2  # Large foundation (e.g. Simons, Gates)
    elif n_all >= 50:
        s += 1  # Mid-size foundation

    # Geographic proximity bonus (0-2)
    if target_state and funder.state:
        if funder.state.upper() == target_state.upper():
            s += 2
        # Check if any grants went to target state
        elif any(g.recipient_state and g.recipient_state.upper() == target_state.upper()
                 for g in funder.all_grants):
            s += 1

    funder.score = s
    if s >= 10:
        funder.score_label = "TOP PROSPECT"
    elif s >= 7:
        funder.score_label = "HIGH PRIORITY"
    elif s >= 4:
        funder.score_label = "STRONG"
    elif s >= 2:
        funder.score_label = "MODERATE"
    else:
        funder.score_label = "LOW"
