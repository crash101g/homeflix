<#
Homeflix installer for Windows - installs the app and a `homeflix` command you run
yourself. Nothing starts automatically unless you ask for it.

Easiest: double-click install.cmd. Or, in PowerShell from this folder:

  powershell -ExecutionPolicy Bypass -File install.ps1                 install; run it yourself with `homeflix`
  powershell -ExecutionPolicy Bypass -File install.ps1 -Service        also add a background task you start/stop
                                                                       yourself; it does NOT start at login
  powershell -ExecutionPolicy Bypass -File install.ps1 -Autostart      add the background task AND start it at login
  powershell -ExecutionPolicy Bypass -File install.ps1 -RemoveService  remove the background task, keep everything else
  powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall      remove the task and the `homeflix` command

(install.cmd takes the same options, e.g. install.cmd -Service)
Works with Windows PowerShell 5.1 and PowerShell 7.
#>
[CmdletBinding()]
param(
    [switch]$Service,
    [switch]$Autostart,
    [switch]$RemoveService,
    [switch]$Uninstall
)
$ErrorActionPreference = 'Stop'

$AppDir   = Join-Path $env:LOCALAPPDATA 'Programs\Homeflix'
$ConfDir  = Join-Path $env:APPDATA 'Homeflix'          # homeflix.py looks here for its settings
$EnvFile  = Join-Path $ConfDir 'env'
$LogFile  = Join-Path $ConfDir 'homeflix.log'
$Launcher = Join-Path $AppDir 'homeflix.cmd'
$TaskName = 'Homeflix'
$StartupLink = Join-Path ([Environment]::GetFolderPath('Startup')) 'Homeflix.lnk'
$Src = Join-Path $PSScriptRoot 'homeflix.py'

function Write-Bold($t) { Write-Host $t -ForegroundColor White }

function Ask([string]$Question, [bool]$Default) {
    $hint = if ($Default) { '[Y/n]' } else { '[y/N]' }
    $a = Read-Host "$Question $hint"
    if ([string]::IsNullOrWhiteSpace($a)) { return $Default }
    return $a.Trim().ToLower().StartsWith('y')
}

function Write-Utf8NoBom([string]$Path, [string]$Text) {
    [IO.File]::WriteAllText($Path, $Text, (New-Object Text.UTF8Encoding $false))
}

# --- user PATH (edited in the registry so %VARIABLES% in it are kept as they are) ---
function Get-UserPathRaw {
    $key = Get-Item 'HKCU:\Environment'
    return [string]$key.GetValue('Path', '', 'DoNotExpandEnvironmentNames')
}
function Set-UserPathRaw([string]$Value) {
    New-ItemProperty -Path 'HKCU:\Environment' -Name 'Path' -Value $Value -PropertyType ExpandString -Force | Out-Null
    # Tell Explorer the environment changed, so new terminals see the new PATH.
    [Environment]::SetEnvironmentVariable('HOMEFLIX_PATH_REFRESH', '1', 'User')
    [Environment]::SetEnvironmentVariable('HOMEFLIX_PATH_REFRESH', $null, 'User')
}
function Add-ToUserPath([string]$Dir) {
    $parts = @((Get-UserPathRaw) -split ';' | Where-Object { $_ })
    if ($parts -contains $Dir) { return $false }
    Set-UserPathRaw ((@($parts) + $Dir) -join ';')
    $env:Path = "$env:Path;$Dir"
    return $true
}
function Remove-FromUserPath([string]$Dir) {
    $parts = @((Get-UserPathRaw) -split ';' | Where-Object { $_ })
    if ($parts -notcontains $Dir) { return }
    Set-UserPathRaw ((@($parts | Where-Object { $_ -ne $Dir })) -join ';')
}

# --- background task -----------------------------------------------------------------
function Get-HomeflixTask { Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue }

function Remove-HomeflixService([switch]$Quiet) {
    $had = $false
    if (Get-HomeflixTask) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        $had = $true
    }
    if (Test-Path $StartupLink) { Remove-Item $StartupLink -Force; $had = $true }
    if ($Quiet) { return }
    if ($had) { Write-Host 'Background task removed.' } else { Write-Host 'No background task installed.' }
}

if ($RemoveService) { Remove-HomeflixService; exit 0 }
if ($Uninstall) {
    Remove-HomeflixService
    if (Test-Path $Launcher) { Remove-Item $Launcher -Force }
    Remove-FromUserPath $AppDir
    Write-Host "Removed the 'homeflix' command. App files left in $AppDir, settings and watch history in $ConfDir."
    if (Get-NetFirewallRule -DisplayName 'Homeflix' -ErrorAction SilentlyContinue) {
        Write-Host "To remove the firewall rule, run in an admin PowerShell:  Remove-NetFirewallRule -DisplayName Homeflix"
    }
    exit 0
}
if ($Autostart) { $Service = $true }

if (-not (Test-Path $Src)) { Write-Host 'homeflix.py must sit next to install.ps1'; exit 1 }

