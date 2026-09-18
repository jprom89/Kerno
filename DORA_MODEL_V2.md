# DORA_MODEL_V2.md — Canonical DORA Register Architecture

**Status:** Authoritative DORA domain architecture
**Version:** 1.1
**Applies to:** DORA Register of Information v2 work
**Implementation status:** Architecture authority; implementation occurs incrementally through approved `DORA-V2-*` tickets.

## 0. Authority and conflict rules

For DORA Register architecture, repository authority is:

1. `CLAUDE.md`
2. `NOW.md`
3. `DORA_MODEL_V2.md`
4. the currently approved `DORA-V2-*` implementation ticket
5. existing `PROMPT_doc*.md` files
6. `KERNO_STRATEGY.md`
7. `FILE_STRUCTURE.md`

`CLAUDE.md` remains authoritative for global engineering constraints including readability, tenant isolation, security, GDPR/data classification, audit behavior and repository conventions.

`NOW.md` remains authoritative for current implementation priority.

`DORA_MODEL_V2.md` is authoritative for DORA domain architecture.

If Documents 14–17 or another historical prompt conflicts with this file regarding the DORA model, this file wins.

Historical prompt files remain useful implementation records. They are not deleted merely because the architecture has evolved.

## 1. Product model

Kerno is an EU operational-resilience system of record.

For DORA, Kerno maintains operational information about ICT third-party arrangements from which a defensible Register of Information can be produced.

The regulatory filing is an output of the operational model.

The regulatory templates are not the database schema.

Do not create 15 persistence tables mirroring the 15 Register templates.

The intended flow is:

```text
Tenant-owned operational graph
              ↓
Maintained DORA register facts
              ↓
Register profile / scope
              ↓
Immutable reference-date snapshot
              ↓
Regulatory projections
              ↓
Versioned validation
              ↓
Official reporting package
              ↓
Frozen package bytes + SHA-256
```

## 2. Three-layer architecture

DORA v2 distinguishes three different kinds of information.

### 2.1 Operational domain

Reusable real-world objects such as:

* legal organizations;
* identifiers;
* contracts;
* ICT services;
* business functions;
* provider/service arrangements;
* consuming-entity relationships;
* subcontracting relationships.

These are tenant-owned operational truth.

### 2.2 Maintained Register facts

DORA-specific facts maintained about operational objects, such as:

* official ICT-service classification;
* licensed-activity designation;
* DORA function identifier;
* function assessment;
* service assessment;
* reporting-period contract costs;
* data sensitivity;
* regulatory country/location facts;
* entity/register-specific definitions.

These are not necessarily independent real-world objects.

They are maintained facts used to produce the Register.

Where a fact changes by technical-package version, reporting period or assessment date, the model must preserve that distinction.

### 2.3 Regulatory projections

The official B_* templates are deterministic projections of the operational graph plus maintained Register facts.

They are output structures.

They are not persistence architecture.

## 3. Ownership model

### 3.1 Tenant owns the graph

The existing `tenants` table remains the Kerno customer/security boundary.

All tenant-specific operational DORA objects belong to a tenant.

A financial entity inside a Register is not the same object as a Kerno tenant.

Do not merge them.

A tenant may maintain information covering:

* one financial entity;
* multiple financial entities;
* branches;
* intra-group ICT providers;
* external ICT providers;
* entity-level registers;
* sub-consolidated registers;
* consolidated registers.

Authorization and Row-Level Security remain tenant-scoped unless a later product requirement explicitly creates finer authorization.

### 3.2 Counterparties remain tenant-private

An organization such as AWS may exist as separate organization records in multiple Kerno tenants.

There is no global cross-tenant vendor master in DORA v2.

Global data is reserved for platform reference information such as:

* regulatory taxonomies;
* controlled lists;
* technical-package metadata;
* submission-window reference data.

This preserves isolation and avoids cross-customer identity coupling.

## 4. Canonical operational graph

The target operational model is:

