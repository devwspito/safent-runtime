<#
  lumen.ps1 - control the local Lumen container from PowerShell (Windows).

    lumen            open Lumen in the browser (starts it if stopped)
    lumen stop       stop Lumen
    lumen start      start Lumen without opening the browser
    lumen restart    restart Lumen
    lumen update     pull the latest image and recreate (keeps your config)
    lumen status     is it running? on which port?
    lumen logs       follow the container journal
    lumen pair <code>   associate with an enterprise tenant (same image, associate mode)
    lumen unpair        remove the enterprise association (reverts to community edition)

  Windows parity note: Lumen ships as ONE container (systemd PID1 + the kernel
  cage: Landlock/seccomp/netns/nftables). On Windows that container runs inside a
  ROOTFUL podman machine (a Linux VM on the WSL2/Hyper-V backend) — exactly the
  macOS model. The run flags are IDENTICAL to Linux/macOS; only the launcher
  language, the machine bootstrap, and "open browser" differ. We mandate Podman
  (Docker has no --systemd=always / unmask equivalent, so its systemd-in-container
  cage cannot be reproduced). The model, Composio, agents and skills are all
  configured IN THE UI.
#>
# Parse args from $args (NOT a param block): a typed param block makes PowerShell
# treat `lumen -h` / `--help` as named parameters and throw before dispatch. With
# no param block, dashed tokens land in $args verbatim and reach the switch — the
# same as the sh `case` seeing "-h"/"--help".
$Command = if ($args.Count -ge 1) { [string]$args[0] } else { 'open' }
$Arg     = if ($args.Count -ge 2) { [string]$args[1] } else { $null }

# Native podman calls write progress/warnings to stderr; under
# $ErrorActionPreference='Stop' (PowerShell 7.3+ PSNativeCommandUseErrorActionPreference)
# a harmless stderr line can become terminating. We drive control flow off
# $LASTEXITCODE explicitly, so keep native stderr non-terminating and scope Stop
# to the web downloads (via -ErrorAction Stop + try/catch).
$ErrorActionPreference = 'Continue'

# Windows PowerShell 5.1 defaults to TLS 1.0; raw.githubusercontent.com needs 1.2+.
try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072 } catch { }

# ONE runtime image for both community and associate (paired) operation. The image
# carries no edition-specific code: pairing state (the .enterprise marker +
# is_associated()) activates associate behavior at runtime, so the same image runs
# both roles. There is intentionally no separate "enterprise" image.
$IMAGE          = if ($env:LUMEN_IMAGE)          { $env:LUMEN_IMAGE }          else { 'ghcr.io/devwspito/lumen:latest' }
$NAME           = if ($env:LUMEN_NAME)           { $env:LUMEN_NAME }           else { 'lumen' }
$PORT_PIN       = $env:LUMEN_PORT
# Enterprise cloud the instance pairs/syncs against. Empty for community edition.
$CLOUD_ENDPOINT = $env:LUMEN_CLOUD_ENDPOINT
# Named volume that holds the persistent state (config, identity, skills, memory).
$DATA_VOLUME    = if ($env:LUMEN_DATA_VOLUME)    { $env:LUMEN_DATA_VOLUME }    else { 'lumen-data' }
$SECCOMP_URL    = if ($env:LUMEN_SECCOMP_URL)    { $env:LUMEN_SECCOMP_URL }    else { 'https://raw.githubusercontent.com/devwspito/lumen-runtime/main/ops/container/seccomp/lumen.json' }
$HomeDir        = if ($env:USERPROFILE) { $env:USERPROFILE } elseif ($env:LOCALAPPDATA) { $env:LOCALAPPDATA } else { $env:TEMP }
$SECCOMP_DIR    = Join-Path $HomeDir '.lumen'
$SECCOMP        = Join-Path $SECCOMP_DIR 'lumen-seccomp.json'

# Pairing marker — written to the persistent volume on pair, removed on unpair.
# config-sync's systemd ConditionPathExists keys off this marker.
$ENTERPRISE_MARKER = '/var/lib/hermes/instance/.enterprise'

# --- runtime resolution: Podman is REQUIRED on Windows -----------------------
# Docker Desktop cannot run the cage: --systemd=always and --security-opt
# unmask=/sys/kernel/security are Podman-only, and Docker's systemd-PID1 story
# can't reproduce the per-unit cgroup/cap setup the confinement self-check
# fail-closes on. So we mandate Podman, exactly as the macOS installer does.
$RT = (Get-Command podman -ErrorAction SilentlyContinue).Source
if (-not $RT) {
  Write-Host "[x] Lumen on Windows needs Podman (the security cage uses systemd + Landlock/"
  Write-Host "    seccomp/netns inside a rootful podman machine; Docker can't reproduce it)."
  Write-Host "    1) Enable WSL2:        wsl --install   (then reboot)"
  Write-Host "    2) Install Podman:     https://podman.io/  (Podman Desktop)"
  Write-Host "    3) Re-run this command."
  exit 1
}

