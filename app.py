"""
Flask web UI for the Foundation Finder.
Wraps all existing CLI functionality with a browser-based interface.
"""

from dotenv import load_dotenv
load_dotenv(override=True)

import csv
import io
import json
import os
import sys
import time
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from flask import (
    Flask, render_template, request, redirect, url_for,
    jsonify, Response, flash
)

import config
from src.api.propublica import ProPublicaClient, build_organization, parse_filing
from src.api.fulltext_search import FullTextSearchClient
from src.api.xml_parser import parse_officers_from_xml
from src.core.scoring import score_organization, score_funder
from src.core.cache import FilingCache
from src.commands.search import run_prospect_search, enrich_organizations
from src.commands.funders import find_funders_for_org
from src.commands.grants import list_foundation_grants
from src.commands.prospect import run_full_pipeline
from src.commands.draft import generate_grant_draft
from src.commands.profile import cmd_profile_gen
from src.core.ai_scoring import is_available as ai_available
from src.export.report import fmt_dollar, fmt_short

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.secret_key = config.SECRET_KEY

# Register Jinja2 template filters
app.jinja_env.filters["dollar"] = fmt_dollar
app.jinja_env.filters["short"] = fmt_short


def _sum_amounts(items):
    """Sum the 'amount' attribute of items, treating None as 0."""
    return sum(getattr(g, 'amount', 0) or 0 for g in items)


app.jinja_env.globals["sum_amounts"] = _sum_amounts


def _display_amount(amount):
    """Display grant amount, handling None gracefully for web-scraped grants."""
    if amount is None:
        return "Confirmed"
    if amount == 0:
        return "In-kind"
    return fmt_dollar(amount)


def _discovery_method(grant):
    """Infer how a grant was discovered from its purpose field."""
    purpose = getattr(grant, 'purpose', '') or ''
    if purpose.startswith("Listed on"):
        return "Donor page"
    if "(from " in purpose and "website)" in purpose:
        return "Foundation website"
    if purpose:
        return "990 Schedule I"
    return "Web discovery"


def _group_grants_by_purpose(grants):
    """Group grants by normalized purpose text for Programs/Funds display."""
    from collections import defaultdict
    NORMALIZATIONS = {
        "general operating": "General Operating Support",
        "general support": "General Operating Support",
        "operating support": "General Operating Support",
        "unrestricted": "General/Unrestricted Support",
        "capital": "Capital Campaign/Construction",
        "scholarship": "Scholarships",
        "program support": "Program Support",
        "research": "Research",
    }
    groups = defaultdict(lambda: {"grants": [], "total": 0, "count": 0, "recipients": set()})

    for g in grants:
        raw = (getattr(g, 'purpose', '') or '').strip()
        if not raw or raw.startswith("Listed on") or raw.startswith("(from "):
            key = "Unspecified"
        else:
            key = raw
            for pattern, normalized in NORMALIZATIONS.items():
                if pattern in raw.lower():
                    key = normalized
                    break

        groups[key]["grants"].append(g)
        groups[key]["total"] += getattr(g, 'amount', 0) or 0
        groups[key]["count"] += 1
        groups[key]["recipients"].add(getattr(g, 'recipient_name', 'Unknown'))

    result = []
    for purpose, data in groups.items():
        result.append({
            "purpose": purpose,
            "total": data["total"],
            "count": data["count"],
            "num_recipients": len(data["recipients"]),
            "sample_grants": data["grants"][:3],
        })
    return sorted(result, key=lambda x: x["total"], reverse=True)


app.jinja_env.filters["display_amount"] = _display_amount
app.jinja_env.filters["discovery_method"] = _discovery_method
app.jinja_env.globals["group_grants_by_purpose"] = _group_grants_by_purpose


def get_client():
    """Create a ProPublica client (with optional cache)."""
    cache = FilingCache() if config.CACHE_ENABLED else None
    return ProPublicaClient(delay=config.API_DELAY, cache=cache)


def load_profiles():
    """Load all search profiles from the profiles/ directory."""
    profiles = {}
    profiles_dir = os.path.join(os.path.dirname(__file__), "profiles")
    if os.path.isdir(profiles_dir):
        for fname in os.listdir(profiles_dir):
            if fname.endswith(".json"):
                path = os.path.join(profiles_dir, fname)
                with open(path, "r") as f:
                    data = json.load(f)
                key = os.path.splitext(fname)[0]
                profiles[key] = data
    return profiles


