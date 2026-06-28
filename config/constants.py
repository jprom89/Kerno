"""Single source of truth for every named numeric constant in the pipeline.

What this file does
-------------------
Defines every numeric constant used across the Kerno learning pipeline.
No logic lives here — only named values with documented origins.

Why this file exists
--------------------
CLAUDE.md §2.4 forbids raw numeric literals anywhere in the codebase except
inside this file. A number sitting in a function is unreadable and unauditable:
nobody can tell where it came from or whether it is allowed to change. Every
constant here carries a comment pointing to the exact specification section it
was derived from, so a reviewer can trace any value to its origin without
asking an engineer.

How to run or test
------------------
This file contains no executable logic and has no tests of its own. To verify
the constants are importable run:

    python -c "import config.constants; print('constants OK')"

To verify individual values against the spec, read LEARNING_PIPELINE_SPEC.md
and compare each constant to the section listed in its inline comment.

All values are sourced from LEARNING_PIPELINE_SPEC.md. Do not change a value
here without updating the spec section it references.
"""

# ---------------------------------------------------------------------------
# Retrieval bias recalculation (the nightly learning step)
# Source: LEARNING_PIPELINE_SPEC.md §5.2 — "The Weight Recalculation Formula"
# ---------------------------------------------------------------------------

# How much of a tenant's existing calibration is preserved on each nightly
# update. Higher means the system changes its mind more slowly and stays stable.
# This is "alpha" in the spec formula — renamed per CLAUDE.md §2.3.
# (LEARNING_PIPELINE_SPEC.md §5.2)
DECAY_FACTOR: float = 0.85

# The fraction of the correction signal applied on each update.
# Derived as 1 - DECAY_FACTOR so the two constants always sum to exactly 1.0
# without floating-point drift. Named "learning rate" because it controls how
# fast the system learns from new corrections.
# (LEARNING_PIPELINE_SPEC.md §5.2)
LEARNING_RATE: float = 1.0 - DECAY_FACTOR

# Confidence weight given to an override made by a senior reviewer
# (a vCISO or fractional CISO). Their corrections carry full weight.
# This is gamma_i = 1.0 in the spec — renamed per CLAUDE.md §2.3.
# (LEARNING_PIPELINE_SPEC.md §5.2)
SENIOR_REVIEWER_WEIGHT: float = 1.0

# Confidence weight given to an override made by a junior reviewer
# (an internal admin). Their corrections carry half weight.
# This is gamma_i = 0.5 in the spec — renamed per CLAUDE.md §2.3.
# (LEARNING_PIPELINE_SPEC.md §5.2)
JUNIOR_REVIEWER_WEIGHT: float = 0.5

# ---------------------------------------------------------------------------
# Calibration / commercial moat thresholds
# Source: LEARNING_PIPELINE_SPEC.md §6 — "The Commercial Switching-Cost Moat"
# ---------------------------------------------------------------------------

# Minimum number of human overrides a tenant must accumulate before its
# personalised bias vector is considered meaningful rather than noise.
# (LEARNING_PIPELINE_SPEC.md §6.1)
CALIBRATION_THRESHOLD_MIN_OVERRIDES: int = 200

# The recommendation acceptance rate the calibrated retrieval layer targets
# once the override threshold above is crossed. 0.75 means we aim for the AI's
# suggestions to be accepted by the human at least 75% of the time.
# (LEARNING_PIPELINE_SPEC.md §6.2)
CALIBRATION_THRESHOLD_TARGET_ACCEPTANCE_RATE: float = 0.75

# ---------------------------------------------------------------------------
# Retrieval query shape
# Source: LEARNING_PIPELINE_SPEC.md §5.3 — "Query Execution with Bias Injection"
# ---------------------------------------------------------------------------

# How many candidate controls a similarity search returns to the user.
# The spec's calibrated query ends in "LIMIT 5".
# (LEARNING_PIPELINE_SPEC.md §5.3)
TOP_K_RETRIEVAL_RESULTS: int = 5

# Document-9 canonical name for the same retrieval limit (PROMPT_doc9 §4.1).
# Defined as an alias of TOP_K_RETRIEVAL_RESULTS so both names resolve to the
# same value without duplicating the literal. Callers introduced in Document 9
# and later should import this name; TOP_K_RETRIEVAL_RESULTS is kept for any
# existing callers.
MAX_SIMILAR_CONTROLS_RETURNED: int = TOP_K_RETRIEVAL_RESULTS