# ---- helpers ----------------------------------------------------------------

function Test-Exists  { & $RT inspect $NAME *> $null; return ($LASTEXITCODE -eq 0) }
function Test-Running {
  $r = (& $RT inspect -f '{{.State.Running}}' $NAME 2>$null)
  return (($r | Out-String).Trim() -eq 'true')
}

# Single image for every role (kept as a function for a future per-instance override).
function Get-Image { return $IMAGE }

# Windows: the cage runs inside a podman machine, which MUST be rootful (the cage
# needs root in the VM for netns, nftables, securityfs, cgroup mounts; a rootless
# machine fails hermes-runtime with 226/NAMESPACE). Mirrors the macOS branch.
function Initialize-Machine {
  $name = (& $RT machine list -q 2>$null | Select-Object -First 1)
  if (-not $name) {
    Write-Host "[*] No podman machine - creating a rootful one (first time only, takes a bit)..."
    & $RT machine init --rootful --cpus 4 --memory 8192 --disk-size 60
    if ($LASTEXITCODE -ne 0) { Write-Host "[x] Could not create the podman machine."; exit 1 }
    & $RT machine start
    if ($LASTEXITCODE -ne 0) { Write-Host "[x] Could not start the podman machine."; exit 1 }
    $name = (& $RT machine list -q 2>$null | Select-Object -First 1)
  }
  $rootful = ((& $RT machine inspect $name --format '{{.Rootful}}' 2>$null | Select-Object -First 1) | Out-String).Trim()
  if ($rootful -ne 'true') {
    Write-Host "[x] The podman machine '$name' is rootless; the cage needs rootful. Convert it:"
    Write-Host "    podman machine stop $name; podman machine set --rootful $name; podman machine start $name"
    exit 1
  }
  & $RT info *> $null
  if ($LASTEXITCODE -ne 0) { & $RT machine start $name *> $null }
  Confirm-CageKernel $name
}

# Parity-critical: the daemon fail-closes (hermes-landlock-assert, ExecStartPre)
# unless Landlock is in /sys/kernel/security/lsm. Ensure the VM kernel is >= 6.6
# (stock WSL2 6.6/6.18 ship CONFIG_SECURITY_LANDLOCK=y + CONFIG_SECURITYFS=y; the
# legacy 5.15 default does NOT) and that securityfs is actually mounted in the VM.
function Confirm-CageKernel($name) {
  $kver = ((& $RT machine ssh $name 'uname -r' 2>$null) | Out-String).Trim()
  if ($kver -match '^(\d+)\.(\d+)') {
    $maj = [int]$Matches[1]; $min = [int]$Matches[2]
    if ($maj -lt 6 -or ($maj -eq 6 -and $min -lt 6)) {
      Write-Host "[!] The VM kernel is $kver; Lumen's cage needs >= 6.6 (Landlock + securityfs)."
      Write-Host "    Update it:  wsl --update   then:  podman machine stop $name; podman machine start $name"
    }
  }
  # securityfs must be mounted so /sys/kernel/security/lsm exists (the assert reads it).
  $lsm = ((& $RT machine ssh $name 'cat /sys/kernel/security/lsm 2>/dev/null' 2>$null) | Out-String).Trim()
  if (-not $lsm) {
    & $RT machine ssh $name 'sudo mount -t securityfs none /sys/kernel/security 2>/dev/null' *> $null
    $lsm = ((& $RT machine ssh $name 'cat /sys/kernel/security/lsm 2>/dev/null' 2>$null) | Out-String).Trim()
  }
  if ($lsm -and ($lsm -notmatch 'landlock')) {
    Write-Host "[!] Landlock is not active in the VM (lsm='$lsm'). Lumen fail-closes without it."
    Write-Host "    Update the WSL2 kernel (wsl --update) to one with Landlock, then restart the machine."
  }
}

# seccomp profile under %USERPROFILE%\.lumen (podman reads it client-side and ships
# the JSON into the machine, so a Windows path is fine).
function Initialize-Seccomp {
  New-Item -ItemType Directory -Force -Path $SECCOMP_DIR *> $null
  try {
    Invoke-RestMethod -Uri $SECCOMP_URL -OutFile $SECCOMP -ErrorAction Stop
  } catch {
    Write-Host "[x] Could not download the seccomp profile ($SECCOMP_URL)."
    Write-Host "    Set LUMEN_SECCOMP_URL to a reachable URL if the repo is not public."
    exit 1
  }
}

