# PROMPT_doc13_decision_layer.md
# Document 13 — Decision Layer: Explainable Recommendation Engine
# Spec version: 1.0 | Status: Authoritative
# Covers: KER-105
# Supersedes: any inline description of KER-105 in Claude prompts

---

## 1. Purpose

This document specifies Document 13 of the Kerno compliance copilot codebase.

Document 13 implements the Decision layer recommendation engine — the capstone
of Sprint 1. It wires together all prior documents into a single end-to-end path:

  Doc 10 (ingested context records)
    + Doc 11 (control catalogue)
    + Doc 12 (evidence links)
    + Doc 9  (bias-calibrated retrieval)
    -> Doc 13 (recommendation with status, confidence, rationale, cited evidence)
    -> Docs 7-9 (human approval / override via feedback layer)

After this document, the pipeline can demonstrate a full vertical slice: ingest
evidence, link it to a NIS2 control, generate an explainable recommendation, and
route it for human review — with every step persisted and auditable.

The four-layer architecture:
  1. Data Context (ingest)        <- Document 10 (closed)
  2. Decision (classify, score)   <- Documents 11-13 <- THIS IS THE FINAL PIECE
  3. Feedback (approve, override) <- Documents 7-9 (closed)
  4. Interface (Trust Center)     <- Future

---

## 2. Scope — KER-105 acceptance criteria (authoritative)

Source: Kerno Sprint 1 Backlog, KER-105.

The implementation is complete when all four acceptance criteria pass:

  AC-1: Given a control with linked evidence, when a recommendation is generated,
        then it returns a status, a confidence indicator and the evidence IDs
        it relied on.

  AC-2: Given a recommendation, when a user opens it, then a human-readable
        explanation lists the reasoning factors and any gaps.

  AC-3: Low-confidence outputs are clearly marked and routed for mandatory
        human review.

  AC-4: Every recommendation persists its input snapshot for later audit
        reproduction.

---

## 3. Recommendation model

### 3.1 RecommendationStatus values

The three possible statuses a recommendation can carry. Define as module-level
constants in src/models/recommendation.py:

  STATUS_MET      = "met"      -- evidence present, coverage sufficient
  STATUS_PARTIAL  = "partial"  -- some evidence present, gaps remain
  STATUS_GAP      = "gap"      -- no meaningful evidence found

These map directly to the Trust Center status taxonomy (Trust Center Spec §5).
Note: "Met" in the Trust Center requires a human sign-off on top of this
recommendation — the recommendation itself never sets the final status.

### 3.2 ConfidenceLevel values

Define as module-level constants in src/models/recommendation.py:

  CONFIDENCE_HIGH   = "high"    -- >= HIGH_CONFIDENCE_THRESHOLD
  CONFIDENCE_MEDIUM = "medium"  -- >= MEDIUM_CONFIDENCE_THRESHOLD and < HIGH
  CONFIDENCE_LOW    = "low"     -- < MEDIUM_CONFIDENCE_THRESHOLD

Thresholds defined in config/constants.py (see §5.4).

### 3.3 Recommendation (the persisted record)

| Field              | Type           | Required | Notes |
|--------------------|----------------|----------|-------|
| recommendation_id  | UUID (v4)      | Yes      | Generated in Python |
| tenant_id          | UUID (v4)      | Yes      | FK -> tenants |
| control_id         | str            | Yes      | FK -> compliance_controls |
| status             | str            | Yes      | STATUS_MET / PARTIAL / GAP |
| confidence_level   | str            | Yes      | CONFIDENCE_HIGH / MEDIUM / LOW |
| confidence_score   | float          | Yes      | Raw score 0.0-1.0 |
| rationale          | str            | Yes      | Plain-language explanation (AC-2) |
| gaps               | str or None    | No       | Plain-language description of gaps; None if STATUS_MET |
| evidence_ids       | list[str]      | Yes      | record_ids that were relied on (AC-1) |
| requires_review    | bool           | Yes      | True when confidence_level = CONFIDENCE_LOW (AC-3) |
| input_snapshot     | dict           | Yes      | Full input state at generation time (AC-4) |
| generated_at       | datetime (UTC) | Yes      | Set at INSERT |
| is_superseded      | bool           | Yes      | True when a newer recommendation exists for same (tenant, control) |

### 3.4 input_snapshot schema (stored as JSONB)

The input_snapshot must contain enough information to reproduce the recommendation
without querying any other table. It must include:

  {
    "control_id": str,
    "control_ref": str,
    "control_title": str,
    "evidence_count": int,
    "evidence_records": [
      {
        "record_id": str,
        "source_system": str,
        "external_id": str,
        "title": str,
        "relevance_score": float or null
      },
      ...
    ],
    "bias_vector_present": bool,
    "generated_at": ISO8601 str
  }

---

## 4. Recommendation logic

### 4.1 Scoring rules (authoritative)

The recommendation service scores a control by inspecting its linked evidence.
These rules are deterministic and rule-based (no LLM required for Sprint 1).

