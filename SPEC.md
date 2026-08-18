# COMPAS Ecosystem Dashboard — Implementation Spec

> This document is the implementation contract for a new repository. An LLM (or
> developer) should be able to build the whole project from this file alone. It
> is self-contained: it describes the goal, the architecture, every data source,
> the exact data schemas, and a milestone plan.

## 1. Goal

A single web page that lets a COMPAS maintainer answer, at a glance:

- **Is each package keeping up?** — maintenance health (last release, CI status,
  issue/PR backlog, staleness).
- **Where are we in the 2.x migration?** — which COMPAS-core version each package
  pins, which Python versions each supports, which host apps (Rhino/Blender/GH).
- **Which features/APIs still need adoption across the board?** — a curated set of
  "things we want every package to adopt or stop using" (new Scene API, new Data
  serialization, dropping deprecated calls, etc.), shown as an adoption matrix.

The dashboard replaces the mental model maintainers currently keep in their heads
and the tab-hopping across ~30 GitHub repos. It is the spiritual successor to the
archived `compas-dev/compas_dashboard`.

**Non-goals (v1):** no write-back to GitHub, no auth, no per-user state, no
real-time data. Nightly freshness is fine.

## 2. Architecture

**Static site built by a scheduled GitHub Action.** No server, no runtime cost,
hosted on GitHub Pages. Two halves:

```
┌─────────────────────────────────────────────────────────────┐
│  GitHub Action (nightly cron + manual dispatch + on push)    │
│                                                              │
│   collector (Python)                                         │
│     reads  repos.yml + features.yml                          │
│     calls  GitHub REST/GraphQL API, PyPI API                 │
│     parses pyproject.toml / requirements.txt / CI workflows  │
│     writes site/public/data.json  (+ data-history/*.json)    │
│                                                              │
│   lightweight materials collector (weekly / manual)           │
│     reads materials.yml; fetches repository metadata only     │
│     writes site/public/materials.json                         │
│                                                              │
│   frontend build (Vite)                                      │
│     bundles static site that fetches data.json               │
│                                                              │
│   deploy → GitHub Pages                                       │
└─────────────────────────────────────────────────────────────┘
```

- The collector is the heart of the project. The frontend is a dumb renderer of
  `data.json` — it must work by opening the built site with no backend.
- Data is at most ~24h stale; a "last updated" timestamp is shown in the UI.
- The Action needs only the default `GITHUB_TOKEN` (read-only, public repos).
  Rate limits are ample for ~30 repos nightly; add simple retry/backoff.

### Recommended stack

- **Collector:** Python 3.11+, `httpx` (or `requests`), `tomllib` (stdlib),
  `PyYAML`, `packaging` (for version specifiers). Ship a `pyproject.toml`,
  runnable as `python -m collector` and locally with a `--token` flag.
- **Frontend:** Vite + Vue 3 (matches the ecosystem — `compas.dev` and
  `compas-webviewer` are Vue). Plain TypeScript. No CSS framework required; a
  small hand-written stylesheet is fine. The stack is swappable — the only hard
  contract is "static site that renders `data.json`".
- **CI:** a nightly ecosystem/deploy workflow and a weekly lightweight
  materials workflow.

## 3. Repository layout

```
compas-dashboard/                (name TBD; compas_ecosystem / compas_dashboard)
├── README.md
├── repos.yml                    # curated list of tracked repositories
├── materials.yml                # workshops/projects outside fleet metrics
├── features.yml                 # curated features/APIs to track for adoption
├── collector/
│   ├── pyproject.toml
│   ├── __main__.py              # entrypoint: orchestrates collection
│   ├── github.py                # GitHub API client (REST + GraphQL)
│   ├── pypi.py                  # PyPI API client
│   ├── parse.py                 # pyproject/requirements/CI-matrix parsing
│   ├── features.py              # feature-detection engine
│   └── model.py                 # dataclasses + JSON schema of data.json
├── site/
│   ├── index.html
│   ├── src/                     # Vue app
│   └── public/
│       ├── data.json            # full ecosystem collection
│       └── materials.json       # lightweight workshop/project metadata
├── data-history/                # committed snapshots (see §7)
└── .github/workflows/
    ├── build-and-deploy.yml
    └── collect-materials.yml
```

## 4. Curated inputs

### 4.1 `repos.yml` — the tracked repository list

Curated (not auto-discovered), because the ecosystem includes archived, external,
and non-Python repos we want to include/exclude deliberately. Schema:

