# Evaluation

This folder holds evaluation only. Nothing under `app/` imports it, and the pipeline never reads the gold files.

- `gold/` holds the 16 September gold CTML files. They are for comparison and for learning the output format. They are not the answers.
- `compare.py` compares a generated CTML with a gold file and scores patient profiles on both:

  ```powershell
  python -m evaluation.compare --ctml <generated.json> --gold <gold.json> --profiles profiles\<NCT>.json --out comparison.md
  ```

  `--no-registry` skips OncoTree and NCIt. The profile results are then not evaluable.
- `profiles/` holds starting patient profiles for NCT02783300, NCT02972034 and NCT04590963, taken from the v7 brief.

## Reading the comparison

- **Metadata:** each field compared with gold after normalization.
- **Arms:** generated arms paired one to one with gold arms.
  - Pairing uses the Part, Arm and Cohort labels of the arm codes first. Arms whose labels conflict ("Arm C" and "Arm D") are never paired. Then the name, the drugs and the leaves decide.
  - A leftover arm that shares labels with a paired arm is reported as `split` (one gold arm encoded as several generated arms) or `merged` (the reverse).
  - A gold arm without `arm_code` is shown as "(no arm_code)" with the start of its description.
  - For each row, the report gives leaf precision and recall, the leaves only one side has, and the doses.
- **Additional criteria:**
  - trial texts: how many gold texts have a close match (similarity 0.8 or more) in the generated list, and the reverse;
  - arm texts: the same, against the aligned arm only;
  - `duplicates`: repeats inside one list. The same text in several arms is counted apart (`repeated_across_arms`), because a criterion that applies to several arms belongs in each;
  - `gold_found_only_in_arms` and `gold_found_only_in_trial`: gold texts found only at the other level.
- **Leaf overlap is a representation metric.** One meaning can be encoded in several ways, and gold files have errors. Use the profiles to judge meaning.

## Patient profiles

A profile is a patient plus the expected result for each arm group:

```json
{"id": "P02", "description": "As P01, plus prior anti-PD-1",
 "patient": {"diagnosis": "Colon Adenocarcinoma",
             "prior_treatments": [{"treatment_category": "Medical Therapy", "agent": "Pembrolizumab",
                                   "agent_classes": ["Anti-PD1 Monoclonal Antibody", "PD1 Inhibitor"]}]},
 "expect": {"Part 1": "not_eligible", "Part 2": "not_eligible"}, "pending": false, "source": ""}
```

- **Arm groups.** `arm_groups` maps each expectation key to text the arm code must contain. An empty text means every arm.
- **Defaults.** The defaults apply to every patient: age 50, no known alterations, no prior therapy.
- **Drug classes.** Before scoring, each drug's NCIt ancestor classes are added to its `agent_classes`. This makes a class leaf match at any level of the NCIt hierarchy.
- **Diagnoses.** They must be OncoTree names. The hierarchy comes from the live OncoTree tree.

**Before a profile gates anything:**

1. A reviewer confirms each expected result against the protocol.
2. The reviewer writes the governing line (page and text) in `source`.
3. The reviewer sets `reviewed` to `true` for the file.

Profiles marked `pending` depend on an open question and never gate. The expected results are never taken from gold. The gold files fail several of these profiles.