# Dimensionality of every stored embedding and of each tenant's bias vector.
# Fixed at 1536 to match the embedding model's output width; the bias vector
# must share this width so the two can be compared in the calibrated query.
# (LEARNING_PIPELINE_SPEC.md §5.3; embedding model output dimension)
EMBEDDING_DIMENSION: int = 1536

# Strength of the tenant bias correction applied during the calibrated
# similarity query (the spec's ":bias_coefficient" bound parameter). A value of
# 1.0 applies the learned bias at full strength; lowering it blends back toward
# an unbiased generic ranking. The spec defines the parameter but pins no value,
# so this is a tunable default surfaced here rather than hidden in a query.
# (LEARNING_PIPELINE_SPEC.md §5.3)
BIAS_INJECTION_COEFFICIENT: float = 1.0

# ---------------------------------------------------------------------------
# Tenant identifier format
# Source: CLAUDE.md §3 (tenant isolation) / KER-101 (UUIDv4 registration)
# ---------------------------------------------------------------------------

# Required UUID version for every tenant identifier. Tenant IDs are minted as
# UUIDv4 at registration (KER-101) and never change; the tenant-isolation guard
# rejects any identifier that is not version 4 rather than run a query with an
# untrusted tenant context. (CLAUDE.md §3.)
TENANT_ID_UUID_VERSION: int = 4

# ---------------------------------------------------------------------------
# Evidence linking and full-text search (Document 12 / KER-104)
# Source: PROMPT_doc12_evidence_linking.md §4.3–4.4
# ---------------------------------------------------------------------------

# Maximum number of context_records returned by a single full-text search call.
# Keeps search responses bounded; callers that need more should paginate.
# (PROMPT_doc12_evidence_linking.md §4.3)
FULL_TEXT_SEARCH_LIMIT: int = 20

# Default row limit for the vector similarity search over context_records
# (retrieve_similar_records in retrieval_service.py). Matches
# MAX_SIMILAR_CONTROLS_RETURNED so both retrieval paths return the same depth by default.
MAX_SIMILAR_RECORDS_RETURNED: int = 5

# Inclusive lower bound for relevance_score on a ControlEvidenceLink row.
# Evidence linking rejects scores below this value with a ValueError.
# (PROMPT_doc12_evidence_linking.md §4.1)
RELEVANCE_SCORE_MIN: float = 0.0

# Inclusive upper bound for relevance_score on a ControlEvidenceLink row.
# Evidence linking rejects scores above this value with a ValueError.
# (PROMPT_doc12_evidence_linking.md §4.1)
RELEVANCE_SCORE_MAX: float = 1.0

# ---------------------------------------------------------------------------
# DORA Register of Information (Document 14 / KER-106)
# Source: PROMPT_doc14_dora_roi_live_register.md §4.4 and §5.5
# ---------------------------------------------------------------------------

# Maximum character length for the exit_strategy_summary field on a
# DORARegisterEntry row. Keeps free-text fields bounded and prevents
# unbounded input from reaching the database.
# (PROMPT_doc14_dora_roi_live_register.md §4.4)
MAX_EXIT_SUMMARY_LENGTH: int = 1000

# ---------------------------------------------------------------------------
# Recommendation engine (Document 13 / KER-105)
# Source: PROMPT_doc13_decision_layer.md §4.1 and §5.4
# ---------------------------------------------------------------------------

# Minimum confidence_score for a recommendation to be classified as STATUS_MET
# and CONFIDENCE_HIGH. Scores at or above this threshold indicate sufficient
# evidence coverage. (PROMPT_doc13_decision_layer.md §4.1 Step 3-4)
HIGH_CONFIDENCE_THRESHOLD: float = 0.75

# Minimum confidence_score for a recommendation to be classified as
# STATUS_PARTIAL and CONFIDENCE_MEDIUM. Scores between this and
# HIGH_CONFIDENCE_THRESHOLD indicate partial coverage.
# (PROMPT_doc13_decision_layer.md §4.1 Step 3-4)
MEDIUM_CONFIDENCE_THRESHOLD: float = 0.40

