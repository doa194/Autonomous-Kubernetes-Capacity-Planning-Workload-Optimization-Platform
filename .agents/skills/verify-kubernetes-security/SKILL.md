---
name: verify-kubernetes-security
description: Verify the project's Kubernetes, identity, network, secrets, admission, AI-tool and supply-chain security controls against accepted S06-S10 specifications. Use for security audits, deployment hardening, negative tests, and security release evidence.
---

# Verify Kubernetes Security

## Mission and authority

Verify **implemented controls and actual denial behavior**, not just YAML presence. Read repository `AGENTS.md` and `CLAUDE.md`, master catalog, the requested slice, and relevant accepted S06-S10, S24-S25, S29-S30 documents under `specs/accepted/` before changing a security boundary. When available, also inspect deployment manifests, policy bundles, and existing test harnesses. Specifications outrank this skill. If a referenced source is missing, report the gap rather than inventing its contents.

Security verification never grants permission to make a production-like, security-sensitive configuration change. Use a disposable kind lab or expressly approved test scope for mutation/adversarial tests. Never weaken a control simply to make a test pass. Capture redacted evidence, not secrets.

## Boundary inventory

1. **Human auth (S06):** Keycloak OIDC, issuer/audience/JWKS validation, CLI PKCE, role+action+namespace/resource+condition checks, durable attribution without stored user tokens, distinct author and approver, Administrator not an automatic bypass.
2. **Workload identity (S07):** dedicated SAs, projected audience-bound short-lived credentials only where needed, Observer narrowly read/list/watch, Executor narrowly read/mutate, `api`/`capacity`/`ai`/demo/driver no Kubernetes mutation token. Verify host scenario runner and watchdog have separate experiment-only credentials.
3. **Network and transport (S07-S08):** Calico default-deny ingress/egress, explicit per-path allowlists, authenticated TLS and selective mTLS, correct certificate names/trust/EKU, and tested host-to-Ollama proxy allowlist and bypass denial. NetworkPolicy alone is not authentication.
4. **Secrets/PKI/data (S08):** Vault + ESO, offline-protected root CA and cert-manager-managed intermediate, differentiated deliveries and rotations, encrypted independent backups, trust restoration, classified data redaction, protected tamper-evident audit. Never include raw Secret data, credentials, keys, tokens, or `Restricted` content in reports or prompts.
5. **Admission (S08):** Pod Security Admission Restricted plus native CEL ValidatingAdmissionPolicy/Bindings, allowlisted exception registry, bounded experiment Jobs, service-token mounting and privilege rules. Assert server-side denial even if Python/client-side validation is bypassed.
6. **AI security (S09, S15-S18):** three logical agents in `ai`; in-process deterministic Tool Authorization Broker plus **independent backend** authorization, trusted role/delegation binding, fixed typed tools, prompt/RAG injection resistance, authorization prefilter before retrieval, redaction before embedding, scoped citations and host Ollama gateway. AI never gains experiment control, approval, direct `kubectl`, SQL, arbitrary PromQL, or mutation tools.
7. **Supply chain (S10, S29):** **P1** lightweight OCI Distribution Registry; pinned builds and dependency locks, Syft SBOM, Trivy scanning, Cosign key signing and provenance, Sigstore Policy Controller verification, PSA/CEL as separate layers, periodic conformance/drift assessment. Do not report P1 controls as installed if only specified.

## Execution procedure

1. Determine the exact target environment, namespaces, resources and authorized test scope. Record cluster incarnation, installed versions and enabled feature/capability flags. Use no default wildcard cluster-admin identity.
2. Map each applicable requirement/ADR to concrete implementation, enforcement point, test, and expected denial. Distinguish `implemented`, `configured`, `observed_verified`, `not_tested`, `blocked`, `not_applicable`, `P1_deferred`.
3. Perform static review of manifests/code/config. Check role/verb/resource/name scope, automount, security contexts, privileged exceptions, issuer/audience, certificate issuance, secret lifecycle, NetworkPolicy selector completeness, admitted/denied image criteria, and source-to-agent disclosure paths.
4. Choose the cheapest reliable test layer in `CLAUDE.md`: pure policy unit tests; API/component auth tests; integration for real tokens/storage; isolated kind operational tests for actual network/admission/RBAC; few E2E paths.
5. Run **positive and negative cases** with least-privileged test identities: authorized access succeeds, all forbidden roles/scopes/actions fail. Verify denial via API/server, not solely mock or command return code.
6. Probe relevant failure modes: revoked/expired token; changed grants; spoofed user headers; wrong SA audience; Pod deletion/rebinding; forbidden cross-namespace query; default-deny both directions; TLS certificate mismatch/expiry; Vault/ESO interruption; insecure Pod/Job admission; poisoned RAG; model-generated tool ID; bypass of broker/backend; direct Ollama exposure; unsigned/mismatched image when P1 active.
7. Inspect actual Kubernetes events, audit evidence, API responses and network requests with safe redaction. Avoid logging bearer tokens and Secret values. Confirm no security test contaminates S05/S31 measurements or modifies governed S25 resources.
8. Undo **only** resources created by the authorized test using exact identity/UID; do not broad-delete workloads or roll back independently approved changes.
9. Rerun the most relevant checks after a repair and record failures, scope and residual risks.

## Non-negotiable safety and truthful interpretation

- A manifest validated offline is **not** proof that Calico, PSA, CEL or Sigstore admission actually enforces it.
- An authenticated service identity does not automatically authorize application actions; backend authorization must independently check current role and scope.
- The S09 broker is an in-process reference monitor, not sandboxing against full `ai` process compromise; account for independent backend and K8s/DB permissions.
- Security evidence never overrides S24 human approval, and signature/admission approval never becomes optimization execution permission.
- Kind worker nodes share one Windows physical host; do not claim host-level isolation or HA.
- Do not install a service mesh, Kyverno, Falco, Harbor, external identity service, or new persistent custom Deployment merely to satisfy this skill; changes require an approved ADR.

## Deliverable in the agent response

Provide a **security verification matrix** (requirement/source, control, enforcement point, test, observed result, evidence ref, verdict), a concise threat/negative-test summary, any reproducible commands executed, unverified/blocked items, remediation suggestions that preserve ADRs, and a clear statement of what was *not* demonstrated. Do not create progress-oriented files under `/docs`; follow `CLAUDE.md` for finalized project documentation.
