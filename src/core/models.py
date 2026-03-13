"""
Data models for the Foundation Finder.
All data classes used across the application.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Officer:
    """A person from Part VII of Form 990 (officers, directors, trustees, key employees)."""
    name: str
    title: str
    compensation: Optional[float] = None
    hours_per_week: Optional[float] = None
    related_org_compensation: Optional[float] = None
    other_compensation: Optional[float] = None


@dataclass
class Filing:
    """Financial data from a single 990 filing year."""
    tax_year: str
    revenue: Optional[float] = None
    contributions: Optional[float] = None
    assets: Optional[float] = None
    expenses: Optional[float] = None
    grants_paid: Optional[float] = None
    net_assets: Optional[float] = None
    form_type: Optional[str] = None
    pdf_url: Optional[str] = None
    xml_url: Optional[str] = None
    object_id: Optional[str] = None  # IRS filing ID, needed for XML download


@dataclass
class Grant:
    """A single grant from a foundation's Schedule I."""
    funder_name: str
    funder_ein: str
    recipient_name: str
    recipient_ein: Optional[str] = None
    amount: Optional[float] = None
    purpose: Optional[str] = None
    tax_year: Optional[str] = None
    recipient_city: Optional[str] = None
    recipient_state: Optional[str] = None


@dataclass
class Funder:
    """A foundation or donor identified through Layer 2 search."""
    name: str
    ein: str
    city: str = ""
    state: str = ""
    total_giving: Optional[float] = None
    grants_to_target: list = field(default_factory=list)  # List[Grant]
    all_grants: list = field(default_factory=list)  # List[Grant]
    latest_filing: Optional[Filing] = None
    officers: list = field(default_factory=list)   # List[Officer] from foundation's 990
    website: Optional[str] = None                  # Foundation website URL
    propublica_url: Optional[str] = None
    score: float = 0.0
    score_label: str = ""
    mission_score: Optional[int] = None       # AI alignment score (0-100)
    mission_rationale: Optional[str] = None   # AI one-sentence rationale


@dataclass
class ProspectResult:
    """Full pipeline output: a funder prospect with all supporting evidence."""
    funder: 'Funder' = None
    similar_orgs_funded: list = field(default_factory=list)   # List of org names
    total_giving_to_similar: float = 0.0
    mission_score: Optional[int] = None       # AI alignment score (0-100)
    mission_rationale: Optional[str] = None
    shared_board_members: list = field(default_factory=list)  # List of person names
    evidence_chain: str = ""   # Human-readable evidence summary


@dataclass
class Organization:
    """A nonprofit organization with financials, officers, and funder data."""
    name: str
    ein: str
    city: str
    state: str
    ntee_code: Optional[str] = None
    mission: Optional[str] = None
    latest_filing: Optional[Filing] = None
    officers: list = field(default_factory=list)   # List[Officer]
    filings: list = field(default_factory=list)     # List[Filing]
    funders: list = field(default_factory=list)     # List[Funder]
    propublica_url: Optional[str] = None
    website: Optional[str] = None
    score: float = 0.0
    score_label: str = ""
    relevance: str = ""
    similarity_note: str = ""