# Recreate the container with the hardened cage (loopback + min caps + seccomp).
# The run flags are IDENTICAL to Linux/macOS (run-lumen.sh) — that is the parity.
function Invoke-Run {
  Initialize-Machine
  Initialize-Seccomp
  $image = Get-Image
  & $RT rm -f $NAME *> $null
  $publish = if ($PORT_PIN) { "127.0.0.1:${PORT_PIN}:7517" } else { "127.0.0.1::7517" }
  $runArgs = @(
    'run','-d','--name', $NAME, '--systemd=always',
    '-p', $publish
  )
  if ($CLOUD_ENDPOINT) { $runArgs += @('-e', "LUMEN_CLOUD_ENDPOINT=$CLOUD_ENDPOINT") }
  $runArgs += @(
    '--cap-add','NET_ADMIN','--cap-add','SYS_ADMIN','--cap-add','AUDIT_READ',
    '--security-opt', "seccomp=$SECCOMP",
    '--security-opt','unmask=/sys/kernel/security',
    '--security-opt','label=disable',
    '-v','/sys/kernel/security:/sys/kernel/security:ro',
    '-v', "${DATA_VOLUME}:/var/lib/hermes",
    '--shm-size=1g',
    $image
  )
  & $RT @runArgs *> $null
  if ($LASTEXITCODE -ne 0) { Write-Host "[x] Could not start the Lumen container."; exit 1 }
}

function Get-Port {
  if ($PORT_PIN) { return $PORT_PIN }
  $line = (& $RT port $NAME 7517 2>$null | Select-Object -First 1)
  if ($line -match ':(\d+)\s*$') { return $Matches[1] }
  return '17517'
}

# Wait until the runtime is active and return the URL with the ?k= handshake.
function Wait-Url {
  $secret = ''
  for ($i = 0; $i -lt 48; $i++) {
    $a = ((& $RT exec $NAME systemctl is-active hermes-runtime 2>$null) | Out-String).Trim()
    if ($a -eq 'active') {
      $secret = ((& $RT exec $NAME cat /var/lib/hermes-bootstrap/bootstrap/webui-bootstrap 2>$null) | Out-String) -replace '[\r\n]', ''
      if ($secret) { break }
    }
    if ($a -eq 'failed') { break }
    Start-Sleep -Seconds 5
  }
  if (-not $secret) { return $null }
  return "http://localhost:$(Get-Port)/?k=$secret"
}

function Open-Browser($url) {
  try { Start-Process $url } catch { }
}

function Open-AndPrint {
  Write-Host "[*] Waiting for Lumen..."
  $url = Wait-Url
  if ($url) {
    Write-Host ""
    Write-Host "  Lumen is ready:"
    Write-Host "     $url"
    Write-Host ""
    Open-Browser $url
  } else {
    Write-Host "  [!] Lumen started but I could not get the token. Check:  lumen logs"
    exit 1
  }
}

# ---- subcommands ------------------------------------------------------------

function Cmd-Open {
  Initialize-Machine
  if (Test-Exists) {
    if (-not (Test-Running)) { Write-Host "[*] Starting Lumen..."; & $RT start $NAME *> $null }
  } else {
    Write-Host "[*] First run - downloading and starting Lumen..."
    & $RT pull $IMAGE *> $null
    Invoke-Run
  }
  Open-AndPrint
}

function Cmd-Start {
  Initialize-Machine
  if (Test-Exists) {
    if (Test-Running) { Write-Host "[ok] Lumen is already running (run 'lumen' to open it)."; return }
    & $RT start $NAME *> $null
    Write-Host "[ok] Lumen started. Open it with:  lumen"
  } else {
    & $RT pull $IMAGE *> $null
    Invoke-Run
    Write-Host "[ok] Lumen started. Open it with:  lumen"
  }
}

function Cmd-Stop {
  if ((Test-Exists) -and (Test-Running)) {
    & $RT stop $NAME *> $null
    if ($LASTEXITCODE -eq 0) { Write-Host "[ok] Lumen stopped." } else { Write-Host "[x] Could not stop it." }
  } else {
    Write-Host "[ok] Lumen was already stopped."
  }
}

function Cmd-Restart { Cmd-Stop; Cmd-Open }

function Cmd-Update {
  Write-Host "[*] Fetching the latest Lumen image..."
  Initialize-Machine
  & $RT pull (Get-Image)
  # Fail-fast like the sh launcher (set -e): a failed pull must NOT fall through to
  # Invoke-Run, which would `rm -f` and recreate — destroying a working container.
  if ($LASTEXITCODE -ne 0) { Write-Host "[x] Could not fetch the latest image (keeping the current one)."; exit 1 }
  Write-Host "[*] Recreating the container with the new image..."
  Invoke-Run   # -v lumen-data keeps your config (keystore, MFA, providers)
  Open-AndPrint
}

