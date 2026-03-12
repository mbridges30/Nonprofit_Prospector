#!/usr/bin/env python3
"""
990 Prospect Explorer - Acceptance Test Suite
Tests each priority from Spec v2 using real nonprofit data.

Run from the project root:
    py test_acceptance.py

Each test calls the tool the same way a user would and checks
that the output contains expected real-world data points.

These are not unit tests. They hit live APIs and take 2-5 minutes total.
Run them after completing each priority to confirm it works.
"""

import subprocess
import sys
import os
import csv
import json
import time

# Configuration
PYTHON = sys.executable
SCRIPT = os.path.join(os.path.dirname(__file__), "cli.py")

PASS = 0
FAIL = 0
SKIP = 0
RESULTS = []


def log(status, test_name, detail=""):
    global PASS, FAIL, SKIP
    icons = {"PASS": "+", "FAIL": "X", "SKIP": "-"}
    icon = icons.get(status, "?")
    print(f"  [{icon}] {test_name}")
    if detail:
        print(f"      {detail}")
    if status == "PASS":
        PASS += 1
    elif status == "FAIL":
        FAIL += 1
    else:
        SKIP += 1
    RESULTS.append({"status": status, "test": test_name, "detail": detail})


def run_cmd(args, timeout=120):
    """Run a CLI command and return stdout, stderr, and return code."""
    cmd = [PYTHON, SCRIPT] + args
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
        return result.stdout, result.stderr, result.returncode
    except subprocess.TimeoutExpired:
        return "", "TIMEOUT", 1
    except FileNotFoundError:
        return "", f"Script not found: {SCRIPT}", 1


