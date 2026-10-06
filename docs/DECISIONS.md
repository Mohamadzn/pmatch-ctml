# Decisions, conventions and open questions

This page lists the output conventions the code follows, with their sources, and the questions for UHN.

Sources:

- the UHN CTML schema (CTIMS catalog, `app/services/ctml/schema/ctml.schema.json`);
- the September gold files (the 09/29 exports for NCT02783300, NCT02972034, NCT04590963 and NCT05059262);
- the reviewer notes of 09/12 to 09/29/2026.

Where a gold file and the protocol disagree, the code follows the protocol, and the case is listed as a question. It never copies a gold value.

## Rules the code follows

1. **No term lists for agents.** Terms come only from three sources:
   - the protocol, through the document tools;
   - live registry lookups (OncoTree, NCIt, HGNC), each recorded with a `lookup_id`;
   - the CTML schema, as typed fields.

   No term mapping is saved for later runs. The 7-day registry cache holds the registries' own answers, not choices.
   - The code holds a few fixed words that describe layout or language, not medicine. Examples: section-title words ("inclusion", "synopsis"), field-label nouns ("title", "number"), English function words and UMLS semantic-type names.
2. **Gold files only in `evaluation/`.** Nothing under `app/` reads them. A test enforces this.
3. **No credentials** in code, logs, manifests or reports. The manifest records only whether a key or Entra ID was used.
4. **Status is always `needs_review`.** A CTML can be "blocked" (defects to fix first), but it is never marked approved.
5. **Every value is traceable.**
   - Leaves carry the exact source words, line IDs and lookup IDs.
   - Metadata values carry line IDs.
   - Dose numbers must appear in the cited lines.

## Output conventions

