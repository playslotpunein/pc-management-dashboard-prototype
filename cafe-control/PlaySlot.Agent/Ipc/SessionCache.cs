using System.Text.Json;
using System.Text.Json.Serialization;

namespace PlaySlot.Agent.Ipc;

/// <summary>
/// The cached session end time, and the fail-safe decision built on it.
///
/// This is Zone 5. The agent loses the control server, waits for a reconnect, then reads
/// this cache and asks exactly one question: is there paid time remaining?
///
///   YES — stay unlocked and keep counting down locally.
///   NO  — engage the lock.
///
/// The rule is fail open during paid time, fail closed otherwise. It matters more for
/// customer trust than anything technical in this codebase: never punish someone because
/// your network dropped. A blip during a paid hour must not strand a paying customer
/// behind a lock screen, and a blip after expiry must not hand out free play.
///
/// The cache is written to disk because the agent can be restarted — by the watchdog,
/// or by a reboot — while the server is unreachable. An in-memory cache would come back
/// empty, with no idea whether the person at the machine had paid, and the safe default
/// in that state is nothing anyone wants.
/// </summary>
internal sealed class SessionCache
{
    private readonly string _path;
    private CacheState _state = new();

    public SessionCache(string path)
    {
        _path = path;
        Load();
    }

    /// <summary>End of paid time. Null means an open-ended session with no deadline.</summary>
    public DateTime? SessionEndUtc => _state.SessionEndUtc;

    /// <summary>End of the grace period. Null when there is no deadline.</summary>
    public DateTime? GraceEndUtc => _state.GraceEndUtc;

    /// <summary>The last lock state the server asserted.</summary>
    public bool ServerSaysLocked => _state.Locked;

    public DateTime? UpdatedUtc => _state.UpdatedUtc;

    public void Update(DateTime? sessionEndUtc, DateTime? graceEndUtc, bool locked)
    {
        _state = new CacheState
        {
            SessionEndUtc = sessionEndUtc,
            GraceEndUtc = graceEndUtc,
            Locked = locked,
            UpdatedUtc = DateTime.UtcNow,
        };

        Save();
    }

    /// <summary>
    /// The fail-safe decision, for use when the server is unreachable.
    /// </summary>
    public FailSafeDecision Decide(DateTime nowUtc)
    {
        // No session on record. The unit was idle when we last heard, or this is a fresh
        // install. Locking an idle machine would be wrong and pointless.
        if (_state.UpdatedUtc is null)
        {
            return new FailSafeDecision(false, "no cached session — leaving unlocked");
        }

        // CRITICAL: null means "no deadline", never "expired". An open-ended walk-in is
        // paying by the minute; reading null as expired would lock out a paying customer
        // the moment the network hiccuped, which is the exact failure this whole branch
        // exists to prevent.
        if (_state.SessionEndUtc is null)
        {
            return new FailSafeDecision(false, "open-ended session — no deadline to enforce");
        }

        // Grace is part of the paid experience. Falling back to the session end when no
        // grace was cached is the conservative direction: it locks sooner, never later.
        var deadline = _state.GraceEndUtc ?? _state.SessionEndUtc.Value;

        if (nowUtc < deadline)
        {
            var remaining = (int)(deadline - nowUtc).TotalSeconds;
            return new FailSafeDecision(false, $"{remaining}s of paid time remaining");
        }

        return new FailSafeDecision(true, $"paid time expired at {deadline:HH:mm:ss}Z");
    }

    private void Load()
    {
        try
        {
            if (!File.Exists(_path))
            {
                return;
            }

            var loaded = JsonSerializer.Deserialize<CacheState>(File.ReadAllText(_path));

            if (loaded is not null)
            {
                _state = loaded;
                Log.Info(
                    $"Session cache loaded: end={Describe(loaded.SessionEndUtc)} " +
                    $"locked={loaded.Locked}");
            }
        }
        catch (Exception ex)
        {
            // A corrupt cache must not stop the agent starting. Starting with no cache is
            // recoverable the moment the server is reachable; not starting is not.
            Log.Warn($"Could not read the session cache ({ex.Message}); starting empty");
        }
    }

    private void Save()
    {
        try
        {
            var directory = Path.GetDirectoryName(_path);

            if (!string.IsNullOrEmpty(directory))
            {
                Directory.CreateDirectory(directory);
            }

            // Written via a temporary file and moved into place, so a power cut mid-write
            // cannot leave a half-written cache that fails to parse on the way back up.
            var temporary = _path + ".tmp";

            File.WriteAllText(temporary, JsonSerializer.Serialize(_state));
            File.Move(temporary, _path, overwrite: true);
        }
        catch (Exception ex)
        {
            Log.Warn($"Could not write the session cache: {ex.Message}");
        }
    }

    private static string Describe(DateTime? moment) =>
        moment?.ToString("O") ?? "none";

    private sealed class CacheState
    {
        [JsonPropertyName("session_end_utc")]
        public DateTime? SessionEndUtc { get; set; }

        [JsonPropertyName("grace_end_utc")]
        public DateTime? GraceEndUtc { get; set; }

        [JsonPropertyName("locked")]
        public bool Locked { get; set; }

        [JsonPropertyName("updated_utc")]
        public DateTime? UpdatedUtc { get; set; }
    }
}

internal readonly record struct FailSafeDecision(bool ShouldLock, string Reason);