Write-Bold '== Homeflix installer =='

# --- Python ----------------------------------------------------------------------------
function Find-Python {
    $candidates = @(@('py', '-3'), @('python'), @('python3'))
    foreach ($c in $candidates) {
        $exe = $c[0]
        $rest = @($c | Select-Object -Skip 1)
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        try {
            # no double quotes in -c code: Windows PowerShell 5.1 strips them from native arguments
            $out = & $exe @rest -c 'import sys; print(sys.executable if sys.version_info >= (3, 8) else str())' 2>$null
        } catch { continue }
        if ($LASTEXITCODE -ne 0 -or -not $out) { continue }
        $path = ([string]($out | Select-Object -Last 1)).Trim()
        if ($path -and (Test-Path $path)) { return $path }
    }
    return $null
}
$Py = Find-Python
if (-not $Py) {
    Write-Host 'Python 3.8 or newer is required.'
    if ((Get-Command winget -ErrorAction SilentlyContinue) -and (Ask 'Install Python 3.12 now with winget?' $true)) {
        winget install --id Python.Python.3.12 -e --scope user --accept-source-agreements --accept-package-agreements
        $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
        $Py = Find-Python
    }
    if (-not $Py) {
        Write-Host 'Install Python from https://www.python.org/downloads/ (tick "Add python.exe to PATH"), then run this again.'
        exit 1
    }
}
Write-Host "Using Python: $Py"

# --- ffmpeg ----------------------------------------------------------------------------
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue) -or -not (Get-Command ffprobe -ErrorAction SilentlyContinue)) {
    Write-Host "ffmpeg is not installed. It's needed for thumbnails, durations and playing MKV/AVI/HEVC files."
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        if (Ask 'Install it now with winget?' $true) {
            winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
        }
    } else {
        Write-Host 'Get it from https://www.gyan.dev/ffmpeg/builds/ and put its bin folder on your PATH.'
    }
}

# --- app files -------------------------------------------------------------------------
New-Item -ItemType Directory -Force -Path $AppDir, $ConfDir | Out-Null
Copy-Item -Force $Src (Join-Path $AppDir 'homeflix.py')
Write-Host "Installed to $AppDir\homeflix.py"