# ---------------------------------------------------------------------------
# Background task system
# ---------------------------------------------------------------------------

@dataclass
class TaskResult:
    task_id: str
    task_type: str  # "search_profile", "funders", "pipeline", "sector"
    state: str = "running"  # "running", "complete", "error"
    progress: str = ""
    result: Any = None
    error: str = ""
    created: float = field(default_factory=time.time)


tasks: dict = {}  # task_id -> TaskResult
tasks_lock = threading.Lock()


class ProgressCapture:
    """Captures print() output as progress messages for a background task."""

    def __init__(self, task: TaskResult):
        self.task = task
        self.lines = []

    def write(self, text):
        text = text.strip()
        if text:
            self.lines.append(text)
            # Keep last meaningful line as progress
            self.task.progress = text

    def flush(self):
        pass


def run_task(task_id: str, func, args=(), kwargs=None):
    """Run a function in a background thread, capturing stdout as progress."""
    kwargs = kwargs or {}
    task = tasks[task_id]
    capture = ProgressCapture(task)
    old_stdout = sys.stdout

    try:
        sys.stdout = capture
        result = func(*args, **kwargs)
        task.result = result
        task.state = "complete"
    except Exception as e:
        task.error = str(e)
        task.state = "error"
    finally:
        sys.stdout = old_stdout


def start_task(task_type: str, func, args=(), kwargs=None) -> str:
    """Start a background task and return its ID."""
    task_id = str(uuid.uuid4())[:8]
    task = TaskResult(task_id=task_id, task_type=task_type)
    with tasks_lock:
        tasks[task_id] = task
    t = threading.Thread(target=run_task, args=(task_id, func, args, kwargs), daemon=True)
    t.start()
    return task_id


def cleanup_old_tasks():
    """Remove tasks older than 1 hour."""
    cutoff = time.time() - 3600
    with tasks_lock:
        old = [tid for tid, t in tasks.items() if t.created < cutoff]
        for tid in old:
            del tasks[tid]


# ---------------------------------------------------------------------------
# CSV generation helpers (in-memory, for download)
# ---------------------------------------------------------------------------

