// Unit tests for the shared W01/W02 contracts: capability verification and deterministic work.
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using CapacityLab.Common;
using Xunit;

public class CapabilityAndWorkTests : IDisposable
{
    private readonly ECDsa _signer = ECDsa.Create(ECCurve.NamedCurves.nistP256);
    private readonly string _publicKeyPath = Path.GetTempFileName();

    public CapabilityAndWorkTests() => File.WriteAllText(_publicKeyPath, _signer.ExportSubjectPublicKeyInfoPem());

    public void Dispose() => File.Delete(_publicKeyPath);

    private static string B64(byte[] b) => Convert.ToBase64String(b).TrimEnd('=').Replace('+', '-').Replace('/', '_');

    private string Token(ECDsa key, object claims, string alg = "ES256")
    {
        var header = B64(Encoding.UTF8.GetBytes(JsonSerializer.Serialize(new { alg, typ = "JWT" })));
        var payload = B64(Encoding.UTF8.GetBytes(JsonSerializer.Serialize(claims)));
        var sig = key.SignData(Encoding.ASCII.GetBytes($"{header}.{payload}"), HashAlgorithmName.SHA256,
            DSASignatureFormat.IeeeP1363FixedFieldConcatenation);
        return $"Bearer {header}.{payload}.{B64(sig)}";
    }

    private object Claims(long exp, string[]? workloads = null, string[]? actions = null) => new
    {
        iss = "capacity-scenario-runner", aud = "capacity-workload-control", exp,
        allowedWorkloads = workloads ?? ["latency-api"], allowedActions = actions ?? ["compute"]
    };

    [Fact]
    public void ValidCapabilityIsAccepted()
    {
        var v = new CapabilityVerifier(_publicKeyPath);
        var token = Token(_signer, Claims(DateTimeOffset.UtcNow.AddMinutes(5).ToUnixTimeSeconds()));
        Assert.Null(v.Check(token, "latency-api", "compute", DateTimeOffset.UtcNow));
    }

    [Fact]
    public void ForgedExpiredAndOutOfScopeCapabilitiesAreRejected()
    {
        var v = new CapabilityVerifier(_publicKeyPath);
        var future = DateTimeOffset.UtcNow.AddMinutes(5).ToUnixTimeSeconds();
        using var attacker = ECDsa.Create(ECCurve.NamedCurves.nistP256);
        Assert.Equal("signature", v.Check(Token(attacker, Claims(future)), "latency-api", "compute", DateTimeOffset.UtcNow));
        Assert.Equal("expired", v.Check(Token(_signer, Claims(1)), "latency-api", "compute", DateTimeOffset.UtcNow));
        Assert.Equal("workload_not_allowed",
            v.Check(Token(_signer, Claims(future, ["burst-api"])), "latency-api", "compute", DateTimeOffset.UtcNow));
        Assert.Equal("missing", v.Check(null, "latency-api", "compute", DateTimeOffset.UtcNow));
        Assert.Equal("algorithm",
            v.Check(Token(_signer, Claims(future), alg: "none"), "latency-api", "compute", DateTimeOffset.UtcNow));
    }

    [Fact]
    public void MissingVerifierKeyFailsClosed()
    {
        var v = new CapabilityVerifier("/nonexistent/key.pem");
        Assert.Equal("verifier_not_configured", v.Check("Bearer a.b.c", "latency-api", "compute", DateTimeOffset.UtcNow));
    }

    [Fact]
    public void ComputeIsDeterministicForTheSameSeed()
    {
        Assert.Equal(WorkloadHost.Compute(42, 100, CancellationToken.None), WorkloadHost.Compute(42, 100, CancellationToken.None));
        Assert.NotEqual(WorkloadHost.Compute(42, 100, CancellationToken.None), WorkloadHost.Compute(43, 100, CancellationToken.None));
    }

    [Fact]
    public void MetricsUseOnlyBoundedLabels()
    {
        var m = new LabMetrics("latency-api", "test");
        m.Started();
        m.Finished("compute", "success", 0.04);
        var text = m.Render();
        Assert.Contains("lab_request_duration_seconds_bucket{workload=\"latency-api\",operation=\"compute\",outcome=\"success\",le=\"0.05\"} 1", text);
        Assert.DoesNotContain("seed", text);
    }
}
