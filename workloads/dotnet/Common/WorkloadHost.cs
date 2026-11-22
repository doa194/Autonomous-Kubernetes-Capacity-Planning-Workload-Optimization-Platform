// Shared hosting for W01/W02: health, metrics, bounded concurrency and graceful drain.
// Readiness means "safe to accept bounded work", not "CPU is idle", so readiness does not
// flap under load (S04 §7). On SIGTERM the host flips readiness off, stops taking new work
// and lets in-flight requests finish within the termination grace period.
using System.Diagnostics;
using System.Security.Cryptography;
using System.Text;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Http;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;

namespace CapacityLab.Common;

public sealed record WorkRequest(string? Profile, long Seed, int WorkUnits);

public static class WorkloadHost
{
    // `workload` is the short name used in metrics labels; `workloadId` is the logical ID
    // (for example "w01-latency-api") that experiment capabilities are issued for.
    public static WebApplication Build(string[] args, string workload, string workloadId, string operation, string route,
        IReadOnlyDictionary<string, int> iterationsPerUnit, int maxConcurrency, int maxWorkUnits)
    {
        var builder = WebApplication.CreateSlimBuilder(args);
        builder.WebHost.UseUrls(Environment.GetEnvironmentVariable("ASPNETCORE_URLS") ?? "http://+:8080");
        builder.Services.Configure<HostOptions>(o => o.ShutdownTimeout = TimeSpan.FromSeconds(20));
        var app = builder.Build();

        var metrics = new LabMetrics(workload, Environment.GetEnvironmentVariable("WORKLOAD_VERSION") ?? "dev");
        var verifier = new CapabilityVerifier(Environment.GetEnvironmentVariable("CAPABILITY_PUBLIC_KEY")
                                              ?? "/etc/capacity/capability/public.pem");
        var gate = new SemaphoreSlim(maxConcurrency);
        var accepting = true;
        app.Lifetime.ApplicationStopping.Register(() => accepting = false);

        app.MapGet("/health/live", () => Results.Ok(new { status = "live" }));
        app.MapGet("/health/ready", () => accepting ? Results.Ok(new { status = "ready" }) : Results.StatusCode(503));
        app.MapGet("/metrics", () => Results.Text(metrics.Render(), "text/plain; version=0.0.4"));

        app.MapPost(route, async (HttpContext ctx, WorkRequest body) =>
        {
            // Protected control route: valid scoped capability required, server-side bounds always apply.
            var denied = verifier.Check(ctx.Request.Headers.Authorization, workloadId, operation, DateTimeOffset.UtcNow);
            if (denied is not null) return Results.Json(new { error = denied }, statusCode: 401);
            if (!accepting) return Results.StatusCode(503);
            var profile = body.Profile ?? "normal";
            if (!iterationsPerUnit.TryGetValue(profile, out var perUnit) || body.WorkUnits < 1 || body.WorkUnits > maxWorkUnits)
                return Results.BadRequest(new { error = "parameters_out_of_bounds" });
            if (!await gate.WaitAsync(0))
            {
                metrics.Rejected(operation);
                return Results.StatusCode(429); // bounded overload: refuse instead of queueing forever
            }
            metrics.Started();
            var sw = Stopwatch.StartNew();
            try
            {
                var checksum = Compute(body.Seed, perUnit * body.WorkUnits, ctx.RequestAborted);
                metrics.Finished(operation, "success", sw.Elapsed.TotalSeconds);
                return Results.Ok(new { checksum, profile, workUnits = body.WorkUnits });
            }
            catch (OperationCanceledException)
            {
                metrics.Finished(operation, "error", sw.Elapsed.TotalSeconds);
                return Results.StatusCode(499);
            }
            finally
            {
                gate.Release();
            }
        });
        return app;
    }

    // Deterministic CPU work: iterated SHA-256 from a seed. Same seed + units => same checksum.
    public static string Compute(long seed, int iterations, CancellationToken token)
    {
        var buffer = Encoding.ASCII.GetBytes($"seed:{seed}");
        for (var i = 0; i < iterations; i++)
        {
            if ((i & 1023) == 0) token.ThrowIfCancellationRequested();
            buffer = SHA256.HashData(buffer);
        }
        return Convert.ToHexString(buffer).ToLowerInvariant();
    }
}
