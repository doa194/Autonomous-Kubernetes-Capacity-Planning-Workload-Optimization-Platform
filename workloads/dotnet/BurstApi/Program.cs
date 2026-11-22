// W02 burst-api (Standard, workloads-standard): bounded CPU per request, driven in bursts by
// S05 scenarios. HPA is opt-in for P1 experiments only and is not installed at baseline.
using CapacityLab.Common;

var iterations = new Dictionary<string, int> { ["normal"] = 1_500, ["burst"] = 6_000 };
WorkloadHost.Build(args, "burst-api", "w02-burst-api", "process", "/api/v1/process", iterations, maxConcurrency: 8, maxWorkUnits: 5)
    .Run();