```yaml
defaults:
  owner: compas-dev
  branch: main            # default branch to inspect; per-repo override allowed

repos:
  - name: compas
    category: core         # core | fabrication | geometry | fea | viz | xr | tooling | template
    role: core             # the package everyone else pins; drives the matrix
    pypi: compas

  - name: compas_fab
    category: fabrication
    pypi: compas_fab

  - name: compas_viewer
    category: viz
    pypi: compas_viewer

  - name: compas_view2
    category: viz
    status: archived        # render greyed-out / EOL; still show last state
    pypi: compas_view2

  - name: some_external_pkg
    owner: some-other-org   # override default owner
    category: geometry
    pypi: some-external-pkg
```

Fields: `name` (required), `owner` (default `compas-dev`), `branch`
(default `main`), `category`, `role`, `pypi` (PyPI project name; omit if not
published), `status` (`active`|`archived`|`wip`, default derived from the API's
`isArchived`), `runtime` (`python`|`node`, default `python`), optional
`distributions` (`pypi`|`npm`|`jsr`), `release_tag_prefix`, explicit
`ecosystem_deps`, and optional `notes`. The legacy `pypi` field remains a
shorthand for a PyPI distribution.

Seed it from the real org listing (see Appendix A).

### 4.2 `materials.yml` — workshops, courses, and projects

Material repositories are intentionally separate from the maintained ecosystem
fleet. They receive one lightweight repository-metadata request during a weekly
or manual collection and do not participate in CI health, issues/PRs, package
registries, feature adoption, dependency-stack totals, or daily history.

```yaml
materials:
  - name: example-workshop
    owner: example-org
    kind: workshop              # workshop | course | tutorial | project | reference
    category: fabrication       # same broad taxonomy as fleet repositories
    ecosystem_deps: [compas, compas_fab]
    notes: Optional curator context.
```

The generated `site/public/materials.json` contains repository identity,
description, language, stars, last activity, archived status, topics, homepage,
and curated dependency hints. Forks and unrelated organizational repositories
remain excluded deliberately.

### 4.3 `features.yml` — the adoption matrix definition

Each feature is a column in the adoption matrix. A feature has a **detection
method** that the collector runs against each repo to produce a cell status of
`adopted` | `not-adopted` | `n/a` | `unknown`.

Runtime-specific features declare `applies_to: [python]` or `[node]`.
Inapplicable cells are emitted as `n/a` without running their detector.

```yaml
features:
  - id: compas2
    label: "On COMPAS 2.x"
    kind: pin                      # inspect the `compas` requirement specifier
    detect:
      package: compas
      satisfied_by: ">=2,<3"       # adopted if the pin's floor is >= 2

  - id: py312
    label: "Python 3.12"
    kind: python                   # inspect supported Python versions
    detect:
      version: "3.12"

  - id: py313
    label: "Python 3.13"
    kind: python
    detect:
      version: "3.13"

  - id: new-scene-api
    label: "New Scene API"
    kind: code                     # GitHub code search within the repo
    detect:
      language: Python             # optional GitHub language qualifier
      # adopted if ANY `present` pattern matches and NO `absent` pattern matches
      present: ["from compas.scene", "import compas.scene", "from compas import scene"]
      absent: ["from compas.artists", "import compas.artists", "from compas import artists"]
      no_match: n/a                # optional status when neither set matches

  - id: no-deprecated-artist
    label: "No deprecated Artist"
    kind: code
    detect:
      language: Python
      absent: ["from compas.artists", "import compas.artists", "from compas import artists"]

  - id: mkdocs
    label: "Docs on mkdocs"
    kind: file                     # presence of a file in the repo
    detect:
      any_of: ["mkdocs.yml"]

  - id: mission-control-badge
    label: "Mission Control badge"
    kind: readme                   # substring match; linked-image Markdown also matches equivalent HTML
    detect:
      present: ["[![Made with COMPAS](https://compas.dev/badge.svg)](https://compas.dev/mission-control/#{name})"]

  - id: some-manual-thing
    label: "Manual review item"
    kind: manual                   # status comes from repos.yml overrides
```

`kind` values the collector must implement: `pin`, `python`, `code`, `file`,
`registry-match`, `conda`, and `manual`. Manual overrides live per-repo:

```yaml
# in repos.yml, optional per repo:
  - name: compas_slicer
    ...
    feature_overrides:
      new-scene-api: adopted        # human override wins over detection
      some-manual-thing: not-adopted
```

## 5. Collector — data sources & rules

