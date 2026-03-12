"""
IRS 990 XML parser for officer extraction (Part VII) and
Schedule I grant parsing (critical for Layers 2 and 3).

Handles Form 990, 990-EZ, and 990-PF XML schemas.
Uses lxml for robust namespace-aware parsing.
"""

from typing import Optional
from lxml import etree

from src.core.models import Officer, Grant


# Common IRS 990 XML namespaces
IRS_NS = {
    "irs": "http://www.irs.gov/efile",
}


def _safe_text(element, xpath: str, namespaces: dict = None) -> Optional[str]:
    """Safely extract text from an XPath match."""
    if element is None:
        return None
    ns = namespaces or IRS_NS
    found = element.xpath(xpath, namespaces=ns)
    if found:
        text = found[0].text if hasattr(found[0], "text") else str(found[0])
        return text.strip() if text else None
    return None


def _safe_float(element, xpath: str, namespaces: dict = None) -> Optional[float]:
    """Safely extract a float from an XPath match."""
    text = _safe_text(element, xpath, namespaces)
    if text:
        try:
            return float(text.replace(",", ""))
        except (ValueError, TypeError):
            pass
    return None


def _find_all(root, xpaths: list, namespaces: dict = None) -> list:
    """Try multiple XPaths and return results from the first that matches."""
    ns = namespaces or IRS_NS
    for xpath in xpaths:
        try:
            results = root.xpath(xpath, namespaces=ns)
            if results:
                return results
        except etree.XPathError:
            continue
    return []


def parse_officers_from_xml(xml_bytes: bytes) -> list:
    """Parse Part VII officers/directors/trustees from a 990 XML filing.
    Returns list of Officer objects."""
    try:
        root = etree.fromstring(xml_bytes)
    except etree.XMLSyntaxError as e:
        print(f"  [XML parse error] {e}")
        return []

    officers = []

    # Try multiple XPath patterns for different 990 versions
    person_xpaths = [
        # Form 990 Part VII
        "//irs:Form990PartVIISectionAGrp",
        "//irs:OfficerDirectorTrusteeEmplGrp",
        # Older schemas
        "//irs:Form990PartVIISectionA",
        # 990-EZ
        "//irs:OfficerDirectorTrusteeKeyEmpl",
        "//irs:OfficersDirectorsTrusteesEtc",
        # 990-PF
        "//irs:OfcrDirTrusteesKeyEmployeeInfo",
        "//irs:OfficerDirTrstKeyEmplInfoGrp",
        # Generic fallbacks
        "//irs:CompensationHighestPaidEmplGrp",
    ]

    people = _find_all(root, person_xpaths)

    for person in people:
        # Extract name - multiple possible element names
        name = (
            _safe_text(person, ".//irs:PersonNm")
            or _safe_text(person, ".//irs:NamePerson")
            or _safe_text(person, ".//irs:PersonName")
            or _safe_text(person, ".//irs:Name")
            or _safe_text(person, ".//irs:BusinessNameLine1Txt")
            or _safe_text(person, ".//irs:BusinessNameLine1")
            or _safe_text(person, ".//irs:NameBusiness/irs:BusinessNameLine1Txt")
        )
        if not name:
            continue

        # Extract title
        title = (
            _safe_text(person, ".//irs:TitleTxt")
            or _safe_text(person, ".//irs:Title")
            or _safe_text(person, ".//irs:PersonTitle")
            or _safe_text(person, ".//irs:TitleOfPosition")
            or ""
        )

        # Extract compensation
        compensation = (
            _safe_float(person, ".//irs:ReportableCompFromOrgAmt")
            or _safe_float(person, ".//irs:CompensationAmt")
            or _safe_float(person, ".//irs:Compensation")
            or _safe_float(person, ".//irs:CompensationOfHighestPaidEmpl")
        )

        # Hours per week
        hours = (
            _safe_float(person, ".//irs:AverageHoursPerWeekRt")
            or _safe_float(person, ".//irs:AverageHrsPerWkDevotedToPosRt")
            or _safe_float(person, ".//irs:AvgHoursPerWkDevotedToPosition")
        )

        # Related org compensation
        related_comp = (
            _safe_float(person, ".//irs:ReportableCompFromRltdOrgAmt")
            or _safe_float(person, ".//irs:CompensationFromOtherSrcsAmt")
        )

        # Other compensation
        other_comp = (
            _safe_float(person, ".//irs:OtherCompensationAmt")
            or _safe_float(person, ".//irs:OtherCompensation")
        )

        officers.append(Officer(
            name=name,
            title=title,
            compensation=compensation,
            hours_per_week=hours,
            related_org_compensation=related_comp,
            other_compensation=other_comp,
        ))

    return officers


