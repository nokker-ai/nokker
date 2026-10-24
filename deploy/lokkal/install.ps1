# Nokker Lokkal — installeur Windows (docs/LOKKAL.md §3).
#
#   irm https://nokker.ai/install.ps1 | iex
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
  [switch]$NoOpen
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
  Start-Process wsl.exe -ArgumentList "--install","-d","Ubuntu","--no-launch" -Verb RunAs -Wait
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
if ($Uninstall) {
  Remove-Item -Force -ErrorAction SilentlyContinue "$HOME\.config\nokker\lokkal.json"
  exit 0
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
  Invoke-WebRequest -UseBasicParsing "$rel/$name" -OutFile $out; return (Invoke-WebRequest -UseBasicParsing "$rel/$name.sha256").Content
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
if (-not $NoOpen -and -not $Update) { Start-Process "$($conf.web_url)/" }