```text
Tenant
 │
 ├── Organizations
 │     ├── Organization Identifiers
 │     ├── Organization Roles
 │     └── Branches where applicable
 │
 ├── Contracts
 │     ├── Contract Parties
 │     └── Contract Relationships
 │
 ├── ICT Services
 │
 ├── Business Functions
 │
 ├── Service Arrangements
 │     Contract + Direct Provider + ICT Service
 │
 ├── Service Usages
 │     Arrangement + Consuming Entity + Function Designation
 │
 └── Supply Relationships
       Provider → immediate recipient provider
       within a Service Arrangement
```

Separate layers then add:

```text
Regulatory classifications
Periodic facts
Assessments
Locations
Register profiles
Snapshots
Projections
```

## 5. Organizations

### 5.1 `dora_organizations`

Canonical legal organization identity.

Required concepts:

* `organization_id`
* `tenant_id`
* `legal_name`
* country of establishment/headquarters where applicable
* active state
* timestamps

Do not duplicate a legal organization because it appears in multiple contracts.

### 5.2 `dora_organization_identifiers`

Stores organization identifiers separately from the organization master.

Required concepts:

* identifier ID
* tenant
* organization
* identifier type
* identifier value
* validity/status as required
* timestamps

Examples include LEI, EUID and other applicable identifiers.

Within one tenant, the same normalized identifier type/value pair must not identify multiple organizations.

Do not make identifier-type descriptors part of a regulatory row identity unless the applicable DPM defines them as such.

### 5.3 `dora_organization_roles`

Represents roles an organization performs inside the tenant's DORA graph.

Initial roles:

* `financial_entity`
* `ict_provider`

One organization may hold both roles.

Example:

```text
Group IT Services GmbH
    financial_entity? no
    ict_provider? yes
```

or:

```text
Group Bank AG
    financial_entity? yes
    ict_provider? yes
```

Do not create duplicate organization masters for dual-hat entities.

Do not use this table as a dumping ground for B_01 or B_05 template columns.

Role-specific regulatory facts belong in explicitly designed maintained-Register-fact structures.

## 6. Branches

Branches are legitimate regulatory entities but are not required to be implemented before realistic data demonstrates the need.

Target concept:

`dora_branches`

Required concepts when implemented:

* branch ID
* tenant
* owning financial entity organization
* branch identifier
* branch name
* country
* timestamps

A branch belongs to one financial entity.

The canonical model allows an ICT-service usage to reference a branch where applicable.

## 7. Contracts

### 7.1 `dora_contracts`

Canonical contractual arrangement.

Required concepts:

* contract ID
* tenant
* stable contractual reference
* descriptive name where useful
* lifecycle state
* applicable start/end dates
* timestamps

Do not store a provider name as contract identity.

Do not store annual expense as a timeless column on the contract master.

### 7.2 `dora_contract_parties`

Represents organizations participating in a contractual arrangement.

Required concepts:

* contract party ID
* tenant
* contract
* organization
* party role
* timestamps

Examples include:

* recipient signatory;
* provider signatory;
* intra-group provider signatory.

Who signs a contract and who consumes an ICT service are independent facts.

Never automatically infer a consuming entity from a recipient signatory.

At entity level they may happen to be the same.

At sub-consolidated/consolidated level they may differ.

### 7.3 `dora_contract_relationships`

This is the single canonical representation of contract hierarchy/relationships.

Do not maintain both:

* an `overarching_contract_id` column;

and

* a duplicate contract-link graph.

Use one relationship representation.

Initial relationship semantics must support:

* overarching/master arrangement;
* subsequent/associated arrangement.

Standalone arrangements have no overarching relationship.

Self-links are forbidden.

Cycles are forbidden where the relationship type is hierarchical.

Additional relationship types require an explicit use case before implementation.

### 7.4 `dora_contract_costs`

Annual expense/estimated cost is a reporting-period fact.

It is not a timeless property of the contract.

Target concepts:

* contract cost ID
* tenant
* contract
* reporting period/year
* currency
* amount
* cost type where required
* source/provenance
* timestamps

