# Documentation

This folder describes the finished platform: what it is, how it is built, how to run it, how it
is secured and tested, and what has been proven on the reference host. The pages are grouped by
the question you are trying to answer. Each page states what is implemented now and links to
deeper pages instead of repeating them.

## Suggested reading paths

| You are… | Read in this order |
|---|---|
| **New to the project** | [how-it-works](how-it-works.md) → [glossary](glossary.md) → [architecture](architecture.md) → [proof-of-operation](proof-of-operation.md) |
| **Installing the lab** | [setup-guide](setup-guide.md) → [cli-reference](cli-reference.md) → [operator-guide](operator-guide.md) → [troubleshooting](troubleshooting.md) |
| **Reviewing the design** | [architecture](architecture.md) → [design-decisions](design-decisions.md) → [cluster-topology](cluster-topology.md) → [data-model](data-model.md) |
| **Reviewing the decision logic** | [telemetry](telemetry.md) → [forecasting](forecasting.md) → [optimization-engine](optimization-engine.md) → [governance-and-execution](governance-and-execution.md) |
| **Reviewing security** | [security](security.md) → [identity-and-access](identity-and-access.md) → [network-isolation](network-isolation.md) → [secrets-and-pki](secrets-and-pki.md) → [ai-advisory](ai-advisory.md) |
| **Checking the evidence** | [proof-of-operation](proof-of-operation.md) → [testing-strategy](testing-strategy.md) → [test-catalog](test-catalog.md) → [evaluation](evaluation.md) |
| **Changing the code** | [codebase-guide](codebase-guide.md) → [configuration-reference](configuration-reference.md) → [test-catalog](test-catalog.md) |

## What is it, and is it real?

| Page | Answers |
|---|---|
| [how-it-works.md](how-it-works.md) | What does the platform do, in plain language, followed through one real change? |
| [proof-of-operation.md](proof-of-operation.md) | What has been observed running, with which evidence, and how can I check it myself? |
| [glossary.md](glossary.md) | What do the terms used in these pages mean? |

## How is it built?

| Page | Answers |
|---|---|
| [architecture.md](architecture.md) | Which components exist, where do they run, how do they talk, where does authority live? |
| [design-decisions.md](design-decisions.md) | Why was each major approach chosen, what else was considered, what does it cost? |
| [cluster-topology.md](cluster-topology.md) | How is the Kubernetes cluster laid out: nodes, taints, namespaces, priorities, quotas, admission? |
| [data-model.md](data-model.md) | What is stored, who may read or write it, and how is history protected? |
| [workflow-and-jobs.md](workflow-and-jobs.md) | How does work move between services without a message broker? |
| [codebase-guide.md](codebase-guide.md) | Where does each responsibility live in the repository? |

## How does it decide?

| Page | Answers |
|---|---|
| [telemetry.md](telemetry.md) | Which metrics are read, how, and how is their quality judged? |
| [forecasting.md](forecasting.md) | How is future demand predicted, and when does the platform refuse to predict? |
| [optimization-engine.md](optimization-engine.md) | How are candidate changes generated, gated, simulated and ranked? |
| [governance-and-execution.md](governance-and-execution.md) | How does a candidate become an approved change, and how is it applied, verified and undone? |
| [ai-advisory.md](ai-advisory.md) | What does the optional AI layer do, and what is it prevented from doing? |

## How is it secured?

| Page | Answers |
|---|---|
| [security.md](security.md) | What must never happen, which control prevents it, and how is that verified? |
| [identity-and-access.md](identity-and-access.md) | How do people and services prove who they are, and what may each role do? |
| [network-isolation.md](network-isolation.md) | Which network flows are allowed, and how is enforcement proven? |
| [secrets-and-pki.md](secrets-and-pki.md) | Where do secrets and certificates come from, and how are they delivered and rotated? |

## How do I install and use it?

| Page | Answers |
|---|---|
| [setup-guide.md](setup-guide.md) | How do I build the lab from scratch, verify it, pause it and remove it? |
| [operator-guide.md](operator-guide.md) | How do the different roles take a change from analysis to verified outcome? |
| [cli-reference.md](cli-reference.md) | What does every command of `capacity-lab`, `capacityctl`, `capacity-scenario` and `capacity-watchdog` do? |
| [api-reference.md](api-reference.md) | Which REST endpoints exist, which role does each need, and what do errors look like? |
| [configuration-reference.md](configuration-reference.md) | Which configuration files, policy values and environment variables exist? |
| [workloads-and-experiments.md](workloads-and-experiments.md) | Which demo workloads exist, and how are reproducible load experiments run safely? |

## How do I operate it?

| Page | Answers |
|---|---|
| [observability.md](observability.md) | What is monitored, where are dashboards and logs, how do I check health? |
| [backup-and-recovery.md](backup-and-recovery.md) | How are backups made and proven, and what do I do after a failure? |
| [troubleshooting.md](troubleshooting.md) | What problems occur on the reference host, and how are they fixed? |
| [release-and-upgrade.md](release-and-upgrade.md) | How are versions pinned, new builds rolled out and releases qualified? |

## How is it tested and evaluated?

| Page | Answers |
|---|---|
| [testing-strategy.md](testing-strategy.md) | Which test layers exist, why, and how do I run them? |
| [test-catalog.md](test-catalog.md) | What does each test file and test protect? |
| [evaluation.md](evaluation.md) | What does the evaluation protocol require, and which results have been measured? |
| [spec-traceability.md](spec-traceability.md) | Where is each specification slice implemented and verified, and where does the build deviate? |

## What are its limits?

| Page | Answers |
|---|---|
| [limitations.md](limitations.md) | What does the platform not do, or not do well yet? |
| [production-considerations.md](production-considerations.md) | What is simplified for the lab, and what would production need? |

## Conventions used in these pages

* Commands are PowerShell and assume the project's virtual environment is active
  (`.venv\Scripts\Activate.ps1`); otherwise prefix them with `.venv\Scripts\`.
* `~/.capacity-lab` means the lab's host state folder in your Windows profile
  (`%USERPROFILE%\.capacity-lab`).
* Times are UTC. "Provisional" marks a threshold taken from the specification that has not been
  calibrated by repeated experiments.
* Section references such as "S14" point to the accepted specification slices in
  `specs/accepted/`; [spec-traceability.md](spec-traceability.md) maps them to code.
