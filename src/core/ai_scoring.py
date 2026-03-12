"""
AI-powered mission alignment scoring using Anthropic Claude API.
Scores how well a prospect foundation's mission and giving patterns
align with the target org's mission.
"""

import os
from typing import Optional

try:
    import anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False


# Model to use for scoring (cost-efficient)
MODEL = "claude-sonnet-4-20250514"
MAX_TOKENS = 300


def is_available() -> bool:
    """Check if AI scoring is available (API key set and library installed)."""
    if not HAS_ANTHROPIC:
        return False
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def score_mission_alignment(
    target_name: str,
    target_mission: str,
    prospect_name: str,
    prospect_grants: list = None,
    prospect_mission: str = "",
) -> Optional[dict]:
    """Score how well a prospect foundation aligns with the target org.

    Args:
        target_name: Target org name
        target_mission: Target org mission statement
        prospect_name: Prospect foundation name
        prospect_grants: List of Grant objects from the prospect
        prospect_mission: Prospect's mission (if known)

    Returns:
        Dict with 'score' (0-100) and 'rationale' (one sentence),
        or None if scoring fails.
    """
    if not is_available():
        return None

    # Build context about the prospect's giving
    grant_context = ""
    if prospect_grants:
        grant_lines = []
        for g in prospect_grants[:15]:  # Limit to 15 for token efficiency
            amt = f"${g.amount:,.0f}" if g.amount else "unknown amount"
            grant_lines.append(f"- {g.recipient_name}: {amt} ({g.purpose or 'no purpose listed'})")
        grant_context = f"\nRecent grants by {prospect_name}:\n" + "\n".join(grant_lines)

    prospect_desc = prospect_mission if prospect_mission else f"(No mission statement available for {prospect_name})"

    prompt = f"""Score the mission alignment between a nonprofit seeking funding and a potential funder foundation.

TARGET NONPROFIT:
Name: {target_name}
Mission: {target_mission}

PROSPECT FUNDER:
Name: {prospect_name}
Mission/Description: {prospect_desc}
{grant_context}

Rate the alignment from 0-100 where:
- 90-100: Perfect match, they fund exactly this type of work
- 70-89: Strong alignment, overlapping priorities
- 50-69: Moderate alignment, some shared interests
- 30-49: Weak alignment, tangential connection
- 0-29: Poor alignment, different focus areas

Respond in EXACTLY this format (two lines only):
SCORE: [number]
RATIONALE: [one sentence explaining why]"""

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )

        text = response.content[0].text.strip()
        lines = text.strip().split("\n")

        score = None
        rationale = ""

        for line in lines:
            if line.startswith("SCORE:"):
                try:
                    score = int(line.replace("SCORE:", "").strip())
                    score = max(0, min(100, score))  # Clamp to 0-100
                except ValueError:
                    pass
            elif line.startswith("RATIONALE:"):
                rationale = line.replace("RATIONALE:", "").strip()

        if score is not None:
            return {"score": score, "rationale": rationale}

    except Exception as e:
        print(f"    [AI scoring error] {e}")

    return None


def batch_score_funders(target_name: str, target_mission: str,
                        funders: list, max_score: int = 20) -> int:
    """Score mission alignment for a batch of funders.

    Args:
        target_name: Target org name
        target_mission: Target org mission
        funders: List of Funder objects to score
        max_score: Max funders to score (API cost control)

    Returns:
        Number of funders successfully scored
    """
    if not is_available():
        print("  AI scoring unavailable. Set ANTHROPIC_API_KEY environment variable.")
        print("  Install: pip install anthropic")
        return 0

    scored = 0
    for i, funder in enumerate(funders[:max_score]):
        print(f"  [{i+1}/{min(len(funders), max_score)}] AI scoring {funder.name}...")

        result = score_mission_alignment(
            target_name=target_name,
            target_mission=target_mission,
            prospect_name=funder.name,
            prospect_grants=funder.all_grants[:10],
        )

        if result:
            funder.mission_score = result["score"]
            funder.mission_rationale = result["rationale"]
            scored += 1
            print(f"    Score: {result['score']}/100 - {result['rationale']}")

    return scored
