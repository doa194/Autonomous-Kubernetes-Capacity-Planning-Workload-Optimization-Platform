// Minimal Prometheus text-format metrics with the bounded S04 §8 label set.
// Labels are fixed enums (workload, operation, outcome); no request IDs, seeds or paths
// ever become labels, so a caller cannot create unbounded time series.
using System.Collections.Concurrent;
using System.Globalization;
using System.Text;

namespace CapacityLab.Common;

public sealed class LabMetrics(string workload, string version)
{
    private static readonly double[] Buckets = [0.005, 0.01, 0.025, 0.05, 0.1, 0.15, 0.25, 0.3, 0.5, 1, 2.5, 5];
    private readonly ConcurrentDictionary<(string Op, string Outcome), long> _requests = new();
    private readonly ConcurrentDictionary<(string Op, string Outcome), Histogram> _durations = new();
    private long _inflight;

    public void Started() => Interlocked.Increment(ref _inflight);

    public void Finished(string operation, string outcome, double seconds)
    {
        Interlocked.Decrement(ref _inflight);
        _requests.AddOrUpdate((operation, outcome), 1, (_, v) => v + 1);
        _durations.GetOrAdd((operation, outcome), _ => new Histogram()).Observe(seconds);
    }

    public void Rejected(string operation) => _requests.AddOrUpdate((operation, "rejected"), 1, (_, v) => v + 1);

    public string Render()
    {
        var sb = new StringBuilder();
        var inv = CultureInfo.InvariantCulture;
        sb.AppendLine("# TYPE lab_requests_total counter");
        foreach (var ((op, outcome), v) in _requests)
            sb.AppendLine(inv, $"lab_requests_total{{workload=\"{workload}\",operation=\"{op}\",outcome=\"{outcome}\"}} {v}");
        sb.AppendLine("# TYPE lab_request_duration_seconds histogram");
        foreach (var ((op, outcome), h) in _durations)
        {
            var labels = $"workload=\"{workload}\",operation=\"{op}\",outcome=\"{outcome}\"";
            var (counts, sum, total) = h.Snapshot();
            for (var i = 0; i < Buckets.Length; i++)
                sb.AppendLine(inv, $"lab_request_duration_seconds_bucket{{{labels},le=\"{Buckets[i].ToString(inv)}\"}} {counts[i]}");
            sb.AppendLine(inv, $"lab_request_duration_seconds_bucket{{{labels},le=\"+Inf\"}} {total}");
            sb.AppendLine(inv, $"lab_request_duration_seconds_sum{{{labels}}} {sum.ToString(inv)}");
            sb.AppendLine(inv, $"lab_request_duration_seconds_count{{{labels}}} {total}");
        }
        sb.AppendLine("# TYPE lab_tasks_inflight gauge");
        sb.AppendLine(inv, $"lab_tasks_inflight{{workload=\"{workload}\"}} {Interlocked.Read(ref _inflight)}");
        sb.AppendLine("# TYPE lab_workload_info gauge");
        sb.AppendLine(inv, $"lab_workload_info{{workload=\"{workload}\",version=\"{version}\"}} 1");
        return sb.ToString();
    }

    private sealed class Histogram
    {
        private readonly long[] _counts = new long[Buckets.Length];
        private double _sum;
        private long _total;
        private readonly Lock _lock = new();

        public void Observe(double seconds)
        {
            lock (_lock)
            {
                for (var i = 0; i < Buckets.Length; i++) if (seconds <= Buckets[i]) _counts[i]++;
                _sum += seconds;
                _total++;
            }
        }

        public (long[] Counts, double Sum, long Total) Snapshot()
        {
            lock (_lock) return ((long[])_counts.Clone(), _sum, _total);
        }
    }
}