The design must support master/subsequent arrangements without double-counting overall cost.

## 8. ICT services

### 8.1 `dora_ict_services`

Represents the real operational ICT service.

Examples:

* Microsoft 365
* Azure Virtual Machines
* SAP S/4HANA Cloud
* AWS EC2
* Managed SOC service

Required concepts:

* service ID
* tenant
* service name
* active state
* timestamps

An operational ICT service is not the same thing as the regulator's controlled ICT-service-type code.

## 9. Service arrangements

### 9.1 `dora_service_arrangements`

Represents one ICT service supplied by one direct provider under one contractual arrangement.

Conceptually:

```text
Contract
    +
Direct provider
    +
ICT service
```

Required concepts:

* service arrangement ID
* tenant
* contract
* direct provider organization
* ICT service
* lifecycle state
* timestamps

This is operational truth.

The direct provider has exactly one canonical home here.

Do not duplicate direct-provider ownership in:

* contract party semantics;
* rank-1 supply-chain master data.

A provider may sign a contract without being the provider of every service under it.

Contract party means who signed.

Service arrangement means who provides the service.

## 10. Regulatory ICT-service classification

Official ICT-service type is a maintained regulatory classification, not the identity of the operational ICT service.

Target concept:

`dora_service_classifications`

Required concepts:

* classification ID
* tenant
* service arrangement
* technical/reference-data version
* official ICT-service-type code
* effective/valid status where needed
* timestamps

A service arrangement may need different mappings as regulatory taxonomies evolve.

The filing package determines which technical/reference version is applicable.

Do not silently overwrite the old meaning when a controlled list changes.

## 11. Business functions

### 11.1 `dora_functions`

Represents an internal business function belonging to a financial entity.

Required concepts:

* function ID
* tenant
* owning financial entity organization
* function name
* active state
* timestamps

Functions remain financial-entity-specific.

Do not create a global cross-entity function catalogue by default.

### 11.2 `dora_function_designations`

Represents the DORA identity of a function under a licensed activity.

Semantic grain:

```text
Financial entity
    +
Licensed activity
    +
Function name
```

Required concepts:

* function designation ID
* tenant
* function
* licensed activity code
* stable DORA function reference such as `F1`, `F2`, ...
* validity/status
* timestamps

The same operational function used under two licensed activities produces two designations and therefore two DORA function identifiers.

The DORA function identifier is not generated by concatenating:

* LEI;
* activity;
* function name.

It is a stable `F<n>`-style identifier maintained for the applicable combination.

## 12. Function assessments

Criticality, RTO/RPO and related function assessments must preserve assessment context rather than living as timeless columns if their values can change.

Target concept:

`dora_function_assessments`

Required concepts as applicable:

* assessment ID
* tenant
* function designation
* criticality/importance assessment
* rationale
* assessment date
* RTO
* RPO
* discontinuation-impact assessment
* timestamps

Legacy `criticality_level` must not automatically become the DORA assessment.

## 13. Service usage

### 13.1 `dora_service_usages`

Represents a consuming financial entity relying on a service arrangement for a DORA function designation.

Conceptually:

```text
Service Arrangement
       +
Consuming Financial Entity
       +
Function Designation
       +
Optional Branch
```

Required concepts:

* service usage ID
* tenant
* service arrangement
* consuming financial entity organization
* function designation
* optional branch
* lifecycle dates/status
* timestamps

Integrity requirements:

* function designation belongs to the consuming financial entity;
* branch, when present, belongs to the consuming financial entity;
* arrangement belongs to the same tenant;
* direct provider belongs to the same tenant.

A service arrangement may have many usages.

Example:

```text
AWS Agreement + AWS + EC2
    → Bank AG + Payments
    → Bank AG + Fraud Detection
    → Subsidiary AG + Customer Onboarding
```

The provider, contract and ICT service remain single reusable objects.

## 14. Incomplete source data

The canonical graph is allowed to enforce integrity.

