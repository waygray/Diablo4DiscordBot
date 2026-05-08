$ErrorActionPreference = "Continue"

$projectDir = "C:\Users\waylo\OneDrive\Desktop\Diablo 4 Discord Bot\Diablo4DiscordBot"
$pythonExe = "C:\Users\waylo\OneDrive\Desktop\Diablo 4 Discord Bot\Diablo4DiscordBot\.venv\bin\python.exe"
$botFile = Join-Path $projectDir "bot.py"
$logFile = Join-Path $projectDir "bot_runner.log"
$envFile = Join-Path $projectDir "bot.env"

function Write-RunnerLog {
    param([string]$Message)
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $logFile -Value "[$timestamp] $Message"
}

function Load-EnvFile {
    if (-not (Test-Path $envFile)) {
        return
    }

    Get-Content $envFile | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith("#")) { return }

        $parts = $line.Split("=", 2)
        if ($parts.Count -eq 2) {
            $name = $parts[0].Trim()
            $value = $parts[1].Trim()
            [System.Environment]::SetEnvironmentVariable($name, $value, "Process")
        }
    }
}

function Get-WatchFingerprint {
    $entries = Get-ChildItem -Path $projectDir -Recurse -File | Where-Object {
        ($_.Extension -eq ".py") -or ($_.Extension -eq ".env") -or ($_.Name -eq "requirements.txt")
    } | Sort-Object FullName | ForEach-Object {
        "{0}|{1}|{2}" -f $_.FullName, $_.LastWriteTimeUtc.Ticks, $_.Length
    }

    if (-not $entries) {
        return "no-files"
    }

    $raw = [string]::Join("`n", $entries)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($raw)
        $hash = $sha.ComputeHash($bytes)
        return [Convert]::ToBase64String($hash)
    }
    finally {
        $sha.Dispose()
    }
}

Set-Location $projectDir
Load-EnvFile

# Force a known-good CA bundle for Python HTTPS requests on this machine.
try {
    $certifiPath = & $pythonExe -c "import certifi; print(certifi.where())"
    if ($certifiPath) {
        [System.Environment]::SetEnvironmentVariable("SSL_CERT_FILE", $certifiPath, "Process")
        [System.Environment]::SetEnvironmentVariable("REQUESTS_CA_BUNDLE", $certifiPath, "Process")
    }
}
catch {
    Write-RunnerLog "Warning: Could not set certifi CA bundle."
}

while ($true) {
    Write-RunnerLog "Starting bot process"

    # Run Python directly, redirecting all output to the log file.
    # This call blocks until Python exits, keeping the scheduled task Running.
    & $pythonExe $botFile *>> $logFile

    $exitCode = $LASTEXITCODE
    Write-RunnerLog "Bot exited (code $exitCode). Restarting in 5 seconds..."
    Start-Sleep -Seconds 5
}
