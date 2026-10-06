# PMATCH CTML

Turns a clinical trial protocol PDF into a CTML review candidate for UHN PMATCH.

- Azure Document Intelligence reads the PDF.
- Code finds the sections, the protocol's abbreviations and the numbered eligibility criteria.
- Agents built with Microsoft Agent Framework extract the metadata and the study design, and encode each criterion. By default they follow the agent split of architecture v5: a supervisor workflow with eight specialist agents (see [Agent layouts](#agent-layouts)).
- Code builds the CTML and a QA report.

**Status.** The offline suite has 161 tests (no network); local verification used Python 3.11.9. The package has made local live calls to Azure Document Intelligence and Foundry model deployments, with the compact agent layout (09/29) and the v5 layout (10/05). The 10/06 fixes to the v5 layout are tested offline only. Outputs remain review candidates, not clinically validated results. No hosted Foundry agent is deployed.

**Every output needs review.** The status of every output is `needs_review`. The CTML is a candidate for clinical review, not a validated result.

**Hosting.** The model is called through the OpenAI v1 endpoint of an Azure AI Foundry resource. This is not the hosted Foundry agent service.

## How it works

| # | Stage | Done by | Output |
|---|---|---|---|
| 1 | Parse the PDF (prebuilt-layout, markdown), cached by file checksum | Azure Document Intelligence | layout JSON |
| 2 | Lines with IDs (`L123`), tables (`T4`), sections, glossary, eligibility criteria with population labels | code | `document/` |
| 3 | Load OncoTree; prepare NCIt, HGNC and ClinicalTrials.gov lookups | code | `registry/`, `lookups.jsonl` |
| 4 | The agents, under the v5 supervisor workflow (default) or in the compact layout | agents | `agents/` |
| 5 | Build the CTML: arms, match trees, additional criteria | code | `<trial>_CTML.json` |
| 6 | QA: schema, output policy, blocking issues, review list | code | `qa_report.md` |

Each agent reads with tools and then calls its submit tool.

- The submit tool runs checks and returns errors until the output passes.
- A check that finds an unusual but possibly correct pattern asks the agent to confirm it with the exact source words.
- Each coded diagnosis, drug, class or gene must cite a real registry lookup made by the agent that uses it, with the returned name copied exactly; the registry response may be served from the seven-day cache.
- Every rule is tested on example patients before it is accepted.

The agents get no term lists. Terms come from three places:

- the protocol;
- live registry lookups, each recorded with a `lookup_id`;
- the CTML schema.

## Agent layouts

`PMATCH_AGENT_LAYOUT` in `.env` chooses the layout.

**`v5` (default).** The agent split of architecture v5, under a supervisor workflow built with Microsoft Agent Framework (`workflow.py`). The supervisor is code: it decides every next step, and the model never chooses the process.

| Agent | Runs | Does |
|---|---|---|
| Metadata | once | Titles, protocol number, version, phase, sponsor, purpose |
| Study design | once | Drugs, arms, dose levels and the populations each arm covers |
| Coverage checker | once per criterion | Lists every condition of the criterion on its own; it never sees the encoding |
| Eligibility logic | once per criterion, after the study design | The AND / OR / NOT structure, with one typed slot per condition CTML can hold and text for the rest |
| Clinical | per criterion with clinical slots | Diagnosis (OncoTree), age, HER2 / ER / PR status |
| Genomics | per criterion with genomic slots | Gene (HGNC) and alteration |
| Prior therapy | per criterion with prior-therapy slots | Drug or drug class (NCI Thesaurus), treatment category, transplant |
| Resolver | once, on the compiled candidate | Problems with exact protocol quotes; it never edits |

Code joins the slot values into the structure (`merge.py`), compares the structure with the coverage listing, sends a criterion with omissions back once, and sends criteria with verified resolver findings back once. A finding that remains after a repair goes to the QA review list. A v5 run makes about 3 to 5 agent runs per criterion instead of 1.

**`compact`.** The earlier layout: one criterion agent per criterion does the work of the eligibility logic and the three domain agents, and code checks take the place of the coverage and resolver agents. It stays for comparison until both layouts have been measured on the same protocols.

The two layouts share the document lane, the registries, the submit loop, the compiler and the QA gate.

For the execution diagram, every module/script, artifacts and a criterion-level audit trail, see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Conventions and open questions are in [docs/DECISIONS.md](docs/DECISIONS.md); the first-run checklist is [docs/RUNBOOK.md](docs/RUNBOOK.md). Repository agent rules are in [.github/copilot-instructions.md](.github/copilot-instructions.md).

## Requirements

- Python 3.10 or newer (`pyproject.toml` declares `>=3.10`); the local live runs used 3.11.9. Run the checks on any other interpreter you deploy.
- An Azure Document Intelligence resource.
- An Azure AI Foundry model deployment, reachable through `https://<resource>.services.ai.azure.com/openai/v1/`.
- Outbound HTTPS to:
  - `oncotree.mskcc.org`
  - `api-evsrest.nci.nih.gov`
  - `rest.genenames.org`
  - `clinicaltrials.gov`

## Setup

Windows (PowerShell), from the project folder:

```powershell
# From this package directory, with a supported Python on PATH:
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
```

macOS or Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Fill in `.env`:

- The endpoints, and the deployment name (not the model name).
- A key, or leave the key empty to sign in with Microsoft Entra ID (for example `az login`).

`.env` is read from the folder the command runs in. Run every command from the project folder.

## Where things live

| Path | Purpose |
|---|---|
| `app/services/ctml/` | CLI, deterministic document lane, registries, MAF agents and the v5 supervisor workflow, checks, compiler and QA; see the [layer and script walkthrough](docs/ARCHITECTURE.md). |
| `app/api/routes/ctml.py`, `app/main.py` | Optional local FastAPI upload/status/check interface. |
| `app/tests/ctml/`, `evaluation/tests/` | Synthetic offline tests; they need no Azure account or client gold. |
| `evaluation/compare.py` | Post-extraction gold comparison and optional, initially unreviewed patient-profile scoring. |
| `docs/RUNBOOK.md`, `docs/DECISIONS.md` | First live-run gates, output conventions and reviewer questions. |
| `pdfs/`, `runs/`, `CTMLs/` | Local PDF inputs, complete per-run audit folders, and manual copies of full-run CTMLs. PDFs, runs and exports are ignored; only `pdfs/.gitkeep` keeps the empty input directory in Git. |
| `.env`, `.cache/pmatch/`, `evaluation/gold/`, `evaluation/profiles/` | Local credentials/cache/client reference data; ignored by Git. `.env.example` is the safe settings template. |

## Run and review a protocol

Copy a protocol PDF into `pdfs/` (or pass its absolute path). Do not read gold
while extracting. Follow [the runbook](docs/RUNBOOK.md) in order and stop when
a step fails. From the package root, after configuring `.env`:

```powershell
# 1. Offline checks: no Azure or gold. The suite has 161 tests.
python -m pytest -q
ruff check app evaluation

# 2. Document lane: a DI call on first use, no model calls.
python -m app.services.ctml parse pdfs\Prot_NCT02972034.pdf
# Open runs\<parse-folder>\document\inventory.md and check it against the PDF:
# criterion numbers, wording, pages, scope, Issues and Group titles.
# A missing/merged/truncated criterion is a stop, even if parse printed parse_only.

# 3. Full model-backed run, in a NEW folder. Record its printed run folder.
python -m app.services.ctml run pdfs\Prot_NCT02972034.pdf
# Inspect runs\<full-folder>\qa_report.md, evidence.json and the CTML.
# needs_review is expected; blocked issues are not an approval.

# 4. Only after a verified full run, check its CTML's schema and output policy.
python -m app.services.ctml check runs\<full-folder>\NCT02972034_CTML.json

# Optional: resume an interrupted run of the SAME PDF and code/instructions.
python -m app.services.ctml run pdfs\Prot_NCT02972034.pdf --run-dir runs\<folder>

# Optional: exercise a few criteria first (NOT a full result or valid full-gold comparison).
python -m app.services.ctml run pdfs\Prot_NCT02972034.pdf --criteria INC-1,EXC-2

# Optional: the compact agent layout, in its own NEW run folder, for a comparison.
$env:PMATCH_AGENT_LAYOUT = "compact"
python -m app.services.ctml run pdfs\Prot_NCT02972034.pdf --run-dir runs\NCT02972034-compact
Remove-Item Env:PMATCH_AGENT_LAYOUT
```

`parse` writes its own run folder; a subsequent `run` also gets a new folder.
DI may be reused from `.cache/pmatch/di`, but new agent calls are made unless
you explicitly resume the same run. If you changed prompts, checks or policies,
use a **new** run folder: resume checks PDF/page range and criterion text, not
every instruction revision. `batch pdfs` processes each PDF in turn; inspect
each resulting run independently.

The output policy includes the key order of `docs/DECISIONS.md` (C1). The gold files are CTIMS exports with another key order. `check` therefore reports key-order errors for them. Key order does not change meaning.

More options:

- `--di-json FILE` uses a saved layout result instead of calling Azure. Accepted forms:
  - the raw analyze result;
  - a REST response with `analyzeResult`;
  - a file with a `layout` key.
- `--pages 189-376` uses only those pages. Use it when one PDF holds several protocol versions.
  - Without it, the pipeline uses the last version that has eligibility criteria.
  - It reports that choice in the QA report.
- `--refresh-di` ignores the Document Intelligence cache (`.cache/pmatch/di`).
- `--no-registry-cache` calls the registries live only. Registry answers are otherwise cached for 7 days.
- `batch <folder>` runs every PDF in a folder.

HTTP API (optional):

1. Start it with `uvicorn app.main:app`.
2. Then use:
   - `POST /ctml/runs` with a form field `pdf` starts a run in the background;
   - `GET /ctml/runs/{run}` returns its status;
   - `POST /ctml/check` checks a CTML document.

This is a local development interface: the routes have no authentication and
background tasks run inside the API process (no durable queue). Do not expose
it publicly or call it a hosted Foundry agent deployment.

## Outputs

Each run has its own folder under `runs/`:

| File | Content |
|---|---|
| `<trial>_CTML.json` | The CTML candidate. It is missing when the study design agent failed. |
| `qa_report.md`, `qa_report.json` | Status, blocking issues, review items, counts (including implied conditions removed), metadata sources, the criteria table and registry labels that add words; in the v5 layout, the repairs and the resolver findings |
| `evidence.json` | Per criterion: text, lines, pages, encoding, leaves with source words and lookup IDs, unmapped items, where its text went, confirmations. Per arm: the implied conditions the compiler removed. In the v5 layout, per criterion (`workflow`): the structure, every eligibility logic run, the coverage listing, omissions left, slot values and the slots kept as text; for the run (`workflow`): the step timeline, the repairs and the resolver findings. |
| `lookups.jsonl` | Every registry lookup: query, candidates, source (`live`, `cache` or `error`) |
| `tool_calls.jsonl` | Every tool call of every agent, with arguments and results |
| `agents/` | The output of each agent run. Resume reads it. In the v5 layout: `metadata.json`, `design.json`, and one folder per agent kind (`coverage/`, `eligibility/`, `clinical/`, `genomics/`, `prior_therapy/`) with one file per criterion; a repeated run of a criterion is saved as `<ID>.attempt2.json`. `resolver.json` holds the resolver's findings and `resolver_view.md` the candidate it audited. |
| `document/` | Lines, tables, routing, glossary and `inventory.md` (the criteria found by code) |
| `registry/oncotree_tumor_types.json` | The OncoTree tree used in this run |
| `manifest.json`, `run.log` | Settings (no secrets), PDF checksum, live or cached Document Intelligence, counts and timings |

## Compare with gold

Only run this **after** extraction; gold never enters `app/` or the agent
context. Supply a local gold JSON under ignored `evaluation/gold/`, or use its
absolute path with `--gold`:

```powershell
python -m evaluation.compare `
  --ctml runs\<folder>\NCT02972034_CTML.json `
  --gold evaluation\gold\NCT02972034_GoldStandard_2026-09-01.json `
  --out runs\<folder>\comparison.md
```

Add `--profiles evaluation\profiles\NCT02972034.json` only when that local
file is available; profile expectations are not gates until reviewed. The
evaluator produces both `.md` and `.json`. Without profiles it performs no
registry calls. The package does not automatically copy outputs into
`CTMLs/`; that local, ignored directory is for manually selected **full**
candidates. A partial run can also produce a CTML, so check `partial_run` and
accepted/total counts in QA before copying or comparing it as a full run.

The comparison reports:

- metadata fields;
- arm pairs, with leaf overlap and doses;
- additional-criteria overlap and duplicates;
- patient profiles, scored on both the generated CTML and the gold file.

Leaf overlap is a representation metric, not clinical accuracy. The profiles are starting profiles: they gate nothing until a reviewer confirms them (see [evaluation/README.md](evaluation/README.md)). The gold files are used only here, never by the pipeline.

## What to send back after a live run

Zip the run folder, without the PDF, and add the console output. The files that matter most:

- `qa_report.md`
- `<trial>_CTML.json`
- `evidence.json`
- `tool_calls.jsonl`
- `lookups.jsonl`
- `run.log`
- `manifest.json`
- `document/inventory.md`
- `comparison.md`

`tool_calls.jsonl` and `evidence.json` hold protocol text. No file holds keys or tokens.

## Configuration (`.env`)

| Variable | Meaning |
|---|---|
| `DOCUMENTINTELLIGENCE_ENDPOINT`, `DOCUMENTINTELLIGENCE_KEY` | Document Intelligence endpoint; an empty key means Entra ID |
| `DI_FEATURES` | Optional analysis features, for example `ocrHighResolution` (changes the cache key) |
| `FOUNDRY_OPENAI_ENDPOINT` | `https://<resource>.services.ai.azure.com/openai/v1/`. A resource URL or a Foundry project URL also works: the v1 URL is derived from it. |
| `FOUNDRY_MODEL_DEPLOYMENT` | Deployment name |
| `FOUNDRY_API_KEY` | An empty key means Entra ID |
| `FOUNDRY_REASONING_EFFORT` | Optional (for example `low`, `medium`, `high`) |
| `PMATCH_AGENT_LAYOUT` | `v5` (default) or `compact` (see [Agent layouts](#agent-layouts)) |
| `PMATCH_MAX_REPAIR_ROUNDS` | v5: repair rounds after the coverage comparison, and whether resolver findings are repaired (default 1; 0 turns repairs off) |
| `PMATCH_RESOLVER` | v5: `0` turns the resolver off (default on) |
| `PMATCH_MAX_PARALLEL_AGENTS` | Agents run at once (default 4). Lower it on rate limits; raise it (for example to 8) when the deployment's quota allows, since the v5 layout runs more agents. |
| `PMATCH_AGENT_TIMEOUT_SECONDS` | Time limit per agent run (default 600) |
| `PMATCH_OMIT_CATEGORIES` | Criterion categories never written as trial-wide text (reviewer rule 09/25; optional sub-studies added 09/30) |
| `PMATCH_RUNS_DIR`, `PMATCH_CACHE_DIR`, `PMATCH_LOG_LEVEL` | Folders and log level |

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `Settings error: Missing settings: FOUNDRY_OPENAI_ENDPOINT` | `.env` is missing or you are not in the project folder. |
| 401 or 403 from the model | Wrong key, or Entra sign-in missing (`az login`). |
| `FOUNDRY_OPENAI_ENDPOINT must end with /openai/v1/` | Use the resource's OpenAI v1 URL, as in `.env.example`. |
| `DeploymentNotFound` or 404 from the model | `FOUNDRY_MODEL_DEPLOYMENT` must be the deployment name. |
| 429 rate limits, agents that time out | Lower `PMATCH_MAX_PARALLEL_AGENTS`. The loop already retries twice (after 20 and 60 seconds). |
| `di_page_count_mismatch` in the QA report | Document Intelligence read fewer pages than the PDF has. The free tier (F0) reads only two pages. |
| `registry_lookups_failed` or `oncotree_unavailable` | A registry could not be reached, for example because of a proxy. Set `HTTPS_PROXY`. Affected items stay as text. |
| `clinicaltrials_unavailable: ... HTTP 403` | The ClinicalTrials.gov firewall refused the request. The code already retries once with the Python standard-library client. If it still fails, metadata comes from the protocol only and a redacted dose stays "dose not disclosed". |
| A criterion is missing or merged in `inventory.md` | This is a document-lane problem, not an agent problem. Send `document/lines.json` and `inventory.md`. |
| `No module named ...` | Run `python -m pip install -r requirements.txt` inside the activated `.venv`. |

## Known limits

- Local live runs have produced review candidates. Prompts and checks still need protocol-by-protocol reviewer assessment; a passing submit is not clinical validation.
- The v5 layout ran live on three protocols on 10/05; the 10/06 fixes have not run live yet. Splitting the work among more agents is not proven to give better CTML: measure both layouts on the same protocols before choosing.
- The v5 workflow runs in-process. It is not hosted in Microsoft Foundry, and the service has no approve, return or hold step: a person reviews the QA report.
- Checks verify form and source support. For example, every registry value must come from a lookup, every dose number must be in the cited lines, and every leaf must cite its criterion. The checks cannot prove clinical meaning. `test_tree` catches trees that contradict the agent's own example patients, not a wrong reading.
- Glossaries from scanned tables are partial: rows that look shifted are skipped.
- Stage and TNM, ECOG, and MSI/MMR stay as text (UHN catalog and reviewer rules).
- The patient profiles in `evaluation/profiles` are unreviewed starting profiles.
