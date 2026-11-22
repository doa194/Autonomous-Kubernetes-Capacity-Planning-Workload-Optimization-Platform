
---
name: implement-architecture-slice
description: Implement or modify a slice of the Autonomous Kubernetes Capacity Planning Platform using its accepted specifications, ADRs, dependencies and acceptance criteria.
---

# Architecture Slice Implementation

1. Read AGENTS.md and CLAUDE.md.
2. Identify the requested slice ID and its owning specification.
3. Read the relevant accepted upstream and companion contracts.
4. Inspect the existing repository implementation.
5. Extract the functional, security, data, API and operational requirements.
6. Identify dependencies and conflicting or missing implementation decisions.
7. Preserve all accepted architectural invariants.
8. Implement a coherent, testable increment.
9. Run the cheapest reliable tests for changed behavior.
10. Verify important integrations and security boundaries.
11. Report implemented behavior, verification results, blockers and unverified capabilities.

Never:
- Silently override an accepted ADR.
- Add a seventh custom persistent platform Deployment.
- Give agents Kubernetes mutation authority.
- Treat missing evidence as proof of safety.
- Claim implementation, verification or release qualification without evidence.
- Change approved execution or security authority boundaries merely to simplify development.

Follow the repository's testing and documentation rules.
