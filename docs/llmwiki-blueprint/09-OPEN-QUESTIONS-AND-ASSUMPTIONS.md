# Open questions and assumptions

Open questions are bounded execution tasks. They are not permission to redesign silently. Defaults below allow specification work to complete while preventing unsafe activation.

| ID | Question/evidence gap | Default decision | Owner and exact resolution | Blocking point |
|---|---|---|---|---|
| Q01 | Which LLMWiki did the user mean? No URL supplied | Treat term as pattern; examine multiple implementations; adopt no external code | Claude records any later user-supplied reference and assesses differences against 02 | Not blocking core slice |
| Q02 | Is deployed runtime identical to checkout? Not inspected exhaustively | Checkout evidence only | WP-09 hashes deployed package/runner against release artifact without reading secrets | Live acceptance |
| Q03 | `mem` shim Python failed in this environment | Use an existing valid Python for isolated tests; no global repair | Claude verifies configured executable in fresh normal shell and corrects only through an authorized runtime deployment | Live CLI pilot |
| Q04 | Herdr server rejected discovery from this session | No claimed live team verification | Claude uses an authorized terminal/session and verifies actual pane identities; do not restart server to bypass access rules | Live Herdr handover |
| Q05 | Current QMD version/models and scoped query behavior | Optional discovery backend; local fallback mandatory | WP-00 records version/hash and tests repeated collection flags and JSON shape on synthetic index | Semantic gate and real retrieval measurements |
| Q06 | Real extraction precision and cost | No benefit claimed; all model claims reported | WP-05 synthetic real-model experiment, blind gold evaluation, observed usage | Adoption and model-enabled pilot |
| Q07 | Tokenizer availability and target model | Offline byte-budget fallback is honest but conservative | WP-00 pins a supported tokenizer for evaluation; caller selects exact tokenizer when available | Exact-token performance claims |
| Q08 | Existing vault material decision authority | Preserve operator acceptance requirement | Claude reviews applicable vault AGENTS before active mode; record approval artifacts for material decisions | Acceptance workflow |
| Q09 | Human edits inside managed state are currently allowed | Version 1 never writes the overview, so edits there are untouched; generated-page edits are preserved and offered as candidates | WP-07 tests generated-page hash drift; the deferred takeover must revisit this with the user | Deferred overview takeover only |
| Q10 | Real scale, source distribution, retention need | Synthetic scale tests; no corpus scan beyond approved project | Pilot counts only, no raw private records; calibrate size limits and freshness review intervals | Broad rollout |
| Q11 | Provider transcript formats may have changed | Current adapters retained; compatibility not assumed from old README | WP-09 safely capture one real session per agent and inspect format-specific success; fix only with synthetic reproducer | Provider acceptance |
| Q12 | HX promotion is documented but absent in source | No direct HX integration dependency | Correct HX documentation in a separate authorized task or design WP-10 explicit promotion | Optional enhancement only |
| Q13 | Exact claim acceptance authentication under shared user account | Team/tool policy boundary; CLI reference is audit, not auth | If hostile local agents are in scope, design authenticated external approval before activation | Security scope expansion |
| Q14 | How much manual review is acceptable? | No auto-acceptance; K0 must show value with zero review (11 X1); pilot measures review burden | WP-05 development pilot estimates; WP-09 observes 20-session workload and gets explicit operational acceptance | G8 rollout |
| Q15 | Recoverability after complete DB+backup loss | Cannot reconstruct approvals from Markdown | Restore drill proves available recovery; disaster runbook imports prose as reported only | Release recovery gate |
| Q16 | Should derived knowledge enter the vault's Git history? | Resolved 2026-10-21: the vault is local only with no remote, so pages may be committed locally; DB, backups, receipts, and previews stay ignored | Resolved by user | None |
| Q17 | Do provider transcripts mark pasted user content? | Apply the 04 heuristics to unmarked providers | WP-00 inspects one synthetic and one approved real transcript per provider and records the result | user_stated derivation per provider |
| Q18 | Which projects and questions form set R? | Three user-approved model_allowed projects with at least 15 sessions | Claude proposes candidates from session counts; user approves; questions written before K runs | WP-05 adoption |
| Q19 | Which fragment projects belong to which parent? | Merge only evidence-based pairs (worktree linkage or shared remote); list guesses for the user | User approves the WP-S1 plan item by item | WP-S1 apply |
| Q21 | Are the A-5 attribution weights right? | Provisional weights in WP-01; content must double the runner-up to beat cwd | Calibrate on the WP-S1 re-scoring of past captures before applying overrides: report agreement with the user's judgment on a sample of 30 | Real-vault overrides |
| Q20 | Should the existing vault be reorganized beyond hygiene, for example a new folder taxonomy? | No; PARA stays and the sweep only fixes boundaries and leftovers | User decides; a taxonomy change would need its own plan | Not blocking |

## Settled assumptions

The initial target is a single-user local Windows installation with a shared Obsidian vault and multiple cooperating agents. It is not a multi-tenant service or a distributed database. Live SQLite remains on local disk. The existing PARA hierarchy remains. The project structure changes through a small internal knowledge package and project-local generated pages.

Hooks remain consistent in both protocol and operational role. The specification may improve content at the same overview path, but it does not expand SessionStart context or replace provider events. Existing `mem search`, `note`, `sessions`, `projects`, `remember`, and capture commands keep their caller contracts.

An assertion captured from a session is evidence of that assertion. It is not independently verified merely because an agent used confident wording. Source counts are not corroboration unless origins are independent. A deterministic command record proves invocation metadata, not a passing result.

Privacy policy is project-specific. Existing regex filtering remains useful but is not sufficient proof of anonymization. Excluded projects contribute no structured content. Local-only projects can produce deterministic reported knowledge but send no content to models.

## Deferred scope

No global concept graph, automated entity merging, raw transcript mirror, vector service, full-text index replacement, web publishing, authenticated multi-user approval service, scheduled autonomous rewriting, or automatic deletion. No migration of the separate Codex memory folder. No changes to Herdr/HX lease, routing, or quota state. These require separate hypotheses and acceptance cases.

## Resolution format

For each resolved question, append date, evidence, observed result, chosen decision, affected contract/task IDs, and reviewer to the decision log. If a provisional threshold changes, record the calibration partition used and confirm sealed fixtures were not consulted. If evidence contradicts an earlier assumption, update the normative contract and its tests together.