For each repo in `repos.yml`, gather the following. **Every field must degrade
gracefully to `null`/`unknown` on error or 404 — one bad repo must never fail the
whole run.**

### 5.1 Maintenance health (GitHub API)

| Field | Source |
|---|---|
| `description`, `stars`, `archived`, `default_branch`, `pushed_at`, `language` | `GET /repos/{owner}/{repo}` |
| `open_issues`, `open_prs` | issues search / PR list (issues count includes PRs — separate them) |
| `oldest_open_issue_age_days` | issues search sorted asc |
| `latest_release` (tag, date) | `GET /repos/{owner}/{repo}/releases/latest` (fallback: tags) |
| `default_branch_ci` (`passing`\|`failing`\|`none`) | `GET /repos/{owner}/{repo}/commits/{branch}/check-runs` or Actions runs on default branch |
| `contributors_90d` | commit activity / contributors API (bus-factor proxy) |
| `last_commit_date` | `pushed_at` or latest commit on default branch |

Derive a **staleness bucket** from `last_commit_date`: `fresh` (<30d),
`aging` (30–120d), `stale` (120–365d), `dormant` (>365d). Archived repos are
always shown as EOL regardless.

### 5.2 Packaging / migration (file parsing)

Fetch these files from the default branch via
`GET /repos/{owner}/{repo}/contents/{path}` (base64 → decode):

- `pyproject.toml` → project `name`, `requires-python`, `classifiers`
  (Python versions), URLs. Use `tomllib`.
- `requirements.txt` (and `requirements-dev.txt`) → dependency specifiers. This
  is where the **`compas` core pin** lives (e.g. `compas >= 2.3, < 3`). Note:
  many repos use `dynamic = ['dependencies']` in pyproject and put the real deps
  in `requirements.txt`, so **always parse `requirements.txt`**, not just
  pyproject. Use `packaging.requirements.Requirement` +
  `packaging.specifiers.SpecifierSet`.
- `.github/workflows/build.yml` (and any workflow with a matrix) → the **CI
  Python matrix** (`strategy.matrix.python-version` / `python`). This is the
  **authoritative** source of supported Python versions.

**Python version resolution rule:** prefer the CI matrix; fall back to
classifiers; then `requires-python`. Record all three but expose one canonical
`python_versions` list plus a `python_source` note. (Rationale: classifiers are
frequently stale — `compas_fab` advertises only 3.9 and 3.13.)

**COMPAS-core version:** extract the `compas` specifier from `requirements.txt`.
Record the raw specifier string and a resolved `compas_major_floor` (e.g. `2`)
used by the `compas2` feature. Repos that are compas core itself, or don't depend
on compas, get `n/a`.

**Host-app support (Rhino/Blender/GH):** detect by presence of known packages/
paths — e.g. a `src/<pkg>_rhino`, `_ghpython`, `_blender` package dir, or a
`publish_yak` workflow (Rhino/GH), or relevant deps. Keep the heuristics in
`parse.py` and document them; allow manual override via `repos.yml`.

### 5.3 PyPI (PyPI JSON API)

If `pypi` is set: `GET https://pypi.org/pypi/{name}/json` → latest published
version, release date, supported classifiers. Flag **version drift**: latest
GitHub release tag vs latest PyPI version (helps spot unreleased packages).

### 5.3.1 Node packages and registries

For `runtime: node`, parse `package.json` for `engines.node`,
`packageManager`, and runtime/peer/optional dependencies. Configured npm and JSR
distributions are queried through their public metadata APIs and participate in
the same GitHub-release drift check as PyPI. GitHub tags may be normalized with
`release_tag_prefix`. Manifest dependencies and explicit `ecosystem_deps` are
combined in the ecosystem graph.

### 5.4 Feature detection engine (`features.py`)

For each `(repo, feature)` pair, compute a cell status:

- `kind: pin` → parse the named package's specifier from requirements; compare
  floor to `satisfied_by`.
- `kind: python` → is the version in the resolved `python_versions`?
- `kind: file` → does any `any_of` path exist in the repo?
- `kind: code` → literal substring scan of the repository source. `present`
  patterns → `adopted` if any match; `absent` patterns → `adopted` if none
  match. With `--local-scan` (the default in CI) the source tarball is fetched
  once and scanned on disk; `language:` is approximated by that language's file
  extensions, and vendored trees (`node_modules`, `.tox`, …) are skipped.

  Without `--local-scan` this falls back to the GitHub code search API
  (`GET /search/code`), which is **capped at 10 requests/minute** and made the
  nightly run take over two hours, mostly asleep in rate-limit backoff.
  Exhausted retries were recorded as `unknown`, so cells flickered between
  runs. That API also *tokenises* queries rather than matching substrings, so
  dotted patterns such as `compas.scene.` never meant what `features.yml`
  reads as though they mean, and its index both misses files and returns
  different answers on repeated identical queries. Prefer the local scan.