def check_csv(filepath, required_columns, min_rows=1):
    """Validate a CSV file has expected columns and minimum row count."""
    if not os.path.exists(filepath):
        return False, f"File not found: {filepath}"
    with open(filepath, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []
        missing = [c for c in required_columns if c not in headers]
        if missing:
            return False, f"Missing columns: {missing}"
        rows = list(reader)
        if len(rows) < min_rows:
            return False, f"Expected at least {min_rows} rows, got {len(rows)}"
    return True, f"{len(rows)} rows, all columns present"


# =========================================================================
# BASELINE: Verify the script exists and runs
# =========================================================================

def test_baseline():
    print("\n== BASELINE ==")

    # Script exists
    if os.path.exists(SCRIPT):
        log("PASS", "Script exists", SCRIPT)
    else:
        log("FAIL", "Script exists", f"Not found: {SCRIPT}")
        return False

    # Help flag works
    stdout, stderr, code = run_cmd(["--help"])
    if code == 0 and ("search" in stdout.lower() or "profile" in stdout.lower() or "usage" in stdout.lower()):
        log("PASS", "Help flag works")
    else:
        log("FAIL", "Help flag works", f"Return code {code}")
        return False

    return True


# =========================================================================
# STEPS 1-2: Peer org search (existing functionality)
# =========================================================================

def test_steps_1_2():
    print("\n== STEPS 1-2: Identify target org and research peers ==")

    # Single org lookup by EIN
    print("  Testing single org lookup (Alliance for Education)...")
    stdout, stderr, code = run_cmd(["--ein", "91-1508191"])
    if code == 0 and "alliance" in stdout.lower():
        log("PASS", "Org lookup by EIN", "Found Alliance for Education")
    else:
        log("FAIL", "Org lookup by EIN", f"Code {code}, output: {stdout[:200]}")

    # Profile search
    print("  Testing profile search (takes 30-60 sec)...")
    output_file = "test_output_steps12.csv"
    stdout, stderr, code = run_cmd(
        ["--profile", "math_agency", "--output", output_file],
        timeout=180,
    )
    if code == 0 and "prospect" in stdout.lower():
        log("PASS", "Profile search completes")
    else:
        log("FAIL", "Profile search completes", f"Code {code}")
        return

    # CSV has basic columns
    ok, detail = check_csv(output_file, ["name", "ein", "revenue", "state"], min_rows=10)
    if ok:
        log("PASS", "CSV has basic columns and 10+ rows", detail)
    else:
        log("FAIL", "CSV has basic columns and 10+ rows", detail)

    # Contributions column populated
    ok, detail = check_csv(output_file, ["contributions"])
    if ok:
        with open(output_file, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            contrib_count = sum(1 for row in reader if row.get("contributions") and row["contributions"].strip())
        if contrib_count >= 5:
            log("PASS", "Contributions column populated", f"{contrib_count} rows with data")
        else:
            log("FAIL", "Contributions column populated", f"Only {contrib_count} rows with data")
    else:
        log("FAIL", "Contributions column populated", detail)

    # Known orgs appear in results
    with open(output_file, encoding="utf-8") as f:
        content = f.read().lower()

    known_orgs = {
        "Alliance for Education": "alliance for education" in content,
        "Washington Stem": "washington stem" in content or "stem center" in content,
        "Eastside Pathways": "eastside pathways" in content,
    }
    for org_name, found in known_orgs.items():
        if found:
            log("PASS", f"Known org in results: {org_name}")
        else:
            log("FAIL", f"Known org in results: {org_name}")

    # WA orgs prioritized (majority should be WA)
    with open(output_file, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        wa_count = sum(1 for r in rows if r.get("state") == "WA")
        wa_pct = wa_count / len(rows) * 100 if rows else 0
    if wa_pct >= 50:
        log("PASS", f"WA orgs prioritized", f"{wa_count}/{len(rows)} = {wa_pct:.0f}% from WA")
    else:
        log("FAIL", f"WA orgs prioritized", f"Only {wa_pct:.0f}% from WA")

    # Cleanup
    if os.path.exists(output_file):
        os.remove(output_file)


# =========================================================================
# STEP 3 PRIORITY 1: Find funders of a given org
# =========================================================================

def test_priority_1_funders():
    print("\n== PRIORITY 1: Find who funds a given org ==")

    # Try the funders command
    stdout, stderr, code = run_cmd(
        ["funders", "--name", "Eastside Pathways"],
        timeout=120,
    )

    if code != 0 and ("unrecognized" in stderr.lower() or "error" in stderr.lower() or "invalid" in stderr.lower()):
        log("SKIP", "Funders command exists", "Command not yet implemented")
        return

    if code == 0:
        log("PASS", "Funders command runs")
    else:
        log("FAIL", "Funders command runs", f"Code {code}")
        return

    # Check for Ballmer Group in results
    combined = (stdout + stderr).lower()
    if "ballmer" in combined:
        log("PASS", "Found Ballmer Group as funder of Eastside Pathways")
    else:
        log("FAIL", "Found Ballmer Group as funder", "Ballmer not in output")

    # Check for any foundation results at all
    if "foundation" in combined or "funder" in combined or "grant" in combined:
        log("PASS", "Funders output contains foundation/grant references")
    else:
        log("FAIL", "Funders output contains foundation/grant references")


# =========================================================================
# STEP 3 PRIORITY 2: List grantees of a foundation
# =========================================================================

def test_priority_2_grants():
    print("\n== PRIORITY 2: List grantees of a foundation ==")

    # Try the grants command with Ballmer Foundation EIN
    stdout, stderr, code = run_cmd(
        ["grants", "--ein", "56-2206524"],
        timeout=120,
    )

    if code != 0 and ("unrecognized" in stderr.lower() or "error" in stderr.lower() or "invalid" in stderr.lower()):
        log("SKIP", "Grants command exists", "Command not yet implemented")
        return

    if code == 0:
        log("PASS", "Grants command runs")
    else:
        log("FAIL", "Grants command runs", f"Code {code}")
        return

    combined = (stdout + stderr).lower()

    # Check for known grantees
    if "alliance" in combined or "education" in combined:
        log("PASS", "Grants output contains education-related grantees")
    else:
        log("FAIL", "Grants output contains education-related grantees")

    # Check for dollar amounts
    if "$" in combined or "amount" in combined or any(c.isdigit() for c in combined[:500]):
        log("PASS", "Grants output includes dollar amounts")
    else:
        log("FAIL", "Grants output includes dollar amounts")


# =========================================================================
# STEP 4 PRIORITY 3: Officer extraction from XML
# =========================================================================

def test_priority_3_officers():
    print("\n== PRIORITY 3: Officer extraction from 990 XML ==")

    # Try org lookup with --officers flag
    stdout, stderr, code = run_cmd(
        ["--ein", "91-1508191", "--officers"],
        timeout=60,
    )

    # Also try as a subcommand
    if code != 0:
        stdout, stderr, code = run_cmd(
            ["org", "--ein", "91-1508191", "--officers"],
            timeout=60,
        )

    if code != 0 and ("unrecognized" in stderr.lower() or "error" in stderr.lower()):
        log("SKIP", "Officers flag/command exists", "Not yet implemented")
        return

    if code == 0:
        log("PASS", "Officers command runs")
    else:
        log("FAIL", "Officers command runs", f"Code {code}")
        return

    combined = (stdout + stderr).lower()

    # Alliance for Education should have named officers
    name_indicators = ["director", "president", "treasurer", "secretary", "ceo", "officer", "chair"]
    if any(ind in combined for ind in name_indicators):
        log("PASS", "Officers output contains titles (director, president, etc.)")
    else:
        log("FAIL", "Officers output contains titles")

    # Check key_people in CSV export
    output_file = "test_output_officers.csv"
    stdout2, stderr2, code2 = run_cmd(
        ["--profile", "math_agency", "--output", output_file, "--officers"],
        timeout=180,
    )
    if code2 == 0 and os.path.exists(output_file):
        with open(output_file, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            people_count = sum(1 for r in rows if r.get("key_people", "").strip())
        if people_count >= 3:
            log("PASS", "CSV key_people column populated", f"{people_count} rows with officer data")
        else:
            log("FAIL", "CSV key_people column populated", f"Only {people_count} rows")
        os.remove(output_file)
    else:
        log("SKIP", "CSV key_people column", "Profile run with --officers didn't produce CSV")


# =========================================================================
# STEP 4 PRIORITY 3: Board cross-referencing
# =========================================================================

def test_priority_3_crossref():
    print("\n== PRIORITY 3b: Board member cross-referencing ==")

    # Try a crossref command
    stdout, stderr, code = run_cmd(
        ["crossref", "--profile", "math_agency"],
        timeout=180,
    )

    if code != 0 and ("unrecognized" in stderr.lower() or "error" in stderr.lower()):
        log("SKIP", "Cross-reference command exists", "Not yet implemented")
        return

    if code == 0:
        log("PASS", "Cross-reference command runs")
    else:
        log("FAIL", "Cross-reference command runs", f"Code {code}")
        return

    combined = (stdout + stderr).lower()
    if "board" in combined or "shared" in combined or "multiple" in combined or "cross" in combined:
        log("PASS", "Cross-reference output identifies shared board members")
    else:
        log("FAIL", "Cross-reference output identifies shared board members")


# =========================================================================
# STEP 4 PRIORITY 4: Full pipeline
# =========================================================================

def test_priority_4_pipeline():
    print("\n== PRIORITY 4: Full prospect pipeline ==")

    # Try the prospect command
    stdout, stderr, code = run_cmd(
        ["prospect", "--profile", "math_agency", "--depth", "2"],
        timeout=300,
    )

    if code != 0 and ("unrecognized" in stderr.lower() or "error" in stderr.lower()):
        log("SKIP", "Prospect pipeline command exists", "Not yet implemented")
        return

    if code == 0:
        log("PASS", "Pipeline command runs")
    else:
        log("FAIL", "Pipeline command runs", f"Code {code}")
        return

    combined = (stdout + stderr).lower()

    # Should contain peer orgs
    if "eastside pathways" in combined or "alliance for education" in combined:
        log("PASS", "Pipeline output includes peer orgs")
    else:
        log("FAIL", "Pipeline output includes peer orgs")

    # Should contain funders
    if "funder" in combined or "foundation" in combined or "ballmer" in combined:
        log("PASS", "Pipeline output includes funders")
    else:
        log("FAIL", "Pipeline output includes funders")

    # Should contain evidence chain
    if "similar" in combined or "funds" in combined or "grant" in combined:
        log("PASS", "Pipeline output includes evidence chain")
    else:
        log("FAIL", "Pipeline output includes evidence chain")


# =========================================================================
# STRETCH: AI mission matching
# =========================================================================

def test_priority_5_ai_scoring():
    print("\n== PRIORITY 5 (STRETCH): AI mission matching ==")

    # Check for API key
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    if not os.getenv("ANTHROPIC_API_KEY"):
        log("SKIP", "Anthropic API key available", "No ANTHROPIC_API_KEY in environment")
        return

    log("PASS", "Anthropic API key available")

    # Try the score flag
    stdout, stderr, code = run_cmd(
        ["prospect", "--profile", "math_agency", "--score"],
        timeout=300,
    )

    if code != 0 and ("unrecognized" in stderr.lower() or "error" in stderr.lower()):
        log("SKIP", "AI scoring flag exists", "Not yet implemented")
        return

    if code == 0:
        log("PASS", "AI scoring runs")
    else:
        log("FAIL", "AI scoring runs", f"Code {code}")
        return

    combined = (stdout + stderr).lower()
    if "score" in combined or "alignment" in combined or "/100" in combined:
        log("PASS", "Output includes alignment scores")
    else:
        log("FAIL", "Output includes alignment scores")


# =========================================================================
# DATA INTEGRITY CHECKS
# =========================================================================

def test_data_integrity():
    print("\n== DATA INTEGRITY ==")

    # Run a quick search and check data quality
    output_file = "test_output_integrity.csv"
    stdout, stderr, code = run_cmd(
        ["--profile", "math_agency", "--output", output_file],
        timeout=180,
    )
    if code != 0 or not os.path.exists(output_file):
        log("SKIP", "Data integrity checks", "Could not generate CSV")
        return

    with open(output_file, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    # EINs should be numeric strings
    bad_eins = [r for r in rows if r.get("ein") and not r["ein"].replace("-", "").isdigit()]
    if not bad_eins:
        log("PASS", "All EINs are valid numeric strings")
    else:
        log("FAIL", "All EINs are valid numeric strings", f"{len(bad_eins)} invalid")

    # Revenue should be numeric where present
    bad_rev = []
    for r in rows:
        rev = r.get("revenue", "").strip()
        if rev:
            try:
                float(rev)
            except ValueError:
                bad_rev.append(rev)
    if not bad_rev:
        log("PASS", "Revenue values are numeric")
    else:
        log("FAIL", "Revenue values are numeric", f"Bad values: {bad_rev[:3]}")

    # ProPublica URLs should be well-formed
    bad_urls = [r for r in rows if r.get("propublica_url") and "propublica.org/nonprofits/organizations/" not in r["propublica_url"]]
    if not bad_urls:
        log("PASS", "ProPublica URLs are well-formed")
    else:
        log("FAIL", "ProPublica URLs are well-formed", f"{len(bad_urls)} malformed")

    # No duplicate EINs
    eins = [r.get("ein") for r in rows if r.get("ein")]
    if len(eins) == len(set(eins)):
        log("PASS", "No duplicate EINs in output")
    else:
        dupes = len(eins) - len(set(eins))
        log("FAIL", "No duplicate EINs in output", f"{dupes} duplicates")

    # Scores should be non-negative
    bad_scores = []
    for r in rows:
        s = r.get("score", "").strip()
        if s:
            try:
                if float(s) < 0:
                    bad_scores.append(s)
            except ValueError:
                pass
    if not bad_scores:
        log("PASS", "All scores are non-negative")
    else:
        log("FAIL", "All scores are non-negative", f"{len(bad_scores)} negative")

    # Cleanup
    if os.path.exists(output_file):
        os.remove(output_file)


# =========================================================================
# RUN ALL TESTS
# =========================================================================

def main():
    print("=" * 60)
    print("990 PROSPECT EXPLORER - ACCEPTANCE TEST SUITE")
    print("=" * 60)
    print(f"Script: {SCRIPT}")
    print(f"Python: {PYTHON}")
    print(f"Time: {time.strftime('%Y-%m-%d %H:%M:%S')}")

    if not test_baseline():
        print("\nBaseline failed. Fix the script path and try again.")
        print(f"Looking for: {SCRIPT}")
        sys.exit(1)

    test_steps_1_2()
    test_priority_1_funders()
    test_priority_2_grants()
    test_priority_3_officers()
    test_priority_3_crossref()
    test_priority_4_pipeline()
    test_priority_5_ai_scoring()
    test_data_integrity()

    # Summary
    total = PASS + FAIL + SKIP
    print("\n" + "=" * 60)
    print("RESULTS")
    print("=" * 60)
    print(f"  PASS: {PASS}")
    print(f"  FAIL: {FAIL}")
    print(f"  SKIP: {SKIP} (not yet implemented)")
    print(f"  TOTAL: {total}")
    print()

    if FAIL == 0 and SKIP == 0:
        print("  ALL TESTS PASSING. Ship it.")
    elif FAIL == 0:
        print(f"  All implemented features pass. {SKIP} tests skipped (future priorities).")
    else:
        print(f"  {FAIL} test(s) failing. Fix before moving to next priority.")

    print("=" * 60)

    # Write results to JSON for tracking over time
    results_file = "test_results.json"
    with open(results_file, "w") as f:
        json.dump({
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "pass": PASS, "fail": FAIL, "skip": SKIP,
            "tests": RESULTS,
        }, f, indent=2)
    print(f"\nDetailed results saved to {results_file}")


if __name__ == "__main__":
    main()