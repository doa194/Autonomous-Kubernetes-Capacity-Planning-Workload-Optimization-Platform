---
name: verify-governed-execution
description: Verify exact human-approved capacity change governance, executor preflight/reconciliation, direct Kubernetes and GitOps ownership boundaries, post-change verification, and bounded recovery under S24-S26. Use for security-critical workflow testing, drift and retry analysis, and mutation safety review.
---

# Verify Governed Execution

## Mandate and trust boundaries

This workflow tests the **only** project path allowed to apply optimized Kubernetes resource changes. Read repository `AGENTS.md` and `CLAUDE.md`, master catalog, S01-S03/S05-S09, S11-S14, combined Domain E S19-S23 (including numerical companion), consolidated Domain F S24-S26, and S27-S32 as applicable. Inspect code, migrations, RBAC, audit records and the current environment. Do not change accepted ADRs or expand mutation scope.

**Never apply a real change merely because the user asked to verify a skill.** Default to unit/component tests, simulated external API adapters and disposable isolated kind test resources. For live optimized mutations, require an independently verified current **S24 exact human approval** and authorized S25 execution path. Never use this skill, CLI shortcuts, a human-admin kubeconfig, S05 runner, Git merge, or AI agent to bypass S24.

## Immutable object boundaries

`ResourceCandidate != OptimizationEvaluation != ExecutionPlan != ApprovalRecord != ExecutionIntent != AppliedChange != VerifiedOutcome`.

- **S24 Governance:** owns immutable plan revisions, exact canonical action+targets+before/after+cluster/UID+policy+evidence+adapter+preconditions+recovery envelope; current authorization and different human author/approver; mode Governed and risk gates; plan approval has time validity and revocation semantics.
- **S25 Executor:** the sole restricted `capacity-executor-sa` optimized mutator; independently verifies durable approval, policy, live target identity/spec and permitted ownership; no AI mutation or experiment authority. Uses PostgreSQL durable jobs/outbox with at-least-once semantics, not an implied atomic SQL+Kubernetes transaction.
- **S26 Verification:** distinguishes configuration observed/rollout progress from qualified operational safety and realized benefit; controls *preauthorized bounded inverse* recovery and escalation. Rollback is another mutation and must check current resource state.

P0 applies to **one allowlisted S04 demo controller at a time** with only exact authorized container CPU/memory request/limit fields. No patching HPA, replicas, scheduling affinity, priority, nodes, Pod eviction, secrets/RBAC, infrastructure, arbitrary YAML or unsupported Git-managed targets.

## Ordered verification procedure

1. Establish environment scope, authorization and protected target. Record cluster incarnation, controller UID, container identity, namespace, desired actual field values, active mode, policy version, actor roles/scopes and configured GitOps ownership. Verify no active S05 exclusive experiment conflict or host-pressure stop condition.
2. Walk the **S19→S22→S24** chain: policy hard gates, qualified candidate/evaluation, immutable plan canonicalization/digest, exact evidence refs and constraints. `unknown` required hard gate cannot become approved.
3. Verify S24 plan lifecycle and legal state transitions. Ensure editing any field generates a new revision requiring new human authorization; test stale/expired/revoked approval; one approver independent from author; no implicit Administrator bypass; no agent or service principal creates human approval.
4. Verify secure operator request intake (S06 scoped actions and separation), persistence/transactional outbox events, least-privileged writer boundaries, idempotency, at-least-once retry/lease fencing, actor attribution without durable human bearer token.
5. Inspect S25 **ordered fresh live preflight**: current approval/policy/actor/mode, target UID/namespace/container, current live spec and ownership, resourceVersion or equivalent compare-and-set, supported patch fields, configured Git/direct ownership, capacity/SLO and host pressure. Historical S11 snapshot alone is not a current precondition.
6. Confirm the selected adapter: **direct Kubernetes API is primary P0**; GitOps adapter only if explicitly implemented and ownership qualified. A Git-managed target must not be silently directly patched; a Git commit/Flux reconciliation is not S24 approval or proof of effect.
7. Test the external-effect journal: intent-before-write; safe fencing; retry after unknown Kubernetes response; read-after-timeout before reissue; duplicated outbox delivery; conflict; rollout changes; Pod restart; executor crash between API write and SQL status. Never claim exactly-once external effects.
8. Check S26 phases separately: observed applied field/configuration, rollout/availability, stabilization, qualified post-change telemetry and workload-specific SLO, protected evidence, `verified_safe` vs `benefit_verified` vs `verification_inconclusive` vs escalation.
9. Evaluate bounded preauthorized inverse recovery only when live controller/field/ownership and recovery envelope still match. In any ambiguous case **suspend/escalate**, do not blindly restore a baseline or delete a user resource.
10. Test source ownership: only Governance writes plan/approval state, only Executor owns operation state, S26 owns outcome adjudication as specified; audit append/integrity protected; AI advisory and S05 experiment authority cannot forge them.
11. Run targeted negative, concurrency and restart cases in isolated disposable lab. Keep E2E few and high-value. Verify observed results via read-back and audit evidence; carefully clean up test-owned resources by exact UID and preserve approved changes.

## Essential failure/abuse matrix

- Observe/Advisory mode attempted mutation; missing/expired approval; approver == plan author; unauthorized namespace; token revoked after issuance; changed policy or evidence; resource UID drift; changed resourceVersion/spec; unsupported mutation field; ownership ambiguity; nonqualifying forecast/SLO; unknown hard gate; duplicate outbox; expired lease; timeout-after-apply; concurrent executor; GitOps divergence; experiment overlap; model tries to approve; misleading `applied` as `benefit_verified`; stale rollback baseline; verification evidence missing.

For each case inspect **authoritative current state**, intended command, durable state transition, Kubernetes/Git external effect (or explicit absence), audit correlation, and recovery/escalation result. Do not rely on a successful API response alone.

## Output and stop conditions

Report a **governance/execution state-transition verification matrix** with preconditions, attempted operation, authoritative result, measured effect, evidence refs, and fail-closed verdict. Include whether any real mutation occurred and under which verified approval, separately list pending verification/inconclusive outcomes, and attach exact cleanup/recovery status. If authorization, fresh state, policy, evidence or ownership cannot be confirmed: **do not mutate; report blocked and required operator action**. No self-granted approvals, no AI actions, no silent bypass.