| # | Convention | Source |
|---|---|---|
| C1 | Key order, empty shells (`drug_list`, `management_group_list`, `site_list`, `staff_list`), `principal_investigator` "", no `status`, `treatment_category` first. The key order is the output template's; the gold files are CTIMS exports with another order. | Catalog; output template; reviewer 09/12 |
| C2 | `trial_id` is the NCT number. The gold suffix "_GoldStandard" is not reproduced. | Catalog |
| C3 | `phase`: the lower phase in Roman numerals ("Phase 1b/2" gives "I") | Main-branch metadata agent; reviewer 09/15 |
| C4 | `protocol_version_date`: the printed date as midnight in Toronto, written in UTC | Gold files |
| C5 | `protocol_version_no` and `protocol_version_date` are left out when the protocol does not state them | Catalog (empty strings fail the schema) |
| C6 | `short_title`: the protocol's short title; else the ClinicalTrials.gov brief title (review item); else the long title | Gold files vary (see Q5) |
| C7 | `nct_purpose`: the protocol's purpose sentence (at most two sentences), naming the drugs, the participants' cancer and the objective; no eligibility details | Reviewer 09/15; gold files |
| C8 | `principal_investigator` is never taken from the registry | Reviewer; site-specific |
| C9 | One step. A randomized comparison with shared eligibility is one "Randomization Arm (A vs B): …" with placebo as a dose level. A study in parts has one arm per part. Within a part, separate arms only for a different regimen (other drugs, or another schedule such as continuous versus intermittent). Dose levels, formulations and disease cohorts of one regimen stay in one arm. An open-label extension or a crossover from placebo is not an arm. | Gold files (09/29: NCT02783300 three arms, NCT02972034 Arms A to D, NCT05059262 one arm); reviewer 09/26 |
| C10 | `arm_code`: part and arm labels, what distinguishes the arm, then the part's title with its drugs. `arm_description`: one or two sentences in the protocol's words: how the recommended dose is found (for example mTPI and RP2D), or what a randomized comparison compares and its ratio. No dose lists. | Reviewer 09/25 and 09/26 |
| C11 | Dose levels: one per drug; for dose escalation the starting dose only, "<dose> (starting dose) <route> <schedule>" (later levels, dose reductions and sub-study doses are not dose levels; check `starting_dose_with_other_levels`); several fixed doses assigned from the start together ("10/20/40 mg PO QD"); when an amendment changes the dose for new participants, the current dose; at most 160 characters. A dose the protocol redacts ("CCI") or does not state comes from the ClinicalTrials.gov record, cited as an `R` line, with a review item; without one, "dose not disclosed". | Gold files; reviewer 09/29 (NCT04590963 dose); 10/05 runs (escalation levels and dose reductions listed as levels) |
| C12 | A population-scoped criterion always appears in the text list of every arm it applies to | Reviewer 09/24 |
| C13 | Exclusion texts start with "Exclusion Criteria: " | Reviewer 09/24 |
| C14 | Trial-wide text leaves out consent, oral administration, laboratory values, contraception and pregnancy, investigator judgement and optional sub-studies. "Laboratory value" means a blood, urine or ECG measurement; body weight and similar measurements are not in it and stay as text. | Reviewer 09/25; reviewer 09/26 answer (see Q2) |
| C15 | A fully encoded trial-wide criterion gets no text | Gold files; reviewer 09/26 ("the transplant leaf covers the exclusion") |
| C16 | ECOG stays as text. There is no `ecog_expression` field. | Catalog; reviewer 09/25 |
| C17 | MSI and MMR stay as text; stage and TNM are not encoded | Catalog (inactive fields); reviewer rule |
| C18 | "Compounds targeting X" is the NCIt class for X. When one returned class is the parent of another, the parent is used. Each target is its own leaf: PD-L1 and PD-L2 are separate. Examples after "e.g." are not encoded. | Reviewer 09/26 ("we need to use NCI thesaurus") |
| C19 | A leaf with only `treatment_category` "Medical Therapy" is used only when the criterion requires any prior systemic therapy and names nothing more specific. Never for counts of prior lines, and never when part of the criterion stays as text (check `category_only_in_partial`). | Reviewer 09/26 and 09/29 |
| C20 | A prior allogeneic transplant exclusion (stem cell, tissue or solid organ) is `Stem Cell Transplant` / `!Allogeneic`, with no text | Reviewer 09/26 (see Q7) |
| C21 | Diagnoses use the most specific OncoTree node that covers the whole population the criterion names | Reviewer 09/26 |
| C22 | `_SOLID_` and `_LIQUID_` follow MatchMiner: liquid means OncoTree tissue Lymphoid or Myeloid | MatchMiner convention |
| C23 | Registry names are copied exactly from the lookup. Gold typos ("EFGR", "Anitneoplastic", "PD-1 Inhibitor" beside "PD1 Inhibitor") are not reproduced. | Project rule 6 |
| C24 | When one PDF holds several protocol versions, the last version with eligibility criteria is used, and the choice is reported | Our default (see Q3) |
| C25 | In a required prior-therapy list, "±", "with or without" and "if indicated" items are further OR alternatives, and a comma before "or" separates the top-level alternatives: "A and B, or C, ± D or E" is (A AND B) OR C OR D OR E | Reviewer 09/26 answer to Q1; NCT02972034 gold |
| C26 | Revised 10/06. In a list of alternatives (diseases, cohorts, prior therapies), the alternatives CTML can hold are encoded and an alternative it cannot hold stays as text, with a review item (`or_alternative_not_encoded`): patients who qualify only through it are not matched automatically. An excluded combination ("X with Y") is never split: when one part cannot be encoded, the exclusion stays as text. A waiver granted case by case ("eligible if the sponsor agrees") is not an alternative: the requirement is encoded and the waiver stays as text. | NCT02783300 gold (Part 2 and Part 3 cohort lists are encoded although one alternative, an HPV-positive tumor of any histology, cannot be); the 09/29 rule "encode none" left multi-cohort arms of the 10/05 runs without any disease rule; reviewer 09/29 (waivers) |
| C27 | Progression on, failure of or intolerance to a therapy means the patient received it: the therapy is encoded when every alternative requires it (for example "prior platinum failure" gives the NCIt platinum class); progression, timing and setting stay as text | NCT04590963 gold; logic |
| C28 | An exclusion with an exception that cannot be encoded ("unless …", "except …", "… is allowed") gets no negated leaf for the excepted item. A negated class is kept when the registry hierarchy keeps the allowed drugs out of it, and a case-by-case waiver does not count as an exception. | Reviewer 09/29 (`!Cetuximab`); logic |
| C29 | A condition implied by another condition of the same arm is removed by the compiler and listed in `evidence.json` | Reviewer 09/29 (redundant Medical Therapy); logic-preserving |
| C30 | NCIt kind: a drug-class concept stays a class when the NCI Drug Dictionary lists it; it goes in `agent_class` | NCI EVS properties; gold files (`CSF1R-targeting Agent` as `agent_class`) |
| C31 | A tissue-level OncoTree node ("Breast") needs a confirmation; the most specific node that covers the population is expected ("Invasive Breast Carcinoma") | NCT02783300 gold; C21 |
| C32 | Criteria of an optional sub-study (for example optional genetics research) are not eligibility: no tree and no text | Logic; protocols state they do not affect participation in the main study |