def parse_schedule_i_grants(xml_bytes: bytes, funder_name: str = "",
                            funder_ein: str = "") -> list:
    """Parse Schedule I grants from a 990 or 990-PF XML filing.
    Returns list of Grant objects.

    For Form 990: Schedule I (Grants and Other Assistance)
    For Form 990-PF: Part XV (Grants and Contributions Paid)
    """
    try:
        root = etree.fromstring(xml_bytes)
    except etree.XMLSyntaxError as e:
        print(f"  [XML parse error] {e}")
        return []

    grants = []

    # Extract tax year
    tax_year = (
        _safe_text(root, "//irs:TaxYr")
        or _safe_text(root, "//irs:TaxPeriodEndDt")
        or _safe_text(root, "//irs:TaxYear")
        or ""
    )
    if tax_year and len(tax_year) > 4:
        tax_year = tax_year[:4]

    # --- Form 990 Schedule I grants ---
    schedule_i_xpaths = [
        "//irs:GrantOrContributionPdDurYrGrp",
        "//irs:RecipientTable",
        "//irs:GrantsOtherAsstToIndivInUSGrp",
        "//irs:GrantsOtherAsstToGovt",
    ]
    grant_entries = _find_all(root, schedule_i_xpaths)

    for entry in grant_entries:
        recipient_name = (
            _safe_text(entry, ".//irs:RecipientBusinessName/irs:BusinessNameLine1Txt")
            or _safe_text(entry, ".//irs:RecipientBusinessName/irs:BusinessNameLine1")
            or _safe_text(entry, ".//irs:RecipientPersonNm")
            or _safe_text(entry, ".//irs:RecipientNameBusiness/irs:BusinessNameLine1Txt")
            or _safe_text(entry, ".//irs:RecipientNameBusiness/irs:BusinessNameLine1")
            or ""
        )
        if not recipient_name:
            continue

        recipient_ein = (
            _safe_text(entry, ".//irs:RecipientEIN")
            or _safe_text(entry, ".//irs:EINOfRecipient")
        )

        amount = (
            _safe_float(entry, ".//irs:Amt")
            or _safe_float(entry, ".//irs:CashGrantAmt")
            or _safe_float(entry, ".//irs:AmountOfCashGrant")
            or _safe_float(entry, ".//irs:GrantOrContributionAmt")
        )

        purpose = (
            _safe_text(entry, ".//irs:GrantOrContributionPurposeTxt")
            or _safe_text(entry, ".//irs:PurposeOfGrantTxt")
            or _safe_text(entry, ".//irs:PurposeOfGrant")
            or ""
        )

        city = (
            _safe_text(entry, ".//irs:RecipientUSAddress/irs:CityNm")
            or _safe_text(entry, ".//irs:USAddress/irs:CityNm")
            or _safe_text(entry, ".//irs:RecipientUSAddress/irs:City")
        )
        state = (
            _safe_text(entry, ".//irs:RecipientUSAddress/irs:StateAbbreviationCd")
            or _safe_text(entry, ".//irs:USAddress/irs:StateAbbreviationCd")
            or _safe_text(entry, ".//irs:RecipientUSAddress/irs:State")
        )

        grants.append(Grant(
            funder_name=funder_name,
            funder_ein=funder_ein,
            recipient_name=recipient_name,
            recipient_ein=recipient_ein,
            amount=amount,
            purpose=purpose,
            tax_year=tax_year,
            recipient_city=city,
            recipient_state=state,
        ))

    # --- 990-PF Part XV grants (if no Schedule I grants found) ---
    if not grants:
        pf_xpaths = [
            "//irs:GrantOrContriApprvForFutGrp",
            "//irs:GrantOrContriPdDurYrGrp",
            "//irs:GrantOrContributionPdDurYr",
            "//irs:SupplementaryInformationGrp/irs:GrantOrContriPdDurYrGrp",
        ]
        pf_entries = _find_all(root, pf_xpaths)

        for entry in pf_entries:
            recipient_name = (
                _safe_text(entry, ".//irs:RecipientPersonNm")
                or _safe_text(entry, ".//irs:RecipientBusinessName/irs:BusinessNameLine1Txt")
                or _safe_text(entry, ".//irs:RecipientBusinessName/irs:BusinessNameLine1")
                or ""
            )
            if not recipient_name:
                continue

            recipient_ein = _safe_text(entry, ".//irs:RecipientEIN")

            amount = (
                _safe_float(entry, ".//irs:Amt")
                or _safe_float(entry, ".//irs:Amount")
                or _safe_float(entry, ".//irs:GrantOrContributionAmt")
            )

            purpose = (
                _safe_text(entry, ".//irs:GrantOrContributionPurposeTxt")
                or _safe_text(entry, ".//irs:PurposeOfGrantOrContriTxt")
                or _safe_text(entry, ".//irs:PurposeOfGrantTxt")
                or ""
            )

            city = _safe_text(entry, ".//irs:RecipientUSAddress/irs:CityNm")
            state = _safe_text(entry, ".//irs:RecipientUSAddress/irs:StateAbbreviationCd")

            grants.append(Grant(
                funder_name=funder_name,
                funder_ein=funder_ein,
                recipient_name=recipient_name,
                recipient_ein=recipient_ein,
                amount=amount,
                purpose=purpose,
                tax_year=tax_year,
                recipient_city=city,
                recipient_state=state,
            ))

    return grants


def get_form_type(xml_bytes: bytes) -> Optional[str]:
    """Detect the form type (990, 990-EZ, 990-PF) from XML."""
    try:
        root = etree.fromstring(xml_bytes)
    except etree.XMLSyntaxError:
        return None

    # Check for form type indicators
    if root.xpath("//irs:Return/irs:ReturnData/irs:IRS990PF", namespaces=IRS_NS):
        return "990-PF"
    if root.xpath("//irs:Return/irs:ReturnData/irs:IRS990EZ", namespaces=IRS_NS):
        return "990-EZ"
    if root.xpath("//irs:Return/irs:ReturnData/irs:IRS990", namespaces=IRS_NS):
        return "990"
    return None