Messy imports must not weaken canonical constraints merely so incomplete spreadsheets can be inserted.

Introduce an import/reconciliation staging layer.

Conceptually:

```text
Source row
    ↓
Import staging
    ↓
Normalization / matching
    ↓
Unresolved issues
    ↓
Human confirmation
    ↓
Canonical v2 graph
```

Incomplete source records may remain in staging until sufficient facts exist to create valid canonical relationships.

Do not invent a function merely because canonical `service_usage` requires one.

## 15. Service locations

### 15.1 `dora_service_locations`

Normalized country/location facts.

Required concepts:

* location ID
* tenant
* service usage or other verified owning object
* location type
* country code
* validity/effective context where required
* timestamps

Initial relevant location types include:

* `service_provision`
* `data_storage`
* `data_processing`

One relationship may have several countries for a given location type.

Do not use a single generic `countries_supported[]` field.

The regulatory projection is responsible for producing the required B_02.02 row combinations when several storage or processing countries exist.

Where the technical package requires explicit Not Applicable values rather than blanks, that is a projection/validation concern.

## 16. Supply chain

### 16.1 Operational representation

Do not model subcontracting as a tree node with exactly one parent.

Subcontracting may form a DAG.

Use provider-to-immediate-recipient relationships.

Target concept:

`dora_supply_edges`

Required concepts:

* supply edge ID
* tenant
* service arrangement
* subcontracting provider organization
* immediate recipient provider organization
* lifecycle/status
* timestamps

Example:

```text
Direct Provider AWS

A → AWS
B → AWS
D → A
D → B
```

This represents a diamond without cloning provider D.

### 16.2 Rank

Rank is maintained Register information but need not be permanent provider master data.

Rank is specific to the provider's position in a particular contract/service chain.

Kerno may deterministically derive and validate rank from the maintained supply graph.

Required behavior:

* direct provider projects as rank 1;
* subcontractors project as rank > 1;
* providers at the same depth receive the applicable equal rank;
* immediate recipient relationships remain explicit;
* a provider used by multiple recipients may produce multiple regulatory rows;
* cycles are forbidden.

Rank must be available and reviewable as maintained Register information, not calculated from stale relationships only at the final submission moment.

Cycle detection belongs in application/service validation and tests.

A SQL CHECK constraint alone cannot guarantee acyclic graph structure.

## 17. Service assessments

B_07.01-style service assessment information belongs at the regulatory grain of:

```text
Contract
    +
Provider identifier
    +
ICT service type
```

It does not gain hidden financial-entity or function dimensions.

Target concept:

`dora_service_assessments`

The implementation must tie an assessment unambiguously to the service arrangement and applicable official ICT-service classification.

Required concepts include:

* substitutability;
* rationale for non-substitutability/difficulty;
* last audit date;
* exit-plan existence;
* reintegration possibility/difficulty;
* discontinuation impact;
* alternative-provider status;
* alternative-provider information;
* assessment date/effective context;
* timestamps.

Do not attach these assessments to each consuming entity/function merely because B_02.02 contains those dimensions.

Do not infer affirmative exit-plan existence from legacy `exit_strategy_summary`.

## 18. Internal definitions

B_99.01-style definitions are neither global regulatory reference data nor core ICT operational entities.

They are entity/register-specific maintained reported information.

Target concept:

`dora_register_definitions`

Required concepts:

* definition ID
* tenant
* applicable financial entity and/or register profile
* definition category
* regulatory option code
* entity's internal explanation/meaning
* validity/status
* timestamps

The regulatory option codes come from versioned reference data.

The explanatory text belongs to the entity/register.

## 19. Register profiles

Entity/sub-consolidated/consolidated level is not data ownership.

It is a required maintenance/reporting scope over the tenant-owned graph.

Use a configuration concept such as:

`dora_register_profiles`

Required concepts:

* profile ID
* tenant
* maintaining financial entity
* register level
* name
* active state
* timestamps

Applicable levels include:

* entity;
* sub-consolidated;
* consolidated.