# Confidence score below which an LLM-generated recommendation is flagged
# for human review. The mapping service sets requires_review=True when
# the LLM's returned confidence is strictly less than this value.
# (KER-105 — mapping_service.py)
LOW_CONFIDENCE_THRESHOLD: float = 0.5

# Score assigned to evidence records that have no explicit relevance_score.
# Represents a neutral "present but unscored" signal.
# (PROMPT_doc13_decision_layer.md §4.1 Step 2)
DEFAULT_RELEVANCE_SCORE: float = 0.5

# Maximum character length for the rationale and gaps fields on a
# Recommendation row. Prevents unbounded text in the persisted record.
# (PROMPT_doc13_decision_layer.md §4.1 Step 6)
MAX_RATIONALE_LENGTH: int = 1000

# Maximum word count the mapping prompt instructs the LLM to use for
# its reasoning field. Not a hard DB constraint — enforced at the
# prompt layer only, so the LLM is guided but not truncated server-side.
# (KER-105 — mapping_service.py)
MAX_REASONING_WORDS: int = 400

# ---------------------------------------------------------------------------
# DORA RoI Export + Validation (Document 15 / KER-106 part 2)
# Source: PROMPT_doc15_dora_roi_export_validation.md §3.4 and §5.3
# ---------------------------------------------------------------------------

# Maximum character length for the provider_name field in a DORAExportRow.
# Warn-level validation fires when the export row exceeds this limit.
# (PROMPT_doc15_dora_roi_export_validation.md §3.4 rule 18)
MAX_PROVIDER_NAME_LENGTH: int = 255

# Maximum character length for the service_name field in a DORAExportRow.
# (PROMPT_doc15_dora_roi_export_validation.md §3.4 rule 19)
MAX_SERVICE_NAME_LENGTH: int = 255

# Maximum character length for the business_function field in a DORAExportRow.
# (PROMPT_doc15_dora_roi_export_validation.md §3.4 rule 20)
MAX_BUSINESS_FUNCTION_LENGTH: int = 500

# Severity string for a validation issue that has no finding — the check passed.
# (PROMPT_doc15_dora_roi_export_validation.md §3.3)
VALIDATION_SEVERITY_PASS: str = "pass"

# Severity string for a validation issue that represents a warning.
# (PROMPT_doc15_dora_roi_export_validation.md §3.3)
VALIDATION_SEVERITY_WARN: str = "warn"

# Severity string for a validation issue that represents a hard failure.
# (PROMPT_doc15_dora_roi_export_validation.md §3.3)
VALIDATION_SEVERITY_FAIL: str = "fail"

# ---------------------------------------------------------------------------
# Authentication — password hashing and JWT issuance (Document 19 / KER-103)
# ---------------------------------------------------------------------------

# Lifetime of a JWT issued at login, in seconds. 86400 = 24 hours (one working day).
# Tokens expire server-side via the 'exp' claim; the dashboard clears the stored
# token and redirects to login on 401. (CLAUDE.md §3 — tenant auth)
JWT_EXPIRY_SECONDS: int = 86400

# CPU and memory cost factor for the scrypt KDF (the 'n' parameter).
# 16384 = 2^14 — the OWASP-recommended minimum for interactive logins
# as of 2024. Doubling this value halves throughput; halving it doubles throughput.
SCRYPT_COST_FACTOR: int = 16384

# Block size parameter for the scrypt KDF (the 'r' parameter).
# Controls internal memory block size. 8 is the standard value from the original
# scrypt paper; there is no practical reason to change it for login workloads.
SCRYPT_BLOCK_SIZE: int = 8

# Parallelization factor for the scrypt KDF (the 'p' parameter).
# 1 means sequential hashing. Increase to run hashing across multiple CPU cores
# when under high login concurrency; the MVP is single-tenant so 1 is correct.
SCRYPT_PARALLELISM: int = 1

# Length of the derived key produced by scrypt, in bytes.
# 32 bytes = 256-bit key — matches AES-256 key length as a convention.
SCRYPT_KEY_LENGTH: int = 32

# Length of the random salt generated for each new password hash, in bytes.
# 32 bytes = 256 bits of entropy; this makes pre-computation attacks infeasible.
SCRYPT_SALT_LENGTH: int = 32
