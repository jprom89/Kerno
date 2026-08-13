# How to run the NIS2 Operating Cycle if you are not a GRC person

This is the job manual for the offer in `kerno_nis2_operating_cycle.md`.
It is for the founder delivering the diagnostic and the monthly review.
It is not a CISO textbook and it is not customer-facing.

The job in one sentence: you make sure a named file exists in Kerno, it is
current, and a named human decided something this month. Kerno scores.
The customer owns the service. You chair.

A NIS2 control is one legal requirement. Example: **Backup, Recovery and
Crisis Management** means “can you restore this service, and have you
tested that?” You do not need to memorise the articles. Open
**Dashboard → Meeting** — each red/amber card says what the control is
and the exact question to ask.

---

## 0. Who you are in the room

You are not their CISO, lawyer, or incident commander.

You **are**:

- The person who gets the tenant live
- The person who chases documents
- The person who links those documents to the 10–20 in-scope controls
- The person who puts Kerno’s scores on a one-page pack
- The person who runs a 90-minute meeting and records the decision

You **never**:

- Say they are NIS2 compliant
- Say NIS2 does or does not apply
- Tell them to “accept a gap” as if that waived a legal duty
- Take an incident, call BSI, or write a legal notice
- Invent a score because the PDF “looks fine”
- Keep their signed originals as your system of record

If a GC asks a legal question: “That is for your counsel. I can record the
treatment you choose.” Then stop talking.

You need **one customer-side operator** who can find files: usually Head of
IT, a platform engineer, or an office manager who knows where the policies
live. Without that person the diagnostic fails. It is not your job to rummage
their Google Drive.

First paying client should be a mid-market tech company you already know,
with one product as the critical service. Do not start with a hospital,
energy operator, or a hostile procurement process.

---

## 1. Dry-run on your own tenant before anyone pays

Do this once, end to end, so the first diagnostic is not your first time
clicking the product.

1. Log in as `compliance_lead` or `vciso`.
2. Open **Dashboard** — coverage will be mostly gap. That is expected.
3. Open **Evidence**, upload a dummy policy PDF and a dummy vendor list.
4. Link each upload to two controls. Put in the note: “Original: dummy,
   not a real customer file.”
5. Open **Recommendations**, generate for one control (or use the generate
   API if the UI button is not there yet), then Approve or Edit as a human.
6. Export an evidence pack for one category (for example `governance`).
7. Write a one-page pack from the coverage view: control, status, evidence
   or gap, owner = you.

If any of those steps fail, fix the product before you sell a diagnostic.
You cannot deliver this job on slides.

The seeded NIS2 catalogue is **12 controls**. That is your default in-scope
list. You do not need ISO 27001 Annex A. You do not need to invent controls.

| Ref | Title | Plain-English ask |
| --- | --- | --- |
| NIS2-Art20-1 | Governance and executive responsibility | Who is accountable? Is there a signed policy / GF decision? |
| NIS2-Art20-2 | Management training | Out of the *training delivery* offer. Still a control: attendance record or explicit gap. |
| NIS2-Art21-1 | Risk-management measures | Is there a list of what could go wrong for this service? |
| NIS2-Art21-2-a | Policies on risk analysis and IS security | Information-security or IT policy PDF. |
| NIS2-Art23-1 | Significant incident notification to CSIRT | Written playbook: who notifies whom. |
| NIS2-Art23-4 | Notification timeline / early warning | Same playbook, with 24h / 72h times, or a gap. |
| NIS2-Art21-2-d | Supply-chain security | Vendors that this service depends on. |
| NIS2-Art22-1 | Coordinated supply-chain assessments | How those vendors were reviewed, even a spreadsheet. |
| NIS2-Art21-2-e | Vulnerability handling | How they patch; last pentest or “we don’t have one.” |
| NIS2-Art21-2-j | Secure ICT solutions | Where it runs (cloud, MFA on admin, who has root). |
| NIS2-Art21-2-b | Incident handling and continuity | On-call rota, incident channel, last drill or gap. |
| NIS2-Art21-2-c | Backup, recovery, crisis management | Backup screenshot, last restore test date or gap. |

