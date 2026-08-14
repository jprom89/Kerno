# File-complete inspection checklist (this cycle only)

This is how you inspect each of the 12 seeded NIS2 controls **without
interpreting the law**.

It is **not** a BSI audit programme and **not** a finding that the company
meets §30 or §38. Ticking every box means: for this operating cycle, the
file is complete enough that you may click **Approve**. Missing a box means
you do **not** Approve.

Kerno does not read the PDF. You do. The scorer only averages relevance of
whatever you linked. **Do not Approve because the dashboard went green.**
Approve only when every required artefact below is linked and matches
**Pass if**.

## How to inspect one control (same steps every time)

1. Open **Dashboard → Meeting** and find the control card (or this list).
2. For each artefact: is a file **linked** that matches **Pass if**?
3. If **Pass if** is not obvious from the first page of the file, it fails.
   You do not get to infer.
4. Then:
   - Every **required** artefact passes → you may **Approve** as met
     (unless a non-required artefact is missing — then it is **partial**).
   - At least one required artefact is missing → **gap**. Ask the card’s
     question. Set owner + date. Do not Approve.
5. Record the decision on **Recommendations** while they watch.

A file with no date and no named person almost always fails.

## The 12 controls

### NIS2-Art20-1 — Governance and executive responsibility

| Artefact | Pass if | Fail if | Needed for met? |
| --- | --- | --- | --- |
| Named accountable person | A document names a **person** (not a department) | “The IT team” | Required |
| Dated management approval | Minutes / signed policy approval / GF decision with a **date** and an **approver** | Undated draft | Required |

### NIS2-Art20-2 — Management body training

| Artefact | Pass if | Fail if | Needed for met? |
| --- | --- | --- | --- |
| Director attendance | Management-body names + a cyber/NIS2 training **date** | Staff awareness cert, or no date | Required |

This cycle does **not** deliver training. No attendance list → gap, owner, date.

### NIS2-Art21-1 — Risk-management measures

| Artefact | Pass if | Fail if | Needed for met? |
| --- | --- | --- | --- |
| Risk list for **this service** | Spreadsheet/register naming this service, owner on each row | Blank template, or a list that never mentions this service | Required |

You are not judging whether the risks are “appropriate.” You are judging
whether a list exists.

### NIS2-Art21-2-a — Policies on risk analysis and IS security

| Artefact | Pass if | Fail if | Needed for met? |
| --- | --- | --- | --- |
| IS / IT policy | Policy PDF with an **approval date** | Undated draft or a marketing page | Required |

### NIS2-Art23-1 — Notify CSIRT / competent authority

| Artefact | Pass if | Fail if | Needed for met? |
| --- | --- | --- | --- |
| Who notifies which authority | Playbook/one-pager names the **role/person** and **BSI or CSIRT** | “We would figure it out” | Required |

### NIS2-Art23-4 — 24h / 72h times

| Artefact | Pass if | Fail if | Needed for met? |
| --- | --- | --- | --- |
| Times written down | The playbook **literally** says 24 hours (early warning) and 72 hours (notification) | “Notify ASAP” with no hours | Required |

Same PDF as Art23-1 is fine if those hours are on the page.

### NIS2-Art21-2-d — Supply-chain security

| Artefact | Pass if | Fail if | Needed for met? |
| --- | --- | --- | --- |
| Vendors this service depends on | List includes at least production hosting, identity, and DNS/email if used, **marked for this service** | Empty, or a 200-row finance dump with no “critical for this service” mark | Required |

### NIS2-Art22-1 — Coordinated supply-chain assessments

This article is about **ENISA / Cooperation Group** assessments, not the
company’s vendor spreadsheet. Do not “pass” it with Art21-2-d’s list.

| Artefact | Pass if | Fail if | Needed for met? |
| --- | --- | --- | --- |
| Status note | Dated note: they **are** in a named coordinated assessment, **or** they are not and they follow BSI/ENISA advisories | Vendor list offered as this article | Required |

A one-paragraph note they write in the diagnostic is enough for this cycle.

### NIS2-Art21-2-e — Vulnerability handling

| Artefact | Pass if | Fail if | Needed for met? |
| --- | --- | --- | --- |
| How vulns are handled | Named tool or written procedure (Dependabot, scanner, patch policy) | “Developers just update things” | Required |
| Last scan or pentest date | Report/ticket with a **date** | No date → **partial**, never met | For met |

### NIS2-Art21-2-j — Secure ICT / MFA

(The seeded text is MFA and secured communications, not an AI-governance exam.)

| Artefact | Pass if | Fail if | Needed for met? |
| --- | --- | --- | --- |
| Where production runs | Named host: AWS / GCP / Azure / other | “In the cloud” | Required |
| MFA on production admin | Console screenshot or IdP export showing MFA **required** for admin of this service | “We use MFA” with no screenshot | Required |

### NIS2-Art21-2-b — Incident handling and continuity

| Artefact | Pass if | Fail if | Needed for met? |
| --- | --- | --- | --- |
| On-call or incident channel | Rota, PagerDuty/Opsgenie, or named Slack/phone channel | “The developer would notice” | Required |
| Incident-handling notes | Written handling page (may be the Art23 playbook) | Channel only, no notes → **partial** | For met |

### NIS2-Art21-2-c — Backup, recovery, crisis management

| Artefact | Pass if | Fail if | Needed for met? |
| --- | --- | --- | --- |
| Backup exists | Console screenshot or vendor page showing backups for this service | “We have backups” with no screenshot | Required |
| Last restore-test date | Email/ticket/log with the **date a restore was tested** | Backups, no restore date → **partial**, never met | For met |

## What you never do

- Approve because Kerno scored green on a weak PDF.
- Accept “we’re working on it” as met.
- Treat a vendor list as Art22.
- Treat staff awareness as director training.
- Treat “notify ASAP” as 24h/72h.
- Tell them this checklist means they are NIS2 compliant.
