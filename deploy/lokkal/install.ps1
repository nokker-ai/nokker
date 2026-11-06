# Nokker Lokkal -- installeur Windows (docs/LOKKAL.md section 3).
#
#   irm https://nokker.ai/install.ps1 | iex
#   (-NoKeepAlive : pas de tache planifiee qui garde la distribution WSL, donc Lokkal, demarree)
#   & ([scriptblock]::Create((irm https://nokker.ai/install.ps1))) -Workspace ~/code -Yes
#
# Lokkal tourne dans WSL 2 : le noyau, ses containers et vos dossiers de travail vivent dans une distribution Linux
# (Ubuntu par defaut). Pas de traduction de chemins C:\ -> /mnt/c pour les bacs a sable, des fichiers sur un vrai
# systeme de fichiers Linux, et le meme install.sh que sous Linux et macOS. Vos dossiers restent visibles dans
# l'Explorateur : \\wsl.localhost\<distribution>\home\<vous>.
#
# Ce script :
#   1. verifie WSL 2 ; sinon l'installe avec Ubuntu (etape 1 sur 2 : un redemarrage de Windows est obligatoire) ;
#   2. au passage suivant : enregistre la distribution si besoin, cree le compte Linux (nom et mot de passe demandes
#      ici), active systemd, puis trouve Docker (Docker Desktop avec l'integration WSL, ou Docker Engine installe
#      dans la distribution par install.sh) ;
#   3. lance install.sh dans la distribution ;
#   4. pose nokker.exe dans %LOCALAPPDATA%\Nokker\bin (PATH utilisateur) et ~/.config/nokker/lokkal.json cote Windows ;
#   5. ouvre l'onboarding web.
#
# Regles de ce fichier (rc.9.5, test Windows du 05.11) :
#   - JAMAIS `exit` dans le corps : sous `irm | iex`, exit ferme la fenetre PowerShell de l'utilisateur, qui ne voit
#     plus le message de fin de passe. Tout est dans Main ; Die leve une erreur rattrapee en bas ; `exit` seulement si
#     le script est lance comme fichier ($PSCommandPath), pour le code de retour.
#   - fichier 100 % ASCII et SANS BOM : irm de Windows PowerShell 5.1 decode souvent en Windows-1252 ; un caractere
#     UTF-8 y devient du mojibake, et certains octets (0x91-0x94) y sont des guillemets typographiques que PowerShell
#     prend pour des guillemets : le script pouvait casser. Les messages accentues s'ecrivent \u00e9 et sont decodes
#     par U() a l'affichage. Un BOM, lui, casse iex.
[CmdletBinding()]
param(
  [string]$Version = "",
  [string]$Workspace = "",          # chemin DANS la distribution (defaut : ~/nokker), separes par ":"
  [string]$Distro = "",             # defaut : la distribution WSL par defaut, sinon Ubuntu
  [switch]$Yes,
  [switch]$Update,
  [switch]$Uninstall,
  [switch]$Purge,
  [switch]$NoOpen,
  [switch]$NoKeepAlive,             # pas de tache planifiee qui garde la distribution (et donc Lokkal) vivante
  [switch]$Vault,                   # coffre OpenBao (profil vault), descelle automatiquement
  [switch]$VaultManual,             # idem, part de descellement affichee une fois, redemandee a chaque redemarrage
  [switch]$LocalLlm,                # moteur d'inference local (Ollama, CPU, profil local-llm), propose dans /setup
  [string]$LocalLlmModel = ""       # modele Ollama a tirer (defaut : qwen3:4b) ; implique -LocalLlm
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$Repo = "nokker-ai/nokker"
$Base = $env:NOKKER_RELEASE_BASE   # tests : URL d'une release locale
$StopMark = "NOKKER_STOP"

# \uXXXX -> caractere (seulement cette forme : un chemin Windows comme C:\Users reste intact)
function U([string]$s) {
  [regex]::Replace($s, '\\u([0-9a-fA-F]{4})', { param($x) [string][char][Convert]::ToInt32($x.Groups[1].Value, 16) })
}
function Say($m)  { Write-Host ((U "\u203a ") + (U $m)) }
function Warn($m) { Write-Host ("! " + (U $m)) -ForegroundColor Yellow }
function Die($m)  { Write-Host ((U "\u00d7 ") + (U $m)) -ForegroundColor Red; throw $StopMark }
function Ask($q)  { if ($Yes) { return $true }; $a = Read-Host ((U $q) + " [o/N]"); return $a -match '^(o|oui|y|yes)$' }

# Commande wsl.exe dont le stderr ne doit pas devenir une erreur bloquante (Windows PowerShell 5.1 + EAP Stop) ;
# $LASTEXITCODE reste celui de la commande.
function Native([scriptblock]$sb) {
  $eap = $ErrorActionPreference; $ErrorActionPreference = "Continue"
  try { & $sb 2>$null } finally { $ErrorActionPreference = $eap }
}
function Wsl-Root([string]$d, [string]$cmd) { Native { & wsl.exe -d $d -u root -- sh -c $cmd } }

# Compte Linux cree ICI (sinon : ouvrir Ubuntu une fois, puis relancer = une passe de plus). Mot de passe demande deux
# fois, transmis a chpasswd par l'entree standard (jamais en argument), en UTF-8.
function New-LinuxUser([string]$d) {
  if ($Yes) { return $false }   # sans dialogue (automatisation) : pas de mot de passe a demander
  $def = ($env:USERNAME.ToLower() -replace '[^a-z0-9_-]', '')
  if ($def -notmatch '^[a-z_]') { $def = "nokker" }
  $name = Read-Host (U "Nom du compte Linux \u00e0 cr\u00e9er dans $d [$def]")
  if (-not $name) { $name = $def }
  if ($name -notmatch '^[a-z_][a-z0-9_-]{0,31}$') { Warn "nom refus\u00e9 ($name) : minuscules, chiffres, - et _"; return $false }
  for ($i = 0; $i -lt 3; $i++) {
    $p1 = Read-Host (U "Mot de passe Linux de $name (sudo)") -AsSecureString
    $p2 = Read-Host (U "Le m\u00eame, une seconde fois") -AsSecureString
    $a = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($p1))
    $b = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($p2))
    if ($a -and $a -eq $b) { break }
    Warn "les deux saisies diff\u00e8rent, ou sont vides"; $a = $null
  }
  if (-not $a) { return $false }
  Wsl-Root $d "id -u '$name' >/dev/null 2>&1 || useradd -m -s /bin/bash -G sudo '$name'"
  if ($LASTEXITCODE -ne 0) { Warn "cr\u00e9ation du compte $name impossible"; return $false }
  $old = $OutputEncoding
  try {
    $OutputEncoding = New-Object Text.UTF8Encoding($false)
    $eap = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    # Le pipe de PowerShell vers un programme natif termine la ligne par CR LF : sans `tr -d '\r'`, chpasswd garde le
    # CR dans le mot de passe et sudo le refuse ensuite (test Windows du 05.11).
    ("${name}:$a") | & wsl.exe -d $d -u root -- sh -c "tr -d '\r' | chpasswd" 2>$null
    $ok = ($LASTEXITCODE -eq 0)
    # Controle : le mot de passe pose ouvre bien sudo pour ce compte.
    if ($ok) { ("$a") | & wsl.exe -d $d -u $name -- sh -c "tr -d '\r' | sudo -S -k -p '' true" 2>$null; $ok = ($LASTEXITCODE -eq 0) }
    $ErrorActionPreference = $eap
  } finally { $OutputEncoding = $old; $a = $null; $b = $null }
  if (-not $ok) { Warn "mot de passe du compte $name non pos\u00e9"; return $false }
  # Compte par defaut de la distribution (/etc/wsl.conf [user]), applique au prochain demarrage de la distribution.
  Wsl-Root $d "grep -q '^\[user\]' /etc/wsl.conf 2>/dev/null && sed -i '/^\[user\]/,/^\[/{s/^default=.*/default=$name/}' /etc/wsl.conf || printf '\n[user]\ndefault=$name\n' >> /etc/wsl.conf"
  $null = Native { & wsl.exe --terminate $d }
  Say "compte Linux $name cr\u00e9\u00e9 dans $d"
  return $true
}