Use a membership/filter relationship to define which financial entities are included.

The profile selects a view over the graph.

It does not own organizations, contracts, services or functions.

Operational authorization remains tenant-scoped.

The model must support one tenant-owned graph producing several valid register views.

## 20. Signatory vs consumer

This distinction is mandatory throughout the model.

Contract signatory:

```text
dora_contract_parties
```

Service consumer:

```text
dora_service_usages
```

Never default one from the other as a general rule.

At consolidated or sub-consolidated level, a parent/group entity may sign while a different financial entity consumes.

Importer and UI behavior must preserve this distinction.

## 21. Provenance

Do not repeat `source_system` and `source_record_id` columns across every domain table unless a local technical requirement justifies them.

Use a generalized provenance capability.

Target concept:

`dora_provenance_links`

Required concepts:

* provenance link ID
* tenant
* target object type
* target object ID
* optional target field
* source system
* external/source record identifier
* source locator where applicable
* timestamps

The optional field dimension is important.

Kerno should eventually be able to answer:

Where did this exact value come from?

not merely:

What document is attached to AWS?

## 22. Evidence

Existing `context_records` remains the evidence store.

Do not copy evidence bodies into DORA domain tables.

A generalized DORA evidence-link mechanism should support:

* target object type;
* target object ID;
* optional target field;
* context record;
* linked by;
* linked at;
* relevance where applicable;
* note;
* removed state.

Examples:

```text
organization AWS
field: LEI
evidence: vendor-master.xlsx
```

and:

```text
service usage 123
field: processing_country
evidence: DPA.pdf
```

Broken/deleted evidence must be surfaced rather than silently disappearing.

Existing control-evidence behavior must remain intact.

## 23. Data issues vs regulatory validation findings

Do not combine operational data quality and regulator/template validation into one overloaded issue model.

### 23.1 Operational data issues

Target concept:

`dora_data_issues`

Examples:

* missing provider identifier;
* unresolved source match;
* contract reference conflict;
* unknown consuming entity;
* missing function designation.

Required concepts should remain minimal:

* tenant;
* target operational object/staging record;
* rule/type;
* severity;
* open/resolved state;
* message;
* first/last seen.

Do not build a second Jira/remediation system.

### 23.2 Regulatory validation findings

Validation findings are tied to:

* a Register snapshot;
* technical-package/rule-set version;
* template;
* column/coordinate;
* rule identifier;
* severity;
* source operational object where traceable.

These may be persisted separately from operational data issues.

A taxonomy change must not mutate the meaning of historical validation results.

## 24. Point-in-time state

Frozen package bytes alone are not sufficient historical modeling.

Kerno must preserve the state used to generate a filing.

Introduce an immutable Register snapshot concept.

Target:

`dora_register_snapshots`

Conceptually records:

* tenant;
* register profile;
* register reference date;
* applicable technical-package/reference version;
* exact resolved operational/register state;
* snapshot hash/manifest;
* creation metadata.

Implementation may use:

* copied snapshot data;
* immutable object versions;
* manifest of versioned records;

provided the historical state is reproducible.

The architectural invariant is:

A historical filing must be explainable from an immutable point-in-time Register state, not reconstructed from today's mutable rows.

## 25. Period-specific and versioned facts

Some facts are intrinsically historical.

Examples:

* annual contract expense;
* assessments;
* regulatory classifications;
* function assessments;
* applicable reference-data version.

Do not model such information as timeless overwrite-only attributes when doing so would destroy the state relevant to earlier reference dates.

The audit ledger records that something changed.

The Register model must still preserve enough semantic state to understand what the applicable fact was.

## 26. Regulatory reference data

Regulatory controlled lists and taxonomies must be versioned.

Examples include:

* ICT-service types;
* licensed activities;
* financial-entity classifications;
* identifier types;
* controlled assessment values;
* other reporting lists.

The applicable technical/reference version is explicit.

Do not mutate old codes in place and thereby change historical meaning.

## 27. Regulatory projections

