# Instructions for GitHub Copilot

This project turns a clinical trial protocol PDF into a CTML review candidate for UHN PMATCH. Read `README.md`, `docs/RUNBOOK.md` and `docs/DECISIONS.md` before you change anything.

## The task

Run the steps in `docs/RUNBOOK.md` in order. Stop at the first step that fails. Report in the format of the runbook's last section. Do not change code unless a step fails.

## Hard rules

- Keys and tokens go only in `.env`. Never write them in another file, a log, a report or a message, and never print them.
- Never add term lists, synonym tables, dictionaries or mappings for the agents. Terms come only from the protocol, live registry lookups and the CTML schema (rule 1 in `docs/DECISIONS.md`).
- Never copy or paraphrase anything from `evaluation/gold/` into `app/`. Only `evaluation/compare.py` reads the gold files.
- Never write a fix for one protocol. The service code (`app/services/`) must not contain NCT numbers, criterion IDs or protocol wording. A fix must work for any protocol.
- Never edit a generated CTML, `evidence.json` or QA report by hand.
- The status is always `needs_review`. Never mark an output approved.
- Keep every run folder. Never delete or overwrite `runs/<folder>`.
- After any code change, `python -m pytest -q` and `ruff check app evaluation` must pass. List each change, with its reason, in the report.

## When something fails

- Report the command, the exact error line and the run folder.
- A check that rejects an agent's output is doing its job. Report it. Do not weaken a check or an agent instruction to make an error go away.
- For model errors (401, 403, 404, 429), use the Troubleshooting table in `README.md`.
- A missing, merged or mis-scoped criterion in `document/inventory.md` is a document-lane problem. Report it with the criterion ID and page before any agent run.

## Where things are

| Folder or file | Content |
|---|---|
| `app/services/ctml/document/` | Document Intelligence parsing, lines, sections, glossary, criteria inventory |
| `app/services/ctml/registries/` | OncoTree, NCIt, HGNC and ClinicalTrials.gov lookups |
| `app/services/ctml/workflow.py`, `merge.py` | The v5 supervisor workflow, and the merge of slot values into the structure |
| `app/services/ctml/agents/` | Agent instructions, the submit loop and the agents of both layouts |
| `app/services/ctml/checks/` | The checks run by each submit tool |
| `app/services/ctml/compiler.py`, `qa.py` | CTML assembly and the QA report |
| `evaluation/` | Comparison with gold and patient profiles |
