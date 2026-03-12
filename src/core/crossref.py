"""
Board member cross-referencing across organizations.
Finds people who serve on multiple boards in the prospect set,
which signals potential advocacy or shared networks.
"""

import re
from collections import defaultdict
from typing import Optional

from src.core.models import Organization, Officer
from src.core.matching import normalize_name


def _normalize_person_name(name: str) -> str:
    """Normalize a person's name for deduplication.
    Strips suffixes like Jr, Sr, III, MD, etc.
    """
    if not name:
        return ""
    s = name.lower().strip()
    # Remove common suffixes
    s = re.sub(r'\b(jr|sr|ii|iii|iv|md|phd|esq|cpa|jd)\b', '', s, flags=re.IGNORECASE)
    # Remove middle initials (single letter followed by period or space)
    s = re.sub(r'\b[a-z]\.\s*', ' ', s)
    # Collapse whitespace
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def find_shared_board_members(orgs: list) -> dict:
    """Find people who appear on multiple boards across the org set.

    Args:
        orgs: List of Organization objects with officers populated.

    Returns:
        Dict mapping normalized person name -> list of dicts with:
            - org_name, org_ein, title, compensation
        Only includes people appearing in 2+ orgs.
    """
    # Build person -> org mapping
    person_map = defaultdict(list)

    for org in orgs:
        if not org.officers:
            continue
        seen_in_org = set()
        for officer in org.officers:
            norm = _normalize_person_name(officer.name)
            if not norm or norm in seen_in_org:
                continue
            seen_in_org.add(norm)
            person_map[norm].append({
                "org_name": org.name,
                "org_ein": org.ein,
                "title": officer.title,
                "compensation": officer.compensation,
                "original_name": officer.name,
            })

    # Filter to people on 2+ boards
    shared = {
        name: entries
        for name, entries in person_map.items()
        if len(entries) >= 2
    }

    return shared


def format_shared_members(shared: dict) -> str:
    """Format shared board members for terminal display."""
    if not shared:
        return "  No shared board members found across the prospect set."

    lines = []
    # Sort by number of connections (most connected first)
    sorted_members = sorted(shared.items(), key=lambda x: len(x[1]), reverse=True)

    for name, entries in sorted_members:
        display_name = entries[0]["original_name"]  # Use first original spelling
        lines.append(f"\n  {display_name} ({len(entries)} boards):")
        for e in entries:
            comp = f" - ${e['compensation']:,.0f}" if e['compensation'] else ""
            lines.append(f"    -> {e['org_name']} ({e['title']}{comp})")

    return "\n".join(lines)


def shared_members_csv_rows(shared: dict) -> list:
    """Convert shared board members to CSV-ready rows."""
    rows = []
    for name, entries in shared.items():
        display_name = entries[0]["original_name"]
        org_list = "; ".join(f"{e['org_name']} ({e['title']})" for e in entries)
        rows.append({
            "person_name": display_name,
            "num_boards": len(entries),
            "organizations": org_list,
        })
    return sorted(rows, key=lambda r: r["num_boards"], reverse=True)
