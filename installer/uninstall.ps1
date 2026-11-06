# Magical Portal System - Uninstall
# Started from Uninstall.bat in the install folder, or Settings > Apps > Installed apps.

$dest = $PSScriptRoot
Set-Location $env:TEMP     # don't sit inside the folder we're about to delete
function Get-WebPort($dir) {
    # the control page port can be changed in Settings - read it from the saved settings
    try {
        $j = Get-Content -LiteralPath (Join-Path $dir 'streams.json') -Raw -ErrorAction Stop | ConvertFrom-Json
        if ($j.settings.web_port) { return [int]$j.settings.web_port }
    } catch { }
    return 8080
}
function Ask($question, $default = 'Y') {
    $hint = if ($default -eq 'Y') { 'Y/n' } else { 'y/N' }
    $a = Read-Host "  $question [$hint]"
    if ([string]::IsNullOrWhiteSpace($a)) { $a = $default }
    return ($a -match '^[Yy]')
}

Write-Host ''
Write-Host "  Magical Portal System - Uninstall  ($dest)" -ForegroundColor Magenta
Write-Host ''
if (-not (Ask 'Remove Magical Portal System from this PC?' 'N')) { Read-Host '  Cancelled. Press Enter to close'; return }

# stop it if running
try {
    Invoke-WebRequest -UseBasicParsing -Method Post -Uri "http://127.0.0.1:$(Get-WebPort $dest)/api/shutdown" `
        -Body '{}' -ContentType 'application/json' -TimeoutSec 3 | Out-Null
    Start-Sleep -Seconds 3
} catch { }

# shortcuts
foreach ($f in @('Desktop', 'Programs', 'Startup')) {
    $lnk = Join-Path ([Environment]::GetFolderPath($f)) 'Magical Portal System.lnk'
    if (Test-Path -LiteralPath $lnk) { Remove-Item -LiteralPath $lnk -Force }
}
Write-Host '  Shortcuts removed.' -ForegroundColor Green

# Windows "Installed apps" entry
Remove-Item -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\MagicalPortalSystem' -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -Path 'HKCU:\Software\MagicalPortalSystem' -Recurse -Force -ErrorAction SilentlyContinue

# firewall rules
if (Ask 'Remove the firewall rules and the PortalSystem address too? (one admin prompt)') {
    $tmp = Join-Path $env:TEMP 'magical_portal_remove_admin.ps1'
    Set-Content -LiteralPath $tmp -Encoding ASCII -Value @(
        'foreach ($n in "FFmpeg","FFplay","VLC","Web page","Name lookup") { netsh advfirewall firewall delete rule name="Magical Portal - $n" | Out-Null }',
        '$h = Join-Path $env:SystemRoot "System32\drivers\etc\hosts"',
        '$lines = @(Get-Content -LiteralPath $h -ErrorAction SilentlyContinue | Where-Object { $_ -notmatch "# Magical Portal System" })',
        'Set-Content -LiteralPath $h -Value $lines -Encoding ASCII',
        'ipconfig /flushdns | Out-Null')
    try {
        Start-Process powershell.exe -Verb RunAs -Wait -WindowStyle Hidden -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$tmp`""
        Write-Host '  Firewall rules and address removed.' -ForegroundColor Green
    } catch { Write-Host '  Skipped (admin prompt declined).' -ForegroundColor Yellow }
    Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
}

# keep a copy of the saved portals
$cfg = Join-Path $dest 'streams.json'
if ((Test-Path -LiteralPath $cfg) -and (Ask 'Keep a backup of your saved portals on the Desktop?')) {
    Copy-Item -LiteralPath $cfg -Destination (Join-Path ([Environment]::GetFolderPath('Desktop')) 'MagicalPortal-streams-backup.json') -Force
    Write-Host '  Saved MagicalPortal-streams-backup.json to the Desktop.' -ForegroundColor Green
}

Write-Host ''
Write-Host '  Magical Portal System has been removed. (Python, FFmpeg and VLC were left installed.)' -ForegroundColor Green
Read-Host '  Press Enter to finish'
# delete the folder a moment after this window closes
Start-Process cmd.exe -ArgumentList "/c timeout /t 2 /nobreak >nul & rmdir /s /q `"$dest`"" -WindowStyle Hidden
