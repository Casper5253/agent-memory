# Evidence and specification review record

## Investigation performed

Date 2026-10-21. AgentMem HEAD `03764f8a61e55514c9c50ea82538a898bfae70f2`. HX HEAD `367ab17c129b58aa5310b91da47265ea42f4b4bf`. Source inspection covered all principal AgentMem modules, installer, OpenCode plugin, existing tests/fixtures, README and assessment; HX architecture, checkpoint/context modules and source search for integration calls.

CodeDB outline failed with `SnapshotLoadFailed`; exact reads and ripgrep replaced structural navigation. `herdr agent list` failed with PermissionDenied because the server requires an administrator terminal. No server restart or permission bypass occurred. `mem search` failed with `The system cannot execute the specified program.` The shim points to a WindowsApps Python path. A separately available existing Python was used for isolated tests, without changing the shim or PATH.

The AgentMem project note in the shared vault, dated 2026-10-14, was read directly. Its operational claims were treated as historical leads. The live source test run below supersedes it only for the inspected checkout, not the deployed application. The vault's governance file was read.

The Codex memory registry search found no relevant AgentMem implementation entry. No claim in this package depends on the unrelated registry hit. The shared vault was not edited.

## Observed verification

Working directory was AgentMem. Executed command:

```powershell
& '<existing Python 3.11 virtualenv>/Scripts/python.exe' -B -I -m unittest discover -s tests -v
```

Observed result was exit 0, 30 tests, OK. Tests used existing synthetic fixtures and fake model functions. This is not a performance benchmark and establishes neither live provider integration nor extraction quality. No application files were edited to achieve the result.

Inspected research sources and scope are listed in 02. External code was read through public source pages. No repository was installed or executed. No third-party marketing performance claim is used as a measurement.

## Designed work checklist

- Read the Principles section of poteto-mode. Completed.
- Phase A: Frame. Completed with specification-only scope and immutable hook boundary.
- Phase B: Design the workflow. Completed with archaeology, primary-source research, contracts, risk tests, evaluation, work packages, and independent review.
- Phase C: Run the loop. Apply the loop to specification hypotheses and evidence; application prototyping skipped because the user prohibited implementation.
- Phase D: Keep the audit trail. `decisions.tsv` records consequential choices and evidence.
- Phase E: Verify and hand back. Check document coverage, links, work-package fields, cross-document contracts, and independent review findings before final delivery.

Throughput checkpoint. Main work is one architecture synthesis with bounded source inspection. Read-only native-agent assistance was attempted for integration archaeology but failed with a provider usage-limit error and returned no findings. The primary agent completed that inspection. No other agent CLI was launched. Final independent review is recorded below; a failed reviewer invocation must never be represented as an independent pass.

## Review status

Independent review was completed and its findings corrected as recorded below. Document link, coverage, and artifact-boundary validation are recorded in the delivery checks. No implementation or live deployment was performed.

## Independent review findings and corrections

An independent gpt-6-astra reviewer read all documents 00-10, the decision trail, and relevant AgentMem source at 03764f8. This was a same-family independent review, not a cross-family panel. It found six P1 and five P2 findings. The original draft was not ready. Corrections were made before delivery.

| Finding | Correction |
|---|---|
| Privacy omitted legacy calls | 03/04/07 now require policy before summarize, consolidate, remember, deferred reconciliation, and extraction |
| Editor race backup was insufficient | 03 requires tested native replace-with-backup, actual displaced-byte hash check, explicit partial-error handling, and unsupported-platform refusal |
| Adoption depended on downstream pilot | 06/07 split slice-investment gates from release/pilot gates |
| Packet ordering contradicted post-capture boundary | 03/07 persist capped packet in existing recovery receipt; DB import occurs after capture commit |
| Effective-time shape was incomplete | 04 adds valid_precision and valid_end_state with validation rules |
| Activation lacked writer fence | 03 specifies shared lock, publication recheck, durable transition, old-worker drain, and restart recovery |
| Packet cardinality and locators absent | 04 adds import_sources, span ordinals, locator_json, original/redacted hash distinction |
| Queue/outbox identity incomplete | 04 adds project outbox, versioned dedupe keys, generation-wide attempt bound, and synchronous idempotent apply |
| CLI could not express mutations | 04 adds operation/revision flags and verification request schema |
| Initial evaluation could not exercise QMD | 06 labels initial local slice and requires fresh full-QMD holdout after WP-07 |
| Minimum byte budget too small | 04 raises minimum to 1024 and defines compact invalid-budget error |

The reviewer also confirmed all seven requested phases were represented and every work package supplied the requested fields. It did not run the proposed implementation, repeat research, or measure system improvement. Final correction verification is a document check, not a claim that the future application is proven safe.

The same independent reviewer rechecked all eleven corrections against the revised documents and reported no remaining blockers in those corrections. One minor null-endpoint wording inconsistency was identified and corrected to respect explicit open intervals. This was a bounded final recheck, not a new application audit.

## Delivery checks

Revision 1.0 contained eleven Markdown documents: the ten required deliverables plus this evidence/review record. Revision 1.1 adds 11, the cross-family review. It also contains decisions.tsv. A mechanical check found zero broken relative document links, zero unclosed fenced blocks, eleven work packages, and fifteen numbered execution fields in every package. The drafting helper script remains outside the repository and is not part of the deliverable.

Application behavior was not changed. The live vault, hook configurations, installed runtime, shared skills, and Herdr/HX state were not modified. Documentation is delivered uncommitted for review. No benchmark outcome or live provider acceptance is claimed.

Attention. Independent review used gpt-6-astra. All eleven identified contract findings were corrected and rechecked. Cross-family review was unavailable. Provisional evaluation thresholds and the operational unknowns in 09 remain future execution gates, not promises of improvement.

## Cross-family review

After delivery, Claude (Opus 5.5) reviewed revision 1.0 from a different model family. It found nine issues: X1 critical, X2-X6 high, X7-X9 medium. Revision 1.1 applies them across 00, 01, 03-07, 09, and this file. The findings, the evidence, and what was kept deliberately are in [11](11-CROSS-FAMILY-REVIEW.md). Revision 1.1 has not yet had its own independent recheck; 11 lists the points Codex should attack.

