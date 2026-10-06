# Task for GitHub Copilot: apply the v5 fixes (v3), re-run four protocols, and fix problems on your own

Work in `C:\mamad\Work-proj\UHN\UHN-AI-Usecase\Pmatch\pmatch-ctml`, in PowerShell, with the project's virtual environment active. Read `.github/copilot-instructions.md` first.

The v2 patch (v5 agent layout) is already applied in this folder. The v3 patch goes on top of it.

## How to work

- Do the steps in order without waiting for confirmation.
- When a command fails, do not stop. Find the cause, fix it under the rules of step 10, write the fix in the report, and continue from the failed step.
- Ask the user only for what only the user can give: a key or other Azure setting, the gold folder, or a decision the rules below do not allow you to make.

## Rules that always apply

- Keys stay in `.env`. Never open, print, log or paste `.env` or any key. To check settings, compare variable names only: `Select-String -Path .env -Pattern '^[A-Z_]+=' | ForEach-Object { ($_.Line -split '=')[0] }` against `.env.example`.
- No term lists, synonym tables or mappings for the agents.
- No NCT numbers, criterion IDs or protocol wording in `app/services/`. Every fix must work for any protocol.
- Never copy anything from the gold files into `app/`, and never give gold to an agent.
- Never edit a generated CTML, `evidence.json` or QA report by hand.
- Do not weaken a check, a test or an agent instruction to make an error go away.
- Keep every run folder, including the 10/05 runs. The status stays `needs_review`.
- After any code change, these must pass: `ruff format app evaluation`, `ruff check app evaluation`, `python -m pytest -q`.
- Do not commit or push.

## 1. Prepare and back up

```powershell
cd C:\mamad\Work-proj\UHN\UHN-AI-Usecase\Pmatch\pmatch-ctml
.\.venv\Scripts\Activate.ps1
robocopy . ..\pmatch-ctml-before-v3 /E /XD .venv runs .cache pdfs __pycache__ .pytest_cache .ruff_cache /XF .env /NFL /NDL /NJH /NJS
```

robocopy exit codes below 8 mean success.

## 2. Apply the v3 patch

Copy `pmatch-ctml-v5-fixes-v3.patch` into the project folder.

```powershell
$prefix = git rev-parse --show-prefix 2>$null
git apply --ignore-whitespace --check -v "--directory=$prefix" pmatch-ctml-v5-fixes-v3.patch
```

If it shows 15 `Checking patch` lines and no error, apply:

```powershell
git apply --ignore-whitespace -v "--directory=$prefix" pmatch-ctml-v5-fixes-v3.patch
```

If the dry run fails on some files (for example because you changed code during the 10/05 runs), apply what applies and merge the rest by hand, keeping your earlier changes:

```powershell
git apply --ignore-whitespace --reject -v "--directory=$prefix" pmatch-ctml-v5-fixes-v3.patch
Get-ChildItem -Recurse -Filter *.rej | Select-Object FullName
```

For each `.rej` file, put its hunks into the matching file by hand: add the hunk's `+` lines, remove its `-` lines, and keep the local edits around them. Then delete the `.rej` file. List every merged file in the report.

Then:

```powershell
ruff format app evaluation
ruff check app evaluation
python -m pytest -q
```

Expected: `All checks passed!` and `161 passed`.

Notes:

- A `Skipped patch` line means the command ran without `--directory=$prefix` or in another folder: run it again from the `pmatch-ctml` folder as written.
- Apply the patch only once. If a second apply reports `patch does not apply` on files that already hold the new code (for example `app\services\ctml\merge.py` contains `def fill_values`), the patch is in place: continue with the checks.

## 3. What the v3 patch changes (for the report, not for editing)

Fixes after the 10/05 runs:

- Alternatives (convention C26, revised): when a criterion offers alternatives (diseases, cohorts, prior therapies) and some cannot be encoded, the others are encoded and the rest stays as text with a review item `or_alternative_not_encoded`. Before, the whole list went to text, which left NCT02783300 Parts 2 and 3 without any disease rule. An excluded combination ("X with Y") is still never split.
- A slot can carry several registry values when no single entry covers its words (a cancer of several sites, an inhibitor of two targets).
- One slot per disease phrase ("colon or rectum" is one slot); the clinical agent prefers the one node that covers it. Lists of cohorts are OR branches.
- Clinical slots are for cancer diagnoses only; other conditions stay as text.
- Dose levels: a dose escalation arm gives its starting dose only; dose reductions and sub-study doses are not dose levels. New design check `starting_dose_with_other_levels`.
- Resolver: it reads the rules in words (ALL of, ANY of, NOT), sees each arm's combined rule, and knows the UHN conventions. A repair request the agents check and do not adopt is listed as `resolver_finding_not_adopted`.
- QA: `arm_without_diagnosis` (review item) and `arm_contradiction` (blocking: an arm that requires a diagnosis and excludes a diagnosis covering it).
- Quotes may run across the cells of a table row.

## 4. Full runs with the v5 layout

Use these new folder names. If one exists already, add `-2` to that name here and in steps 6 and 7.

Each run takes roughly 5–15 minutes. Wait until the console prints `Status:` before the next command. If the terminal stops waiting, check that the run folder's `manifest.json` has `finished_at`. Never stop a run before it ends: an unfinished folder has no CTML and no QA report.

```powershell
python -m app.services.ctml run pdfs\Prot_NCT02972034.pdf --run-dir runs\NCT02972034-v5b
python -m app.services.ctml run pdfs\Prot_NCT02783300.pdf --run-dir runs\NCT02783300-v5b
python -m app.services.ctml run pdfs\Prot_NCT04590963.pdf --pages 1-190 --run-dir runs\NCT04590963-v5b
python -m app.services.ctml run pdfs\Prot_NCT05059262.pdf --run-dir runs\NCT05059262-v5b
```

- After the first run ends, check that its `manifest.json` has `"agent_layout": "v5"` and that `qa_report.md` ends with the "Supervisor workflow (v5)" section, then continue.
- When the deployment's quota is high, `$env:PMATCH_MAX_PARALLEL_AGENTS = "8"` before the runs makes them faster.
- `--pages 1-190` selects protocol version 2.0 of NCT04590963, as used by the reviewer and the gold.

## 5. One comparison run with the compact layout

```powershell
$env:PMATCH_AGENT_LAYOUT = "compact"
python -m app.services.ctml run pdfs\Prot_NCT02972034.pdf --run-dir runs\NCT02972034-compact
Remove-Item Env:PMATCH_AGENT_LAYOUT
```

Check that this folder's `manifest.json` has `"agent_layout": "compact"`.

## 6. Compare with gold

Set `$gold` to the folder that holds the gold JSON files (the folder zipped as `gold.zip`, or `evaluation\gold`). Never copy these files into `app/`. If a CTML file has another name, use the `*_CTML.json` file of the run folder.

```powershell
$gold = "<folder with the gold JSON files>"
python -m evaluation.compare --ctml runs\NCT02972034-v5b\NCT02972034_CTML.json --gold $gold\NCT02972034_GoldStandard_2026-09-01.json --out runs\NCT02972034-v5b\comparison.md
python -m evaluation.compare --ctml runs\NCT02783300-v5b\NCT02783300_CTML.json --gold $gold\NCT02783300_GoldStandard_2026-09-24.json --out runs\NCT02783300-v5b\comparison.md
python -m evaluation.compare --ctml runs\NCT04590963-v5b\NCT04590963_CTML.json --gold $gold\NCT04590963_GoldStandard_2026-09-01.json --out runs\NCT04590963-v5b\comparison.md
python -m evaluation.compare --ctml runs\NCT05059262-v5b\NCT05059262_CTML.json --gold $gold\NCT05059262_GoldStandard_2026-09-01.json --out runs\NCT05059262-v5b\comparison.md
python -m evaluation.compare --ctml runs\NCT02972034-compact\NCT02972034_CTML.json --gold $gold\NCT02972034_GoldStandard_2026-09-01.json --out runs\NCT02972034-compact\comparison.md
python -m evaluation.compare --ctml runs\NCT02972034-v5\NCT02972034_CTML.json --gold $gold\NCT02972034_GoldStandard_2026-09-01.json --out runs\NCT02972034-v5\comparison.md
python -m evaluation.compare --ctml runs\NCT02783300-v5\NCT02783300_CTML.json --gold $gold\NCT02783300_GoldStandard_2026-09-24.json --out runs\NCT02783300-v5\comparison.md
```

