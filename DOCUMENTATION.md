# Foundation Finder - Technical Documentation

## 1. Project Overview

**Foundation Finder** (formerly 990 Prospect Explorer) is a nonprofit prospect research tool that helps organizations identify foundations likely to fund their work. It does this by finding similar nonprofits, discovering who funds them, and ranking those funders by relevance.

**Built for**: Bridges Strategy / The Math Agency (Sid Gorham)

**Core value proposition**: Enter your nonprofit's name or EIN, and the system generates a ranked list of foundation prospects with evidence trails, financial profiles, program data, and contact signals.

**No API keys required** for core functionality (ProPublica is free and keyless). Optional keys for Brave Search (website discovery) and Anthropic (AI mission scoring).

---

## 2. Architecture

```
src/
├── api/                    # External data sources
│   ├── propublica.py       # ProPublica Nonprofit Explorer API
│   ├── fulltext_search.py  # ProPublica HTML scraper (full-text filing search)
│   ├── xml_parser.py       # IRS 990 XML parser (officers, Schedule I grants)
│   ├── web_scraper.py      # Foundation grant page scraper (two-hop bridge)
│   ├── donor_scraper.py    # Nonprofit donor/sponsor page scraper
│   ├── website_resolver.py # Nonprofit website URL discovery
│   └── brave_search.py     # Brave Search API (optional)
├── commands/               # Pipeline layers
│   ├── search.py           # Layer 1: Find comparable organizations
│   ├── funders.py          # Layer 2: Identify funders via 990 filings
│   ├── grants.py           # Layer 3: Fetch complete foundation grant lists
│   ├── prospect.py         # Full pipeline orchestration
│   ├── draft.py            # AI grant letter drafting
│   └── profile.py          # Search profile generation
├── core/                   # Scoring, models, utilities
│   ├── models.py           # Data classes (Organization, Funder, Grant, Officer, Filing)
│   ├── scoring.py          # Org scoring (0-10) and funder scoring (0-22)
│   ├── cache.py            # SQLite cache (~/.prospect-explorer/cache.db)
│   ├── matching.py         # Fuzzy name matching (token_sort_ratio)
│   ├── crossref.py         # Board member cross-referencing
│   └── ai_scoring.py       # Claude-based mission alignment scoring
└── export/                 # Output formatting
    └── report.py           # Terminal table formatting

app.py                      # Flask web UI
cli.py                      # CLI entry point
config.py                   # Configuration (env vars)
profiles/                   # Saved search profiles (JSON)
data/foundation_sources.json # Pre-configured foundation scraping targets
templates/                  # Jinja2 HTML templates
static/                     # CSS + JS
```

---

## 3. The Three-Layer Pipeline

The pipeline runs in `src/commands/prospect.py` via `run_full_pipeline()`.

### Layer 1: Find Comparable Organizations

**Goal**: Find nonprofits with a similar mission.

**Process**:
1. Search ProPublica API with profile keywords across specified states
2. Filter by NTEE codes if provided
3. Remove the target org itself
4. Enrich top results with 990 filing data (revenue, assets, etc.)
5. Optionally extract officers from 990 XML Part VII
6. Score each org (0-10 scale based on financial capacity)

**Key function**: `run_prospect_search()` in `src/commands/search.py`

### Layer 2: Identify Funders

**Goal**: Discover which foundations fund those comparable orgs.

Three strategies run in parallel and results are merged:

**Strategy A** (`find_funders_for_org`): Per-org full-text search. Searches ProPublica filing text for mentions of each comp org's name. Finds foundations whose 990s reference the target org.

**Strategy B** (`find_funders_by_sector`): Sector search fallback. Searches for grantmakers in the same NTEE code and state. Matches grant purposes against keywords.

**Strategy C** (`find_funders_for_sector`): Batch sector search. Searches for foundation candidates across the sector, downloads their Schedule I grant lists, and batch-matches all grants against all comp orgs simultaneously. Most effective strategy.

**Web Discovery** (two-hop bridge): See Section 4 below.

Results are deduplicated by EIN and re-scored with merged grant data.

### Layer 3: Fetch Complete Grant Lists

**Goal**: Get full giving history for top funders.

For the top 10 funders (score >= 4):
- Download 990 XML and parse all Schedule I grants
- Adds `all_grants` to each funder for the Programs/Funds report section
- Re-scores funder with complete portfolio data

