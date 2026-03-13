# Foundation Finder

Find mission-adjacent nonprofits and build prioritized prospect lists using
ProPublica Nonprofit Explorer API and IRS 990 data.

## Three-Layer Workflow

| Layer | Question | How |
|-------|----------|-----|
| 1 | Who is similar to us? | ProPublica search + 990 financials |
| 2 | Who funds those similar orgs? | Full-text search across 990-PF filings |
| 3 | What else do those funders support? | Parse Schedule I grant lists from XML |

## Setup

```bash
pip install -r requirements.txt
```

Requires Python 3.10+ with `requests` and `lxml`. No API keys needed (ProPublica is free and open).

## Quick Start

```bash
# Layer 1: Run full prospect search for The Math Agency
python cli.py search --profile math_agency

# Layer 1: Quick keyword search
python cli.py search --query "math education" --state WA

# Look up a single org with officer names
python cli.py org --ein 91-1508191 --officers

# Layer 2: Find who funds Eastside Pathways
python cli.py funders --name "Eastside Pathways"
python cli.py funders --ein 45-3005820

# Layer 3: List all grantees of a foundation
python cli.py grants --ein 56-2206524
python cli.py grants --name "Ballmer" --sector education

# Full pipeline: comps + funders + grant lists
python cli.py prospect --profile math_agency --depth 2

# Reverse query: foundations funding education in WA
python cli.py sector --query "education" --state WA --min-grants 100000
```

## Commands

### `search` - Layer 1: Find Comparable Orgs

```bash
python cli.py search --profile math_agency --limit 40 --detail 10 --officers
python cli.py search --query "STEM education" --state WA --output results.csv
```

### `org` - Single Org Lookup

```bash
python cli.py org --ein 33-4593800 --officers
python cli.py org --ein 91-1508191 --dump-fields  # debug API fields
```

### `funders` - Layer 2: Find Who Funds an Org

```bash
python cli.py funders --name "Eastside Pathways" --detail 5
python cli.py funders --ein 45-3005820 --output funders.csv
```

### `grants` - Layer 3: List Foundation Grantees

```bash
python cli.py grants --ein 56-2206524 --sector education --state WA
python cli.py grants --name "Gates Foundation" --min-amount 500000 --years 3
```

### `prospect` - Full Pipeline

```bash
python cli.py prospect --profile math_agency --depth 2 --detail 5
python cli.py prospect --profile math_agency --depth 3 --officers --output full.csv
```

### `sector` - Reverse Query

```bash
python cli.py sector --query "education" --state WA --min-grants 100000
python cli.py sector --ntee B --state WA --limit 30
```

## Search Profiles

Create JSON profiles in the `profiles/` directory:

```json
{
  "name": "Your Nonprofit",
  "ein": "123456789",
  "city": "Seattle",
  "state": "WA",
  "mission": "Your mission statement",
  "keywords": ["keyword1", "keyword2"],
  "ntee_codes": ["2"],
  "search_states": ["WA", ""]
}
```

## Caching

The tool caches API responses and XML filings locally in `~/.prospect-explorer/cache.db`.
Use `--no-cache` to bypass.

## Data Sources

- **ProPublica Nonprofit Explorer API v2** - Search and financial summaries
- **IRS 990 XML E-Filings** - Officer names (Part VII), grants (Schedule I)
- **ProPublica Full-Text Search** - Cross-reference filings that mention an org

## Project Structure

```
src/
  api/
    propublica.py        # ProPublica API client
    xml_parser.py        # IRS 990 XML parser (officers + Schedule I)
    fulltext_search.py   # Full-text search across filings
  core/
    models.py            # Data classes
    scoring.py           # Prospect scoring and ranking
    cache.py             # SQLite cache
  commands/
    search.py            # Layer 1: find comparable orgs
    funders.py           # Layer 2: find who funds a given org
    grants.py            # Layer 3: list all grantees of a foundation
    prospect.py          # Full pipeline: all three layers combined
  export/
    csv_export.py        # CSV output
    report.py            # Formatted terminal output
profiles/
  math_agency.json       # Pre-built search profile
cli.py                   # Main CLI entry point
```