Implement typed projections for the applicable DORA Register templates.

Do not persist the templates as operational tables.

Conceptually:

```text
Snapshot
   ↓
Profile scope
   ↓
Projection services
   ↓
B_01.*
B_02.*
B_03.*
B_04.*
B_05.*
B_06.*
B_07.*
B_99.*
```

Every projected value should be traceable back to:

* operational object;
* maintained Register fact;
* reference-data version;
* snapshot.

Projection must account for regulatory row multiplication caused by values such as multiple storage or processing countries.

## 28. Validation

Validation is layered:

```text
Operational/domain integrity
        ↓
Register completeness
        ↓
Projection integrity
        ↓
Controlled-value validation
        ↓
Cross-template validation
        ↓
Technical-package validation
```

Do not permanently architect around the phrase "116 checks."

Rule sets are versioned.

A specific technical release may contain a specific number of checks.

The architecture must support later rule sets without schema redesign.

Blocking validation prevents a filing run from becoming `ready`.

## 29. Filing package lifecycle

Preserve Kerno's existing freeze invariant.

Target flow:

```text
Live tenant graph
      ↓
Register profile
      ↓
Immutable reference-date snapshot
      ↓
Regulatory projections
      ↓
Validation
      ↓
Official reporting package
      ↓
Freeze exact package bytes
      ↓
SHA-256
```

A later live-register edit must not alter:

* the historical snapshot;
* the historical package.

Repeated downloads of one run must return byte-identical content.

A new run may produce new bytes.

`dora_submission_runs` eventually needs concepts including:

* register profile;
* register snapshot;
* technical-package version;
* package format;
* package SHA-256;
* frozen package bytes or immutable object reference.

The existing frozen JSON mechanism remains valid transitional infrastructure.

## 30. Submission windows

The codebase currently contains:

* `dora_reporting_windows`
* `dora_submission_windows`

V2 standardizes on:

`dora_submission_windows`

because it is already connected to the submission lifecycle and contains register-reference-date semantics.

Do not build new functionality on `dora_reporting_windows`.

Migrate remaining callers before removal.

## 31. Audit ledger

Retain the existing generic hash-chained `audit_log`.

Do not build a parallel DORA audit system.

DORA writes should use explicit object types, for example:

* `dora_organization`
* `dora_contract`
* `dora_ict_service`
* `dora_function`
* `dora_service_arrangement`
* `dora_service_usage`
* `dora_supply_edge`
* `dora_service_assessment`
* `dora_register_profile`
* `dora_register_snapshot`

Important mutations should capture:

* actor;
* role;
* action;
* object;
* before state where applicable;
* after state where applicable;
* timestamp.

Audit history and historical Register state solve different problems.

Keep both.

## 32. Tenant isolation

All tenant-owned DORA tables follow existing Kerno isolation requirements.

Unless explicitly justified:

* direct `tenant_id`;
* PostgreSQL RLS;
* FORCE ROW LEVEL SECURITY;
* `set_tenant_context()` before access;
* explicit tenant filtering where appropriate;
* service-layer tenant validation;
* cross-tenant reference prevention;
* live-database security tests.

Global regulatory reference tables and global submission-window data remain outside tenant scope.

## 33. Legacy `dora_register_entries`

`dora_register_entries` is legacy v1 storage and an import source.

It remains live until migration and cutover are proven.

Do not:

* add new DORA regulatory concepts to it;
* destructively transform it;
* delete it during initial v2 migrations.

Migration path:

```text
Legacy v1 row
      ↓
Import staging
      ↓
Normalization / matching
      ↓
Explicit unresolved issues
      ↓
Canonical v2 graph
```

## 34. Legacy-field interpretation

Never silently invent DORA facts.

Safe examples:

`provider_name`
May create/match an organization candidate.

`service_name`
May create/match an ICT-service candidate.

`business_function`
May create/match a function candidate for later designation.

`source_record_id`
May be recorded through provenance.

Unsafe automatic assumptions:

`provider_type`
is not automatically the official ICT-service type.