# --- settings --------------------------------------------------------------------------
function Esc([string]$s) { return $s.Replace('\', '\\').Replace('"', '\"') }
function Expand-Home([string]$p) { if ($p.StartsWith('~')) { return $HOME + $p.Substring(1) } return $p }

if (-not (Test-Path $EnvFile)) {
    do {
        $port = Read-Host 'Port [8484]'
        if (-not $port) { $port = '8484' }
    } until ($port -match '^\d{1,5}$')
    $defVid = Join-Path $HOME 'Downloads\movies'
    $vid = Read-Host "Movies folder [$defVid]"
    if (-not $vid) { $vid = $defVid }
    $defMus = Join-Path $HOME 'Music'
    $mus = Read-Host "Music folder [$defMus]"
    if (-not $mus) { $mus = $defMus }
    $vid = Expand-Home $vid.Trim('"')
    $mus = Expand-Home $mus.Trim('"')
    Write-Host 'Set a password so only people you give it to can watch (Enter = no password).'
    $sec = Read-Host 'Password' -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
    try { $pw = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
    $lines = @("HOMEFLIX_PORT=$port", "HOMEFLIX_VIDEOS=`"$(Esc $vid)`"", "HOMEFLIX_MUSIC=`"$(Esc $mus)`"")
    if ($pw) { $lines += "HOMEFLIX_PASSWORD=`"$(Esc $pw)`"" }
    Write-Utf8NoBom $EnvFile (($lines -join "`r`n") + "`r`n")
    Write-Host "Settings saved to $EnvFile (edit it any time with Notepad)"
} else {
    Write-Host "Keeping existing settings in $EnvFile"
}
$Port = '8484'
$m = Select-String -Path $EnvFile -Pattern '^HOMEFLIX_PORT=(\d+)' | Select-Object -First 1
if ($m) { $Port = $m.Matches[0].Groups[1].Value }

# --- the `homeflix` command ------------------------------------------------------------
# Written in the console's code page, with %LOCALAPPDATA%/%USERPROFILE% in place of the
# literal folders, so user names with accents still work in cmd.exe.
function To-CmdPath([string]$p) {
    foreach ($v in @('LOCALAPPDATA', 'APPDATA', 'USERPROFILE')) {
        $val = [Environment]::GetEnvironmentVariable($v)
        if ($val -and $p.StartsWith($val, [StringComparison]::OrdinalIgnoreCase)) {
            return "%$v%" + $p.Substring($val.Length)
        }
    }
    return $p
}
$cmdText = "@echo off`r`nrem Runs Homeflix with the settings in %APPDATA%\Homeflix\env. Options are passed through.`r`n" +
           "`"$(To-CmdPath $Py)`" `"%~dp0homeflix.py`" %*`r`n"
$oem = [Text.Encoding]::GetEncoding([Globalization.CultureInfo]::CurrentCulture.TextInfo.OEMCodePage)
[IO.File]::WriteAllText($Launcher, $cmdText, $oem)
$pathAdded = Add-ToUserPath $AppDir
Write-Host "Installed the 'homeflix' command to $Launcher"

# --- firewall: allow the port from the local network only ------------------------------
if (-not (Get-NetFirewallRule -DisplayName 'Homeflix' -ErrorAction SilentlyContinue)) {
    if (Ask "Allow port $Port through Windows Firewall from your local network only? (asks for admin)" $true) {
        $fw = "New-NetFirewallRule -DisplayName 'Homeflix' -Direction Inbound -Protocol TCP -LocalPort $Port " +
              "-RemoteAddress LocalSubnet -Action Allow -Profile Any | Out-Null"
        try {
            Start-Process powershell -Verb RunAs -Wait -WindowStyle Hidden -ArgumentList @('-NoProfile', '-Command', $fw)
            Write-Host 'Firewall rule added.'
        } catch {
            Write-Host 'Skipped the firewall rule (admin permission was declined).'
        }
    }
}

$ip = $null
try {
    $ip = & $Py -c 'import socket; s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect((''10.255.255.255'', 1)); print(s.getsockname()[0])' 2>$null
} catch { }
if ($ip) { $ip = ([string]($ip | Select-Object -Last 1)).Trim() }
if (-not $ip) { $ip = '<this-computer-ip>' }
Write-Host ''

# --- manual (default) ------------------------------------------------------------------
if (-not $Service) {
    if ((Get-HomeflixTask) -or (Test-Path $StartupLink)) {
        Write-Host 'Note: a Homeflix background task from an earlier install is still set up.'
        if (Ask 'Remove it so Homeflix only runs when you start it?' $true) { Remove-HomeflixService }
        Write-Host ''
    }
    Write-Bold 'Homeflix is installed. Start it whenever you want with:'
    Write-Host '  homeflix                 (Ctrl+C to stop)'
    if ($pathAdded) { Write-Host '  (open a NEW terminal window first, so it picks up the homeflix command)' }
    Write-Host ''
    Write-Host "  Then open on this PC:     http://localhost:$Port"
    Write-Host "  Others on your network:   http://${ip}:$Port"
    Write-Host ''
    Write-Host 'The first time, Windows may ask whether Python can use the network. Allow it on private networks.'
    Write-Host 'Want it as a background task instead? Re-run with -Service or -Autostart.'
    exit 0
}

# --- background task (-Service / -Autostart) --------------------------------------------
$Pyw = Join-Path (Split-Path $Py) 'pythonw.exe'      # no console window
if (-not (Test-Path $Pyw)) { $Pyw = $Py }
Remove-HomeflixService -Quiet

$user = "$env:USERDOMAIN\$env:USERNAME"
$action = New-ScheduledTaskAction -Execute $Pyw -Argument "`"$AppDir\homeflix.py`" --quiet" -WorkingDirectory $AppDir
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$reg = @{
    TaskName    = $TaskName
    Action      = $action
    Settings    = $settings
    Principal   = $principal
    Description = 'Homeflix media server (installed by install.ps1)'
    Force       = $true
}
$atLogon = $false
if ($Autostart) {
    try {
        Register-ScheduledTask @reg -Trigger (New-ScheduledTaskTrigger -AtLogOn -User $user) | Out-Null
        $atLogon = $true
    } catch {
        # Some PCs only let admins add logon triggers. Fall back to a Startup-folder shortcut.
        Register-ScheduledTask @reg | Out-Null
        $sh = New-Object -ComObject WScript.Shell
        $lnk = $sh.CreateShortcut($StartupLink)
        $lnk.TargetPath = Join-Path $env:WINDIR 'System32\schtasks.exe'
        $lnk.Arguments = "/Run /TN `"$TaskName`""
        $lnk.WindowStyle = 7   # minimized
        $lnk.Save()
        $atLogon = $true
    }
} else {
    Register-ScheduledTask @reg | Out-Null
}

Start-ScheduledTask -TaskName $TaskName
$up = $false
for ($i = 0; $i -lt 10 -and -not $up; $i++) {
    Start-Sleep -Seconds 1
    try {
        $c = New-Object Net.Sockets.TcpClient
        $c.Connect('127.0.0.1', [int]$Port)
        $c.Close()
        $up = $true
    } catch { }
}

Write-Host ''
$suffix = if ($atLogon) { ' (starts at login)' } else { '' }
if ($up) { Write-Bold "Homeflix is running in the background$suffix." }
else { Write-Bold "The Homeflix task is set up$suffix, but it isn't answering yet. Check the log below." }
Write-Host "  On this PC:          http://localhost:$Port"
Write-Host "  Others on your LAN:  http://${ip}:$Port"
Write-Host "  Stop / start:        Stop-ScheduledTask Homeflix  /  Start-ScheduledTask Homeflix"
Write-Host "  Log:                 Get-Content -Wait `"$LogFile`""
