// W01 latency-api (Critical, workloads-critical): deterministic bounded CPU work per request.
// Used to test that a CPU request reduction is refused when the p95 latency/error guardrail
// or forecast evidence does not support it (S04 §4.1). Iterations per work unit are fixed
// per profile; the server caps work units and concurrency regardless of the caller.
using CapacityLab.Common;

var iterations = new Dictionary<string, int> { ["idle"] = 0, ["normal"] = 2_000, ["heavy"] = 8_000 };
WorkloadHost.Build(args, "latency-api", "w01-latency-api", "compute", "/api/v1/compute", iterations, maxConcurrency: 4, maxWorkUnits: 10)
    .Run();
