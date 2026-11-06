# Magical Portal System - Setup
# Run through Install.bat (double-click). Run it again any time to update.

$ErrorActionPreference = 'Stop'
try { $Host.UI.RawUI.WindowTitle = 'Magical Portal System - Setup' } catch {}

$pkg        = Split-Path -Parent $PSScriptRoot      # folder that holds Install.bat
$app        = Join-Path $pkg 'app'
$defaultDir = 'C:\MagicalPortal'
$regApp     = 'HKCU:\Software\MagicalPortalSystem'
$regUninst  = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\MagicalPortalSystem'

function Say($text, $color = 'Gray') { Write-Host $text -ForegroundColor $color }
function Step($text) { Write-Host ''; Write-Host "  > $text" -ForegroundColor Cyan }
function Ask($question, $default = 'Y') {
    $hint = if ($default -eq 'Y') { 'Y/n' } else { 'y/N' }
    $a = Read-Host "    $question [$hint]"
    if ([string]::IsNullOrWhiteSpace($a)) { $a = $default }
    return ($a -match '^[Yy]')
}
function AskPath($question, $default) {
    $a = Read-Host "    $question`n    (press Enter for $default)"
    if ([string]::IsNullOrWhiteSpace($a)) { return $default }
    return $a.Trim().Trim('"').TrimEnd('\')
}
function Get-AppVersion($serverPy) {
    if (-not (Test-Path -LiteralPath $serverPy)) { return $null }
    $m = Select-String -LiteralPath $serverPy -Pattern '__version__\s*=\s*"([\d.]+)"' | Select-Object -First 1
    if ($m) { return $m.Matches[0].Groups[1].Value }
    return '1.2'   # versions before 1.3 had no version number
}
function Get-WebPort($dir) {
    # the control page port can be changed in Settings - read it from the saved settings
    try {
        $j = Get-Content -LiteralPath (Join-Path $dir 'streams.json') -Raw -ErrorAction Stop | ConvertFrom-Json
        if ($j.settings.web_port) { return [int]$j.settings.web_port }
    } catch { }
    return 8080
}
function Get-WebName($dir) {
    try {
        $j = Get-Content -LiteralPath (Join-Path $dir 'streams.json') -Raw -ErrorAction Stop | ConvertFrom-Json
        if ($j.settings.web_name -match '^[A-Za-z][A-Za-z0-9-]{0,62}$') { return [string]$j.settings.web_name }
    } catch { }
    return 'PortalSystem'
}
function Exit-Setup($code = 0) { Write-Host ''; exit $code }

$newVersion = Get-AppVersion (Join-Path $app 'server.py')

Write-Host ''
Write-Host '  ==============================================' -ForegroundColor Magenta
Write-Host "     Magical Portal System  v$newVersion  -  Setup" -ForegroundColor Magenta
Write-Host '  ==============================================' -ForegroundColor Magenta

# ------------------------------------------------------------------ 1. unblock
Step 'Removing the "downloaded from the internet" flag'
Get-ChildItem -LiteralPath $pkg -Recurse -File | Unblock-File -ErrorAction SilentlyContinue
Say '    Done - no more security pop-ups for these files.'

# ------------------------------------------------------------------ 2. existing install?
$installedDir = $null
try { $installedDir = (Get-ItemProperty -Path $regApp -ErrorAction Stop).InstallDir } catch { }
if (-not $installedDir -or -not (Test-Path -LiteralPath (Join-Path $installedDir 'server.py'))) {
    $installedDir = if (Test-Path -LiteralPath (Join-Path $defaultDir 'server.py')) { $defaultDir } else { $null }
}

Step 'Checking for an existing installation'
if ($installedDir) {
    $oldVersion = Get-AppVersion (Join-Path $installedDir 'server.py')
    Say "    Found v$oldVersion in $installedDir"
    $cmp = ([version]$newVersion).CompareTo([version]$oldVersion)
    if ($cmp -gt 0) {
        if (-not (Ask "Update v$oldVersion -> v$newVersion? (your portals and settings are kept)")) { Exit-Setup }
    } elseif ($cmp -eq 0) {
        Say "    v$newVersion is already installed." Green
        if (-not (Ask 'Repair / reinstall it anyway?' 'N')) { Exit-Setup }
    } else {
        Say "    The installed version (v$oldVersion) is NEWER than this package (v$newVersion)." Yellow
        if (-not (Ask "Replace it with the older v$newVersion?" 'N')) { Exit-Setup }
    }
    $dest = $installedDir
    Say '    (To move it to a different folder, run Uninstall first, then install again.)'
} else {
    Say '    Not installed yet - fresh install.'
    while ($true) {
        $dest = AskPath 'Where should Magical Portal System be installed?' $defaultDir
        if ($dest -match '^[A-Za-z]:\\Program Files') {
            Say '    Please choose a folder outside "Program Files" - the app saves its settings next to itself.' Yellow
            continue
        }
        if (-not [IO.Path]::IsPathRooted($dest)) { Say '    Please type a full path, e.g. D:\Apps\MagicalPortal' Yellow; continue }
        break
    }
}

# ------------------------------------------------------------------ 3. stop running copy
try {
    Invoke-WebRequest -UseBasicParsing -Method Post -Uri "http://127.0.0.1:$(Get-WebPort $dest)/api/shutdown" `
        -Body '{}' -ContentType 'application/json' -TimeoutSec 3 | Out-Null
    Step 'Stopped the copy that was running (start it again at the end)'
    Start-Sleep -Seconds 3
} catch { }

# ------------------------------------------------------------------ 4. copy files
Step "Copying v$newVersion to $dest"
New-Item -ItemType Directory -Force -Path $dest | Out-Null
robocopy $app $dest /E /NFL /NDL /NJH /NJS /NP /XF streams.json /XD logs __pycache__ | Out-Null
if ($LASTEXITCODE -ge 8) { throw "Copying files failed (robocopy code $LASTEXITCODE)." }
Copy-Item -LiteralPath (Join-Path $pkg 'README.txt') -Destination $dest -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'uninstall.ps1') -Destination $dest -Force
foreach ($old in @('Start Magical Portal System (hidden).vbs', 'Start (network access).bat')) {   # files from older versions
    $p = Join-Path $dest $old
    if (Test-Path -LiteralPath $p) { Remove-Item -LiteralPath $p -Force }
}
Set-Content -LiteralPath (Join-Path $dest 'Uninstall.bat') -Encoding ASCII -Value @'
@echo off
set "MPS_DIR=%~dp0"
cd /d "%TEMP%"
powershell -NoProfile -ExecutionPolicy Bypass -File "%MPS_DIR%uninstall.ps1"
'@
Get-ChildItem -LiteralPath $dest -Recurse -File | Unblock-File -ErrorAction SilentlyContinue
Say '    Done.' Green

# ------------------------------------------------------------------ 5. Python / FFmpeg / VLC
function Get-PyVersion($pythonw) {
    $py = Join-Path (Split-Path $pythonw) 'python.exe'
    if (-not (Test-Path -LiteralPath $py)) { return $null }
    $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try { return (& $py -c "import sys;print('%d.%d.%d' % sys.version_info[:3])" 2>$null | Select-Object -First 1) }
    catch { return $null }
    finally { $ErrorActionPreference = $old }
}

function Find-Pythonw($extraDir) {
    $c = @()
    if ($extraDir) { $c += (Join-Path $extraDir 'pythonw.exe') }
    # every Python registered with Windows (covers custom install folders)
    foreach ($root in 'HKCU:\Software\Python\PythonCore', 'HKLM:\Software\Python\PythonCore', 'HKLM:\Software\WOW6432Node\Python\PythonCore') {
        Get-ChildItem -Path $root -ErrorAction SilentlyContinue | Sort-Object { try { [version]$_.PSChildName } catch { [version]'0.0' } } -Descending | ForEach-Object {
            try {
                $ip = Get-ItemProperty -Path (Join-Path $_.PSPath 'InstallPath') -ErrorAction Stop
                if ($ip.WindowedExecutablePath) { $c += $ip.WindowedExecutablePath }
                if ($ip.'(default)') { $c += (Join-Path $ip.'(default)' 'pythonw.exe') }
            } catch { }
        }
    }
    $g = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($g) { $c += $g.Source }
    $c += Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\pythonw.exe", "$env:ProgramFiles\Python3*\pythonw.exe" -ErrorAction SilentlyContinue |
          Sort-Object FullName -Descending | ForEach-Object { $_.FullName }
    foreach ($p in $c) {
        if ($p -and ($p -notmatch 'WindowsApps') -and (Test-Path -LiteralPath $p)) {
            $v = Get-PyVersion $p
            if ($v -and ([version]$v -ge [version]'3.8')) { return $p }
        }
    }
    return $null
}

function Find-FFmpeg {
    $c = @()
    $g = Get-Command ffmpeg.exe -ErrorAction SilentlyContinue
    if ($g) { $c += $g.Source }
    $c += Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\Gyan.FFmpeg*\*\bin\ffmpeg.exe" -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName }
    $c += "$env:LOCALAPPDATA\Microsoft\WinGet\Links\ffmpeg.exe", 'C:\ffmpeg\bin\ffmpeg.exe', "$env:ProgramFiles\ffmpeg\bin\ffmpeg.exe"
    foreach ($p in $c) { if ($p -and (Test-Path -LiteralPath $p)) { return $p } }
    return $null
}

function Find-VLC {
    foreach ($p in @("$env:ProgramFiles\VideoLAN\VLC\vlc.exe", "${env:ProgramFiles(x86)}\VideoLAN\VLC\vlc.exe")) {
        if ($p -and (Test-Path -LiteralPath $p)) { return $p }
    }
    $g = Get-Command vlc.exe -ErrorAction SilentlyContinue
    if ($g) { return $g.Source }
    return $null
}

function Resolve-RealPath($p) {
    try {
        $i = Get-Item -LiteralPath $p -Force
        if ($i.LinkType -and $i.Target) {
            $t = @($i.Target)[0]
            if (-not [IO.Path]::IsPathRooted($t)) { $t = Join-Path (Split-Path $p) $t }
            if (Test-Path -LiteralPath $t) { return (Get-Item -LiteralPath $t).FullName }
        }
    } catch { }
    return $p
}

function Have-Winget {
    if (Get-Command winget.exe -ErrorAction SilentlyContinue) { return $true }
    Say '    winget is not available on this PC - please install it manually.' Yellow
    return $false
}

Step 'Checking for Python, FFmpeg and VLC'

# Python
$pyw = Find-Pythonw $null
if ($pyw) {
    Say "    Python $(Get-PyVersion $pyw) found: $pyw" Green
} elseif ((Ask 'Python (3.8 or newer) is not installed. Install Python 3.12 now?') -and (Have-Winget)) {
    $pyDefault = "$env:LOCALAPPDATA\Programs\Python\Python312"
    $pyDir = AskPath 'Where should Python be installed?' $pyDefault
    Say '    Installing Python (this can take a minute)...' Yellow
    if ($pyDir -ieq $pyDefault) {
        & winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements --silent
    } else {
        # custom folder: pass the official Python installer options straight through
        $override = "/quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1 Include_tcltk=1 Include_test=0 TargetDir=\`"$pyDir\`""
        $wgArgs = "install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements --override `"$override`""
        Start-Process -FilePath 'winget.exe' -ArgumentList $wgArgs -Wait -NoNewWindow
    }
    $pyw = Find-Pythonw $pyDir
    if ($pyw) { Say "    Python $(Get-PyVersion $pyw) installed: $pyw" Green }
}
function Test-Tkinter($pythonw) {
    try {
        $p = Start-Process -FilePath (Join-Path (Split-Path $pythonw) 'python.exe') -ArgumentList '-c "import tkinter"' `
             -Wait -PassThru -WindowStyle Hidden
        return ($p.ExitCode -eq 0)
    } catch { return $false }
}
if (-not $pyw) { Say '    Python : NOT FOUND - the program cannot run without it. Install Python and run Setup again.' Red }
elseif (-not (Test-Tkinter $pyw)) {
    Say '    Note: this Python has no tkinter, so the Browse... buttons will not work (typing paths still works).' Yellow
}

# FFmpeg
$ff = Find-FFmpeg
if (-not $ff -and (Ask 'FFmpeg is not installed. Install it now?') -and (Have-Winget)) {
    Say '    Installing FFmpeg (this can take a minute)...' Yellow
    & winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements --silent
    $ff = Find-FFmpeg
}
if ($ff) { $ff = Resolve-RealPath $ff; Say "    FFmpeg : $ff" Green } else { Say '    FFmpeg : not found (set it later in Settings)' Yellow }

# VLC
$vlc = Find-VLC
if (-not $vlc -and (Ask 'VLC is not installed. Install it now? (optional - FFplay can be used instead)') -and (Have-Winget)) {
    Say '    Installing VLC...' Yellow
    & winget install -e --id VideoLAN.VLC --accept-source-agreements --accept-package-agreements --silent
    $vlc = Find-VLC
}
if ($vlc) { Say "    VLC    : $vlc" Green } else { Say '    VLC    : not found (optional)' Yellow }

# save tool paths for the app (first install only - keeps your Settings on update)
$cfg = Join-Path $dest 'streams.json'
if (-not (Test-Path -LiteralPath $cfg)) {
    $settings = [ordered]@{ ffmpeg_path = [string]$ff; vlc_path = [string]$vlc }
    $json = [ordered]@{ settings = $settings; streams = @() } | ConvertTo-Json -Depth 4
    [IO.File]::WriteAllText($cfg, $json)   # UTF-8 without BOM
}

# ------------------------------------------------------------------ 6. shortcuts
$startupLnk = Join-Path ([Environment]::GetFolderPath('Startup')) 'Magical Portal System.lnk'
if ($pyw) {
    Step 'Creating shortcuts'
    $ws = New-Object -ComObject WScript.Shell
    function New-PortalShortcut($path) {
        $s = $ws.CreateShortcut($path)
        $s.TargetPath = $pyw
        $s.Arguments = "`"$dest\server.py`" --hidden"
        $s.WorkingDirectory = $dest
        $s.IconLocation = "$dest\web\portal.ico,0"
        $s.Description = "Magical Portal System v$newVersion"
        $s.Save()
    }
    New-PortalShortcut (Join-Path ([Environment]::GetFolderPath('Desktop')) 'Magical Portal System.lnk')
    New-PortalShortcut (Join-Path ([Environment]::GetFolderPath('Programs')) 'Magical Portal System.lnk')
    Say '    Desktop + Start menu shortcuts ready.' Green
    $hadStartup = Test-Path -LiteralPath $startupLnk
    if (Ask 'Start Magical Portal System automatically when this user logs in?' $(if ($installedDir -and -not $hadStartup) { 'N' } else { 'Y' })) {
        New-PortalShortcut $startupLnk
        Say '    Will start at logon.' Green
    } elseif ($hadStartup) {
        Remove-Item -LiteralPath $startupLnk -Force
    }
}