### Foundation Enrichment

After scoring, the pipeline enriches top funders:
- **Score >= 4** (up to 10 funders): Fetch website URL and ProPublica link
- **Score >= 7**: Also download XML and extract foundation officers (Part VII)

---

## 4. Web Scraping Pipeline

This is the most complex part of the system. It implements a **two-hop bridge** to discover funders that don't appear in 990 cross-references.

```
Comp Org Website → Donor Page → Foundation Names → Foundation Grant Page → Grantee Matching
```

### 4.1 Website Resolution (`website_resolver.py`)

**Purpose**: Find the website URL for each comparable nonprofit.

**Three-strategy cascade**:
1. ProPublica org detail API (`organization.website` field)
2. IRS 990 XML (`WebsiteAddressTxt` XPath)
3. Brave Search API (query: "{org_name} official website {state}")

The Brave Search fallback skips known aggregators (GuideStar, Charity Navigator, Candid, Wikipedia) and social media sites.

**Cache**: 30 days per EIN.

### 4.2 Donor Page Discovery (`donor_scraper.py`)

**Purpose**: Find and scrape donor/sponsor/funder pages on comp org websites.

**Step 1 - Find the donor page**:

The scraper tries 37 URL path patterns appended to the org's website:
```
/donors, /supporters, /our-supporters, /partners, /funders,
/our-funders, /sponsors, /our-sponsors, /corporate-sponsors,
/about/donors, /about/supporters, /annual-report, /impact-report,
/community-partners, /funding-partners, /acknowledgements, /thank-you, ...
```

Each candidate page is validated by checking for donor-related keywords:
```
donor, supporter, funder, partner, sponsor, thank, grateful,
acknowledge, contributor, underwriter, benefactor, philanthropi,
foundations, corporations
```

**Fallback**: If no path pattern matches, the scraper parses the homepage for links containing donor-related terms in their text or href, then follows those links (staying on the same domain).

**Step 2 - Extract foundation names**:

Five extraction strategies are tried (in order of reliability):

1. **Logo extraction**: Alt text from `<img>` tags, title attributes, and domain names from linked images. Domain-to-name conversion removes TLD and splits camelCase. Skips social media domains.

2. **List items**: Text from `<li>` elements, filtered to 4-200 characters, validated as plausible organization names.

3. **Tier-based sections**: Looks for donation tier headings ($100,000+, Platinum, Leadership Circle, etc.) and extracts names from the following elements.

4. **Comma-separated paragraphs**: `<p>` elements with 3+ commas are split and each segment validated as an org name.

5. **Content blocks**: Divs/sections with class/id containing "donor", "sponsor", "partner", "funder", or "supporter". Extracts child text elements.

#### Name Validation Filters

**`_looks_like_org_name(text)`** requires at least one indicator keyword:
```
foundation, fund, trust, group, association, institute, society,
council, corporation, inc, llc, philanthropi, endowment, charitable,
community, united way, family, memorial, arts, science, education, health
```

**`_is_plausible_donor_name(text)`** (broader, for confirmed donor pages):
- Requires 2-8 words, <= 80 characters, title-cased or ALL-CAPS
- Rejects sentences (common verbs), UI text, statistics
- Requires >= 60% capitalized words

**`_clean_name(text)`** removes:
- Leading bullets, asterisks, numbers
- Trailing punctuation
- Prefixes like "Sponsored by", "Funded by", "Supported by"

**Cache**: 30 days per URL.

### 4.3 Foundation Grant Page Scraping (`web_scraper.py`)

**Purpose**: For each foundation name found on donor pages, find their grant page and check if comp orgs appear as grantees.

**Step 1 - Foundation lookup**:
- Check `data/foundation_sources.json` for pre-configured info (17 major foundations)
- Look up on ProPublica to get EIN and website
- Discover grant page URL

**Step 2 - Grant page discovery** (`_discover_grant_page`):

Tries 14 URL path patterns on the foundation's website:
```
/grants, /our-grants, /grantees, /committed-grants,
/what-we-fund, /our-grantees, /grant-recipients,
/awarded-grants, /grants-database, /grants-awarded,
/impact/grants, /programs/grants, /giving/grants,
/annual-report
```