For a first diagnostic, freeze **all 12**. If they insist on fewer, drop
Art20-2 and Art22-1. Do not add DORA / CRA / AI Act rows unless the buyer
is actually in that regime.

---

## 2. What you need from them before day 1

Written, before the clock starts:

- Legal entity name (the company, not the product brand)
- One critical service in one sentence: “The SaaS app paying customers log
  into” is enough
- Named sponsor who can decide in the readout (GF, MD, or delegated IT lead)
- Named operator who will send files (not the GF)
- Their SharePoint / Drive folder for **originals** (you do not become the
  archive)
- Confirmation they have an incident process *outside* this engagement, even
  if it is “call the MD and our lawyer”

If they cannot name a service and an operator, do not start.

---

## 3. The ten working days

Elapsed time is two calendar weeks. Working days below assume they answer.
If they miss the 3-working-day evidence deadline, **pause the clock** and
say so in writing. Do not eat the overrun yourself.

### Day 1 — Kickoff (90 minutes)

Agenda, in this order:

1. Read the non-sufficiency sentence out loud. They are buying a file for
   one service, not a compliance certificate.
2. Write the critical service on a slide. Get “yes, that one.”
3. Show the 12-control table. Freeze it in the meeting notes.
4. Fill an owner column: one name per control. The same person can own
   several. You need names, not departments.
5. Book the readout (day 10) and the first monthly review (day 30) **now**.
6. Send the evidence request the same afternoon.

You are done when: service, 12 controls, owners, two dates, and an email
with the shopping list are in writing.

### Days 2–5 — Collect and link

Your job is nagging and filing, not reading 80-page policies.

For each document they send:

1. They keep the original in their folder.
2. You (or they) upload to **Dashboard → Evidence**.
3. Link it to the relevant control(s). Relevance: if you are unsure, use
   the default and say so in the note.
4. In the link note write: original filename, where it lives (URL or
   folder path), who owns it, today’s date.

Shopping list to paste into the day-1 email:

```
Please send, for [SERVICE NAME], by [DATE]:

1. Org chart or note naming who is accountable for cybersecurity
2. Latest information-security / IT policy (PDF is fine)
3. Any risk register, even a spreadsheet, for this service
4. Vendor / hosting list this service depends on (cloud, IdP, payments, DNS)
5. Incident playbook or on-call notes, including who would notify a CSIRT
6. How you patch / handle vulnerabilities (tool name, last pentest if any)
7. Where production runs, and whether admin access uses MFA
8. Backup setup and the date of the last restore test
9. Last management / GF discussion of cyber (minutes, email, or “never”)
10. Training / briefing attendance for directors, or “none yet”

Keep the originals in your folder. We will store extracted text in Kerno
and note where the original lives.
```

A missing document is not a failure. It is a **gap** with an owner. That is
the product.

Stop collecting at 40 items. If they dump a wiki, pick the 40 that map to
the 12 controls. Say no to “while you’re here, also ISO.”

### Days 6–8 — Score and human-review

1. Open **Dashboard**. You should see some met / partial, not a wall of gap.
2. For each of the 12 controls, generate a recommendation (dashboard
   generate action, or `POST /api/v1/recommendations/generate` with
   `control_id`). Kerno sets status and confidence. You do not.
3. Sit with their operator for 60–90 minutes. For each recommendation:
   - If the evidence is obviously the right document, **Approve**.
   - If they say “that PDF is obsolete, use this one,” upload the new one
     and **Edit** with a justification.
   - If they say “we don’t do this,” leave it gap and put an owner + date
     on the register. Do not click Approve to be nice.
4. One review round. Not three.

You are not judging whether their MFA is “enough.” You are judging whether
the file they handed you is linked to the control Kerno scored.

### Days 9–10 — Pack and readout

The decision pack is **one to two pages**, not a report:

