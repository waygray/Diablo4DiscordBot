$ErrorActionPreference = "Continue"

$projectDir = "C:\Users\waylo\OneDrive\Desktop\Diablo 4 Discord Bot\Diablo4DiscordBot"
$pythonExe = "C:\Users\waylo\OneDrive\Desktop\Diablo 4 Discord Bot\Diablo4DiscordBot\.venv\bin\python.exe"
$botFile = Join-Path $projectDir "bot.py"
$logFile = Join-Path $projectDir "bot_runner.log"
$envFile = Join-Path $projectDir "bot.env"
$maxLogSizeBytes = 5MB
$maxLogBackups = 3

Set-Location $projectDir

if (Test-Path $envFile) {
    $allowedEnvVars = @(
        "DISCORD_BOT_TOKEN"
    )

    Get-Content $envFile | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith("#")) { return }

        $parts = $line.Split("=", 2)
        if ($parts.Count -eq 2) {
            $name = $parts[0].Trim()
            $value = $parts[1].Trim()
            if ($allowedEnvVars -contains $name) {
                [System.Environment]::SetEnvironmentVariable($name, $value, "Process")
            }
        }
    }
}

# Force a known-good CA bundle for Python HTTPS requests on this machine.
try {
    $certifiPath = & $pythonExe -c "import certifi; print(certifi.where())"
    if ($certifiPath) {
        [System.Environment]::SetEnvironmentVariable("SSL_CERT_FILE", $certifiPath, "Process")
        [System.Environment]::SetEnvironmentVariable("REQUESTS_CA_BUNDLE", $certifiPath, "Process")
    }
}
catch {
    Add-Content -Path $logFile -Value "[$(Get-Date -Format \"yyyy-MM-dd HH:mm:ss\")] Warning: Could not set certifi CA bundle."
}

function Rotate-LogIfNeeded {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][long]$MaxBytes,
        [Parameter(Mandatory = $true)][int]$MaxBackups
    )

    if (-not (Test-Path $Path)) { return }

    $item = Get-Item $Path -ErrorAction SilentlyContinue
    if (-not $item -or $item.Length -lt $MaxBytes) { return }

    for ($i = $MaxBackups - 1; $i -ge 1; $i--) {
        $src = "$Path.$i"
        $dst = "$Path." + ($i + 1)
        if (Test-Path $src) {
            Move-Item -Path $src -Destination $dst -Force
        }
    }

    try {
        Move-Item -Path $Path -Destination "$Path.1" -Force
    }
    catch {
        $item = Get-Item $Path -ErrorAction SilentlyContinue
        if ($item -and $item.Length -ge $MaxBytes) {
            Clear-Content -Path $Path -ErrorAction SilentlyContinue
        }
    }
}

while ($true) {
    Rotate-LogIfNeeded -Path $logFile -MaxBytes $maxLogSizeBytes -MaxBackups $maxLogBackups

    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $logFile -Value "[$timestamp] Starting bot process"

    & $pythonExe $botFile 2>&1 | Tee-Object -FilePath $logFile -Append

    $exitCode = $LASTEXITCODE
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $logFile -Value "[$timestamp] Bot exited with code $exitCode. Restarting in 5 seconds."

    Start-Sleep -Seconds 5
}