Validation: page must be > 500 bytes and contain grant-related keywords (grant, grantee, recipient, awarded, funded, investment, commitment).

**Fallback**: Brave Search (query: "{foundation_name} grants grantees awarded").

**Step 3 - Parse grantees**:

Three parsing approaches:

**Generic parser** (`_parse_generic`): Used for most foundations.
- Strategy 1 - Tables: Looks for `<tr>` rows with dollar amounts in one cell and org names in another.
- Strategy 2 - Lists: Extracts `<li>` text, splits on `$` to separate name from amount.
- Validates names with `_looks_like_org_or_grantee()`.

**Name-only extraction** (`_extract_names_as_grants`): Fallback when no dollar amounts found.
- Extracts org names from `<li>`, table first cells, and paragraph tags in grantee containers.
- Names must be 8-200 chars, properly capitalized, multi-word.

**Foundation-specific parsers**: Custom parsers for Ballmer Group (`<article class="grant">` structure).

**Step 4 - Match grantees to comp orgs**:

Uses fuzzy matching (`token_sort_ratio`) with **threshold 75** to match scraped grantee names against comp org names. This is intentionally strict to avoid false positives from web-scraped data.

#### Foundation Name Filtering

**`_is_likely_foundation(name)`** validates names extracted from donor pages before investigating:
- Rejects: < 5 or > 100 characters, data labels, decimal numbers, 21 junk patterns ("share on", "click here", "subscribe", etc.), sentences with common verbs
- **Requires** at least one funder keyword: "foundation", "fund", "trust", "endowment", "philanthropi", "group", "association", "institute", etc.
- Without a funder keyword, the name is rejected.

#### Donor-Page-Confirmed Grants

When a foundation has 0 matched grantees on their grant page but WAS found on a comp org's donor page, the system creates a placeholder grant:
```python
Grant(
    purpose="Listed on donor/sponsor page",
    amount=None,
    recipient_name=comp_org_name,
)
```
This preserves the evidence that the foundation has a relationship with the sector.

### 4.4 Pre-Configured Foundation Sources (`data/foundation_sources.json`)

17 major foundations have pre-configured scraping targets:

Ballmer Group, Gates Foundation, Bezos Family Foundation, Simons Foundation, M.J. Murdock Charitable Trust, Paul G. Allen Family Foundation, Seattle Foundation, Raikes Foundation, Norcliffe Foundation, 4Culture, Alfred P. Sloan Foundation, National Science Foundation, Kellogg Foundation, Carnegie Corporation, Walmart Foundation, MacArthur Foundation, Hewlett Foundation.

Each entry specifies: name, EIN, website, grants URL, search parameter (if the page supports search), and which parser to use.

---

## 5. Scoring System

### Organization Scoring (0-10)

Applied to Layer 1 comparable orgs. Based on financial capacity from latest 990 filing.

| Dimension | Points | Thresholds |
|-----------|--------|-----------|
| Revenue | 0-3 | > $10M = 3, > $1M = 2, > $200K = 1 |
| Contributions | 0-3 | > $5M = 3, > $500K = 2, > $50K = 1 |
| Assets | 0-2 | > $10M = 2, > $1M = 1 |
| Grants Paid | 0-2 | > $500K = 2, > $50K = 1 |

**Labels**: >= 7 HIGH PRIORITY, >= 4 STRONG, >= 2 MODERATE, < 2 SMALL

### Funder Scoring (0-22)

Applied to foundation prospects. Combines financial evidence, web evidence, and geographic proximity.

| Dimension | Points | Thresholds |
|-----------|--------|-----------|
| Giving to similar orgs | 0-4 | > $1M = 4, > $500K = 3, > $100K = 2, > $10K = 1 |
| Number of grants to similar | 0-3 | >= 5 = 3, >= 3 = 2, >= 1 = 1 |
| **Web evidence bonus** | 0-4 | >= 3 web matches = 4, >= 2 = 3, >= 1 = 2 |
| Sector match bonus | 0-3 | >= 5 sector grants = 3, >= 2 = 2, >= 1 = 1 |
| Overall giving (filing) | 0-3 | > $10M = 3, > $1M = 2, > $100K = 1 |
| Total giving (all grants) | 0-3 | > $50M = 3, > $10M = 2, > $1M = 1 |
| **Portfolio size proxy** | 0-2 | >= 500 grantees = 2, >= 50 = 1 |
| Geographic proximity | 0-2 | Same state = 2, funds in state = 1 |