function Main {
  if ([Environment]::Is64BitOperatingSystem -eq $false) { Die "Windows 64 bits requis." }
  $arch = if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") { "arm64" } else { "x64" }
  $build = [int](Get-CimInstance Win32_OperatingSystem).BuildNumber
  if ($build -lt 19041) { Die "Windows 10 version 2004 (build 19041) ou plus r\u00e9cent requis pour WSL 2 (build actuel : $build)." }

  # ------------------------------------------------------------------------------------------- WSL 2 (etape 1 sur 2)
  $wslOk = $false
  try { $null = & wsl.exe --status 2>$null; $wslOk = ($LASTEXITCODE -eq 0) } catch { }
  if (-not $wslOk) {
    if (-not (Ask "WSL 2 n'est pas install\u00e9. L'installer maintenant (droits administrateur, puis red\u00e9marrage de Windows) ?")) {
      Die "WSL 2 est requis : https://learn.microsoft.com/windows/wsl/install"
    }
    # Deja administrateur (session elevee, ssh d'un compte admin) : directement ; RunAs sans bureau n'installe rien.
    $admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
      [Security.Principal.WindowsBuiltInRole]::Administrator)
    if ($admin) { & wsl.exe --install -d Ubuntu --no-launch }
    else { Start-Process wsl.exe -ArgumentList "--install","-d","Ubuntu","--no-launch" -Verb RunAs -Wait }
    Warn "\u00c9tape 1 sur 2 termin\u00e9e : WSL 2 et Ubuntu sont install\u00e9s."
    Warn "Red\u00e9marrez Windows, puis relancez EXACTEMENT la m\u00eame commande : l'\u00e9tape 2 cr\u00e9e votre compte Linux, installe Docker et Lokkal."
    return
  }
  # Sortie de wsl.exe en UTF-16 : forcer l'UTF-8
  $env:WSL_UTF8 = "1"
  $distros = @(& wsl.exe --list --quiet 2>$null | ForEach-Object { $_.Trim() } | Where-Object { $_ -and $_ -notmatch '^docker-desktop' })
  if (-not $Distro) {
    $def = (& wsl.exe --list --verbose 2>$null | Where-Object { $_ -match '^\s*\*' } | ForEach-Object { ($_ -replace '^\s*\*\s*','').Split()[0] })
    $Distro = if ($def -and $def -notmatch '^docker-desktop') { $def } elseif ($distros) { $distros[0] } else { "Ubuntu" }
  }
  if (-not ($distros -contains $Distro)) {
    # Distribution absente, ou telechargee (--no-launch) mais pas encore enregistree : on l'enregistre ici, sans
    # passer par la fenetre de premier lancement (le compte est cree juste apres).
    if (-not (Ask "La distribution $Distro n'est pas pr\u00eate dans WSL. L'installer maintenant ?")) { Die "Une distribution WSL est requise (wsl --install -d Ubuntu)." }
    $null = Native { & wsl.exe --install -d $Distro --no-launch }
    $null = Wsl-Root $Distro "true"
    if ($LASTEXITCODE -ne 0) {
      $launcher = Get-Command (($Distro -replace '[^A-Za-z0-9.]', '').ToLower() + ".exe") -ErrorAction SilentlyContinue
      if ($launcher) { $null = Native { & $launcher.Source install --root } }
      $null = Wsl-Root $Distro "true"
    }
    if ($LASTEXITCODE -ne 0) {
      Warn "Ouvrez \u00ab $Distro \u00bb une fois depuis le menu D\u00e9marrer (cr\u00e9ation de votre compte Linux), puis relancez cette commande : c'est la derni\u00e8re \u00e9tape."
      return
    }
  }
  $ver = (& wsl.exe --list --verbose 2>$null | Where-Object { $_ -match "\b$([regex]::Escape($Distro))\b" } | ForEach-Object { ($_ -split '\s+')[-1] })
  if ($ver -and $ver.Trim() -eq "1") { Die "La distribution $Distro est en WSL 1 : wsl --set-version $Distro 2, puis relancez." }
  $user = (& wsl.exe -d $Distro -- sh -c 'id -un' 2>$null)
  if (-not $user -or $user.Trim() -eq "root") {
    if (-not (New-LinuxUser $Distro)) {
      Warn "Ouvrez \u00ab $Distro \u00bb une fois pour cr\u00e9er votre compte Linux (Lokkal ne s'installe pas en root), puis relancez cette commande : c'est la derni\u00e8re \u00e9tape."
      return
    }
    $user = (& wsl.exe -d $Distro -- sh -c 'id -un' 2>$null)
    if (-not $user -or $user.Trim() -eq "root") { Die "le compte Linux n'est pas le compte par d\u00e9faut de $Distro : wsl --terminate $Distro, puis relancez." }
  }
  Say "WSL 2 : $Distro (compte $($user.Trim()))"

  # ------------------------------------------------------------------------------------------- Docker dans la distribution
  & wsl.exe -d $Distro -- sh -c 'docker info >/dev/null 2>&1 && docker compose version >/dev/null 2>&1'
  if ($LASTEXITCODE -ne 0) {
    $desktop = Test-Path "$env:ProgramFiles\Docker\Docker\Docker Desktop.exe"
    if ($desktop) {
      Die ("Docker Desktop est install\u00e9 mais pas joignable depuis $Distro : lancez Docker Desktop, puis Settings > Resources > " +
           "WSL integration > activez $Distro, et relancez.")
    }
    Warn "Docker n'est pas dans $Distro : install.sh va installer Docker Engine (script officiel ; mot de passe Linux demand\u00e9 par sudo)."
    & wsl.exe -d $Distro -- sh -c 'ps -p 1 -o comm= | grep -q systemd'
    if ($LASTEXITCODE -ne 0) {
      # systemd (requis pour Docker Engine) : active ici plutot qu'une consigne et une passe de plus.
      Say "activation de systemd dans $Distro (requis pour Docker Engine)"
      Wsl-Root $Distro "grep -q '^systemd=true' /etc/wsl.conf 2>/dev/null || printf '\n[boot]\nsystemd=true\n' >> /etc/wsl.conf"
      $null = Native { & wsl.exe --terminate $Distro }
      & wsl.exe -d $Distro -- sh -c 'ps -p 1 -o comm= | grep -q systemd'
      if ($LASTEXITCODE -ne 0) {
        Die ("systemd n'est pas actif dans $Distro (requis pour Docker Engine) : wsl --shutdown dans PowerShell, puis relancez. " +
             "Ou installez Docker Desktop avec l'int\u00e9gration WSL.")
      }
    }
  }

  # Une autre distribution WSL qui fait tourner dockerd partage le MEME espace reseau que celle-ci (une seule VM, un seul
  # netns) : ses regles iptables cassent les reseaux docker de Lokkal (test Windows du 05.11 : roles -> postgres en
  # timeout). On le dit avant d'installer, avec la marche a suivre ; Docker Desktop (docker-desktop) n'est pas concerne.
  if (-not $Uninstall) {
    $others = @(& wsl.exe --list --running --quiet 2>$null | ForEach-Object { $_.Trim() } |
                Where-Object { $_ -and $_ -ne $Distro -and $_ -notmatch '^docker-desktop' })
    foreach ($o in $others) {
      $null = Native { & wsl.exe -d $o -- sh -c "pgrep -x dockerd >/dev/null" }
      if ($LASTEXITCODE -ne 0) { continue }
      Warn ("Docker tourne aussi dans la distribution $o. Dans WSL, toutes les distributions partagent le m\u00eame r\u00e9seau, " +
            "et deux Docker s'y g\u00eanent : les services de Lokkal ne se joignent plus entre eux. Avant d'utiliser Lokkal dans " +
            "$Distro, arr\u00eatez $o (wsl --terminate $o), ou d\u00e9sactivez son Docker (dans $o : " +
            "sudo systemctl disable --now docker docker.socket).")
      if (Get-ScheduledTask -TaskName "Nokker Lokkal (WSL $o)" -ErrorAction SilentlyContinue) {
        Warn "La t\u00e2che planifi\u00e9e \u00ab Nokker Lokkal (WSL $o) \u00bb red\u00e9marre $o : d\u00e9sactivez-la aussi (Disable-ScheduledTask -TaskName 'Nokker Lokkal (WSL $o)')."
      }
    }
  }

  # ------------------------------------------------------------------------------------------- install.sh dans la distribution
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
  # NOKKER_NO_CLI : la commande nokker Linux reste posee dans la distribution ; Windows recoit nokker.exe plus bas.
  & wsl.exe -d $Distro -- bash -lc "$src | env $envs sh -s -- $quoted"
  if ($LASTEXITCODE -ne 0) { Die "install.sh a \u00e9chou\u00e9 dans $Distro (journal ci-dessus)." }
  $keepTask = "Nokker Lokkal (WSL $Distro)"
  if ($Uninstall) {
    Remove-Item -Force -ErrorAction SilentlyContinue "$HOME\.config\nokker\lokkal.json"
    Unregister-ScheduledTask -TaskName $keepTask -Confirm:$false -ErrorAction SilentlyContinue
    Say "Nokker Lokkal d\u00e9sinstall\u00e9."
    return
  }

  # ------------------------------------------------------------------------------------------- distribution gardee vivante
  # WSL arrete la distribution quand sa derniere session se ferme : Lokkal (dans Docker, dans la distribution)
  # s'arreterait avec elle. Tache planifiee a l'ouverture de session (compte courant, sans mot de passe stocke, sans
  # limite de duree), lancee tout de suite : `wsl -d <distro> --exec sleep infinity`, fenetre cachee. -NoKeepAlive :
  # pas de tache (et retrait d'une tache posee avant).
  if ($NoKeepAlive) {
    Unregister-ScheduledTask -TaskName $keepTask -Confirm:$false -ErrorAction SilentlyContinue
  } else {
    try {
      $act = New-ScheduledTaskAction -Execute "powershell.exe" -Argument (
        "-NoProfile -WindowStyle Hidden -Command `"& wsl.exe -d '$Distro' --exec sleep infinity`"")
      # MACHINE\compte reel : $env:USERDOMAIN vaut WORKGROUP dans une session ssh (No mapping between account names)
      $me = [Security.Principal.WindowsIdentity]::GetCurrent().Name
      $trg = New-ScheduledTaskTrigger -AtLogOn -User $me
      $prn = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Limited
      $set = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -Hidden
      $null = Register-ScheduledTask -TaskName $keepTask -Action $act -Trigger $trg -Principal $prn -Settings $set `
        -Description (U "Garde la distribution WSL de Nokker Lokkal d\u00e9marr\u00e9e (sinon Lokkal s'arr\u00eate avec la derni\u00e8re session).") -Force `
        -ErrorAction Stop
      Start-ScheduledTask -TaskName $keepTask -ErrorAction Stop
      Say "distribution $Distro gard\u00e9e d\u00e9marr\u00e9e (t\u00e2che planifi\u00e9e \u00ab $keepTask \u00bb ; -NoKeepAlive pour s'en passer)"
    } catch {
      Warn "t\u00e2che planifi\u00e9e non pos\u00e9e ($($_.Exception.Message)) : Lokkal s'arr\u00eate quand la derni\u00e8re fen\u00eatre WSL se ferme"
    }
  }

  # ------------------------------------------------------------------------------------------- cote Windows : lokkal.json, nokker.exe
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
    wsl_distro   = $Distro        # la CLI Windows passe par `wsl -d <distro>` pour update/logs/doctor cote docker
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
    # Windows PowerShell 5.1 : une reponse application/octet-stream (GitHub) arrive en Byte[] ; la lire en texte,
    # sinon l'empreinte comparee est "51" (le code du premier caractere) et nokker.exe n'est jamais pose.
    $c = (Invoke-WebRequest -UseBasicParsing "$rel/$name.sha256").Content
    if ($c -is [byte[]]) { $c = [Text.Encoding]::ASCII.GetString($c) }
    return [string]$c
  }
  $tmp = New-TemporaryFile
  try {
    # PC ARM : l'exe arm64 s'il existe, sinon l'exe x64 (emulation de Windows on ARM)
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
    Say "terminal : nokker   (nouvelle fen\u00eatre PowerShell)"
  } catch {
    if ("$_" -eq $StopMark) { throw }
    Warn "commande nokker indisponible pour windows-$arch dans cette version (le web fonctionne)"
  } finally { Remove-Item -Force -ErrorAction SilentlyContinue $tmp }

  Say "Nokker Lokkal $v est pr\u00eat : $($conf.web_url)/"
  Say "dossiers de travail dans $Distro, visibles dans l'Explorateur : $unc\home\$($user.Trim())"
  # Premier lancement : le setup local s'ouvre avec le jeton local dans le FRAGMENT (/setup#token=..., docs/LOKKAL.md
  # section 7.1) ; la racine sert la connexion du cloud. Le jeton n'est passe qu'au navigateur, jamais ecrit dans un journal.
  $setupDone = [bool](& wsl.exe -d $Distro -- sh -c "grep -Eq 'done.: *true' '$($conf.install_dir)/state/setup.json' 2>/dev/null && echo 1")
  $openUrl = "$($conf.web_url)/"
  $tok = ((& wsl.exe -d $Distro -- sh -c "cat '$($conf.install_dir)/state/local-token' 2>/dev/null") -join "").Trim()
  # Setup deja fait (reinstallation) : la racine avec le jeton local dans le FRAGMENT, comme /setup ; sans lui, la racine
  # montre la connexion du cloud (test Windows du 05.11). Jamais en query ni dans un journal.
  if ($setupDone -and $tok) { $openUrl = "$($conf.web_url)/#token=$tok" }
  if (-not $setupDone) {
    if ($tok) {
      $openUrl = "$($conf.web_url)/setup#token=$tok"
      if ($NoOpen) { Say "premier r\u00e9glage (choisir un mod\u00e8le) : ouvrez $($conf.web_url)/setup#token=<contenu de $($conf.token_path)>" }
      else { Say "premier r\u00e9glage (choisir un mod\u00e8le) : la page s'ouvre dans le navigateur" }
    } else {
      Warn "premier r\u00e9glage : jeton local introuvable ($($conf.install_dir)/state/local-token) ; relancez l'installeur une fois le noyau d\u00e9marr\u00e9"
    }
  }
  if (-not $NoOpen -and (-not $Update -or -not $setupDone)) { Start-Process $openUrl }
}

# Jamais `exit` sous irm | iex (la fenetre se fermerait) : code de retour seulement quand le script est un fichier.
$code = 0
try { Main } catch {
  $code = 1
  if ("$_" -ne $StopMark) { Write-Host ((U "\u00d7 ") + "$_") -ForegroundColor Red }
}
if ($PSCommandPath) { exit $code }