| Control | Kerno status | Evidence or gap | Owner | Ask for the readout |
| --- | --- | --- | --- | --- |
| NIS2-Art21-2-c | gap | no restore test | Ada | Set a restore-test date |
| … | … | … | … | … |

Plus: coverage screenshot, and one evidence-pack export for the category
with the most gaps.

Readout agenda (90 minutes):

1. Two minutes: this is not a compliance certificate.
2. Walk only reds and ambers. Skip greens unless they ask.
3. Pick **one** decision they already owe. Example: “Backup restore will
   be tested by 30 September, Ada owns it, weekly snapshots stay as the
   interim safeguard.”
4. They say yes / no / different date. You record it in Kerno as the
   named human decision (they click, or you click while they watch).
5. Confirm monthly date, owners, and whether they want the six-month cycle.
6. If they stop: send the export within the DPA rules. Do not leave them
   thinking they still have a licence.

---

## 4. The monthly 90 minutes

Open **Dashboard → Meeting** before the call. That page is the agenda: it
pulls live coverage, linked evidence, and open recommendations, and writes
the exact question to ask for every red and amber control. Share the
screen. Do not rebuild a Google Doc.

Between meetings you send **one** reminder email for open items, then you
stop. Missed customer deadlines stay on the register. They are not your
overrun.

Agenda (already on the Meeting page):

1. Share **Dashboard → Meeting**. Read the preamble once.
2. Walk **Decisions needed today** only. For each card, read “What this is”
   then the **Ask**. Write their answer on the card (owner, date).
3. After they speak, record the decision on **Recommendations** (Approve /
   Edit / leave as gap).
4. If they are in an incident, end the meeting. That is their process.
5. Confirm next date. Export only if an auditor or customer is asking.

After the call: update the register the same day. Same table as the pack.
That table *is* the gap and exception register. Kerno holds the scores and
decisions; the table holds owner, age, and due date. Keep it in the tenant’s
workspace, not in a private Notion only you can see.

---

## 5. Phrases that keep you honest

Use:

- “Kerno scored this control gap because nothing is linked.”
- “You own the treatment. I will record owner, date, and interim safeguard.”
- “That is an incident. You activate your process. We are not that function.”
- “This file is for [service], 12 controls. It is not your NIS2 programme.”

Do not use:

- “You’re in good shape for NIS2.”
- “I think this is enough for BSI.”
- “Just accept the risk.”
- “We’ll pick it up at the next monthly if something blows up.”

---

## 6. When you are out of your depth

Stop and say so if:

- They want you to write the incident playbook, the ISMS, or a legal
  opinion
- The critical service is safety-critical (hospital, energy, industrial
  control) and you have no domain partner
- A GC is asking you to attest sufficiency
- They want 80 controls or three entities “in the same fee”

Then either: walk, or hire a real vCISO for two days of the diagnostic as
a named subcontractor — and tell the customer that person is the judgment
layer for those two days. Do not fake it.

You can deliver the first two cycles on process plus the product. You
cannot deliver a regulated-entity CISO function. The offer was written so
you would not have to.

---

## 7. Checklist you can print

**Before kickoff**

- [ ] Dry-run on your tenant: upload, link, generate, approve, export
- [ ] Customer tenant live, you as `vciso` or `compliance_lead`
- [ ] Their operator has a login that can upload
- [ ] Originals folder exists on *their* side
- [ ] Kickoff, readout, and first monthly are in the calendar

**Diagnostic done when**

- [ ] 12 controls frozen in writing
- [ ] Each has an owner
- [ ] Evidence linked or an explicit gap
- [ ] Recommendations human-reviewed once
- [ ] One named decision recorded in Kerno
- [ ] One evidence-pack export exists
- [ ] They have either signed the six months or received the export

**Each month**

- [ ] Open **Dashboard → Meeting** (live agenda, not a homemade doc)
- [ ] One reminder sent
- [ ] 90-minute review held from that screen
- [ ] Record decisions in Recommendations after they speak
- [ ] No incident handled by you