# ------------------------------------------------------------------ 7. register with Windows (Settings > Apps)
$sizeKB = [int]((Get-ChildItem -LiteralPath $dest -Recurse -File | Measure-Object Length -Sum).Sum / 1KB)
New-Item -Path $regApp -Force | Out-Null
Set-ItemProperty -Path $regApp -Name InstallDir -Value $dest
Set-ItemProperty -Path $regApp -Name Version -Value $newVersion
New-Item -Path $regUninst -Force | Out-Null
$props = @{
    DisplayName     = 'Magical Portal System'
    DisplayVersion  = $newVersion
    Publisher       = 'Magical Portal System'
    DisplayIcon     = "$dest\web\portal.ico"
    InstallLocation = $dest
    UninstallString = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$dest\uninstall.ps1`""
    InstallDate     = (Get-Date -Format 'yyyyMMdd')
}
foreach ($k in $props.Keys) { Set-ItemProperty -Path $regUninst -Name $k -Value $props[$k] }
foreach ($k in 'NoModify', 'NoRepair') { New-ItemProperty -Path $regUninst -Name $k -Value 1 -PropertyType DWord -Force | Out-Null }
New-ItemProperty -Path $regUninst -Name EstimatedSize -Value $sizeKB -PropertyType DWord -Force | Out-Null
Step "Registered v$newVersion with Windows (Settings > Apps > Installed apps)"

# ------------------------------------------------------------------ 8. Windows setup (one admin prompt)
# a) the friendly address  http://PortalSystem:8080  (hosts file)   b) firewall rules
$webName = Get-WebName $dest
$webPort = Get-WebPort $dest
$hostsFile = Join-Path $env:SystemRoot 'System32\drivers\etc\hosts'
$haveName = $false
try { $haveName = [bool](Select-String -LiteralPath $hostsFile -Pattern "^\s*127\.0\.0\.1\s+$([regex]::Escape($webName.ToLower()))(\s|$)" -ErrorAction Stop) } catch { }

$progs = [ordered]@{}
if ($ff) {
    $progs['FFmpeg'] = $ff
    $ffplay = Join-Path (Split-Path $ff) 'ffplay.exe'
    if (Test-Path -LiteralPath $ffplay) { $progs['FFplay'] = $ffplay }
}
if ($vlc) { $progs['VLC'] = $vlc }
& netsh advfirewall firewall show rule name="Magical Portal - FFmpeg" > $null 2>&1
$haveRules = ($LASTEXITCODE -eq 0)

Step 'Windows setup'
$doName = $false
if ($haveName) { Say "    Address http://$($webName):$webPort already works on this PC." Green }
else { $doName = Ask "Open the control page as  http://$($webName):$webPort  on this PC? (otherwise http://localhost:$webPort)" }
$doFw = $false
if ($progs.Count -gt 0) {
    if ($haveRules) { $doFw = Ask 'Firewall rules already exist. Refresh them?' 'N' }
    else { $doFw = Ask 'Allow FFmpeg/VLC through Windows Firewall? (needed to RECEIVE streams)' }
}
if ($doName -or $doFw) {
    Say '    Windows will ask once for admin permission...'
    $ps = @('$ErrorActionPreference = "Continue"')
    if ($doName) {
        $ps += "`$h = '$hostsFile'"
        $ps += '$lines = @(Get-Content -LiteralPath $h -ErrorAction SilentlyContinue | Where-Object { $_ -notmatch "# Magical Portal System" })'
        $ps += "`$lines += '127.0.0.1    $($webName.ToLower())    # Magical Portal System'"
        $ps += 'Set-Content -LiteralPath $h -Value $lines -Encoding ASCII'
        $ps += 'ipconfig /flushdns | Out-Null'
    }
    if ($doFw) {
        foreach ($k in $progs.Keys) {
            $ps += "netsh advfirewall firewall delete rule name=`"Magical Portal - $k`" | Out-Null"
            $ps += "netsh advfirewall firewall add rule name=`"Magical Portal - $k`" dir=in action=allow program=`"$($progs[$k])`" protocol=UDP profile=any enable=yes | Out-Null"
        }
    }
    $tmp = Join-Path $env:TEMP 'magical_portal_setup_admin.ps1'
    Set-Content -LiteralPath $tmp -Value $ps -Encoding ASCII
    try {
        Start-Process -FilePath 'powershell.exe' -Verb RunAs -Wait -WindowStyle Hidden `
            -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$tmp`""
        if ($doName) { Say "    Address http://$($webName):$webPort is ready." Green }
        if ($doFw)   { Say '    Firewall rules added.' Green }
    } catch {
        Say '    Skipped (admin prompt was declined). You can do both later from Settings in the app.' Yellow
    }
    Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
}

# ------------------------------------------------------------------ done
Write-Host ''
Write-Host '  ==============================================' -ForegroundColor Green
Write-Host "     Magical Portal System v$newVersion is ready!" -ForegroundColor Green
Write-Host '  ==============================================' -ForegroundColor Green
Say "    Installed in : $dest"
Say '    Start it     : "Magical Portal System" icon on the Desktop / Start menu'
Say "    Control page : http://$(Get-WebName $dest):$(Get-WebPort $dest)  (name and port: Settings > Web page)"
Say '    While running: portal icon in the hidden icons (^) next to the clock'
Say '    Stop it      : right-click that icon > Shut down  (or the button in the page)'
Say '    Remove it    : Settings > Apps > Installed apps, or Uninstall.bat in the install folder'
if ($pyw -and (Ask 'Start Magical Portal System now?')) {
    Start-Process -FilePath $pyw -ArgumentList "`"$dest\server.py`" --hidden" -WorkingDirectory $dest
}