**Labels**: >= 10 TOP PROSPECT, >= 7 HIGH PRIORITY, >= 4 STRONG, >= 2 MODERATE, < 2 LOW

**Why web evidence bonus exists**: Web-scraped grants have `amount=None` (no dollar figure available from grant pages). Without this bonus, a foundation confirmed on 3 comp org grant pages would score 0 on the "Giving to similar" dimension. The bonus ensures web-confirmed matches are valued appropriately.

**Why portfolio size proxy exists**: When a foundation's grant page lists names without dollar amounts, the number of grantees serves as a capacity signal. A foundation with 500+ grantees is likely a major funder even if we can't determine exact dollar amounts.

---

## 6. Data Sources & APIs

### ProPublica Nonprofit Explorer API

**Base URL**: `https://projects.propublica.org/nonprofits/api/v2`
**Authentication**: None required (free, public API)
**Rate limit**: 0.5s delay between requests (configurable via `API_DELAY` env var)

**Endpoints used**:
- `/search.json?q={query}&state[id]={state}` - Organization search
- `/organizations/{ein}.json` - Organization detail + filing history
- XML download via `object_id` from org detail

**Known quirk**: Combining `state[id]` + `c_code[id]` filters sometimes returns 404. The client implements a retry cascade that drops filters on failure.

**Critical detail**: The `latest_object_id` needed for XML download is on the **organization** object, NOT in `filings_with_data[0]`.

### ProPublica Full-Text Search (HTML Scraping)

**URL**: `https://projects.propublica.org/nonprofits/search`
**Not a JSON API** - scrapes HTML search results page.
Searches filing text for mentions of organization names. Used to find foundations that reference comp orgs in their 990 filings.

### IRS 990 XML

Parsed by `src/api/xml_parser.py`. Handles Form 990, 990-EZ, and 990-PF with multiple schema variations.

**Extracted data**:
- **Part VII**: Officers/directors/trustees (name, title, compensation, hours)
- **Schedule I**: Grants made (recipient name, EIN, amount, purpose, city, state, tax year)

The parser tries multiple XPath patterns per field to handle schema variations across filing years.

### Brave Search API (optional)

**URL**: `https://api.search.brave.com/res/v1/web/search`
**Requires**: `BRAVE_API_KEY` environment variable
**Used for**: Finding nonprofit websites and foundation grant pages when other methods fail.
**Cost**: Free tier ~1000 queries/month.

### Anthropic Claude API (optional)

**Requires**: `ANTHROPIC_API_KEY` environment variable
**Used for**: AI mission alignment scoring (0-100) and grant letter drafting.

---

## 7. Web App (Flask UI)

### Entry Points

| Route | Purpose |
|-------|---------|
| `/` | Home page with hero CTA |
| `/profile/new` | Profile builder wizard (3-step form) |
| `/profile/run` | Save profile + start pipeline |
| `/loading/<task_id>` | Loading page with progress polling |
| `/results/<task_id>` | Pipeline results (tabbed view) |
| `/report/<task_id>` | Printable foundation report |
| `/org/<ein>` | Single org detail page |
| `/funder/<ein>` | Foundation detail with grant history |
| `/api/org-search?q=` | Autocomplete endpoint (JSON) |
| `/api/status/<task_id>` | Task progress polling (JSON) |

### Background Task System

Long-running operations (pipeline, funder search) run in background threads. The `/loading` page polls `/api/status/<task_id>` every 2 seconds. Tasks auto-cleanup after 1 hour.

### Profile Builder Flow

1. **Step 1**: Search for nonprofit by name (autocomplete) or enter EIN manually
2. **Step 2**: Confirm/edit org details, keywords, mission, search states
3. **Step 3**: Set pipeline depth and max comps, click Run
4. Profile saved to `profiles/` directory, pipeline starts as background task

### Report Template

Per-funder cards with 4 sections:
- **Evidence Trail**: Grants to similar orgs with amounts, years, discovery method
- **Foundation Profile**: Assets, annual giving, revenue, portfolio size, links
- **Programs & Funds**: Grants grouped by normalized purpose
- **Contact Signals**: Officers with titles/compensation, shared board members

