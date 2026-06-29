<#
  get-lumen.ps1 - one-line installer for Windows.

    iwr -useb https://raw.githubusercontent.com/devwspito/lumen-runtime/main/get-lumen.ps1 | iex

  Installs the `lumen` command on your PATH, then starts Lumen with the security
  cage (loopback) and opens your browser at this boot's unique token. Afterwards:
    lumen          open it          lumen stop     stop it
    lumen update   update it        lumen status   status
  The model, Composio, agents and skills are all configured IN THE UI.

  Windows requires Podman + the WSL2 backend (the cage uses systemd + Landlock/
  seccomp/netns inside a rootful podman machine). This installer guides those.
#>
$ErrorActionPreference = 'Stop'

# Windows PowerShell 5.1 defaults to TLS 1.0; raw.githubusercontent.com needs 1.2+.
try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072 } catch { }

$LUMEN_CLI_URL = if ($env:LUMEN_CLI_URL) { $env:LUMEN_CLI_URL } else { 'https://raw.githubusercontent.com/devwspito/lumen-runtime/main/lumen.ps1' }

# --- prerequisites: WSL2 + Podman -------------------------------------------
# WSL2 hosts the Linux VM the cage runs in. Podman provides the systemd PID1 +
# caps + seccomp + unmask the cage needs (Docker can't reproduce it on Windows).
$wslOk = $false
try { wsl --status *> $null; $wslOk = ($LASTEXITCODE -eq 0) } catch { $wslOk = $false }
if (-not $wslOk) {
  Write-Host "[!] WSL2 is not ready. Enable it (admin PowerShell), reboot, then re-run:"
  Write-Host "      wsl --install"
}
if (-not (Get-Command podman -ErrorAction SilentlyContinue)) {
  Write-Host "[x] Podman is required on Windows (Docker can't run the cage)."
  Write-Host "    Install Podman Desktop from https://podman.io/ , then re-run this installer."
  exit 1
}

# --- install the CLI (no admin / no sudo) -----------------------------------
$BIN = Join-Path $env:LOCALAPPDATA 'Programs\lumen'
New-Item -ItemType Directory -Force -Path $BIN *> $null

Write-Host "[*] Installing the 'lumen' command into $BIN ..."
$ps1Path = Join-Path $BIN 'lumen.ps1'
try {
  Invoke-RestMethod -Uri $LUMEN_CLI_URL -OutFile $ps1Path -ErrorAction Stop
} catch {
  Write-Host "[x] Could not download the CLI ($LUMEN_CLI_URL)."
  exit 1
}
# Strip the mark-of-the-web so the freshly-downloaded .ps1 is not blocked.
Unblock-File -Path $ps1Path -ErrorAction SilentlyContinue

# A tiny lumen.cmd shim so `lumen <cmd>` works from cmd.exe AND PowerShell, with
# the right ExecutionPolicy (no global policy change required).
$cmdPath = Join-Path $BIN 'lumen.cmd'
@'
@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0lumen.ps1" %*
'@ | Set-Content -Path $cmdPath -Encoding ASCII

# --- put $BIN on the User PATH (idempotent) ---------------------------------
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if (-not $userPath) { $userPath = '' }
# Case-insensitive check (Windows PATH is case-insensitive) so re-runs don't append duplicates.
$onPath = $false
foreach ($p in ($userPath -split ';')) { if ($p.TrimEnd('\') -ieq $BIN.TrimEnd('\')) { $onPath = $true; break } }
if (-not $onPath) {
  $newPath = if ($userPath.TrimEnd(';')) { "$($userPath.TrimEnd(';'));$BIN" } else { $BIN }
  [Environment]::SetEnvironmentVariable('Path', $newPath, 'User')
  Write-Host "[*] Added $BIN to your PATH (new terminals will pick it up)."
}
# Make `lumen` resolvable in THIS session too.
if (($env:Path -split ';') -notcontains $BIN) { $env:Path = "$env:Path;$BIN" }

# --- first run: pull the image, run with the cage, open the browser ---------
# Invoke through the .cmd shim (it applies -ExecutionPolicy Bypass), NOT the .ps1
# directly — a stock host's Restricted/RemoteSigned policy would block running the
# freshly-downloaded file by path. (Forwards LUMEN_IMAGE/PORT/SECCOMP_URL env.)
$ErrorActionPreference = 'Continue'
& $cmdPath update

# --- enterprise pairing: if LUMEN_PAIR_CODE is set, associate after first run ---
# The code is copied to a local var and the env var cleared immediately so it
# does not persist; `lumen pair` passes it to the container via stdin (not argv).
if ($env:LUMEN_PAIR_CODE) {
  $pairCode = $env:LUMEN_PAIR_CODE
  Remove-Item Env:LUMEN_PAIR_CODE -ErrorAction SilentlyContinue
  Write-Host "[*] Pairing with enterprise code (from env)..."
  & $cmdPath pair $pairCode
  $pairCode = $null
}
