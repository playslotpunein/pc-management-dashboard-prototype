using PlaySlot.Watchdog;

// ContentRootPath is set explicitly because a Windows service starts with its working
// directory set to C:\Windows\System32. Without this, appsettings.json is looked for
// there and silently not found.
var builder = Host.CreateApplicationBuilder(new HostApplicationBuilderSettings
{
    Args = args,
    ContentRootPath = AppContext.BaseDirectory
});

builder.Services.AddWindowsService(options =>
{
    options.ServiceName = WatchdogOptions.ServiceName;
});

// Service failures have to be visible with nobody logged in, so they go to the
// Windows Event Log (Event Viewer > Windows Logs > Application).
builder.Logging.AddEventLog(settings =>
{
    settings.SourceName = WatchdogOptions.ServiceName;
    settings.LogName = "Application";
});

builder.Services.Configure<WatchdogOptions>(
    builder.Configuration.GetSection(WatchdogOptions.SectionName));

builder.Services.AddSingleton<SessionLauncher>();

// Registered twice on purpose: once so WatchdogWorker can read the last beat, and
// once as a hosted service so its pipe accept-loop actually runs. Both resolve to
// the same instance.
builder.Services.AddSingleton<HeartbeatListener>();
builder.Services.AddHostedService(sp => sp.GetRequiredService<HeartbeatListener>());

builder.Services.AddHostedService<WatchdogWorker>();

var host = builder.Build();
host.Run();