Print-optimized with `@media print` styles.

---

## 8. Configuration & Caching

### Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `SECRET_KEY` | `dev-prospect-explorer-key` | Flask session encryption |
| `API_DELAY` | `0.5` | Seconds between ProPublica API calls |
| `CACHE_ENABLED` | `1` | Enable SQLite caching (`0` to disable) |
| `PORT` | `5000` | Flask server port |
| `FLASK_DEBUG` | `0` | Debug mode (`1` to enable) |
| `BRAVE_API_KEY` | (none) | Brave Search API key (optional) |
| `ANTHROPIC_API_KEY` | (none) | Claude API key (optional) |

### SQLite Cache

Location: `~/.prospect-explorer/cache.db`

| Table | Key | TTL | Purpose |
|-------|-----|-----|---------|
| `org_data` | EIN | 7 days | ProPublica org detail responses |
| `xml_filings` | object_id | 1 year | Downloaded 990 XML files |
| `parsed_grants` | (ein, year) | No expiry | Parsed Schedule I grants |
| `officers` | EIN | 1 year | Extracted Part VII officers |
| `web_pages` | cache_key | 7 days | Scraped web pages (donor pages, grant pages) |

To clear cache for re-scraping:
```sql
DELETE FROM web_pages WHERE cache_key LIKE 'donor:%';
DELETE FROM web_pages WHERE cache_key LIKE 'fdn_%';
```

---

## 9. Known Limitations

### Web Scraping

- **JavaScript-rendered pages**: Foundation grant pages that require JavaScript (e.g., Gates Foundation) produce limited results with HTML-only parsing.
- **PDF-only grant lists**: Foundations that publish grants only as PDFs are not scraped.
- **Paywalled/login-required pages**: Not accessible.
- **URL discovery**: Grant page path guessing is limited to 14 common patterns. Non-standard URLs may be missed.
- **False positives**: Despite filtering, navigation text or UI elements can occasionally be extracted as foundation names. The `_is_likely_foundation()` filter mitigates this but isn't perfect.

### API Quirks

- **ProPublica filter combos**: `state[id]` + `c_code[id]` combinations sometimes return 404. The retry cascade drops filters progressively.
- **Officer names**: Not available in ProPublica summary API. Must download and parse 990 XML (expensive but cached).
- **Stale data**: ProPublica data lags IRS filing dates by months. Latest filing may be 1-2 years old.

### Scoring

- **No negative signals**: Funders that primarily fund unrelated sectors still receive points if they fund ANY comp org. There's no penalty for misalignment.
- **Web evidence bonus parity**: A foundation confirmed on one comp org's grant page (no dollar amount) scores similar to a $20K confirmed grant. This is by design (any confirmation is valuable) but may overweight weak evidence.
- **Common name collisions**: Board member cross-referencing assumes same normalized name = same person. Common names like "John Smith" may produce false matches.

### Fuzzy Matching Thresholds

| Context | Threshold | Rationale |
|---------|-----------|-----------|
| Web bridge (grant page to comp org) | 75 | Strict to avoid false positives from scraped data |
| 990 Schedule I matching | 55 | Lower because data is structured and reliable |
| Donor name cross-reference | 60 | Moderate for name-to-name comparison |
| General matching | 70 | Default balance of precision vs recall |

### Scale

- Pipeline runs 3-8 minutes for 25 comp orgs at depth 2
- ProPublica API delay (0.5s) is the primary bottleneck
- Web scraping adds 1-3 minutes depending on number of donor pages found
- Free Brave Search tier limits to ~1000 queries/month

---

## 10. Critical Assumptions

1. **Donor pages list actual funders**: The system assumes nonprofit donor/sponsor pages list legitimate foundation funders, not random corporate sponsors or individual donors.

2. **Foundation names contain funder keywords**: The `_is_likely_foundation()` filter requires names to contain words like "foundation", "fund", "trust", etc. Foundations without these words in their name (rare but possible) are filtered out.

3. **Grant pages contain parseable HTML**: Assumes grantee lists appear as tables, lists, or structured text in HTML. Database-driven pages with dynamic loading are not fully supported.

4. **Fuzzy matching handles name variations**: Uses `token_sort_ratio` which handles word reordering ("Gates Foundation" vs "Foundation, Gates") but may miss abbreviations, acronyms, or DBA names.

