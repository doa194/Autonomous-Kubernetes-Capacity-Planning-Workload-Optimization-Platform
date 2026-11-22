# Test catalog

Every automated test, grouped by layer and file, with the behaviour it protects. The approach behind
the layers is in [testing-strategy.md](testing-strategy.md); the latest results are in
[proof-of-operation.md](proof-of-operation.md#3-automated-test-results).

| Layer | Files | Functions | junit cases | Latest result (2026-11-21) |
|---|---|---|---|---|
| Unit | 9 | 117 | 156 | 156 passed |
| Security | 2 | 19 | 31 | 31 passed |
| Component / API | 1 | 11 | 21 | 21 passed |
| Integration | 3 | 24 | 31 | 31 passed |
| Operational | 1 | 7 | 13 | 13 passed |
| End-to-end | 1 | 1 | 1 | full chain ran once (recovered); see proof |
| .NET | 1 | 5 | 5 | 5 passed |

---

## Unit tests (`tests/unit`)

### `test_accounting_and_rightsizing.py` — quantities, effective requests, candidate generation

| Test | Protects |
|---|---|
| `test_cpu_quantities_parse_to_exact_millicores` | `250m`, `1`, `0.5`, `1500m` parse to exact integer millicores |
| `test_memory_quantities_distinguish_binary_and_decimal` | `Mi`/`Gi` (binary) vs `G` (decimal) vs plain bytes |
| `test_invalid_or_sub_millicore_cpu_is_rejected` | `0.0001`, `abc`, `-1`, `1.5.2` are rejected, not rounded |
| `test_quantity_rendering_round_trips` | integers render back to Kubernetes quantities without loss |
| `test_effective_request_uses_init_max_and_sidecars` | effective request = max(init containers) vs sum(app + sidecars) + overhead |
| `test_extended_resources_are_flagged_not_dropped` | GPUs and other extended resources are flagged, never silently ignored |
| `test_terminating_pod_still_holds_reservation_and_pending_pod_does_not` | node accounting follows what the scheduler really reserves |
| `test_node_accounting_keeps_negative_residual_visible` | over-committed nodes stay visible (no clipping at zero) |
| `test_under_reserved_workload_gets_increase_candidates` | increases are generated when a workload is under-reserved |
| `test_missing_cpu_evidence_is_a_refusal_not_a_fabricated_candidate` | missing evidence → typed refusal, never invented numbers |
| `test_candidate_count_is_bounded_and_ordering_stable` | at most `maxCandidates`, deterministic order |

### `test_architecture.py` — authority boundaries

| Test | Protects |
|---|---|
| `test_service_package_does_not_cross_authority_boundaries` | AI, API, capacity and governance code never import executor code or the Kubernetes client |
| `test_exactly_six_platform_deployments` | the manifests define exactly the six platform services |

### `test_executor.py` — the executor, verification and recovery

| Test | Protects |
|---|---|
| `test_happy_path_applies_exact_fields_with_preconditions` | the patch changes only the approved fields, preceded by `test` operations |
| `test_t01_forged_intent_digest_causes_no_mutation` | a tampered intent never reaches the cluster |
| `test_t02_recreated_controller_with_same_name_is_refused` | a Deployment recreated with the same name (new UID) is refused |
| `test_t03_concurrent_edit_between_get_and_patch_never_overwrites` | a concurrent edit makes the `test` operations fail — no lost update |
| `test_t05_active_hpa_refuses_cpu_request_change` | no CPU-request change on an HPA target |
| `test_t06_gitops_owned_target_is_never_direct_patched` | GitOps-owned targets are never patched directly |
| `test_t09_lost_response_after_successful_patch_reconciles_without_duplicate` | a timeout after success is reconciled by reading the live object, without a second patch |
| `test_t11_crash_after_patch_before_journal_ack_is_reconciled_not_repatched` | a crash between patch and journal is reconciled, not re-applied |
| `test_t10_crash_after_intent_before_patch_does_not_blindly_patch` | a crash before the patch does not cause a blind patch on restart |
| `test_t12_stale_fence_cannot_commit_or_mutate` | an executor with an old fencing epoch can neither write state nor patch |
| `test_authority_changes_after_approval_are_refused` (4 cases) | after approval: mode no longer Governed, approval revoked, requester grant revoked, policy superseded — each refuses with its reason |
| `test_t14_exclusive_experiment_blocks_new_mutation` | no change during an exclusive experiment |
| `test_expired_intent_is_refused` | intents expire after 15 minutes |
| `test_before_image_drift_is_refused` | live values must equal the plan's before-values |
| `test_admission_rejection_records_refusal` | an admission rejection is recorded as a refusal, not retried |
| `test_ownership_detection_fails_closed_on_conflicting_signals` | conflicting ownership markers → ambiguous → refused |
| `test_absent_limit_is_added_not_assumed` | a missing limit is added explicitly, not assumed to exist |
| `test_s26_t01_ack_without_rollout_is_not_verified` | an acknowledged patch without a finished rollout is not verified |
| `test_missing_post_window_is_unknown_never_pass` | no post-change data → `unknown`, never a pass |
| `test_lower_traffic_after_change_makes_benefit_inconclusive` | incomparable load → benefit `inconclusive` |
| `test_queue_growth_after_cpu_cut_fails_guardrail` | a growing queue after a CPU cut fails the guardrail |
| `test_comparable_safe_reduction_supports_benefit` | safe guardrails + comparable load → benefit supported |
| `test_recovery_builds_exact_inverse_only_when_after_image_matches` | the inverse is applied only if the live object still shows the planned after-values |
| `test_recovery_escalates_on_concurrent_edit_recreation_or_attempt_limit` | otherwise the operation escalates; recovery never loops |
| `test_healthy_fully_observed_w04_window_passes_its_guardrails` | the verification window, read through the real registry and client with the real W04 policy, passes when the workload is healthy (each query at its minimum step) |
| `test_failed_guardrail_query_is_unknown_never_pass` | a failed guardrail query leaves the guardrail `unknown` |

### `test_forecasting.py` — S14 forecasting

| Test | Protects |
|---|---|
| `test_bucket_grid_counts_and_contiguity` | the bucket grid has the right count and no holes |
| `test_stationary_cpu_series_produces_calibrated_forecast` | a clean series yields an available forecast with a calibrated interval |
| `test_same_input_gives_identical_forecast` | determinism |
| `test_invalid_numeric_values_are_rejected_not_coerced` | NaN/negative values are input errors |
| `test_duplicate_timestamps_are_a_typed_input_error` | duplicate buckets are rejected |
| `test_missing_samples_are_never_treated_as_zero` | missing ≠ zero |
| `test_history_after_cutoff_is_refused_as_leakage` | no future data in a forecast |
| `test_short_history_is_refused` | `insufficient_history` |
| `test_compressed_scenario_time_never_counts_as_24h_history` | accelerated experiment time is not real history |
| `test_suspended_batch_workload_is_not_applicable_not_zero_demand` | a suspended CronJob is "not applicable", not zero demand |
| `test_source_quality_flags_map_to_typed_refusals` (4 cases) | `hpa_confounded` → `hpa_unmodeled`, `uid_mapping_ambiguous` → `identity_unresolved`, `host_pressure` → `host_or_generator_contamination`, an unknown flag → `unclassified_quality_issue` |
| `test_peak_flag_blocks_peak_statistic_but_only_limits_mean` | peak flags block peak claims but not mean forecasts |
| `test_censored_cpu_demand_is_refused` | throttled (censored) demand refuses |
| `test_better_average_error_with_bad_tail_is_not_promoted` | a model with a worse tail is not chosen for a better average |
| `test_undefined_metrics_are_none_not_zero` | undefined metrics stay `None` |
| `test_mae_arithmetic_matches_companion_example` | MAE matches the specification's worked example |
| `test_run_envelope_requires_comparable_declared_runs` | batch envelopes need comparable runs of the same profile |
| `test_aggregate_projection_never_claims_joint_coverage` | aggregated projections do not claim joint interval coverage |

### `test_governance.py` — authorization, plans and approvals

| Test | Protects |
|---|---|
| `test_viewer_cannot_create_or_approve` | read-only role |
| `test_approver_outside_namespace_scope_is_denied` | grants are namespace-scoped |
| `test_revoked_grant_denies_despite_valid_token_role` | the token's role is not enough without a live grant |
| `test_administrator_is_not_an_implicit_approver_or_executor` | roles are not hierarchical |
| `test_machine_principal_cannot_hold_sensitive_actions` | services cannot approve or execute |
| `test_approval_and_execution_denied_outside_governed_mode` | Governed-only actions |
| `test_degraded_grant_store_fails_closed_for_sensitive_actions` | no fresh grants → deny |
| `test_qualified_evaluation_issues_awaiting_approval_plan` | the happy-path plan state |
| `test_t01_author_cannot_approve_own_plan_even_with_approver_grant` | separation of duties |
| `test_t03_changed_payload_does_not_match_approval` | approval binds to the exact digest |
| `test_t05_expired_or_revoked_approval_cannot_create_intent` | approval TTL and revocation |
| `test_t06_unknown_mandatory_gate_blocks_plan` | `unknown` gates block the plan |
| `test_deterministic_only_execution_needs_explicit_policy` | governance policy must explicitly allow deterministic-only execution |
| `test_gitops_owned_target_is_not_executable_in_p0` | GitOps targets are advisory only |
| `test_advisory_mode_plan_is_not_actionable` | plans created outside Governed mode cannot run |
| `test_t10_execution_request_refused_after_mode_change` | a mode change after approval blocks execution |
| `test_valid_chain_creates_digest_bound_intent` | the intent binds plan digest and approval |
| `test_illegal_plan_transitions_are_rejected` | the plan state machine |

### `test_optimization_golden.py` — golden fixtures for S19–S22

| Test | Protects |
|---|---|
| `test_f01_split_fit_aggregate_is_not_node_fit` | spare capacity spread over nodes does not mean a Pod fits |
| `test_f02_memory_gap_across_burst_blocks_safe_downsize` | a telemetry gap over a burst blocks a memory decrease |
| `test_f03_latency_guard_blocks_cpu_downsize_despite_low_cpu` | low CPU with bad latency is not spare capacity |
| `test_f03_missing_latency_evidence_is_unknown_not_pass` | no latency evidence → unknown |
| `test_f04_low_cpu_with_growing_queue_is_not_qualified` | a growing queue blocks a CPU cut |
| `test_f05_job_template_is_advisory_only_for_governed_execution` | Job templates are not directly executable |
| `test_f06_active_hpa_blocks_cpu_request_change` | HPA coupling |
| `test_f07_platform_tainted_node_never_used_for_demo_pods` | the platform taint is honoured in simulation |
| `test_f08_only_qualified_candidates_are_ranked` | ranking never includes non-qualified candidates |
| `test_f09_recreated_controller_changes_candidate_identity` | a new UID is a new candidate |
| `test_f10_unsupported_scheduler_constraint_is_indeterminate` | unmodelled constraints → indeterminate |
| `test_f11_same_inputs_give_same_digests_and_order` | determinism of digests and ranking |
| `test_f15_oom_in_window_rejects_memory_downsize` | an OOM in the window rejects a memory decrease |
| `test_well_evidenced_cpu_downsize_qualifies_and_is_ranked` | a well-evidenced decrease qualifies and is ranked |
| `test_platform_namespace_is_rejected_before_generation` | platform namespaces never get candidates |
| `test_greedy_failure_does_not_prove_infeasibility` | backtracking placement finds fits greedy search misses |
| `test_four_way_gate_oracle` | pass/fail/unknown/not_applicable → qualified/rejected/indeterminate |
| `test_workload_override_may_not_relax_class_floor` | tighten-only merge |
| `test_override_may_tighten_and_narrow_actions` | tightening overrides apply |

### `test_release_gates.py` — release-gate rules

| Test | Protects |
|---|---|
| `test_gate_without_any_evidence_is_not_qualified` | missing evidence never passes |
| `test_hard_failure_blocks_even_when_other_items_pass` | hard items block |
| `test_soft_failure_or_not_run_is_not_qualified_rather_than_blocked` | soft items make a gate not qualified |
| `test_release_is_blocked_by_any_blocked_gate_and_needs_all_six_gates` | overall outcome rules |
| `test_reviewer_can_refuse_a_pass_but_cannot_override_a_failure` | human review limits |
| `test_junit_results_map_missing_skipped_and_failed_runs_honestly` | skipped → not_run, failed → failed |

### `test_scenarios.py` — experiments and the watchdog

| Test | Protects |
|---|---|
| `test_same_seed_gives_identical_planned_schedule` | reproducible demand plans |
| `test_named_streams_isolate_actions_from_each_other` | changing one action does not shift another |
| `test_planned_rate_matches_declared_rate_approximately` | the plan delivers the declared rate |
| `test_invalid_or_unsafe_scenarios_are_rejected` (6 cases) | unbounded duration, a rate above the hard cap, an unknown target, an arbitrary command, a deferred fault, an un-allow-listed profile |
| `test_measured_phase_after_cooldown_is_rejected` | phase order |
| `test_journal_transitions_and_idempotent_abort` | run state machine; abort is idempotent |
| `test_exclusive_lease_fencing` | an exclusive lease fences out other runs |
| `test_dead_watchdog_fails_closed` | no heartbeat → heavy runs refused |
| `test_watchdog_thresholds` | 2.0 GiB pressure, 1.5 GiB abort |
| `test_capability_is_scoped_short_lived_and_unforgeable` | capability tokens |
| `test_expired_capability_is_rejected` | capability expiry |
| `test_driver_job_is_tokenless_restricted_and_experiment_pinned` | the driver Job template |
| `test_batch_action_accepts_only_profiles_the_batch_program_knows` | batch profiles are allow-listed |
| `test_batch_job_changes_only_profile_and_seed_of_the_installed_template` | batch Jobs come only from installed templates |
| `test_serial_preflight_ignores_the_run_being_checked_but_not_other_open_runs` | serial execution check |
| `test_run_quality_never_reports_undelivered_or_failed_load_as_a_result` | run-quality classification |

### `test_worker_readiness.py`

| Test | Protects |
|---|---|
| `test_capacity_worker_readiness_recovers_on_an_idle_queue` | readiness clears after a transient database error even when no job is queued |

---

## Security tests (`tests/security`)

### `test_ai_tool_security.py` — AI tool broker and output validation

| Test | Protects |
|---|---|
| `test_registry_has_no_forbidden_or_mutating_tools` | the tool registry is read-only |
| `test_unknown_or_invented_tools_are_denied` | no tool outside the registry |
| `test_role_matrix_blocks_reviewer_from_planner_only_tool` | per-role tool permissions |
| `test_unexpected_or_malicious_arguments_are_rejected` (3 cases) | an SSRF URL smuggled into arguments, an SQL-injection-style workload ID, a forged role in arguments |
| `test_cross_namespace_request_is_denied_without_detail` | scope; no information leak |
| `test_expired_delegation_and_budget_exhaustion` | delegation expiry; 12-call / 4-per-tool / 60 KB budget |
| `test_grounded_output_completes_diagnostic_review` | a valid output completes |
| `test_injected_approval_field_is_rejected_by_schema` | no approval fields in model output |
| `test_fabricated_evidence_reference_is_rejected` | citations must be real evidence references |
| `test_invented_number_is_rejected_by_numeric_validator` | numbers must come from evidence |
| `test_model_requested_forbidden_tool_is_denied_and_workflow_continues` | forbidden tool requests |
| `test_inference_outage_yields_unavailable_not_invented_findings` | degraded mode |
| `test_optimization_review_requires_deterministic_analysis` | reviews need a deterministic analysis |
| `test_validator_rejects_number_not_in_cited_evidence_even_if_elsewhere` | numbers must be in the *cited* evidence |
| `test_agent_framework_path_uses_gateway_without_tools` | Agent Framework agents have no tools and use the gateway |
| `test_canary_secrets_are_redacted_before_prompting_or_embedding` | secrets never reach the model |

### `test_internal_mtls.py` — API → governance relay

| Test | Protects |
|---|---|
| `test_relay_presents_client_certificate_to_mtls_listener` | a real mutual-TLS handshake succeeds and the listener sees the API's certificate |
| `test_relay_without_client_certificate_is_refused_as_unavailable` | without a certificate the handshake fails → typed `unavailable` |
| `test_relay_with_unreadable_certificate_is_unavailable_not_internal` | a missing certificate file → `unavailable`, not a 500 |

---

## Component / API tests (`tests/component/test_api_boundaries.py`)

| Test | Protects |
|---|---|
| `test_missing_token_is_401_with_typed_envelope` | typed error envelope |
| `test_invalid_tokens_are_rejected` (6 cases) | an ID token, wrong audience, expired, signed by a foreign key, wrong client, wrong issuer |
| `test_alg_none_token_is_rejected` | no unsigned tokens |
| `test_viewer_sees_only_granted_namespaces` | grant-scoped listing |
| `test_viewer_cannot_request_analysis` | role matrix at the API |
| `test_analysis_request_is_async_and_idempotent` | 202 + operation; same key → same job |
| `test_out_of_scope_analysis_and_unknown_operation_are_opaque` | no existence leak |
| `test_no_generic_relay_endpoints_exist` | no pass-through routes |
| `test_arbitrary_request_fields_are_rejected` | strict request bodies |
| `test_governance_requires_api_machine_identity` | governance accepts only the API's ServiceAccount token |
| `test_forged_user_header_does_not_authenticate` | identity comes from tokens, not headers |

---

## Integration tests (`tests/integration`)

### `test_schema_boundaries.py` — database security

| Test | Protects |
|---|---|
| `test_all_migrations_applied_and_rerun_is_noop` | migrations apply once |
| `test_changed_migration_file_is_detected_as_drift` | checksum ledger |
| `test_cross_schema_writes_and_privileged_views_are_denied` | a role cannot write another service's schema or read privileged views |
| `test_owner_can_write_its_own_schema` | each owner role can write its own schema |
| `test_job_queue_row_level_security` | RLS per queue |
| `test_idempotent_enqueue_and_conflicting_payload` | idempotency keys |
| `test_expired_lease_reclaim_fences_out_the_old_worker` | leases and fencing |
| `test_audit_is_append_only_and_tamper_is_detected` | audit chain |
| `test_finalized_records_are_immutable` | append-only triggers |

### `test_governed_chain.py` — the governed path across real roles

| Test | Protects |
|---|---|
| `test_governed_chain_applies_once_and_journals_everything` | capacity → governance → executor; one patch, full journal, intent-only handoff job |
| `test_idempotent_execution_request_returns_same_intent` | idempotent execution requests |
| `test_author_cannot_approve_even_holding_approver_grant` | separation of duties in the database path |
| `test_grant_revoked_after_intent_blocks_execution` | authority re-checked at execution |
| `test_mode_downgrade_after_approval_blocks_execution` | mode re-checked at execution |
| `test_out_of_scope_plan_read_is_indistinguishable_from_missing` | opaque denials |
| `test_second_operation_on_same_controller_waits_for_first` | one operation per controller |
| `test_refused_commands_leave_durable_denial_records` | denials survive the refused transaction's rollback, with precise reasons |

### `test_knowledge_retrieval.py` — knowledge base on PostgreSQL + pgvector

| Test | Protects |
|---|---|
| `test_redaction_happens_before_storage` | secrets never stored |
| `test_scope_prefilter_hides_foreign_namespace_completely` | authorization in SQL first |
| `test_hybrid_search_finds_relevant_in_scope_document` | full-text + vector fusion |
| `test_lexical_only_mode_when_embeddings_unavailable` | degraded mode |
| `test_revoked_source_is_never_returned` | revocation |
| `test_classification_ceiling_and_restricted_refusal` | classification limits |
| `test_new_revision_supersedes_old_one` | revisions |

---

## Operational tests (`tests/operational/test_live_lab.py`)

Described in [testing-strategy.md](testing-strategy.md#5-operational-tests-in-detail): the seven live
checks of `capacity-lab verify all`, the six Deployments, token-protected TLS API, model-gateway
isolation, restore drill, audit checkpoints, executor recovery after Pod loss.

## End-to-end test (`tests/e2e/test_governed_change_live.py`)

`test_governed_change_from_analysis_to_verified_outcome` — the full chain with four real users; see
[testing-strategy.md](testing-strategy.md#6-the-end-to-end-test--and-when-it-skips).

## .NET tests (`workloads/dotnet/Tests/CapabilityAndWorkTests.cs`)

| Test | Protects |
|---|---|
| `ValidCapabilityIsAccepted` | a correctly scoped capability is accepted |
| `ForgedExpiredAndOutOfScopeCapabilitiesAreRejected` | forged, expired and wrong-scope tokens are refused |
| `MissingVerifierKeyFailsClosed` | no public key → refuse everything |
| `ComputeIsDeterministicForTheSameSeed` | reproducible work |
| `MetricsUseOnlyBoundedLabels` | no unbounded metric labels |