Step 1 -- Retrieve evidence
  Call evidence_service.get_evidence_for_control() for the control.
  Only LINK_STATUS_ACTIVE evidence is used for scoring.
  Broken links are noted in gaps text but do not contribute to the score.

Step 2 -- Compute raw confidence score

  evidence_count = number of active evidence records
  weighted_score = sum(relevance_score for each record where relevance_score
                       is not None, defaulting to DEFAULT_RELEVANCE_SCORE
                       for records with no score)
  normalised_score = weighted_score / max(evidence_count, 1)
  confidence_score = min(normalised_score, 1.0)

  DEFAULT_RELEVANCE_SCORE defined in config/constants.py (value: 0.5).

Step 3 -- Determine status

  if evidence_count == 0:
      status = STATUS_GAP
  elif confidence_score >= HIGH_CONFIDENCE_THRESHOLD:
      status = STATUS_MET
  elif confidence_score >= MEDIUM_CONFIDENCE_THRESHOLD:
      status = STATUS_PARTIAL
  else:
      status = STATUS_GAP

Step 4 -- Determine confidence level

  if confidence_score >= HIGH_CONFIDENCE_THRESHOLD:
      confidence_level = CONFIDENCE_HIGH
  elif confidence_score >= MEDIUM_CONFIDENCE_THRESHOLD:
      confidence_level = CONFIDENCE_MEDIUM
  else:
      confidence_level = CONFIDENCE_LOW

Step 5 -- Set requires_review
  requires_review = (confidence_level == CONFIDENCE_LOW)  [AC-3]

Step 6 -- Build rationale and gaps (plain language, AC-2)

  rationale must name:
    - how many active evidence records were found
    - the highest-relevance source system and record title (if any)
    - the resulting status and why

  gaps (None if STATUS_MET) must name:
    - count of broken links, if any
    - whether evidence_count was zero
    - whether score fell below thresholds and by how much

  Both fields are plain strings. No templates from external systems.
  Keep each under MAX_RATIONALE_LENGTH characters (defined in constants.py,
  value: 1000).

### 4.2 Superseding prior recommendations

When a new recommendation is generated for a (tenant_id, control_id) pair
that already has a recommendation, all prior non-superseded rows for that pair
must be marked is_superseded = True before the new row is inserted.
This ensures at most one active (is_superseded = False) recommendation per
(tenant, control) at any time.

---

## 5. Files in scope

### 5.1 src/models/recommendation.py -- NEW

ORM model for Recommendation (§3.3).
- All fields from §3.3.
- STATUS_ and CONFIDENCE_ constants as module-level constants.
- evidence_ids stored as ARRAY(Text) in Postgres.
- input_snapshot stored as JSONB.
- generated_at set via server_default=func.now().
- RLS will be applied in the migration (§5.3); model itself issues no queries.

### 5.2 src/services/recommendation_service.py -- NEW

The recommendation engine. All DB operations via conn.execute(sql, dict)
with :name-style parameters. No SQLAlchemy Session API.
resolve_and_set_tenant_context(conn, tenant_id) called before any DB access.
TenantContextMissingError (from src.exceptions) raised if tenant_id is invalid.

Responsibilities:

- generate_recommendation(conn, tenant_id, control_id: str) -> RecommendationOutput
    Implements the full scoring logic from §4.1.
    Calls evidence_service.get_evidence_for_control() internally.
    Marks prior recommendations as superseded (§4.2).
    Persists the new Recommendation row.
    Returns a RecommendationOutput dataclass (see below).

- get_recommendation(conn, tenant_id, control_id: str) -> RecommendationOutput | None
    Returns the current (is_superseded=False) recommendation for a control,
    or None if none exists.

- get_recommendation_by_id(conn, tenant_id, recommendation_id: str)
    -> RecommendationOutput | None
    Returns a specific recommendation by its ID (for audit reproduction).

- RecommendationOutput: frozen dataclass. Fields mirror Recommendation
  plus all fields from §3.3. Used as the return type for all three methods.

Rules:
- All functions under 40 lines; factor private helpers if needed.
- Scoring logic factored into a private _score_evidence(evidence: list) helper
  that takes a list of EvidenceResult and returns a ScoringResult dataclass
  (confidence_score, status, confidence_level, requires_review).
  This makes the scoring logic independently testable.
- ScoringResult: frozen dataclass with fields:
    confidence_score: float
    status: str
    confidence_level: str
    requires_review: bool
- _build_rationale and _build_gaps are separate private helpers, each under
  40 lines.

### 5.3 migrations/versions/010_create_recommendations_table.py -- NEW

Creates the recommendations table.