def orgs_to_csv(orgs):
    """Generate CSV string from a list of Organization objects."""
    output = io.StringIO()
    fieldnames = [
        "priority", "score", "name", "ein", "city", "state", "ntee_code",
        "revenue", "contributions", "assets", "expenses", "grants_paid",
        "net_assets", "fiscal_year", "key_people", "propublica_url"
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for org in orgs:
        f = org.latest_filing
        officers_str = "; ".join(f"{o.name} ({o.title})" for o in org.officers[:5])
        writer.writerow({
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
            "propublica_url": org.propublica_url or "",
        })
    return output.getvalue()


def funders_to_csv(funders):
    """Generate CSV string from a list of Funder objects."""
    output = io.StringIO()
    fieldnames = [
        "priority", "score", "funder_name", "ein", "city", "state",
        "grants_to_similar_orgs", "total_giving", "num_grants",
        "revenue", "assets", "fiscal_year", "funded_similar_orgs", "propublica_url"
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for funder in funders:
        f = funder.latest_filing
        target_total = sum(g.amount or 0 for g in funder.grants_to_target)
        all_total = funder.total_giving or sum(g.amount or 0 for g in funder.all_grants)
        target_orgs = "; ".join(
            f"{g.recipient_name} ({g.amount or 0:,.0f})" for g in funder.grants_to_target[:10]
        )
        writer.writerow({
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
            "fiscal_year": f.tax_year if f else "",
            "funded_similar_orgs": target_orgs,
            "propublica_url": funder.propublica_url or "",
        })
    return output.getvalue()


def grants_to_csv(grants):
    """Generate CSV string from a list of Grant objects."""
    output = io.StringIO()
    fieldnames = [
        "funder_name", "funder_ein", "recipient_name", "recipient_ein",
        "amount", "purpose", "tax_year", "recipient_city", "recipient_state"
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for g in grants:
        writer.writerow({
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
    return output.getvalue()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def home():
    """Landing page with search forms."""
    profiles = load_profiles()
    return render_template("home.html", profiles=profiles)


@app.route("/search", methods=["POST"])
def search():
    """Layer 1: Quick keyword search (blocking - usually fast)."""
    query = request.form.get("query", "").strip()
    state = request.form.get("state", "").strip()
    limit = int(request.form.get("limit", "25"))
    fetch_officers = request.form.get("officers") == "on"

    if not query:
        flash("Please enter a search query.", "warning")
        return redirect(url_for("home"))

    client = get_client()
    orgs, total = client.search(query, state=state if state else None)

    raw_dict = {str(o["ein"]): o for o in orgs}
    enriched = enrich_organizations(
        client, raw_dict, max_enrich=limit,
        priority_states=[state] if state else [],
        fetch_officers=fetch_officers,
    )

    return render_template(
        "search_results.html",
        orgs=enriched,
        query=query,
        state=state,
        total=total,
    )


@app.route("/search/profile", methods=["POST"])
def search_profile():
    """Layer 1: Profile-based search (background task)."""
    profile_key = request.form.get("profile", "")
    limit = int(request.form.get("limit", "30"))
    fetch_officers = request.form.get("officers") == "on"

    profiles = load_profiles()
    profile = profiles.get(profile_key)
    if not profile:
        flash(f"Profile '{profile_key}' not found.", "danger")
        return redirect(url_for("home"))

    client = get_client()

    def do_profile_search():
        raw_orgs = run_prospect_search(
            client,
            queries=profile["keywords"],
            states=profile.get("search_states", [""]),
            ntee_codes=profile.get("ntee_codes"),
        )
        raw_orgs.pop(profile.get("ein", ""), None)
        enriched = enrich_organizations(
            client, raw_orgs, max_enrich=limit,
            priority_states=profile.get("search_states", []),
            fetch_officers=fetch_officers,
        )
        return {"orgs": enriched, "profile": profile}

    task_id = start_task("search_profile", do_profile_search)
    return redirect(url_for("loading", task_id=task_id))


@app.route("/org/<ein>")
def org_detail(ein):
    """Single org detail page."""
    ein = ein.replace("-", "")
    client = get_client()

    detail = client.get_organization(ein)
    if not detail:
        flash("Organization not found.", "warning")
        return redirect(url_for("home"))

    raw = detail.get("organization", {"ein": ein, "name": "Unknown"})
    org = build_organization(raw, detail)
    score_organization(org)

    # Fetch officers from XML
    org_data = detail.get("organization", {})
    object_id = org_data.get("latest_object_id")
    if not object_id:
        filings = detail.get("filings_with_data", [])
        if filings:
            object_id = filings[0].get("object_id")
    if object_id:
        xml_bytes = client.download_xml(str(object_id))
        if xml_bytes:
            org.officers = parse_officers_from_xml(xml_bytes)

    return render_template("org_detail.html", org=org)


@app.route("/funders", methods=["POST"])
def funders():
    """Layer 2: Find funders (background task)."""
    name = request.form.get("name", "").strip()
    ein = request.form.get("ein", "").strip().replace("-", "")
    state = request.form.get("state", "").strip()
    limit = int(request.form.get("limit", "25"))

    if not name and not ein:
        flash("Please provide an organization name or EIN.", "warning")
        return redirect(url_for("home"))

    client = get_client()

    def do_funder_search():
        org_name = name
        org_ein = ein
        target_state = state

        if org_ein and not org_name:
            detail = client.get_organization(org_ein)
            if detail:
                org_info = detail.get("organization", {})
                org_name = org_info.get("name", "Unknown")
                target_state = target_state or org_info.get("state", "")

        ft_client = FullTextSearchClient(delay=client.delay * 2)
        funder_list = find_funders_for_org(
            client, ft_client,
            org_name=org_name,
            org_ein=org_ein,
            max_results=limit,
            target_state=target_state,
        )
        return {"funders": funder_list, "org_name": org_name, "org_ein": org_ein}

    task_id = start_task("funders", do_funder_search)
    return redirect(url_for("loading", task_id=task_id))


@app.route("/grants", methods=["POST"])
def grants():
    """Layer 3: Foundation grant list (blocking - usually manageable)."""
    name = request.form.get("name", "").strip()
    ein = request.form.get("ein", "").strip().replace("-", "")
    sector = request.form.get("sector", "").strip() or None
    state_filter = request.form.get("state", "").strip() or None
    min_amount = request.form.get("min_amount", "").strip()
    min_amount = float(min_amount) if min_amount else None
    years = int(request.form.get("years", "3"))

    if not name and not ein:
        flash("Please provide a foundation name or EIN.", "warning")
        return redirect(url_for("home"))

    client = get_client()
    funder_name = name

    if not ein and name:
        orgs, total = client.search(name)
        if orgs:
            for o in orgs:
                if name.lower() in o.get("name", "").lower():
                    ein = str(o.get("ein", ""))
                    funder_name = o.get("name", name)
                    break
            if not ein:
                ein = str(orgs[0].get("ein", ""))
                funder_name = orgs[0].get("name", name)

    if not ein:
        flash("Could not find the foundation.", "warning")
        return redirect(url_for("home"))

    grant_list = list_foundation_grants(
        client, ein,
        funder_name=funder_name,
        sector_filter=sector,
        state_filter=state_filter,
        min_amount=min_amount,
        max_years=years,
    )

    total_amount = sum(g.amount or 0 for g in grant_list)
    return render_template(
        "grants.html",
        grants=grant_list,
        funder_name=funder_name,
        funder_ein=ein,
        total_amount=total_amount,
        sector=sector,
        state_filter=state_filter,
    )


@app.route("/pipeline", methods=["POST"])
def pipeline():
    """Full 3-layer pipeline (background task)."""
    profile_key = request.form.get("profile", "")
    depth = int(request.form.get("depth", "2"))
    limit = int(request.form.get("limit", "25"))
    fetch_officers = request.form.get("officers") == "on"
    use_ai_scoring = request.form.get("ai_score") == "on"

    profiles = load_profiles()
    profile = profiles.get(profile_key)
    if not profile:
        flash(f"Profile '{profile_key}' not found.", "danger")
        return redirect(url_for("home"))

    client = get_client()

    def do_pipeline():
        enriched, funder_list, shared_members, pipeline_diag = run_full_pipeline(
            client, profile,
            max_comps=limit,
            depth=depth,
            fetch_officers=fetch_officers,
            use_ai_scoring=use_ai_scoring,
        )

        # Build funder -> comp org mapping (which comp orgs each funder supports)
        funder_to_comps = {}
        for org in enriched:
            for f in getattr(org, 'funders', None) or []:
                funder_to_comps.setdefault(f.ein, set()).add(org.name)
        funder_to_comps = {k: sorted(v) for k, v in funder_to_comps.items()}

        # Compute total giving to similar orgs across all funders
        total_giving_similar = sum(
            sum(g.amount or 0 for g in f.grants_to_target)
            for f in funder_list
        )

        return {
            "orgs": enriched,
            "funders": funder_list,
            "shared_board_members": shared_members,
            "funder_to_comps": funder_to_comps,
            "total_giving_similar": total_giving_similar,
            "diagnostics": pipeline_diag or [],
            "profile": profile,
            "profile_key": profile_key,
            "depth": depth,
        }

    task_id = start_task("pipeline", do_pipeline)
    return redirect(url_for("loading", task_id=task_id))


@app.route("/loading/<task_id>")
def loading(task_id):
    """Loading/spinner page that polls for task completion."""
    task = tasks.get(task_id)
    if not task:
        flash("Task not found.", "warning")
        return redirect(url_for("home"))
    if task.state == "complete":
        return redirect(url_for("results", task_id=task_id))
    return render_template("loading.html", task_id=task_id, task_type=task.task_type)


@app.route("/api/status/<task_id>")
def task_status(task_id):
    """JSON endpoint for polling task progress."""
    task = tasks.get(task_id)
    if not task:
        return jsonify({"state": "error", "error": "Task not found"})
    return jsonify({
        "state": task.state,
        "progress": task.progress,
        "error": task.error,
    })


@app.route("/results/<task_id>")
def results(task_id):
    """Render completed task results."""
    task = tasks.get(task_id)
    if not task:
        flash("Task not found or expired.", "warning")
        return redirect(url_for("home"))
    if task.state == "running":
        return redirect(url_for("loading", task_id=task_id))
    if task.state == "error":
        flash(f"Task failed: {task.error}", "danger")
        return redirect(url_for("home"))

    result = task.result

    if task.task_type == "search_profile":
        return render_template(
            "search_results.html",
            orgs=result["orgs"],
            query=f"Profile: {result['profile']['name']}",
            state="",
            total=len(result["orgs"]),
            task_id=task_id,
        )

    if task.task_type == "funders":
        return render_template(
            "funders.html",
            funders=result["funders"],
            org_name=result["org_name"],
            org_ein=result["org_ein"],
            task_id=task_id,
        )

    if task.task_type == "pipeline":
        return render_template(
            "pipeline.html",
            orgs=result["orgs"],
            funders=result["funders"],
            shared_board_members=result.get("shared_board_members", {}),
            funder_to_comps=result.get("funder_to_comps", {}),
            total_giving_similar=result.get("total_giving_similar", 0),
            diagnostics=result.get("diagnostics", []),
            profile=result["profile"],
            profile_key=result.get("profile_key", ""),
            depth=result["depth"],
            task_id=task_id,
        )

    if task.task_type == "draft":
        return render_template(
            "draft.html",
            draft=result["draft"],
            foundation_name=result["foundation_name"],
            foundation_ein=result["foundation_ein"],
            profile=result["profile"],
        )

    flash("Unknown task type.", "warning")
    return redirect(url_for("home"))


@app.route("/download/<result_type>/<task_id>")
def download(result_type, task_id):
    """Download CSV for a completed task."""
    task = tasks.get(task_id)
    if not task or task.state != "complete":
        flash("No data to download.", "warning")
        return redirect(url_for("home"))

    result = task.result

    if result_type == "orgs":
        orgs = result.get("orgs", [])
        csv_data = orgs_to_csv(orgs)
        filename = "prospect_orgs.csv"
    elif result_type == "funders":
        funder_list = result.get("funders", [])
        csv_data = funders_to_csv(funder_list)
        filename = "prospect_funders.csv"
    elif result_type == "grants":
        grant_list = result.get("grants", [])
        csv_data = grants_to_csv(grant_list)
        filename = "grants.csv"
    else:
        flash("Unknown download type.", "warning")
        return redirect(url_for("home"))

    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@app.route("/download/grants-inline")
def download_grants_inline():
    """Download grants CSV from query params (for non-task grant results)."""
    # Grants are rendered directly (not via tasks), so we re-fetch
    ein = request.args.get("ein", "").strip().replace("-", "")
    funder_name = request.args.get("name", "").strip()
    sector = request.args.get("sector", "").strip() or None
    state_filter = request.args.get("state", "").strip() or None
    min_amount = request.args.get("min_amount", "").strip()
    min_amount = float(min_amount) if min_amount else None
    years = int(request.args.get("years", "3"))

    if not ein:
        flash("No EIN provided.", "warning")
        return redirect(url_for("home"))

    client = get_client()
    grant_list = list_foundation_grants(
        client, ein,
        funder_name=funder_name,
        sector_filter=sector,
        state_filter=state_filter,
        min_amount=min_amount,
        max_years=years,
    )

    csv_data = grants_to_csv(grant_list)
    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={funder_name or ein}_grants.csv"}
    )


@app.route("/profile/generate", methods=["POST"])
def profile_gen():
    """Auto-generate a search profile from an EIN."""
    ein = request.form.get("ein", "").strip().replace("-", "")
    if not ein:
        flash("Please provide an EIN.", "warning")
        return redirect(url_for("home"))

    client = get_client()
    detail = client.get_organization(ein)
    if not detail:
        flash(f"Organization not found: {ein}", "warning")
        return redirect(url_for("home"))

    org_info = detail.get("organization", {})
    name = org_info.get("name", "Unknown")
    city = org_info.get("city", "")
    state = org_info.get("state", "")
    ntee_code = org_info.get("ntee_code", "")
    mission = org_info.get("mission", "") or ""

    # Build keywords from name
    import re
    _stop = {"the", "of", "and", "for", "in", "a", "an", "to", "inc", "co", "org",
             "foundation", "fund", "trust", "association", "society", "corporation",
             "national", "american", "united", "international", "community"}
    text = f"{name} {mission}".lower()
    words = re.findall(r"[a-z]+", text)
    keywords = []
    seen = set()
    for w in words:
        if len(w) >= 4 and w not in _stop and w not in seen:
            seen.add(w)
            keywords.append(w)
    keywords = keywords[:10]

    profile = {
        "name": name,
        "ein": ein,
        "city": city,
        "state": state,
        "mission": mission or name,
        "keywords": keywords,
        "ntee_codes": [ntee_code] if ntee_code else [],
        "search_states": [state] if state else [""],
    }

    # Save to profiles directory
    import os
    safe_name = re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')[:40]
    output = os.path.join("profiles", f"{safe_name}.json")
    os.makedirs("profiles", exist_ok=True)

    import json
    with open(output, "w", encoding="utf-8") as f:
        json.dump(profile, f, indent=2)

    flash(f"Profile created for {name} and saved as '{safe_name}'. You can now use it in the Full Pipeline.", "success")
    return redirect(url_for("home"))


# ---------------------------------------------------------------------------
# Profile Builder + Report routes
# ---------------------------------------------------------------------------

@app.route("/api/org-search")
def org_search_api():
    """AJAX autocomplete endpoint for nonprofit name search."""
    q = request.args.get("q", "").strip()
    if len(q) < 3:
        return jsonify([])
    client = get_client()
    try:
        orgs, total = client.search(q)
        results = [{
            "name": o.get("name", ""),
            "ein": str(o.get("ein", "")),
            "city": o.get("city", ""),
            "state": o.get("state", ""),
            "ntee_code": o.get("ntee_code", ""),
        } for o in orgs[:10]]
        return jsonify(results)
    except Exception:
        return jsonify([])


@app.route("/profile/new")
def profile_new():
    """Multi-step profile builder page."""
    return render_template("profile_builder.html")


@app.route("/profile/run", methods=["POST"])
def profile_run():
    """Save profile and immediately run the pipeline."""
    import re

    name = request.form.get("name", "").strip()
    ein = request.form.get("ein", "").strip().replace("-", "")
    city = request.form.get("city", "").strip()
    state = request.form.get("state", "").strip()
    mission = request.form.get("mission", "").strip()
    keywords_raw = request.form.get("keywords", "").strip()
    ntee_code = request.form.get("ntee_code", "").strip()
    search_states = request.form.get("search_states", state).strip()
    depth = int(request.form.get("depth", "2"))
    limit = int(request.form.get("limit", "25"))
    fetch_officers = request.form.get("officers") == "on"

    if not name or not ein:
        flash("Name and EIN are required.", "warning")
        return redirect(url_for("profile_new"))

    keywords = [k.strip() for k in keywords_raw.split(",") if k.strip()]
    if not keywords:
        # Auto-generate keywords from name + mission
        _stop = {"the", "of", "and", "for", "in", "a", "an", "to", "inc", "co", "org",
                 "foundation", "fund", "trust", "association", "society", "corporation",
                 "national", "american", "united", "international", "community"}
        text = f"{name} {mission}".lower()
        words = re.findall(r"[a-z]+", text)
        seen = set()
        for w in words:
            if len(w) >= 4 and w not in _stop and w not in seen:
                seen.add(w)
                keywords.append(w)
        keywords = keywords[:10]

    search_states_list = [s.strip() for s in search_states.split(",") if s.strip()]
    if not search_states_list:
        search_states_list = [state] if state else [""]

    profile = {
        "name": name,
        "ein": ein,
        "city": city,
        "state": state,
        "mission": mission or name,
        "keywords": keywords,
        "ntee_codes": [ntee_code] if ntee_code else [],
        "search_states": search_states_list,
    }

    # Save to profiles directory
    safe_name = re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')[:40]
    output_path = os.path.join("profiles", f"{safe_name}.json")
    os.makedirs("profiles", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(profile, f, indent=2)

    # Start pipeline
    client = get_client()

    def do_pipeline():
        enriched, funder_list, shared_members, pipeline_diag = run_full_pipeline(
            client, profile,
            max_comps=limit,
            depth=depth,
            fetch_officers=fetch_officers,
        )

        funder_to_comps = {}
        for org in enriched:
            for f_obj in getattr(org, 'funders', None) or []:
                funder_to_comps.setdefault(f_obj.ein, set()).add(org.name)
        funder_to_comps = {k: sorted(v) for k, v in funder_to_comps.items()}

        total_giving_similar = sum(
            sum(g.amount or 0 for g in f_obj.grants_to_target)
            for f_obj in funder_list
        )

        return {
            "orgs": enriched,
            "funders": funder_list,
            "shared_board_members": shared_members,
            "funder_to_comps": funder_to_comps,
            "total_giving_similar": total_giving_similar,
            "diagnostics": pipeline_diag or [],
            "profile": profile,
            "profile_key": safe_name,
            "depth": depth,
        }

    task_id = start_task("pipeline", do_pipeline)
    return redirect(url_for("loading", task_id=task_id))


@app.route("/report/<task_id>")
def report(task_id):
    """Printable foundation prospect report."""
    task = tasks.get(task_id)
    if not task:
        flash("Task not found or expired.", "warning")
        return redirect(url_for("home"))
    if task.state != "complete":
        flash("Report not ready yet.", "info")
        return redirect(url_for("loading", task_id=task_id))

    result = task.result
    return render_template(
        "report.html",
        orgs=result["orgs"],
        funders=result["funders"],
        shared_board_members=result.get("shared_board_members", {}),
        funder_to_comps=result.get("funder_to_comps", {}),
        total_giving_similar=result.get("total_giving_similar", 0),
        profile=result["profile"],
        task_id=task_id,
    )


@app.route("/funder/<ein>")
def funder_detail(ein):
    """Foundation detail page showing grants and giving patterns."""
    ein = ein.replace("-", "")
    client = get_client()

    detail = client.get_organization(ein)
    if not detail:
        flash("Foundation not found.", "warning")
        return redirect(url_for("home"))

    raw = detail.get("organization", {"ein": ein, "name": "Unknown"})
    org = build_organization(raw, detail)
    score_organization(org)

    # Fetch officers from XML
    org_data = detail.get("organization", {})
    object_id = org_data.get("latest_object_id")
    if not object_id:
        filings = detail.get("filings_with_data", [])
        if filings:
            object_id = filings[0].get("object_id")
    if object_id:
        xml_bytes = client.download_xml(str(object_id))
        if xml_bytes:
            org.officers = parse_officers_from_xml(xml_bytes)

    # Fetch grants
    grant_list = list_foundation_grants(client, ein, funder_name=org.name, max_years=3)
    total_giving = sum(g.amount or 0 for g in grant_list)

    # Top recipients by total amount
    recipient_totals = {}
    for g in grant_list:
        name = g.recipient_name or "Unknown"
        recipient_totals[name] = recipient_totals.get(name, 0) + (g.amount or 0)
    top_recipients = sorted(recipient_totals.items(), key=lambda x: x[1], reverse=True)[:10]

    # Load profiles for draft button
    profiles = load_profiles()

    return render_template(
        "funder_detail.html",
        org=org,
        grants=grant_list,
        total_giving=total_giving,
        top_recipients=top_recipients,
        profiles=profiles,
    )


@app.route("/draft", methods=["POST"])
def draft():
    """Generate a grant application draft (background task)."""
    foundation_ein = request.form.get("ein", "").strip().replace("-", "")
    profile_key = request.form.get("profile", "")

    if not foundation_ein:
        flash("Please provide a foundation EIN.", "warning")
        return redirect(url_for("home"))

    if not ai_available():
        flash("AI features require ANTHROPIC_API_KEY to be set. Add it to .env and restart.", "danger")
        return redirect(url_for("home"))

    profiles = load_profiles()
    profile = profiles.get(profile_key)
    if not profile:
        flash(f"Profile '{profile_key}' not found.", "danger")
        return redirect(url_for("home"))

    client = get_client()

    def do_draft():
        draft_text = generate_grant_draft(
            client,
            foundation_ein=foundation_ein,
            target_profile=profile,
            output_file=None,
        )
        # Look up foundation name
        detail = client.get_organization(foundation_ein)
        foundation_name = "Unknown Foundation"
        if detail:
            foundation_name = detail.get("organization", {}).get("name", foundation_name)

        return {
            "draft": draft_text or "Failed to generate draft. Check that ANTHROPIC_API_KEY is set.",
            "foundation_name": foundation_name,
            "foundation_ein": foundation_ein,
            "profile": profile,
        }

    task_id = start_task("draft", do_draft)
    return redirect(url_for("loading", task_id=task_id))


# ---------------------------------------------------------------------------
# Cleanup old tasks periodically
# ---------------------------------------------------------------------------

@app.before_request
def before_request():
    cleanup_old_tasks()


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(f"\n  Foundation Finder - Web UI")
    print(f"  http://127.0.0.1:{config.PORT}")
    print(f"  Press Ctrl+C to stop\n")

    try:
        from waitress import serve
        serve(app, host="0.0.0.0", port=config.PORT)
    except ImportError:
        app.run(host="0.0.0.0", port=config.PORT, debug=config.DEBUG)