# ---- enterprise pairing -----------------------------------------------------
# Pairing turns this SAME image into the cloud-managed associate at runtime — no
# separate image to pull. The handshake writes the association row + the
# .enterprise marker; the marker gates config-sync, which pulls + applies signed
# cloud policy.
function Cmd-Pair($code) {
  if (-not $code) { Write-Host "[x] Usage: lumen pair <code>"; exit 1 }
  Initialize-Machine
  if (-not (Test-Running)) {
    Write-Host "[*] Starting Lumen to complete pairing..."
    Cmd-Start
  }
  Write-Host "[*] Running pairing handshake..."
  # Code is passed via STDIN (not argv) to avoid exposure in process listings.
  $pairArgs = @('exec','-i', $NAME, 'python3','-m','hermes.instance','pair','--stdin')
  if ($CLOUD_ENDPOINT) { $pairArgs += @('--cloud', $CLOUD_ENDPOINT) }
  # Send the code as raw UTF-8 (no BOM); the container reads stdin.readline().strip()
  # so the trailing newline PowerShell adds is harmless. Forcing UTF-8 avoids 5.1's
  # default ASCII transcoding corrupting any non-ASCII byte.
  $prevEnc = $OutputEncoding
  $OutputEncoding = New-Object System.Text.UTF8Encoding $false
  $code | & $RT @pairArgs
  $OutputEncoding = $prevEnc
  if ($LASTEXITCODE -ne 0) { Write-Host "[x] Pairing failed."; exit 1 }
  # Write the pairing marker so config-sync's ConditionPathExists gate opens.
  & $RT exec $NAME sh -c "mkdir -p /var/lib/hermes/instance && touch $ENTERPRISE_MARKER" *> $null
  & $RT exec $NAME systemctl start hermes-config-sync.service *> $null
  Write-Host "[ok] Enterprise association complete.  Restart Lumen to activate: lumen restart"
}

function Cmd-Unpair {
  Initialize-Machine
  if (-not (Test-Running)) { Write-Host "[x] Lumen must be running to unpair.  Start it with: lumen start"; exit 1 }
  & $RT exec $NAME python3 -m hermes.instance unpair
  if ($LASTEXITCODE -ne 0) { Write-Host "[x] Unpair failed."; exit 1 }
  & $RT exec $NAME rm -f $ENTERPRISE_MARKER *> $null
  Write-Host "[ok] Instance unpaired.  Restart Lumen to revert to community edition: lumen restart"
}

function Cmd-Status {
  if ((Test-Exists) -and (Test-Running)) {
    Write-Host "[ok] Lumen running at  http://localhost:$(Get-Port)/   (open with: lumen)"
  } elseif (Test-Exists) {
    Write-Host "[ ] Lumen is stopped.  Start it with: lumen"
  } else {
    Write-Host "[ ] Lumen is not installed.  Install with: lumen update"
  }
}

function Cmd-Logs { & $RT logs -f $NAME }

function Show-Usage {
  @'
lumen - control your local Lumen

  lumen            open Lumen in the browser (starts it if stopped)
  lumen stop       stop Lumen
  lumen start      start Lumen without opening the browser
  lumen restart    restart Lumen
  lumen update     pull the latest version and apply it (keeps your config)
  lumen status     is it running? on which port?
  lumen logs       follow the container journal
  lumen pair <code>   associate with an enterprise tenant (same image, associate mode)
  lumen unpair        remove the enterprise association (reverts to community edition)
'@ | Write-Host
}

switch -CaseSensitive ($Command) {
  ''          { Cmd-Open }
  'open'      { Cmd-Open }
  'abrir'     { Cmd-Open }
  'start'     { Cmd-Start }
  'arranca'   { Cmd-Start }
  'stop'      { Cmd-Stop }
  'para'      { Cmd-Stop }
  'parar'     { Cmd-Stop }
  'restart'   { Cmd-Restart }
  'reinicia'  { Cmd-Restart }
  'update'    { Cmd-Update }
  'actualiza' { Cmd-Update }
  'actualizar'{ Cmd-Update }
  'status'    { Cmd-Status }
  'estado'    { Cmd-Status }
  'logs'      { Cmd-Logs }
  'log'       { Cmd-Logs }
  'pair'      { Cmd-Pair $Arg }
  'vincular'  { Cmd-Pair $Arg }
  'unpair'    { Cmd-Unpair }
  'desvincular' { Cmd-Unpair }
  '-h'        { Show-Usage }
  '--help'    { Show-Usage }
  'help'      { Show-Usage }
  'ayuda'     { Show-Usage }
  default     { Write-Host "Unknown command: $Command"; Write-Host ""; Show-Usage; exit 1 }
}