- revision chains after migration 009 (i4j5k6l7).
- upgrade():
    - Creates recommendations table with all fields from §3.3.
    - evidence_ids as TEXT[].
    - input_snapshot as JSONB.
    - Index on (tenant_id, control_id, is_superseded) for fast current-
      recommendation lookups.
    - ENABLE ROW LEVEL SECURITY.
    - CREATE POLICY tenant_isolation_policy:
      USING (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
      (direct tenant_id column -- same pattern as prior RLS tables).
- downgrade(): drops table.
- Full module docstring: What / Why / How.
- All functions under 40 lines.

### 5.4 config/constants.py -- extend only

Add if not already present:
- HIGH_CONFIDENCE_THRESHOLD = 0.75
- MEDIUM_CONFIDENCE_THRESHOLD = 0.40
- DEFAULT_RELEVANCE_SCORE = 0.5
- MAX_RATIONALE_LENGTH = 1000

Do not remove or rename existing constants.

### 5.5 tests/unit/services/test_recommendation_service.py -- NEW

Unit tests for recommendation_service.py. No live database.

Required tests (implement all):

| # | Test name | What it asserts |
|---|---|---|
| 1 | test_generate_recommendation_met | High relevance evidence -> STATUS_MET, CONFIDENCE_HIGH |
| 2 | test_generate_recommendation_partial | Medium relevance -> STATUS_PARTIAL, CONFIDENCE_MEDIUM |
| 3 | test_generate_recommendation_gap_no_evidence | Zero evidence -> STATUS_GAP |
| 4 | test_generate_recommendation_gap_low_score | Evidence present but score below MEDIUM threshold -> STATUS_GAP |
| 5 | test_low_confidence_sets_requires_review | CONFIDENCE_LOW -> requires_review = True |
| 6 | test_high_confidence_clears_requires_review | CONFIDENCE_HIGH -> requires_review = False |
| 7 | test_prior_recommendation_superseded | Second generate call -> first row marked is_superseded=True |
| 8 | test_input_snapshot_persisted | input_snapshot contains control_id, evidence_count, generated_at |
| 9 | test_rationale_non_empty_string | rationale is a non-empty str under MAX_RATIONALE_LENGTH |
| 10 | test_gaps_none_when_met | STATUS_MET -> gaps is None |
| 11 | test_gaps_present_when_partial | STATUS_PARTIAL -> gaps is non-empty str |
| 12 | test_tenant_context_set_before_query | SET LOCAL is first SQL call |
| 13 | test_none_tenant_raises | TenantContextMissingError on None |
| 14 | test_no_sqlalchemy_session_api | conn.add() and conn.flush() never called |
| 15 | test_broken_links_noted_in_gaps | Broken evidence link -> mentioned in gaps text |

### 5.6 tests/unit/services/test_scoring.py -- NEW

Isolated unit tests for the _score_evidence helper. Import and call it directly.

Required tests (implement all):

| # | Test name | What it asserts |
|---|---|---|
| 1 | test_score_empty_list_returns_gap | [] -> STATUS_GAP, score=0.0 |
| 2 | test_score_high_relevance_returns_met | All scores >= HIGH threshold -> STATUS_MET |
| 3 | test_score_mixed_relevance_returns_partial | Mixed scores -> STATUS_PARTIAL |
| 4 | test_score_none_relevance_uses_default | None scores -> DEFAULT_RELEVANCE_SCORE used |
| 5 | test_score_capped_at_1_0 | Computed score never exceeds 1.0 |
| 6 | test_confidence_level_boundaries | Score at exact threshold values -> correct level |
| 7 | test_requires_review_only_on_low | Only CONFIDENCE_LOW sets requires_review=True |

---

## 6. Gate checks (apply to every file produced)

| Check | Rule |
|---|---|
| Module docstring present | Answers What, Why, and How to run or test |
| All functions have docstrings | No exceptions |
| No spec notation in variable names | No Greek letters, subscripts, raw spec symbols |
| No magic numbers | All numeric literals named in config/constants.py |
| No function longer than 40 lines | Factor helpers if needed |
| Tenant isolation rule followed | resolve_and_set_tenant_context before any DB access |
| TenantContextMissingError from src.exceptions | Not from src.db.rls or any other location |

---

## 7. Out of scope for Document 13

- LLM-generated rationale text (Sprint 1 uses deterministic rule-based scoring).
- Confidence calibration using the bias vector from retrieval_service.py
  (the bias layer improves retrieval ranking upstream; Document 13 scores
  whatever evidence is already linked -- no direct coupling to retrieval_bias).
- API endpoints exposing recommendations (future Interface layer).
- Recommendation approval / override (that is Docs 7-9, already closed).
- Evidence pack export (KER-111, future).

---

## 8. Authoritative references

| Document | Authority |
|---|---|
| CLAUDE.md (current version in working directory) | Highest |
| This file (PROMPT_doc13_decision_layer.md) | Authoritative for Document 13 scope |
| Kerno_Sprint1_Backlog.pdf KER-105 | Source of acceptance criteria |
| PROMPT_doc12_evidence_linking.md §3.2 | EvidenceResult and LINK_STATUS constants |
| PROMPT_doc11_nis2_control_mapping.md §3.1 | ComplianceControl schema |