5. **IRS 990 XML schemas are stable**: The parser tries multiple XPath patterns per field. Schema changes in new filing years could require adding new patterns.

6. **ProPublica data is reasonably current**: Latest filing data may be 1-2 years old. Financial figures and officer data reflect the most recent available filing, not necessarily the current year.

7. **Web scraping is legally permissible**: All scraping includes respectful delays (1.5s between requests) and standard User-Agent headers. No login bypass or CAPTCHA solving.

8. **Web-scraped grants without dollar amounts still indicate alignment**: A `Grant` with `amount=None` means "we confirmed this foundation funds similar orgs but don't know the exact amount." The scoring system treats this as meaningful evidence via the web evidence bonus.

---

## 11. Testing & Debugging

### Running the Pipeline

**CLI**:
```bash
python cli.py prospect --profile profiles/math_agency.json --depth 2 --limit 25
```

**Web UI**:
```bash
python app.py
# Navigate to http://127.0.0.1:5000
```

### Diagnostics

Most pipeline functions accept a `diagnostics: list` parameter. Diagnostic messages are appended during execution and displayed in the web UI's collapsible "Pipeline Diagnostics" section.

### Cache Management

```bash
# Check cache location
ls ~/.prospect-explorer/

# Clear all web page cache (forces re-scraping)
sqlite3 ~/.prospect-explorer/cache.db "DELETE FROM web_pages;"

# Clear specific donor page cache
sqlite3 ~/.prospect-explorer/cache.db "DELETE FROM web_pages WHERE cache_key LIKE 'donor:%';"

# Clear foundation grant page cache
sqlite3 ~/.prospect-explorer/cache.db "DELETE FROM web_pages WHERE cache_key LIKE 'fdn_%';"
```

### Individual Layer Testing

```python
from src.api.propublica import ProPublicaClient
from src.core.cache import FilingCache

client = ProPublicaClient(delay=0.5, cache=FilingCache())

# Layer 1: Search
from src.commands.search import run_prospect_search
raw_orgs = run_prospect_search(client, ["math education"], ["WA"])

# Layer 2: Find funders for a specific org
from src.api.fulltext_search import FullTextSearchClient
ft_client = FullTextSearchClient(delay=1.0)
from src.commands.funders import find_funders_for_org
funders = find_funders_for_org(client, ft_client, "Math Academy", org_ein="334593800")

# Full pipeline
import json
with open("profiles/math_agency.json") as f:
    profile = json.load(f)
from src.commands.prospect import run_full_pipeline
enriched, funders, shared, diag = run_full_pipeline(client, profile)
```

### Test Profile

`profiles/math_agency.json` is a pre-built example for The Math Agency (EIN 33-4593800, Seattle WA, NTEE B90). Use this for integration testing.

Expected results at depth 2: 15-25 comparable orgs, 5-15 funder prospects including Ballmer Group, Simons Foundation, and local WA foundations.

---

## 12. Development Process Notes

### Key Design Decisions

1. **Two-hop web discovery was added late** to supplement 990-based funder discovery. Many smaller foundations don't appear in ProPublica's full-text search but DO appear on nonprofit donor pages.

2. **The web evidence bonus (0-4 points)** was added after discovering that web-scraped funders like Simons Foundation were scoring LOW despite having confirmed evidence. The root cause: web grants have `amount=None`, so all dollar-based scoring dimensions returned 0.

3. **Donor-page-confirmed grants** ("Listed on donor/sponsor page") were added after Stemtac Foundation showed no evidence despite being listed on Seattle Universal Math Museum's sponsor page. The system now creates placeholder grants for foundations found on donor pages even when their grant page shows no matches.

4. **The portfolio size proxy (0-2 points)** compensates for foundations whose grant pages list names without dollar amounts. A foundation with 500+ grantees on file is likely a major funder even without specific dollar data.

5. **Foundation name filtering (`_is_likely_foundation`)** was iteratively tightened after false positives from navigation text, statistics, and UI elements on donor pages were being treated as foundation names.

### Dependencies

- Python 3.10+
- Flask 3.x (web UI)
- Waitress (production WSGI server)
- requests (HTTP client)
- lxml (XML/HTML parsing)
- python-dotenv (env var management)
- No database required beyond SQLite cache
