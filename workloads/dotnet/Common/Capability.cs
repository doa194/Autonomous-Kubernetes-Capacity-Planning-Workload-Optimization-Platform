// Verifies the short-lived experiment capability (ES256 JWT) that load drivers present.
// Only the public key is mounted into the Pod, so this workload can check a capability
// but can never mint one. Checks mirror the Python reference verifier: signature, issuer,
// audience, expiry and that the workload+action pair is explicitly allowed (S07 §6).
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace CapacityLab.Common;

public sealed class CapabilityVerifier
{
    private const string Issuer = "capacity-scenario-runner";
    private const string Audience = "capacity-workload-control";
    private readonly ECDsa? _key;

    public CapabilityVerifier(string? publicKeyPath)
    {
        if (publicKeyPath is not null && File.Exists(publicKeyPath))
        {
            _key = ECDsa.Create();
            _key.ImportFromPem(File.ReadAllText(publicKeyPath));
        }
    }

    // Returns null when the capability is valid, otherwise a short reason for the 401/403.
    public string? Check(string? authorization, string workload, string action, DateTimeOffset now)
    {
        if (_key is null) return "verifier_not_configured"; // fail closed when the key is missing
        if (authorization is null || !authorization.StartsWith("Bearer ", StringComparison.Ordinal)) return "missing";
        var parts = authorization[7..].Split('.');
        if (parts.Length != 3) return "malformed";
        try
        {
            using var header = JsonDocument.Parse(Base64Url(parts[0]));
            if (header.RootElement.GetProperty("alg").GetString() != "ES256") return "algorithm";
            var signed = Encoding.ASCII.GetBytes($"{parts[0]}.{parts[1]}");
            if (!_key.VerifyData(signed, Base64Url(parts[2]), HashAlgorithmName.SHA256,
                    DSASignatureFormat.IeeeP1363FixedFieldConcatenation)) return "signature";
            using var payload = JsonDocument.Parse(Base64Url(parts[1]));
            var c = payload.RootElement;
            if (c.GetProperty("iss").GetString() != Issuer) return "issuer";
            if (c.GetProperty("aud").GetString() != Audience) return "audience";
            if (DateTimeOffset.FromUnixTimeSeconds(c.GetProperty("exp").GetInt64()) <= now) return "expired";
            if (!Contains(c.GetProperty("allowedWorkloads"), workload)) return "workload_not_allowed";
            if (!Contains(c.GetProperty("allowedActions"), action)) return "action_not_allowed";
            return null;
        }
        catch (Exception e) when (e is FormatException or JsonException or KeyNotFoundException or InvalidOperationException)
        {
            return "malformed";
        }
    }

    private static bool Contains(JsonElement array, string value) =>
        array.ValueKind == JsonValueKind.Array && array.EnumerateArray().Any(e => e.GetString() == value);

    private static byte[] Base64Url(string s)
    {
        var padded = s.Replace('-', '+').Replace('_', '/');
        return Convert.FromBase64String(padded.PadRight(padded.Length + (4 - padded.Length % 4) % 4, '='));
    }
}
