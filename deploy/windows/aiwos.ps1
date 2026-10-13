# AI WORK OS local CLI on Windows: PowerShell drives the controller inside WSL2 Ubuntu.
#
#   .\deploy\windows\aiwos.ps1 doctor
#   .\deploy\windows\aiwos.ps1 init --repo-dir /var/lib/aiwos/repo --state-dir /var/lib/aiwos/state `
#       --workspaces-dir /var/lib/aiwos/workspaces --agent-user aiwos-agent --agent-group aiwos-ws
#   .\deploy\windows\aiwos.ps1 start | status | stop | sync
#   .\deploy\windows\aiwos.ps1 review tasks/DUMMY-0001/task.json
#
# Everything runs in WSL as the controller user (aiwos-ctl); the AI CLIs run as the
# separate agent user (aiwos-agent). No VPS, no GitHub Actions, no API keys.
# Arguments are passed to wsl.exe as an argument list: no Invoke-Expression, no shell.
# Setup and what is still unverified on a real PC: docs/local-cli.md.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet('init', 'doctor', 'start', 'status', 'stop', 'sync', 'review')]
    [string] $Command,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $ExtraArguments = @(),
    [string] $Distro = 'Ubuntu',
    [string] $LinuxUser = 'aiwos-ctl',
    [string] $LinuxCode = '/opt/aiwos/code',
    [string] $LinuxConfig = '/var/lib/aiwos-ctl/local.json'
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
    throw 'WSL is not installed. In an administrator PowerShell: wsl --install -d Ubuntu'
}
if ($LinuxUser -eq 'root' -or $LinuxUser -eq 'aiwos-agent') {
    throw "Run as the controller user, not $LinuxUser."
}

# wsl.exe prints UTF-16 unless WSL_UTF8 is set; strip NULs either way
$env:WSL_UTF8 = '1'
$distros = @(& wsl.exe --list --quiet 2>$null | ForEach-Object { ($_ -replace "`0", '').Trim() } | Where-Object { $_ })
if ($LASTEXITCODE -ne 0) {
    throw 'Could not list the WSL distributions. Check the WSL2 installation.'
}
if ($distros -notcontains $Distro) {
    throw "WSL distribution '$Distro' not found (found: $($distros -join ', ')). Use -Distro <name>."
}

# --exec: no Linux shell in between (the controller user has /usr/sbin/nologin as its shell)
# --cd /: do not start in a translated Windows directory (drive mounts are off under docs/local-cli.md)
$wslArgs = @('-d', $Distro, '--cd', '/', '-u', $LinuxUser, '--exec', '/usr/bin/python3',
             "$LinuxCode/scripts/aiwos-local.py", '--config', $LinuxConfig, $Command) + $ExtraArguments
& wsl.exe @wslArgs
exit $LASTEXITCODE