- `kind: manual` → read from `feature_overrides`; else `unknown`.
- `kind: registry-match` → compare the normalized GitHub release version with
  every configured PyPI, npm, or JSR distribution.
- **Any** `feature_overrides` entry always wins over auto-detection.

Output per cell: `{status, source: "auto"|"manual", detail}` where `detail` is a
short human string ("compas >= 2.3, < 3", "CI: 3.9–3.13", "matched Artist(").

## 6. `data.json` schema (the frontend contract)

```jsonc
{
  "generated_at": "2026-07-06T02:00:00Z",
  "features": [
    { "id": "compas2", "label": "On COMPAS 2.x", "kind": "pin" }
    // ...in column order
  ],
  "categories": ["core", "fabrication", "geometry", "fea", "viz", "xr", "tooling"],
  "repos": [
    {
      "name": "compas_fab",
      "owner": "compas-dev",
      "url": "https://github.com/compas-dev/compas_fab",
      "category": "fabrication",
      "runtime": "python",
      "status": "active",
      "description": "Robotic fabrication package for the COMPAS Framework",
      "stars": 134,
      "health": {
        "last_commit_date": "2026-06-20",
        "staleness": "aging",
        "ci": "passing",
        "open_issues": 12,
        "open_prs": 3,
        "oldest_open_issue_age_days": 210,
        "contributors_90d": 4
      },
      "release": {
        "github_tag": "1.0.2",
        "github_date": "2026-05-01",
        "pypi_version": "1.0.2",
        "pypi_date": "2026-05-01",
        "drift": false
      },
      "distributions": [
        { "registry": "pypi", "name": "compas_fab", "version": "1.0.2", "date": "2026-05-01" }
      ],
      "packaging": {
        "compas_pin": "compas >= 2.3, < 3",
        "compas_major_floor": 2,
        "python_versions": ["3.9", "3.10", "3.11", "3.12", "3.13"],
        "python_source": "ci-matrix",
        "hosts": { "rhino": true, "ghpython": true, "blender": false }
      },
      "features": {
        "compas2":       { "status": "adopted",     "source": "auto", "detail": "compas >= 2.3, < 3" },
        "py313":         { "status": "adopted",     "source": "auto", "detail": "CI matrix" },
        "new-scene-api": { "status": "not-adopted", "source": "auto", "detail": "no Scene import" }
      }
    }
    // ...one per repo
  ]
}
```

## 7. Trend history (optional but cheap)

At the end of each run, append a compact snapshot to `data-history/YYYY-MM-DD.json`
(a subset: per-repo staleness, ci, open_issues/prs, compas_major_floor, and the
count of adopted features). Version 2 snapshots also preserve the feature
definitions active that day and each repository's status for every feature,
including `n/a` and `unknown`. Readers should treat snapshots without a
`schema_version` as version 1. Commit snapshots back from the Action. Keep them
small; prune older than N months if size becomes a concern.

## 8. Frontend — views

One static application with deep-linked Fleet, Ecosystem, Roadmap, Materials,
and repository-detail views. Consult a data-viz design pass for color/consistency;
keep it legible in light and dark.

