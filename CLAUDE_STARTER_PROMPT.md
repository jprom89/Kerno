# CLAUDE_STARTER_PROMPT.md — Ready-to-Paste Claude Session Opener

**Instructions:** Copy the block below and paste it as your opening message to Claude. Do not modify it before pasting.

---

```
You are building the Kerno Compliance Copilot core learning pipeline.

Before writing any code, complete these steps in order:

STEP 1 — Read CLAUDE.md completely. This is the codebase constitution. Acknowledge that you have read it by stating the three non-negotiable tenant isolation rules from Section 3.

STEP 2 — Read FILE_STRUCTURE.md. State where the following files live: set_tenant_context(), the nightly batch scheduler, and the cross-tenant isolation test.

STEP 3 — Read LEARNING_PIPELINE_SPEC.md. Answer the three pre-flight questions from CLAUDE.md Section 0:
  Q1: Where is the tenant isolation boundary enforced? Name the PostgreSQL mechanism and the application-layer function.
  Q2: What is the GDPR legal basis for cross-tenant model optimisation, and what must the anonymisation pipeline strip?
  Q3: Which function emits the audit log entry when a human overrides an AI recommendation? What fields must it contain?

Do not write any code until you have answered all three questions correctly. If you cannot answer a question from the documents, stop and ask me to clarify.

STEP 4 — Read PROMPT_doc8_learning_pipeline.md. Confirm the 12-file build order and state which file you will build first and why.

STEP 5 — Begin building File 1: config/constants.py. Follow the quality gate checklist at the end of PROMPT_doc8_learning_pipeline.md before marking it complete.

Proceed file by file. Do not move to the next file until the current file passes the quality gate. After every file, state: the file name, the quality gate result, and the next file you will build.
```

---

## What Claude Will Do

After receiving this prompt, Claude will:

1. Acknowledge all three rules from CLAUDE.md §3 (tenant isolation)
2. Locate `set_tenant_context()`, the scheduler, and the security test in the directory tree
3. Answer the three pre-flight questions correctly from the spec
4. Confirm the 12-file build order
5. Build `config/constants.py` first and check it against the quality gate
6. Continue file by file, self-checking at each step

If Claude skips any step or starts writing code before completing the pre-flight, respond: **"Stop. Complete the pre-flight questions before writing any code."**

---

## Signs Claude is Off-Track

Stop Claude immediately if you see any of the following:

- Spec variable names in code: `W_ret`, `alpha`, `gamma_i`, `V_err`, `V_target`, `V_source`
- A database query function without a prior call to `set_tenant_context()` or `resolve_and_set_tenant_context()`
- A function longer than 40 lines
- A constant written as a raw number (e.g., `0.85`) instead of a named constant from `config/constants.py`
- A docstring that just restates the function signature instead of explaining what it does
- Any test that only tests the happy path without a negative/error case

If you see any of these, paste this correction: **"This violates CLAUDE.md. Identify the rule you broke, fix it, and re-run the quality gate before continuing."**

