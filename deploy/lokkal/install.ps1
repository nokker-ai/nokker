# Nokker Lokkal — installeur Windows (docs/LOKKAL.md §3).
#
#   irm https://nokker.ai/install.ps1 | iex
#   (-NoKeepAlive : pas de tâche planifiée qui garde la distribution WSL, donc Lokkal, démarrée)
#   & ([scriptblock]::Create((irm https://nokker.ai/install.ps1))) -Workspace ~/code -Yes
#
# Lokkal tourne dans WSL 2 : le noyau, ses containers et vos dossiers de travail vivent dans une distribution Linux
# (Ubuntu par défaut). C'est le chemin le plus sûr et le plus rapide sous Windows : pas de traduction de chemins
# C:\ → /mnt/c pour les bacs à sable, des fichiers sur un vrai système de fichiers Linux, et le même install.sh que
# sous Linux et macOS. Vos dossiers restent visibles dans l'Explorateur : \\wsl.localhost\<distribution>\home\<vous>.
#
# Ce script :
#   1. vérifie WSL 2 et une distribution (et propose `wsl --install`, qui demande un redémarrage) ;
#   2. trouve Docker dans cette distribution : Docker Desktop (intégration WSL activée) ou Docker Engine installé
#      dans la distribution par install.sh (--yes) ;
#   3. lance install.sh dans la distribution ;
#   4. pose nokker.exe dans %LOCALAPPDATA%\Nokker\bin (PATH utilisateur) et ~/.config/nokker/lokkal.json côté Windows
#      (l'API de la distribution est joignable sur 127.0.0.1 : redirection de localhost de WSL) ;
#   5. ouvre l'onboarding web.
[CmdletBinding()]
param(
  [string]$Version = "",
  [string]$Workspace = "",          # chemin DANS la distribution (défaut : ~/nokker), séparés par « : »
  [string]$Distro = "",             # défaut : la distribution WSL par défaut, sinon Ubuntu
  [switch]$Yes,
  [switch]$Update,
  [switch]$Uninstall,
  [switch]$Purge,
  [switch]$NoOpen,
  [switch]$NoKeepAlive,             # pas de tâche planifiée qui garde la distribution (et donc Lokkal) vivante
  [switch]$Vault,                   # coffre OpenBao (profil vault), descellé automatiquement
  [switch]$VaultManual,             # idem, part de descellement affichée une fois, redemandée à chaque redémarrage
  [switch]$LocalLlm,                # moteur d'inférence local (Ollama, CPU, profil local-llm), proposé dans /setup
  [string]$LocalLlmModel = ""       # modèle Ollama à tirer (défaut : qwen3:4b) ; implique -LocalLlm
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$Repo = "nokker-ai/nokker"
$Base = $env:NOKKER_RELEASE_BASE   # tests : URL d'une release locale

function Say($m)  { Write-Host "› $m" }
function Warn($m) { Write-Host "! $m" -ForegroundColor Yellow }
function Die($m)  { Write-Host "✗ $m" -ForegroundColor Red; exit 1 }
function Ask($q)  { if ($Yes) { return $true }; $a = Read-Host "$q [o/N]"; return $a -match '^(o|oui|y|yes)$' }

if ([Environment]::Is64BitOperatingSystem -eq $false) { Die "Windows 64 bits requis." }
$arch = if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") { "arm64" } else { "x64" }
$build = [int](Get-CimInstance Win32_OperatingSystem).BuildNumber
if ($build -lt 19041) { Die "Windows 10 version 2004 (build 19041) ou plus récent requis pour WSL 2 (build actuel : $build)." }

# --------------------------------------------------------------------------------------------- WSL 2
function Wsl { param([Parameter(ValueFromRemainingArguments)]$a) & wsl.exe @a }
$wslOk = $false
try { $null = & wsl.exe --status 2>$null; $wslOk = ($LASTEXITCODE -eq 0) } catch { }
if (-not $wslOk) {
  if (-not (Ask "WSL 2 n'est pas installé. L'installer maintenant (droits administrateur, puis redémarrage) ?")) {
    Die "WSL 2 est requis : https://learn.microsoft.com/windows/wsl/install"
  }
  # Déjà administrateur (session élevée, ssh d'un compte admin) : directement ; RunAs sans bureau n'installe rien.
  $admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
  if ($admin) { & wsl.exe --install -d Ubuntu --no-launch }
  else { Start-Process wsl.exe -ArgumentList "--install","-d","Ubuntu","--no-launch" -Verb RunAs -Wait }
  Warn "WSL 2 installé. Redémarrez Windows, ouvrez « Ubuntu » une fois (création de votre compte Linux), puis relancez cette commande."
  exit 0
}
# Sortie de wsl.exe en UTF-16 : forcer l'UTF-8
$env:WSL_UTF8 = "1"
$distros = @(& wsl.exe --list --quiet 2>$null | ForEach-Object { $_.Trim() } | Where-Object { $_ -and $_ -notmatch '^docker-desktop' })
if (-not $Distro) {
  $def = (& wsl.exe --list --verbose 2>$null | Where-Object { $_ -match '^\s*\*' } | ForEach-Object { ($_ -replace '^\s*\*\s*','').Split()[0] })
  $Distro = if ($def -and $def -notmatch '^docker-desktop') { $def } elseif ($distros) { $distros[0] } else { "" }
}
if (-not $Distro) {
  if (-not (Ask "Aucune distribution Linux dans WSL. Installer Ubuntu ?")) { Die "Une distribution WSL est requise (wsl --install -d Ubuntu)." }
  & wsl.exe --install -d Ubuntu --no-launch
  Warn "Ubuntu installé. Ouvrez « Ubuntu » une fois (création de votre compte Linux), puis relancez cette commande."
  exit 0
}
$ver = (& wsl.exe --list --verbose 2>$null | Where-Object { $_ -match "\b$([regex]::Escape($Distro))\b" } | ForEach-Object { ($_ -split '\s+')[-1] })
if ($ver -and $ver.Trim() -eq "1") { Die "La distribution $Distro est en WSL 1 : wsl --set-version $Distro 2, puis relancez." }
$user = (& wsl.exe -d $Distro -- sh -c 'id -un' 2>$null)
if (-not $user -or $user.Trim() -eq "root") {
  Die "Ouvrez « $Distro » une fois pour créer votre compte Linux (Lokkal ne s'installe pas en root), puis relancez."
}
Say "WSL 2 : $Distro (compte $($user.Trim()))"

# --------------------------------------------------------------------------------------------- Docker dans la distribution
& wsl.exe -d $Distro -- sh -c 'docker info >/dev/null 2>&1 && docker compose version >/dev/null 2>&1'
if ($LASTEXITCODE -ne 0) {
  $desktop = Test-Path "$env:ProgramFiles\Docker\Docker\Docker Desktop.exe"
  if ($desktop) {
    Die ("Docker Desktop est installé mais pas joignable depuis $Distro : lancez Docker Desktop, puis Settings → Resources → " +
         "WSL integration → activez $Distro, et relancez.")
  }
  Warn "Docker n'est pas dans $Distro : install.sh va proposer Docker Engine (script officiel, sudo dans Linux)."
  & wsl.exe -d $Distro -- sh -c 'ps -p 1 -o comm= | grep -q systemd'
  if ($LASTEXITCODE -ne 0) {
    Die ("systemd n'est pas actif dans $Distro (requis pour Docker Engine). Dans $Distro : " +
         "printf '[boot]\nsystemd=true\n' | sudo tee -a /etc/wsl.conf ; puis dans PowerShell : wsl --shutdown ; et relancez. " +
         "Ou installez Docker Desktop avec l'intégration WSL.")
  }
}

# --------------------------------------------------------------------------------------------- install.sh dans la distribution
$shArgs = @()
if ($Version)   { $shArgs += @("--version", $Version) }
if ($Workspace) { $shArgs += @("--workspace", $Workspace) }
if ($Yes)       { $shArgs += "--yes" }
if ($Update)    { $shArgs += "--update" }
if ($Uninstall) { $shArgs += "--uninstall" }
if ($Purge)     { $shArgs += "--purge" }
if ($VaultManual) { $shArgs += "--vault-manual" } elseif ($Vault) { $shArgs += "--vault" }
if ($LocalLlmModel) { $shArgs += @("--local-llm-model", $LocalLlmModel) } elseif ($LocalLlm) { $shArgs += "--local-llm" }
$shArgs += "--no-open"
$quoted = ($shArgs | ForEach-Object { "'" + ($_ -replace "'", "'\''") + "'" }) -join " "
$envs = ""
foreach ($k in "NOKKER_RELEASE_BASE","NOKKER_SKIP_PULL") {
  $v = [Environment]::GetEnvironmentVariable($k); if ($v) { $envs += "$k='$v' " }
}
if ($Base -and $Base -like "file://*") { $src = "cat '$($Base.Substring(7))/install.sh'" }
elseif ($Base) { $src = "curl -fsSL '$Base/install.sh'" }
else { $src = "curl -fsSL https://nokker.ai/install.sh" }
# NOKKER_NO_CLI : la commande nokker Linux reste posée dans la distribution ; Windows reçoit nokker.exe plus bas.
& wsl.exe -d $Distro -- bash -lc "$src | env $envs sh -s -- $quoted"
if ($LASTEXITCODE -ne 0) { Die "install.sh a échoué dans $Distro (journal ci-dessus)." }
$keepTask = "Nokker Lokkal (WSL $Distro)"
if ($Uninstall) {
  Remove-Item -Force -ErrorAction SilentlyContinue "$HOME\.config\nokker\lokkal.json"
  Unregister-ScheduledTask -TaskName $keepTask -Confirm:$false -ErrorAction SilentlyContinue
  exit 0
}

# --------------------------------------------------------------------------------------------- distribution gardée vivante
# WSL arrête la distribution quand sa dernière session se ferme : Lokkal (dans Docker, dans la distribution) s'arrêterait
# avec elle. Tâche planifiée à l'ouverture de session (compte courant, sans mot de passe stocké, sans limite de durée),
# lancée tout de suite : `wsl -d <distro> --exec sleep infinity`, fenêtre cachée. -NoKeepAlive : pas de tâche (et
# retrait d'une tâche posée avant).
if ($NoKeepAlive) {
  Unregister-ScheduledTask -TaskName $keepTask -Confirm:$false -ErrorAction SilentlyContinue
} else {
  try {
    $act = New-ScheduledTaskAction -Execute "powershell.exe" -Argument (
      "-NoProfile -WindowStyle Hidden -Command `"& wsl.exe -d '$Distro' --exec sleep infinity`"")
    # MACHINE\compte réel : $env:USERDOMAIN vaut WORKGROUP dans une session ssh (« No mapping between account names »)
    $me = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $trg = New-ScheduledTaskTrigger -AtLogOn -User $me
    $prn = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Limited
    $set = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries `
      -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -Hidden
    $null = Register-ScheduledTask -TaskName $keepTask -Action $act -Trigger $trg -Principal $prn -Settings $set `
      -Description "Garde la distribution WSL de Nokker Lokkal démarrée (sinon Lokkal s'arrête avec la dernière session)." -Force `
      -ErrorAction Stop
    Start-ScheduledTask -TaskName $keepTask -ErrorAction Stop
    Say "distribution $Distro gardée démarrée (tâche planifiée « $keepTask » ; -NoKeepAlive pour s'en passer)"
  } catch {
    Warn "tâche planifiée non posée ($($_.Exception.Message)) : Lokkal s'arrête quand la dernière fenêtre WSL se ferme"
  }
}

# --------------------------------------------------------------------------------------------- côté Windows : lokkal.json, nokker.exe
$linuxConf = (& wsl.exe -d $Distro -- sh -c 'cat "${XDG_CONFIG_HOME:-$HOME/.config}/nokker/lokkal.json"') -join "`n"
$conf = $linuxConf | ConvertFrom-Json
$unc = "\\wsl.localhost\$Distro"
$win = [ordered]@{
  compose      = $conf.compose
  project      = $conf.project
  version      = $conf.version
  core_service = $conf.core_service
  api_url      = $conf.api_url
  web_url      = $conf.web_url
  token_path   = $unc + ($conf.token_path -replace '/', '\')
  env_file     = $conf.env_file
  install_dir  = $conf.install_dir
  wsl_distro   = $Distro        # la CLI Windows passe par `wsl -d <distro>` pour update/logs/doctor côté docker
}
$confDir = Join-Path $HOME ".config\nokker"
New-Item -ItemType Directory -Force -Path $confDir | Out-Null
# UTF-8 SANS BOM (Windows PowerShell 5.1 en met un avec -Encoding UTF8, et un parseur JSON strict le refuse)
[IO.File]::WriteAllText((Join-Path $confDir "lokkal.json"), ($win | ConvertTo-Json), (New-Object Text.UTF8Encoding($false)))
# lokkal.json lisible par l'utilisateur seul
$acl = Get-Acl $confDir; $acl.SetAccessRuleProtection($true, $false)
$acl.Access | ForEach-Object { $null = $acl.RemoveAccessRule($_) }
$acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($env:USERNAME, "FullControl", "ContainerInherit,ObjectInherit", "None", "Allow")))
Set-Acl $confDir $acl

$v = $conf.version
$rel = if ($Base) { $Base } else { "https://github.com/$Repo/releases/download/lokkal-v$v" }
$binDir = Join-Path $env:LOCALAPPDATA "Nokker\bin"
New-Item -ItemType Directory -Force -Path $binDir | Out-Null
function Get-Asset($name, $out) {
  if ($rel -like "file://*") { Copy-Item "$($rel.Substring(7))/$name" $out -Force; return (Get-Content "$($rel.Substring(7))/$name.sha256" -Raw) }
  Invoke-WebRequest -UseBasicParsing "$rel/$name" -OutFile $out
  # Windows PowerShell 5.1 : une réponse application/octet-stream (GitHub) arrive en Byte[] ; la lire en texte, sinon
  # l'empreinte comparée est « 51 » (le code du premier caractère) et nokker.exe n'est jamais posé.
  $c = (Invoke-WebRequest -UseBasicParsing "$rel/$name.sha256").Content
  if ($c -is [byte[]]) { $c = [Text.Encoding]::ASCII.GetString($c) }
  return [string]$c
}
$tmp = New-TemporaryFile
try {
  # PC ARM : l'exe arm64 s'il existe, sinon l'exe x64 (émulation de Windows on ARM)
  $cands = if ($arch -eq "arm64") { @("nokker-windows-arm64.exe", "nokker-windows-x64.exe") } else { @("nokker-windows-x64.exe") }
  $sum = $null
  foreach ($bin in $cands) { try { $sum = Get-Asset $bin $tmp; break } catch { $sum = $null } }
  if (-not $sum) { throw "absent" }
  $want = ($sum -split '\s+')[0].ToLower()
  if ((Get-FileHash $tmp -Algorithm SHA256).Hash.ToLower() -ne $want) { Die "empreinte sha256 de $bin incorrecte : abandon" }
  Move-Item -Force $tmp (Join-Path $binDir "nokker.exe")
  $path = [Environment]::GetEnvironmentVariable("Path", "User")
  if (-not ($path -split ';' | Where-Object { $_ -eq $binDir })) {
    [Environment]::SetEnvironmentVariable("Path", ($path.TrimEnd(';') + ";" + $binDir), "User")
  }
  Say "terminal : nokker   (nouvelle fenêtre PowerShell)"
} catch {
  Warn "commande nokker indisponible pour windows-$arch dans cette version (le web fonctionne)"
} finally { Remove-Item -Force -ErrorAction SilentlyContinue $tmp }

Say "Nokker Lokkal $v est prêt : $($conf.web_url)/"
Say "dossiers de travail dans $Distro, visibles dans l'Explorateur : $unc\home\$($user.Trim())"
# Premier lancement : le setup local s'ouvre avec le jeton local dans le FRAGMENT (/setup#token=…, docs/LOKKAL.md §7.1) ;
# la racine sert la connexion du cloud. Le jeton n'est passé qu'au navigateur, jamais écrit dans un journal.
$setupDone = [bool](& wsl.exe -d $Distro -- sh -c "grep -Eq 'done.: *true' '$($conf.install_dir)/state/setup.json' 2>/dev/null && echo 1")
$openUrl = "$($conf.web_url)/"
if (-not $setupDone) {
  $tok = ((& wsl.exe -d $Distro -- sh -c "cat '$($conf.install_dir)/state/local-token' 2>/dev/null") -join "").Trim()
  if ($tok) {
    $openUrl = "$($conf.web_url)/setup#token=$tok"
    if ($NoOpen) { Say "premier réglage (choisir un modèle) : ouvrez $($conf.web_url)/setup#token=<contenu de $($conf.token_path)>" }
    else { Say "premier réglage (choisir un modèle) : la page s'ouvre dans le navigateur" }
  } else {
    Warn "premier réglage : jeton local introuvable ($($conf.install_dir)/state/local-token) ; relancez l'installeur une fois le noyau démarré"
  }
}
if (-not $NoOpen -and (-not $Update -or -not $setupDone)) { Start-Process $openUrl }