The last two compare the 10/05 runs, for the before-and-after table. When `evaluation\profiles\<NCT>.json` exists for a trial, add `--profiles evaluation\profiles\<NCT>.json` to its commands. The profiles are unreviewed: report their results, but they decide nothing.

## 7. What to look at

Report what you see. These are expectations to check, not targets: never change code or instructions to force them.

**Every run:**

- Blocking issues, with their codes.
- Review items counted by code:

  ```powershell
  (Get-Content runs\<folder>\qa_report.json -Raw | ConvertFrom-Json).review_items | ForEach-Object { ($_ -split ':')[0] } | Group-Object | Sort-Object Count -Descending | Format-Table Count, Name
  ```

- Every `or_alternative_not_encoded`, `arm_without_diagnosis`, `arm_contradiction`, `slot_not_encoded`, `coverage_omission`, `agent_not_accepted`, `resolver_repaired` and `resolver_finding_not_adopted` item, in full.
- The "Supervisor workflow (v5)" section of `qa_report.md`: total seconds, the repairs table and the resolver findings table.
- `clinicaltrials_unavailable`, `registry_lookups_failed`, every `registry_dose` item, and `implied_conditions_removed` in Counts.
- Each arm's `dose_level` entries, copied from the CTML.

**NCT02783300:**

- Part 2 rule: the cohort diagnoses (breast cancer with its receptor status, urothelial carcinoma, glioblastoma, adenoid cystic carcinoma, non-Hodgkin lymphoma without the excluded subtypes, NSCLC with TP53 wild-type) and an `or_alternative_not_encoded` item for the HPV-positive tumor alternative.
- Part 3 rule: NSCLC, urothelial carcinoma, HNSCC or melanoma with prior PD-1 or PD-L1 therapy, or cervical squamous cell carcinoma.
- No `arm_without_diagnosis` item.
- Dose levels: Part 1 gives only the starting dose. For Part 2, the current protocol text gives 300 mg QD to new participants after Amendment 4, while the gold says 400 mg: report which one the run gives.

**NCT02972034:**

- Part 2 diagnosis: one node for "colon or rectum" (for example Colorectal Adenocarcinoma) and Appendiceal Adenocarcinoma (the protocol includes appendiceal cancer).
- Part 2 prior therapy: fluoropyrimidine and irinotecan together, or oxaliplatin, or an anti-VEGF class, or an anti-EGFR class. Fluoropyrimidine in `agent_class`.
- Part 2 exclusions include the OX-40 and CD-137 classes.
- Part 1 arms give only the starting dose of MK-8353.
- Part 1 and Part 2 criteria appear only in their own arms.

**NCT04590963:**

- Version 2.0 metadata.
- The monalizumab level: a dose with an `R` line and a `registry_dose` item, or "dose not disclosed".
- No `!Cetuximab`. No leaf with only "Medical Therapy". A platinum class leaf. Prior PD-1 or PD-L1 therapy.
- No `arm_contradiction` item.
- "Body weight > 30 kg" is in the trial texts.

**NCT05059262:**

- One arm.
- The CSF1 and CSF1R classes in `agent_class`.
- `nct_purpose` of at most two sentences.

**Before and after (NCT02972034 and NCT02783300):** from the `comparison.md` files, give the 10/05 run (`-v5`) and the new run (`-v5b`) side by side: per arm the leaf precision and recall, and the trial and arm additional-criteria counts.

**v5 against compact (NCT02972034):** give both runs' numbers side by side: criteria accepted, full / partial / none, trial additional criteria, arm additional criteria, blocked, minutes, agent runs, tool calls. Then list the criteria whose encoding differs between the two runs:

```powershell
python -c "import json,sys; L=lambda p: {c['criterion_id']: c for c in json.load(open(p, encoding='utf-8'))['criteria']}; a, b = L(sys.argv[1]), L(sys.argv[2]); [print(k, '| v5:', a[k].get('representable'), json.dumps(a[k].get('compiled')), '| compact:', b.get(k, {}).get('representable'), json.dumps(b.get(k, {}).get('compiled'))) for k in a if (a[k].get('compiled'), a[k].get('representable')) != (b.get(k, {}).get('compiled'), b.get(k, {}).get('representable'))]" runs\NCT02972034-v5b\evidence.json runs\NCT02972034-compact\evidence.json
```

For each listed criterion, add the criterion text from `evidence.json`. Do not judge which encoding is clinically right; mark the ones that need a reviewer.

## 8. Agents that end without an accepted output

This is a finding, not a bug. Run the same command once more with the same `--run-dir`: only the failed agents run again. If the agent fails again, do not edit the check or the instruction. Report:

- the criterion ID and the agent (`eligibility`, `clinical`, `genomics`, `prior_therapy`, `coverage`, `resolver`, `metadata` or `design`);
- the `last_errors` entry in `agents\<agent>\<ID>.json` (the highest `attemptN` file when there are several);
- the agent's last `submit_*` call in `tool_calls.jsonl` (its `owner` is `<ID>/<agent>`).

## 9. What to send back

1. One zip per new run folder, without the PDF.
2. The console output of every command, including errors.
3. A report `runs\v5b-validation-report.md` with:
   - this table, one row per run:

     | Protocol | Layout | Run folder | Minutes | Criteria accepted / total | Full / partial / none | Blocked (yes/no) | Blocking issues | Trial additional criteria | Resolver findings (repair / review) | Agent runs | Profiles pass (generated / gold, or none) |
     |---|---|---|---|---|---|---|---|---|---|---|---|

   - the notes of step 7, the before-and-after table and the v5 against compact comparison;
   - a "Fixes made" section: for each fix, the error line, the cause, the file, the diff, the test that covers it, and the runs that used the fixed code. Write "None" when there were none.

## 10. When something fails

| Problem | What to do |
|---|---|
| The dry run of step 2 fails on some files | Apply with `--reject` and merge the `.rej` hunks by hand (step 2). |
| `ruff check` fails | Fix the reported lines. `ruff format app evaluation` fixes formatting and line endings. |
| A test fails | Read the traceback and fix the cause in the code. Change a test only when the test itself is wrong, and explain why in the report. |
| `No module named ...`, or an import error from `agent_framework` | `python -m pip install -r requirements.txt`. `python -m pip show agent-framework-core` must show 1.15.0. |
| `Settings error`, or 401, 403, 404 or `DeploymentNotFound` from the model | Compare the variable names in `.env` with `.env.example` (names only, never values). Ask the user for a missing or wrong value. |
| 429 (rate limit) | `$env:PMATCH_MAX_PARALLEL_AGENTS = "2"`, then rerun the same command with the same `--run-dir`. |
| `timeout after ...s` in `run.log` | `$env:PMATCH_AGENT_TIMEOUT_SECONDS = "1800"`, then rerun with the same `--run-dir`. |
| A registry cannot be reached (`registry_lookups_failed`, `oncotree_unavailable`, connection errors) | Check `HTTPS_PROXY` and the network, then rerun with the same `--run-dir`. `clinicaltrials_unavailable` alone is not a failure. |
| A Python traceback stops a run, or `The supervisor workflow ended without an outcome` | A bug. Find the cause in `run.log`, make the smallest fix that works for any protocol, add a test that reproduces it when you can, run the checks of "Rules that always apply", then rerun with the same `--run-dir`. |
| A Windows path or encoding error | Fix it in the code in a way that works on both Windows and Linux. Add a test when you can. |
| An agent ends without an accepted output | Step 8. |
| A CTML differs from gold, a profile fails, or an expectation of step 7 is not met | A finding. Report it; do not change code for it. |
| `evaluation.compare` cannot find a file | Use the run folder's `*_CTML.json` and the gold file whose name starts with the NCT number. |

Rerunning with the same `--run-dir` resumes: accepted agent outputs are reused and only missing or failed parts run again. If a fix changed `app\services\ctml\agents\instructions.py` or a file in `app\services\ctml\checks\`, use a new run folder (`-2` suffix) instead, and say so in the report.