1. **Header / summary band** — generated-at timestamp, ecosystem totals
   (repos tracked, % on COMPAS 2.x, # failing CI, # dormant), and a filter/search
   box (by name, category, status).

2. **Health table** (default view) — one row per repo, sortable columns:
   name, category, staleness (color dot + relative date), CI badge, open
   issues / PRs, latest release (with PyPI-drift warning), stars. Archived repos
   greyed and sortable to the bottom. Click a row → link to the repo.

3. **Migration & compatibility matrix** — repos as rows, columns = COMPAS-core
   pin + a Python-version grid (3.9…3.13 ✓/✗) + host-app icons (Rhino/GH/Blender).
   This is the "are we all on 2.x / modern Python" view.

4. **Feature adoption matrix** — repos as rows, `features` as columns, each cell a
   status chip (`adopted` green ✓ / `not-adopted` red ✗ / `n/a` grey / `unknown` ?).
   Hover shows `detail`. A per-column footer shows adoption % ("18/24 adopted").
   This is the "what still needs support across the board" view. Manual cells
   marked with a small dot so auto vs human-set is distinguishable.

Interaction: filters apply to all three sections at once. No routing needed;
tabs or anchor sections are both fine. Everything renders from `data.json` —
no live API calls in the browser.

5. **Materials library** — a separate `#materials` catalog rendered from
   `materials.json`, ordered newest-first and grouped by last-activity year.
   It is filterable by the fleet's broad categories, material type,
   organization, archive status, and text. It shows lightweight repository
   metadata and links known COMPAS dependencies back to package detail pages.
   Materials never enter the header's fleet totals, health/adoption views,
   dependency graph, or history.

## 9. CI workflow

`.github/workflows/build-and-deploy.yml`:

- Triggers: `schedule` (nightly cron, e.g. `0 2 * * *`), `workflow_dispatch`,
  and `push` to main (rebuild frontend on code changes).
- Job steps: checkout → set up Python → run collector (writes
  `site/public/data.json`, appends `data-history/`) → set up Node → build Vite
  site → upload Pages artifact → deploy to Pages. Commit `data-history/` back
  (use a bot commit; skip if unchanged).
- Permissions: `contents: write` (for history commit), `pages: write`,
  `id-token: write`. Uses default `GITHUB_TOKEN`.
- Add concurrency guard so overlapping runs don't fight.

## 10. Milestones

1. **M1 — Scaffold + health.** Repo, `repos.yml` seeded from Appendix A, collector
   that produces `data.json` with §5.1 health fields, minimal frontend health
   table, CI deploying to Pages. Ship this first — it's already useful.
2. **M2 — Migration matrix.** Add §5.2 packaging parsing (compas pin, Python
   versions from CI matrix, hosts) + PyPI drift + the compatibility matrix view.
3. **M3 — Feature adoption.** Add `features.yml`, the detection engine (§5.4)
   incl. code-search, manual overrides, and the adoption matrix view.
4. **M4 — Polish.** Trend history + sparklines, dark mode, filters, empty/error
   states, a11y, and a data-viz design pass on colors and chips.

## 11. Implementation notes / gotchas

- **Fail soft, per repo and per field.** Wrap every fetch; record `unknown`/`null`
  and a `warnings[]` array in `data.json` rather than crashing.
- **Rate limits.** GitHub REST: 5000/h authenticated — fine. Code search: 30/min —
  throttle and cache; consider running code-search features only in the nightly
  job, not on every push.
- **`dynamic` dependencies.** Real deps are usually in `requirements.txt`, not
  pyproject — parse both.
- **Python support signals differ.** Prefer the normative `requires-python`
  declaration. Use classifiers as a fallback, and the CI matrix last: CI says
  what is tested, but a minimal matrix is not an exhaustive compatibility list.
- **Archived repos.** Keep them visible but visually demoted (they carry
  historical migration signal, e.g. `compas_view2` → `compas_viewer`).
- **Determinism.** Sort everything (repos by category then name, features by
  declared order) so `data.json` diffs are clean in git history.
- **Local dev.** Collector runnable locally with a PAT; frontend runnable against
  a committed sample `data.json` so contributors don't need to run collection.

## Appendix A — Seed repo list (compas-dev, live snapshot 2026-07-06)

Active Python packages worth tracking (stars in parens):

- **core:** compas (373)
- **fabrication/robots:** compas_fab (134), compas_robots (4), compas_slicer (44),
  compas_rrc*, compas_eve (6), compas_xr (17)
- **geometry/CAD:** compas_occ (27), compas_cgal (32, C++), compas_libigl (5, C++),
  compas_nurbs (13), compas_gmsh (2), compas_triangle (2), compas_shapeop (3, C++),
  compas_ifc (21), compas_usd
- **fea:** compas_fea2 (14)  (compas_fea is ARCHIVED)
- **viz:** compas_viewer (12), compas_plotters, compas_notebook, compas-webviewer (7)
  (compas_view2 ARCHIVED)
- **tooling/templates:** compas_invocations2, compas_package_template (6),
  compas_nanobind_package_template, compas-actions.* (build/docs/publish/
  ghpython_components/docversions), sphinx_compas2_theme
- **meta:** compas_framework

Archived (show as EOL): compas_fea, compas_view2, compas_dashboard, compas_fea's
peers, examples, sphinx_compas_theme, compas-exchange.

Non-Python / infra to decide on: compas.dev (website), .github, staged-recipes.

> Curate this into `repos.yml` — decide inclusion/exclusion deliberately rather
> than auto-importing everything.