## Agent layout (10/05)

| # | Decision | Reason |
|---|---|---|
| A1 | The default agent layout follows architecture v5: a supervisor workflow (Agent Framework) with the metadata, study design, eligibility logic, clinical, genomics, prior therapy, coverage and resolver agents. The compact layout (one criterion agent per criterion, 09/29) stays behind `PMATCH_AGENT_LAYOUT=compact`. | Team decision to follow v5; the compact layout departed from it (`claude/AGENTIC_DESIGN_2026-09-29.md`). Neither layout is proven better: both are to be measured on the same protocols. |
| A2 | Polarity belongs to the eligibility logic agent. Domain agents give positive values; code writes the "!" form, the opposite age rule, `!Mutation` or a negated copy-number call. | One owner for NOT; a domain agent cannot flip a criterion's meaning. |
| A3 | A slot without a registry value is never guessed. In an AND the other conditions stay; in an OR the other alternatives stay (C26); an excluded combination goes entirely to text. | C26 (revised 10/06). |
| A4 | The coverage checker lists the criterion's conditions without seeing the encoding. Omissions get one targeted repair; an omission that remains goes to review and the structure is not changed. | v5: a finding that repeats goes to a person. Changing the structure automatically would add text for criteria the agent judged complete. |
| A5 | The resolver audits the compiled candidate once. Its findings must quote the protocol. Repair findings send at most 10 criteria back once; design and metadata findings go to review only. | Bounded repair without a full rerun (v5). |
| A6 | No gold file and no term list reaches any agent, in either layout. | Rules 1 and 2 above. |
| A7 | A slot can carry several registry values when no single entry covers its words (a cancer of several sites, an inhibitor of two targets): any of them satisfies the slot, and a negated slot excludes each of them. One disease named in several sites ("colon or rectum") is one slot, and the clinical agent prefers the node that covers it. | 10/05 runs: "mTCC" and "PD-1 or PD-L1 targeted therapy" could not be stated with one value, and "colon or rectum" was split into two narrower nodes. |
| A8 | The resolver reads every rule in words (ALL of, ANY of, NOT) with each arm's combined rule, and knows the UHN conventions. A finding that the agents check and do not adopt is listed as `resolver_finding_not_adopted`, not as a repair. | 10/05 runs: every resolver repair request contradicted a UHN convention (±, allogeneic transplant) or misread "NOT", and none noticed arms without a disease rule. |
| A9 | QA lists an arm whose rule requires no diagnosis (`arm_without_diagnosis`, review item, not blocking). | 10/05 runs: NCT02783300 Parts 2 and 3 had only an age rule. |
| A10 | QA blocks an arm whose rule requires a diagnosis and excludes a diagnosis that covers it (`arm_contradiction`): no patient can match. | 10/05 runs: negated "head and neck cancer of any other location" slots could map to a parent of the required diagnosis. |

