# Runbook: first live runs

Follow these steps in order. They work by hand or with GitHub Copilot. Stop at the first step that fails and report it (see the last section).

## Rules while running

- **Do not change code unless a step fails.** If you change code, run the tests again, keep them passing, and list every change in the report.
- Never paste keys or tokens into files, logs or messages. Keys go only in `.env`.
- Never edit a generated CTML by hand, and never copy gold values into `app/`.
- Keep every run folder. A new run gets a new folder.
- Do not add term lists, synonym files or mappings for the agents.

## 1. Install

Unzip `pmatch-ctml.zip` into `C:\mamad\Work-proj\UHN\UHN-AI-Usecase\Pmatch`. This creates the folder `pmatch-ctml`. It does not replace the repository or `pmatch_loop`. Then run:

```powershell
cd C:\mamad\Work-proj\UHN\UHN-AI-Usecase\Pmatch\pmatch-ctml
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pytest -q
ruff check app evaluation
```

Expected: `161 passed`, and `All checks passed!`.

Any Python from 3.10 to 3.14 works. `py -0` lists the versions installed on the computer. If 3.13 is not installed, use another version in the `venv` command, for example `py -3.12`.

## 2. Settings

```powershell
Copy-Item .env.example .env
notepad .env
```

- Fill in the Document Intelligence endpoint and key (or leave the key empty after `az login`).
- Fill in the Foundry OpenAI v1 endpoint, the deployment name and the key.
- Test the model settings with one criterion in step 5 before a full run.

## 3. Protocols

Copy the PDFs into `pdfs\`:

- `Prot_NCT02972034.pdf`
- `Prot_NCT02783300.pdf`
- `Prot_NCT04590963.pdf`
- `Prot_NCT05059262.pdf`

## 4. Document lane only

```powershell
python -m app.services.ctml parse pdfs\Prot_NCT02972034.pdf
python -m app.services.ctml parse pdfs\Prot_NCT02783300.pdf
python -m app.services.ctml parse pdfs\Prot_NCT04590963.pdf
```

Each command makes one Document Intelligence call. Later runs reuse the cache. For each run folder, open `document\inventory.md` and compare it with the protocol:

- **NCT02972034:** 13 inclusion and 26 exclusion criteria. INC-1 and EXC-2 are Part 1; INC-2, EXC-3 and EXC-26 are Part 2.
- **NCT02783300:** 13 inclusion and 13 exclusion criteria.
  - INC-5 is split into INC-5.1 (Part 1), INC-5.2 (Part 2) and INC-5.3 (Part 3).
  - INC-10 and EXC-4 are Part 3.
- **NCT04590963:** the PDF holds two protocol versions: 2.0 on pages 1-190 and 3.0 on pages 191-376. `manifest.json` lists the segments and the page range used. The reviewer and the gold use version 2.0, so add `--pages 1-190` to every NCT04590963 command until UHN answers Q3 (`docs/DECISIONS.md`).
- **NCT05059262:** 12 inclusion and 13 exclusion criteria, no population labels.

In each `inventory.md`, check two more things:

- **Issues:** `criterion_text_unfinished:<ID>` means a criterion's text stops mid-sentence.
- **Group titles:** every line listed there must be a real title, such as "Medical Conditions". A line that continues a criterion's sentence is a capture error.

Report any missing, merged, truncated or mis-scoped criterion before going on. Include its criterion ID, what is wrong, and the page.

## 5. A small live test

```powershell
python -m app.services.ctml run pdfs\Prot_NCT02972034.pdf --criteria INC-1,EXC-2,EXC-25
```

With the default v5 layout this runs the metadata, study design, coverage, eligibility logic, domain and resolver agents for three criteria. Check the following:

- The console ends with the run folder, the CTML path and `Status: needs_review`.
- `tool_calls.jsonl` shows the agents calling tools and `submit_*`, with `"accepted": true` at the end. The `owner` field names the criterion and the agent, for example `EXC-2/eligibility` or `EXC-2/prior_therapy`.
- `qa_report.md` ends with a "Supervisor workflow (v5)" section: the repairs and the resolver findings.
- `qa_report.md` has a `partial_run` item. It is expected, because only three criteria ran.

If the model call fails, stop and report the error line from `run.log`. Do not paste keys.

## 6. Full runs

```powershell
python -m app.services.ctml run pdfs\Prot_NCT02972034.pdf
python -m app.services.ctml run pdfs\Prot_NCT02783300.pdf
python -m app.services.ctml run pdfs\Prot_NCT04590963.pdf --pages 1-190
python -m app.services.ctml run pdfs\Prot_NCT05059262.pdf
```

- The v5 layout makes about 3 to 5 agent runs per criterion. One run takes roughly 15–45 minutes with 4 agents in parallel; set `PMATCH_MAX_PARALLEL_AGENTS=8` in `.env` when the deployment's quota allows.
- A run that stops can be resumed with `--run-dir runs\<folder>`. Accepted agent outputs are reused.
- On many 429 errors, set `PMATCH_MAX_PARALLEL_AGENTS=2` in `.env` and resume.
- ClinicalTrials.gov: if `qa_report.md` still lists `clinicaltrials_unavailable`, the site refused both HTTP clients. Report the line; do not work around it.

To compare with the compact layout, run it into its own folder (PowerShell):

```powershell
$env:PMATCH_AGENT_LAYOUT = "compact"
python -m app.services.ctml run pdfs\Prot_NCT02972034.pdf --run-dir runs\NCT02972034-compact
Remove-Item Env:PMATCH_AGENT_LAYOUT
```

## 7. Compare with gold

Replace `<folder>` with the run folder of each protocol:

```powershell
python -m evaluation.compare --ctml runs\<folder>\NCT02972034_CTML.json --gold evaluation\gold\NCT02972034_GoldStandard_2026-09-01.json --profiles evaluation\profiles\NCT02972034.json --out runs\<folder>\comparison.md
python -m evaluation.compare --ctml runs\<folder>\NCT02783300_CTML.json --gold evaluation\gold\NCT02783300_GoldStandard_2026-09-24.json --profiles evaluation\profiles\NCT02783300.json --out runs\<folder>\comparison.md
python -m evaluation.compare --ctml runs\<folder>\NCT04590963_CTML.json --gold evaluation\gold\NCT04590963_GoldStandard_2026-09-01.json --profiles evaluation\profiles\NCT04590963.json --out runs\<folder>\comparison.md
python -m evaluation.compare --ctml runs\<folder>\NCT05059262_CTML.json --gold evaluation\gold\NCT05059262_GoldStandard_2026-09-01.json --out runs\<folder>\comparison.md
```

The 09/29 gold exports of these four trials differ from the files above only in their `uuid` values; either can be used.

Arms are paired by their Part, Arm and Cohort labels first. A `split` row means one gold arm was encoded as several generated arms; `merged` means the reverse.

## 8. What to send back

1. One zip per run folder, without the PDF.
2. The console output of each command, including any error.
3. A short table:

| Protocol | Layout | Run folder | Minutes | Criteria accepted / total | Blocked (yes/no) | Blocking issues | Profiles pass (generated / gold) |
|---|---|---|---|---|---|---|---|

4. Any code change you made, as a diff, with the reason.