`criticality_level`
is not automatically the DORA function assessment.

`countries_supported`
is not automatically service-provision, storage or processing location.

`data_types`
is not automatically regulatory data sensitivity.

`exit_strategy_summary`
does not automatically prove an exit plan exists.

Ambiguity becomes an unresolved import issue.

It does not become fabricated regulatory data.

## 35. UI philosophy

The user should not experience the database graph as a database graph.

The main Register may still present:

```text
Provider | Service | Function | Contract | Criticality | Status
```

but that row becomes a read model/projection over normalized domain objects.

Provider pages can show:

* legal identity;
* identifiers;
* contracts;
* services;
* consumers;
* supported functions;
* subcontractors;
* evidence;
* provenance;
* change history.

Reuse should be visible through behavior.

Example:

Update a provider's legal name once and every arrangement referencing it reflects the change.

## 36. Implementation sequence

The intended sequence is:

```text
DORA-V2-000
Architecture authority in repository

DORA-V2-001
Organizations, identifiers, roles

DORA-V2-002
Contracts, ICT services, functions/designations

DORA-V2-003
Service arrangements, usages, locations

        ↓

MANDATORY REAL-DATA CHECKPOINT

        ↓

DORA-V2-004
Import staging, reconciliation and provenance

DORA-V2-005
Supply graph and maintained regulatory facts

DORA-V2-006
Register profiles and immutable snapshots

DORA-V2-007
Relational Register API/UI cutover

DORA-V2-008
Regulatory projection layer

DORA-V2-009
Versioned regulatory validation

DORA-V2-010
Official reporting package + frozen filing integration

DORA-V2-011
Field-level DORA evidence provenance
```

Ticket numbering may evolve before implementation.

The dependency order and checkpoints matter more than the number.

Do not skip the real-data checkpoint.

## 37. Real-data checkpoint

After the relational core is capable of representing organizations, contracts, services, functions and usages, test it with messy real-world Register/vendor data.

Required scenarios include:

* same provider with spelling variations;
* provider with multiple contracts;
* several services under one contract;
* one service supporting several functions;
* one function supported by several services;
* parent signatory / subsidiary consumer;
* missing identifiers;
* incomplete function mapping;
* unclear country semantics;
* partial subcontractor data;
* named ICT service without official service classification;
* conflicting source records.

Architecture changes discovered here happen before regulatory compiler work.

## 38. Explicit non-goals

DORA v2 foundation work does not justify unrelated roadmap expansion.

Unless separately approved, do not add:

* DORA incident reporting;
* CRA reporting;
* NIS2 asset-inventory redesign;
* Trust Center expansion;
* RAG;
* embeddings/personalization;
* MSP platform;
* billing;
* country packs;
* generic GRC-platform functionality.

## 39. Definition of success

The architecture succeeds when:

1. One legal provider exists once per tenant and participates in many arrangements.
2. One contract is reusable across services and consumers.
3. ICT service identity is separate from regulatory ICT-service classification.
4. Financial-entity function identity correctly accounts for licensed activity.
5. Signatory and consumer can differ.
6. One service arrangement can support many consuming functions/entities.
7. Supply chains can represent diamonds and branching without cloning providers.
8. Rank can be deterministically maintained and projected from the supply graph.
9. Multiple storage/processing countries are represented without ambiguous arrays.
10. Reporting-period costs do not overwrite historical costs.
11. Regulatory classifications are version-aware.
12. Historical filing state is preserved through an immutable Register snapshot.
13. Historical filing bytes remain immutable.
14. Regulatory rows are projections, not operational persistence.
15. Individual reported values can eventually be traced to provenance/evidence.
16. Messy source data can remain unresolved rather than being fabricated into valid-looking DORA data.
17. The model works against real partner data rather than synthetic examples only.

The architectural objective is not:

Kerno fills the DORA spreadsheets.

It is:

Kerno maintains the operational and regulatory facts from which a traceable, point-in-time DORA Register can be produced and defended.
