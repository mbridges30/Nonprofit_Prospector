"""
Auto-generate a search profile from an organization's EIN.
Looks up the org via ProPublica and creates a ready-to-use JSON profile.
"""

import json
import os
import re

from src.api.propublica import ProPublicaClient


# Common NTEE major categories
_NTEE_NAMES = {
    "A": "Arts, Culture, Humanities",
    "B": "Education",
    "C": "Environment",
    "D": "Animal-Related",
    "E": "Health Care",
    "F": "Mental Health",
    "G": "Disease/Disorders",
    "H": "Medical Research",
    "I": "Crime/Legal",
    "J": "Employment",
    "K": "Food/Agriculture/Nutrition",
    "L": "Housing/Shelter",
    "M": "Public Safety",
    "N": "Recreation/Sports",
    "O": "Youth Development",
    "P": "Human Services",
    "Q": "International",
    "R": "Civil Rights",
    "S": "Community Improvement",
    "T": "Philanthropy/Voluntarism",
    "U": "Science/Technology",
    "V": "Social Science",
    "W": "Public/Society Benefit",
    "X": "Religion",
    "Y": "Mutual/Membership Benefit",
    "Z": "Unknown",
}

# Stop words for keyword extraction
_STOP_WORDS = {
    "the", "of", "and", "for", "in", "a", "an", "to", "inc", "co", "org",
    "foundation", "fund", "trust", "association", "society", "corporation",
    "national", "american", "united", "international", "community",
}


def _extract_keywords(name: str, mission: str = "") -> list:
    """Generate search keywords from org name and mission."""
    text = f"{name} {mission}".lower()
    words = re.findall(r"[a-z]+", text)
    keywords = [w for w in words if len(w) >= 4 and w not in _STOP_WORDS]
    # Deduplicate while preserving order
    seen = set()
    unique = []
    for w in keywords:
        if w not in seen:
            seen.add(w)
            unique.append(w)
    return unique[:10]


def cmd_profile_gen(args, client: ProPublicaClient):
    """Auto-generate a search profile from an EIN."""
    ein = args.ein.replace("-", "")
    print(f"\nLooking up EIN: {ein}")

    detail = client.get_organization(ein)
    if not detail:
        print(f"  Organization not found: {args.ein}")
        return

    org_info = detail.get("organization", {})
    name = org_info.get("name", "Unknown")
    city = org_info.get("city", "")
    state = org_info.get("state", "")
    ntee_code = org_info.get("ntee_code", "")
    mission = org_info.get("mission", "") or ""

    # Determine NTEE category
    ntee_major = ntee_code[0].upper() if ntee_code else ""
    ntee_name = _NTEE_NAMES.get(ntee_major, "")

    print(f"  Name: {name}")
    print(f"  Location: {city}, {state}")
    print(f"  NTEE: {ntee_code} ({ntee_name})" if ntee_code else "  NTEE: not available")
    if mission:
        print(f"  Mission: {mission[:100]}...")

    # Build keywords
    keywords = _extract_keywords(name, mission)
    if ntee_name and ntee_major:
        # Add NTEE category name words as keywords
        for w in ntee_name.lower().split("/"):
            w = w.strip()
            if len(w) >= 4 and w not in keywords:
                keywords.append(w)

    print(f"  Keywords: {', '.join(keywords)}")

    # Build profile
    profile = {
        "name": name,
        "ein": ein,
        "city": city,
        "state": state,
        "mission": mission or f"{name} - {ntee_name}" if ntee_name else name,
        "keywords": keywords,
        "ntee_codes": [ntee_code] if ntee_code else [],
        "search_states": [state] if state else [""],
    }

    # Determine output path
    output = args.output
    if not output:
        safe_name = re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')[:40]
        output = os.path.join("profiles", f"{safe_name}.json")

    # Ensure directory exists
    os.makedirs(os.path.dirname(output) or ".", exist_ok=True)

    with open(output, "w", encoding="utf-8") as f:
        json.dump(profile, f, indent=2)

    print(f"\n  Profile saved to {output}")
    print(f"  To run: python cli.py prospect --profile {output}")

    return profile