## Differences from the gold files that are intended

Each needs clinical confirmation (see the questions). The code follows the protocol and the registries; it never copies a gold value.

- **Drug classes.** A class is encoded as `agent_class`, not `agent`. The NCT02972034 gold puts "Fluoropyrimidines" in `agent`; NCIt's class is "Fluoropyrimidine" (Q8).
- **Anti-VEGF.** NCT02972034 says "anti-vascular endothelial growth factor" (the ligand). The gold uses "Anti-VEGFR Monoclonal Antibody" (the receptor). The code takes the class that NCIt returns for the protocol's words (Q9).
- **Part 2 of NCT02783300.**
  - The gold places lymphoma negations as OR siblings of Non-Hodgkin Lymphoma, which admits almost every patient to Part 2 (see `evaluation/`).
  - The gold requires a prior anti-PD-1 or anti-PD-L1 antibody in the NSCLC branch. The protocol also admits patients "considered ineligible to receive therapy with these agents". Since 10/06 the therapy is encoded and that alternative stays as text, as in the gold (C26, Q11).
- **NCT05059262 exclusion of prior CSF1 or CSF1R therapy.** The gold puts the two negated classes in an OR, which admits a patient treated with one of them. Excluding any of several items is an AND of negations.
- **Gold typos** are not reproduced (C23): "Anti-EFGR", "Anitneoplastic", "CLTA-4", and "PD-1 Inhibitor" beside "PD1 Inhibitor" in the NCT02972034 gold.

## Questions for UHN

Answered:

1. **Q1.** "±" and "if indicated" items are alternatives (reviewer 09/26). Now C25.
2. **Q2.** Text requirements (reviewer 09/26): include a criterion when it can make an otherwise matched patient temporarily ineligible, can disqualify the patient or require stopping a medication, or names a condition that needs clinical review. Washout windows and interacting medications are therefore kept. Catch-all investigator-judgement criteria stay out (our reading).
3. **Q4.** One arm per part unless the regimen differs (the 09/29 gold files). Now C9.
4. **Q7.** Allogeneic transplant under `Stem Cell Transplant` / `!Allogeneic` without text (reviewer 09/26). Now C20.

Open:

1. **Q3.** Which version to use when a PDF holds several? NCT04590963 holds versions 2.0 and 3.0. The reviewer (09/29) expects 2.0, as in the gold. Is the rule "the version the UHN site approved"? That cannot be read from the PDF, so `--pages` chooses it (`--pages 1-190` for NCT04590963).
2. **Q5.** `short_title` when the protocol prints none: the ClinicalTrials.gov brief title (current) or the long title?
3. **Q6.** Registry labels that add words to the protocol's phrase (listed in each QA report): acceptable as they are?
   - Example: "adenocarcinoma originating from the colon or rectum" becomes "Colorectal Adenocarcinoma".
4. **Q8.** A drug class named by the protocol ("fluoropyrimidines"): `agent_class` with NCIt's class name (current), or `agent` as in the NCT02972034 gold?
5. **Q9.** "Anti-vascular endothelial growth factor": an anti-VEGF class (protocol words), or "Anti-VEGFR Monoclonal Antibody" (gold)?
6. **Q10.** "Able to swallow oral medication" is in the NCT02972034 and NCT05059262 gold texts, while the reviewer (09/25) leaves administration criteria out. Keep it out (current)?
7. **Q11.** A required prior therapy with an "or considered ineligible for it" alternative: the therapy leaf plus the alternative as text (current since 10/06, C26, as in the NCT02783300 gold), or text only?
