# nokker-install-capabilities: sites
# nokker-install-capabilities: desktop
# Nokker Lokkal -- installeur Windows (docs/LOKKAL.md section 3).
#
#   irm https://nokker.ai/install.ps1 | iex
#   (-NoKeepAlive : pas de tache planifiee qui garde la distribution WSL, donc Lokkal, demarree ;
#    -Lang en|fr : langue des messages, sinon NOKKER_LANG, LC_ALL, LC_MESSAGES, LANG, anglais par defaut)
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
  [switch]$Sites,                   # sites connectes (profil sites : navigateur des sites, ecran de passation)
  [switch]$SitesOnly,               # installation existante : (re)tire et reverifie sites-chrome (nokker sites enable)
  [switch]$Desktop,                 # Bureau : Linux seulement (refuse ici, rien n'est touche ; docs/DESKTOP.md)
  [switch]$Vault,                   # coffre OpenBao (profil vault), descelle automatiquement
  [switch]$VaultManual,             # idem, part de descellement affichee une fois, redemandee a chaque redemarrage
  [switch]$LocalLlm,                # moteur d'inference local (Ollama, CPU, profil local-llm), propose dans /setup
  [string]$LocalLlmModel = "",      # modele Ollama a tirer (defaut : qwen3:4b) ; implique -LocalLlm
  [string]$Lang = ""                # en|fr ; defaut : NOKKER_LANG, LC_ALL, LC_MESSAGES, LANG ; anglais sinon
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
# Langue des messages : meme regle que la commande nokker et install.sh. -Lang en|fr > NOKKER_LANG > LC_ALL >
# LC_MESSAGES > LANG (la premiere variable non vide) ; francais si elle commence par "fr", anglais sinon. Le texte
# source est l'anglais (format -f entre apostrophes, sans variable : les valeurs passent en arguments) ; $FR en donne
# la traduction, accents en \uXXXX (fichier ASCII). Test : deploy/lokkal/tests/i18n.ps1. install.sh recoit la langue
# par NOKKER_LANG (jamais --lang, qu'un install.sh plus ancien refuserait).
function Get-NokkerLang([string]$opt) {
  if ($opt) { if ($opt -match '^fr([_-]\w+)?$') { return "fr" }; if ($opt -match '^en([_-]\w+)?$') { return "en" }; return "" }
  $v = @($env:NOKKER_LANG, $env:LC_ALL, $env:LC_MESSAGES, $env:LANG) | Where-Object { $_ } | Select-Object -First 1
  if ("$v".Trim() -match '^fr') { return "fr" } else { return "en" }
}
$L = Get-NokkerLang $Lang
$LangBad = [bool]($Lang -and -not $L)
if (-not $L) { $L = Get-NokkerLang "" }
# >>> FR (catalogue : format anglais -> format francais)
$FR = @{
  ' [y/N]' = ' [o/N]'
  'Name of the Linux account to create in {0} [{1}]' = 'Nom du compte Linux \u00e0 cr\u00e9er dans {0} [{1}]'
  'name refused ({0}): lowercase letters, digits, - and _' = 'nom refus\u00e9 ({0}) : minuscules, chiffres, - et _'
  'Linux password of {0} (sudo)' = 'Mot de passe Linux de {0} (sudo)'
  'The same, once more' = 'Le m\u00eame, une seconde fois'
  'the two entries differ, or are empty' = 'les deux saisies diff\u00e8rent, ou sont vides'
  'cannot create the account {0}' = 'cr\u00e9ation du compte {0} impossible'
  'password of the account {0} not set' = 'mot de passe du compte {0} non pos\u00e9'
  'Linux account {0} created in {1}' = 'compte Linux {0} cr\u00e9\u00e9 dans {1}'
  '-Lang: en or fr expected (got {0})' = '-Lang : en ou fr attendu (re\u00e7u {0})'
  '64-bit Windows required.' = 'Windows 64 bits requis.'
  'Windows 10 version 2004 (build 19041) or newer is required for WSL 2 (current build: {0}).' = 'Windows 10 version 2004 (build 19041) ou plus r\u00e9cent requis pour WSL 2 (build actuel : {0}).'
  'WSL 2 is not installed. Install it now (administrator rights, then a Windows restart)?' = 'WSL 2 n''est pas install\u00e9. L''installer maintenant (droits administrateur, puis red\u00e9marrage de Windows) ?'
  'WSL 2 is required: https://learn.microsoft.com/windows/wsl/install' = 'WSL 2 est requis : https://learn.microsoft.com/windows/wsl/install'
  'Step 1 of 2 done: WSL 2 and Ubuntu are installed.' = '\u00c9tape 1 sur 2 termin\u00e9e : WSL 2 et Ubuntu sont install\u00e9s.'
  'Restart Windows, then run EXACTLY the same command again: step 2 creates your Linux account, installs Docker and Lokkal.' = 'Red\u00e9marrez Windows, puis relancez EXACTEMENT la m\u00eame commande : l''\u00e9tape 2 cr\u00e9e votre compte Linux, installe Docker et Lokkal.'
  'The distribution {0} is not ready in WSL. Install it now?' = 'La distribution {0} n''est pas pr\u00eate dans WSL. L''installer maintenant ?'
  'A WSL distribution is required (wsl --install -d Ubuntu).' = 'Une distribution WSL est requise (wsl --install -d Ubuntu).'
  'Open {0} once from the Start menu (it creates your Linux account), then run this command again: it is the last step.' = 'Ouvrez \u00ab {0} \u00bb une fois depuis le menu D\u00e9marrer (cr\u00e9ation de votre compte Linux), puis relancez cette commande : c''est la derni\u00e8re \u00e9tape.'
  'The distribution {0} runs WSL 1: wsl --set-version {0} 2, then run again.' = 'La distribution {0} est en WSL 1 : wsl --set-version {0} 2, puis relancez.'
  'Open {0} once to create your Linux account (Lokkal does not install as root), then run this command again: it is the last step.' = 'Ouvrez \u00ab {0} \u00bb une fois pour cr\u00e9er votre compte Linux (Lokkal ne s''installe pas en root), puis relancez cette commande : c''est la derni\u00e8re \u00e9tape.'
  'the Linux account is not the default account of {0}: wsl --terminate {0}, then run again.' = 'le compte Linux n''est pas le compte par d\u00e9faut de {0} : wsl --terminate {0}, puis relancez.'
  'WSL 2: {0} (account {1})' = 'WSL 2 : {0} (compte {1})'
  'Docker Desktop is installed but cannot be reached from {0}: start Docker Desktop, then Settings > Resources > WSL integration > turn on {0}, and run again.' = 'Docker Desktop est install\u00e9 mais pas joignable depuis {0} : lancez Docker Desktop, puis Settings > Resources > WSL integration > activez {0}, et relancez.'
  'Docker is not in {0}: install.sh will install Docker Engine (official script; sudo asks for the Linux password).' = 'Docker n''est pas dans {0} : install.sh va installer Docker Engine (script officiel ; mot de passe Linux demand\u00e9 par sudo).'
  'turning on systemd in {0} (required for Docker Engine)' = 'activation de systemd dans {0} (requis pour Docker Engine)'
  'systemd is not active in {0} (required for Docker Engine): wsl --shutdown in PowerShell, then run again. Or install Docker Desktop with the WSL integration.' = 'systemd n''est pas actif dans {0} (requis pour Docker Engine) : wsl --shutdown dans PowerShell, puis relancez. Ou installez Docker Desktop avec l''int\u00e9gration WSL.'
  'Docker also runs in the distribution {0}. In WSL all distributions share the same network, and two Dockers get in the way of each other: the Lokkal services can no longer reach each other. Before using Lokkal in {1}, stop {0} (wsl --terminate {0}), or turn off its Docker (in {0}: sudo systemctl disable --now docker docker.socket).' = 'Docker tourne aussi dans la distribution {0}. Dans WSL, toutes les distributions partagent le m\u00eame r\u00e9seau, et deux Docker s''y g\u00eanent : les services de Lokkal ne se joignent plus entre eux. Avant d''utiliser Lokkal dans {1}, arr\u00eatez {0} (wsl --terminate {0}), ou d\u00e9sactivez son Docker (dans {0} : sudo systemctl disable --now docker docker.socket).'
  'The scheduled task Nokker Lokkal (WSL {0}) restarts {0}: turn it off too (Disable-ScheduledTask -TaskName ''Nokker Lokkal (WSL {0})'').' = 'La t\u00e2che planifi\u00e9e \u00ab Nokker Lokkal (WSL {0}) \u00bb red\u00e9marre {0} : d\u00e9sactivez-la aussi (Disable-ScheduledTask -TaskName ''Nokker Lokkal (WSL {0})'').'
  'install.sh failed in {0} (log above).' = 'install.sh a \u00e9chou\u00e9 dans {0} (journal ci-dessus).'
  'Nokker Lokkal uninstalled.' = 'Nokker Lokkal d\u00e9sinstall\u00e9.'
  'Keeps the WSL distribution of Nokker Lokkal running (otherwise Lokkal stops with the last session).' = 'Garde la distribution WSL de Nokker Lokkal d\u00e9marr\u00e9e (sinon Lokkal s''arr\u00eate avec la derni\u00e8re session).'
  'distribution {0} kept running (scheduled task {1}; -NoKeepAlive to do without)' = 'distribution {0} gard\u00e9e d\u00e9marr\u00e9e (t\u00e2che planifi\u00e9e \u00ab {1} \u00bb ; -NoKeepAlive pour s''en passer)'
  'scheduled task not set ({0}): Lokkal stops when the last WSL window closes' = 't\u00e2che planifi\u00e9e non pos\u00e9e ({0}) : Lokkal s''arr\u00eate quand la derni\u00e8re fen\u00eatre WSL se ferme'
  'wrong sha256 checksum of {0}: aborted' = 'empreinte sha256 de {0} incorrecte : abandon'
  'terminal: nokker   (new PowerShell window)' = 'terminal : nokker   (nouvelle fen\u00eatre PowerShell)'
  'nokker command not available for windows-{0} in this version (the web works)' = 'commande nokker indisponible pour windows-{0} dans cette version (le web fonctionne)'
  'Nokker Lokkal {0} is ready: {1}/' = 'Nokker Lokkal {0} est pr\u00eat : {1}/'
  'work folders in {0}, visible in File Explorer: {1}\home\{2}' = 'dossiers de travail dans {0}, visibles dans l''Explorateur : {1}\home\{2}'
  'first setup (choose a model): run nokker open /setup' = 'premier r\u00e9glage (choisir un mod\u00e8le) : lancez nokker open /setup'
  'first setup (choose a model): {0}' = 'premier r\u00e9glage (choisir un mod\u00e8le) : {0}'
  '  this link works once, for 60 seconds; later: nokker open /setup' = '  ce lien ne sert qu''une fois, pendant 60 secondes ; plus tard : nokker open /setup'
  'Nokker opens in the browser with: nokker open /' = 'Nokker s''ouvre dans le navigateur avec : nokker open /'
  'connected sites not turned on in {0} (reason above).' = 'sites connect\u00e9s non allum\u00e9s dans {0} (raison ci-dessus).'
  '-Desktop: the Desktop is Linux only (install.sh --desktop on an amd64 Linux machine); nothing was touched.' = '-Desktop : le Bureau est r\u00e9serv\u00e9 \u00e0 Linux (install.sh --desktop sur une machine Linux amd64) ; rien n''a \u00e9t\u00e9 touch\u00e9.'
}
# <<< FR
function T([string]$f) {   # T FORMAT [VALEURS...]
  if ($script:L -eq "fr" -and $FR.ContainsKey($f)) { $f = $FR[$f] }
  $f = U $f
  if ($args.Count) { return ($f -f $args) } else { return $f }
}
function Say  { Write-Host ((U "\u203a ") + (T @args)) }
function Warn { Write-Host ("! " + (T @args)) -ForegroundColor Yellow }
function Die  { Write-Host ((U "\u00d7 ") + (T @args)) -ForegroundColor Red; throw $StopMark }
function Ask  { if ($Yes) { return $true }; $a = Read-Host ((T @args) + (T ' [y/N]')); return $a -match '^(o|oui|y|yes)$' }

# Commande wsl.exe dont le stderr ne doit pas devenir une erreur bloquante (Windows PowerShell 5.1 + EAP Stop) ;
# $LASTEXITCODE reste celui de la commande.
function Native([scriptblock]$sb) {
  $eap = $ErrorActionPreference; $ErrorActionPreference = "Continue"
  try { & $sb 2>$null } finally { $ErrorActionPreference = $eap }
}
function Wsl-Root([string]$d, [string]$cmd) { Native { & wsl.exe -d $d -u root -- sh -c $cmd } }

# Script POSIX joue DANS WSL (jamais ici) pour parler au noyau avec le jeton local : PowerShell ne lit ni n'affiche le jeton, et
# curl le recoit sur son entree standard (-H @-), jamais dans sa ligne de commande (visible dans ps). Arguments : le dossier
# d'installation, puis le mode. `setup` : affiche 1 si le premier reglage est fait. `code` : affiche SEUL un code d'acces a
# usage unique (60 s, POST /v1/local/session-code). Rien sur stdout sinon. Teste par tests/unit/test_install_ps1_code.py.
$WslLocal = @'
# >>> WSLLOCAL
d=$1; mode=$2
keyfile=$d/state/local-token
if [ "$mode" = setup ] && grep -Eq 'done.: *true' "$d/state/setup.json" 2>/dev/null; then echo 1; exit 0; fi
[ -r "$keyfile" ] || exit 0
p=$(sed -n 's/^NOKKER_API_PORT=//p' "$d/.env" 2>/dev/null | head -n 1)
[ -n "$p" ] || exit 0
# Le noyau n'est pas toujours pret a la fin de l'installation : healthz (sans jeton), une dizaine d'essais a 1 s ; sinon rien
# (PowerShell affiche alors `nokker open /setup`), et AUCUN appel avec le jeton.
i=0
while ! curl -fsS -m 3 "http://127.0.0.1:$p/healthz" >/dev/null 2>&1; do
  i=$((i + 1)); [ "$i" -lt 10 ] || exit 0
  sleep 1
done
# printf est un BUILTIN du shell (dash, bash, ash) : le jeton ne paraitra donc pas dans la liste des processus. Jamais
# /usr/bin/printf ni `env printf`.
call() { # methode chemin [corps] : le jeton va a curl par stdin, jamais en argument
  printf 'Authorization: Bearer %s\n' "$(cat "$keyfile")" |
    curl -fsS -m 10 -X "$1" -H @- -H 'Content-Type: application/json' ${3:+--data "$3"} "http://127.0.0.1:$p$2" 2>/dev/null
}
case "$mode" in
  setup) call GET /v1/local/setup | grep -Eq 'done.: *true|linked.: *true|default.: *[^n[:space:]]' && echo 1 ;;
  code) call POST /v1/local/session-code '{}' | sed -n 's/.*"code": *"\([A-Za-z0-9_-]\{16,64\}\)".*/\1/p' | head -n 1 ;;
esac
exit 0
# <<< WSLLOCAL
'@
# Lance le script ci-dessus dans la distribution : en base64 (aucun guillemet a echapper entre PowerShell 5.1 et wsl.exe, pas de
# fin de ligne Windows). Rend la sortie (vide si rien, ou si le dossier n'est pas un chemin sain).
function Invoke-WslLocal([string]$d, [string]$dir, [string]$mode) {
  if ($dir -notmatch '^[^''"\x00-\x1f]+$') { return "" }   # entre apostrophes cote sh : ni apostrophe ni guillemet ni caractere de controle
  $b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(($WslLocal -replace "`r", "")))
  $out = Native { & wsl.exe -d $d -- sh -c "echo $b64 | base64 -d | sh -s -- '$dir' $mode" }
  return (($out | Out-String).Trim())
}

# Compte Linux cree ICI (sinon : ouvrir Ubuntu une fois, puis relancer = une passe de plus). Mot de passe demande deux
# fois, transmis a chpasswd par l'entree standard (jamais en argument), en UTF-8.
function New-LinuxUser([string]$d) {
  if ($Yes) { return $false }   # sans dialogue (automatisation) : pas de mot de passe a demander
  $def = ($env:USERNAME.ToLower() -replace '[^a-z0-9_-]', '')
  if ($def -notmatch '^[a-z_]') { $def = "nokker" }
  $name = Read-Host (T 'Name of the Linux account to create in {0} [{1}]' $d $def)
  if (-not $name) { $name = $def }
  if ($name -notmatch '^[a-z_][a-z0-9_-]{0,31}$') { Warn 'name refused ({0}): lowercase letters, digits, - and _' $name; return $false }
  for ($i = 0; $i -lt 3; $i++) {
    $p1 = Read-Host (T 'Linux password of {0} (sudo)' $name) -AsSecureString
    $p2 = Read-Host (T 'The same, once more') -AsSecureString
    $a = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($p1))
    $b = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($p2))
    if ($a -and $a -eq $b) { break }
    Warn 'the two entries differ, or are empty'; $a = $null
  }
  if (-not $a) { return $false }
  Wsl-Root $d "id -u '$name' >/dev/null 2>&1 || useradd -m -s /bin/bash -G sudo '$name'"
  if ($LASTEXITCODE -ne 0) { Warn 'cannot create the account {0}' $name; return $false }
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
  if (-not $ok) { Warn 'password of the account {0} not set' $name; return $false }
  # Compte par defaut de la distribution (/etc/wsl.conf [user]), applique au prochain demarrage de la distribution.
  Wsl-Root $d "grep -q '^\[user\]' /etc/wsl.conf 2>/dev/null && sed -i '/^\[user\]/,/^\[/{s/^default=.*/default=$name/}' /etc/wsl.conf || printf '\n[user]\ndefault=$name\n' >> /etc/wsl.conf"
  $null = Native { & wsl.exe --terminate $d }
  Say 'Linux account {0} created in {1}' $name $d
  return $true
}

function Main {
  if ($LangBad) { Die '-Lang: en or fr expected (got {0})' $Lang }
  # Bureau : Linux seulement en B1 (docs/DESKTOP.md) ; refuse AVANT tout (WSL, Docker, paquet)
  if ($Desktop) { Die '-Desktop: the Desktop is Linux only (install.sh --desktop on an amd64 Linux machine); nothing was touched.' }
  if ([Environment]::Is64BitOperatingSystem -eq $false) { Die '64-bit Windows required.' }
  $arch = if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") { "arm64" } else { "x64" }
  $build = [int](Get-CimInstance Win32_OperatingSystem).BuildNumber
  if ($build -lt 19041) { Die 'Windows 10 version 2004 (build 19041) or newer is required for WSL 2 (current build: {0}).' $build }

  # ------------------------------------------------------------------------------------------- WSL 2 (etape 1 sur 2)
  $wslOk = $false
  try { $null = & wsl.exe --status 2>$null; $wslOk = ($LASTEXITCODE -eq 0) } catch { }
  if (-not $wslOk) {
    if (-not (Ask 'WSL 2 is not installed. Install it now (administrator rights, then a Windows restart)?')) {
      Die 'WSL 2 is required: https://learn.microsoft.com/windows/wsl/install'
    }
    # Deja administrateur (session elevee, ssh d'un compte admin) : directement ; RunAs sans bureau n'installe rien.
    $admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
      [Security.Principal.WindowsBuiltInRole]::Administrator)
    if ($admin) { & wsl.exe --install -d Ubuntu --no-launch }
    else { Start-Process wsl.exe -ArgumentList "--install","-d","Ubuntu","--no-launch" -Verb RunAs -Wait }
    Warn 'Step 1 of 2 done: WSL 2 and Ubuntu are installed.'
    Warn 'Restart Windows, then run EXACTLY the same command again: step 2 creates your Linux account, installs Docker and Lokkal.'
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
    if (-not (Ask 'The distribution {0} is not ready in WSL. Install it now?' $Distro)) { Die 'A WSL distribution is required (wsl --install -d Ubuntu).' }
    $null = Native { & wsl.exe --install -d $Distro --no-launch }
    $null = Wsl-Root $Distro "true"
    if ($LASTEXITCODE -ne 0) {
      $launcher = Get-Command (($Distro -replace '[^A-Za-z0-9.]', '').ToLower() + ".exe") -ErrorAction SilentlyContinue
      if ($launcher) { $null = Native { & $launcher.Source install --root } }
      $null = Wsl-Root $Distro "true"
    }
    if ($LASTEXITCODE -ne 0) {
      Warn 'Open {0} once from the Start menu (it creates your Linux account), then run this command again: it is the last step.' $Distro
      return
    }
  }
  $ver = (& wsl.exe --list --verbose 2>$null | Where-Object { $_ -match "\b$([regex]::Escape($Distro))\b" } | ForEach-Object { ($_ -split '\s+')[-1] })
  if ($ver -and $ver.Trim() -eq "1") { Die 'The distribution {0} runs WSL 1: wsl --set-version {0} 2, then run again.' $Distro }
  $user = (& wsl.exe -d $Distro -- sh -c 'id -un' 2>$null)
  if (-not $user -or $user.Trim() -eq "root") {
    if (-not (New-LinuxUser $Distro)) {
      Warn 'Open {0} once to create your Linux account (Lokkal does not install as root), then run this command again: it is the last step.' $Distro
      return
    }
    $user = (& wsl.exe -d $Distro -- sh -c 'id -un' 2>$null)
    if (-not $user -or $user.Trim() -eq "root") { Die 'the Linux account is not the default account of {0}: wsl --terminate {0}, then run again.' $Distro }
  }
  Say 'WSL 2: {0} (account {1})' $Distro ($user.Trim())

  # ------------------------------------------------------------------------------------------- Docker dans la distribution
  & wsl.exe -d $Distro -- sh -c 'docker info >/dev/null 2>&1 && docker compose version >/dev/null 2>&1'
  if ($LASTEXITCODE -ne 0) {
    $desktop = Test-Path "$env:ProgramFiles\Docker\Docker\Docker Desktop.exe"
    if ($desktop) {
      Die 'Docker Desktop is installed but cannot be reached from {0}: start Docker Desktop, then Settings > Resources > WSL integration > turn on {0}, and run again.' $Distro
    }
    Warn 'Docker is not in {0}: install.sh will install Docker Engine (official script; sudo asks for the Linux password).' $Distro
    & wsl.exe -d $Distro -- sh -c 'ps -p 1 -o comm= | grep -q systemd'
    if ($LASTEXITCODE -ne 0) {
      # systemd (requis pour Docker Engine) : active ici plutot qu'une consigne et une passe de plus.
      Say 'turning on systemd in {0} (required for Docker Engine)' $Distro
      Wsl-Root $Distro "grep -q '^systemd=true' /etc/wsl.conf 2>/dev/null || printf '\n[boot]\nsystemd=true\n' >> /etc/wsl.conf"
      $null = Native { & wsl.exe --terminate $Distro }
      & wsl.exe -d $Distro -- sh -c 'ps -p 1 -o comm= | grep -q systemd'
      if ($LASTEXITCODE -ne 0) {
        Die 'systemd is not active in {0} (required for Docker Engine): wsl --shutdown in PowerShell, then run again. Or install Docker Desktop with the WSL integration.' $Distro
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
      Warn 'Docker also runs in the distribution {0}. In WSL all distributions share the same network, and two Dockers get in the way of each other: the Lokkal services can no longer reach each other. Before using Lokkal in {1}, stop {0} (wsl --terminate {0}), or turn off its Docker (in {0}: sudo systemctl disable --now docker docker.socket).' $o $Distro
      if (Get-ScheduledTask -TaskName "Nokker Lokkal (WSL $o)" -ErrorAction SilentlyContinue) {
        Warn 'The scheduled task Nokker Lokkal (WSL {0}) restarts {0}: turn it off too (Disable-ScheduledTask -TaskName ''Nokker Lokkal (WSL {0})'').' $o
      }
    }
  }

  # Sites seuls : l'installeur DEJA pose dans la distribution (meme version que Lokkal), sans paquet (CONNECTED-SITES 3.1.1)
  if ($SitesOnly) {
    & wsl.exe -d $Distro -- bash -lc "NOKKER_LANG=$L sh ~/.nokker/install.sh --sites-only"
    if ($LASTEXITCODE -ne 0) { Die 'connected sites not turned on in {0} (reason above).' $Distro }
    return
  }
  # ------------------------------------------------------------------------------------------- install.sh dans la distribution
  $shArgs = @()
  if ($Version)   { $shArgs += @("--version", $Version) }
  if ($Workspace) { $shArgs += @("--workspace", $Workspace) }
  if ($Yes)       { $shArgs += "--yes" }
  if ($Update)    { $shArgs += "--update" }
  if ($Uninstall) { $shArgs += "--uninstall" }
  if ($Purge)     { $shArgs += "--purge" }
  if ($Sites)     { $shArgs += "--sites" }
  if ($VaultManual) { $shArgs += "--vault-manual" } elseif ($Vault) { $shArgs += "--vault" }
  if ($LocalLlmModel) { $shArgs += @("--local-llm-model", $LocalLlmModel) } elseif ($LocalLlm) { $shArgs += "--local-llm" }
  $shArgs += "--no-open"
  $quoted = ($shArgs | ForEach-Object { "'" + ($_ -replace "'", "'\''") + "'" }) -join " "
  $envs = ""
  foreach ($k in "NOKKER_RELEASE_BASE","NOKKER_SKIP_PULL") {
    $v = [Environment]::GetEnvironmentVariable($k); if ($v) { $envs += "$k='$v' " }
  }
  $envs += "NOKKER_LANG=$L "   # en|fr : la langue choisie ici, pour install.sh
  if ($Base -and $Base -like "file://*") { $src = "cat '$($Base.Substring(7))/install.sh'" }
  elseif ($Base) { $src = "curl -fsSL '$Base/install.sh'" }
  else { $src = "curl -fsSL https://nokker.ai/install.sh" }
  # NOKKER_NO_CLI : la commande nokker Linux reste posee dans la distribution ; Windows recoit nokker.exe plus bas.
  & wsl.exe -d $Distro -- bash -lc "$src | env $envs sh -s -- $quoted"
  if ($LASTEXITCODE -ne 0) { Die 'install.sh failed in {0} (log above).' $Distro }
  $keepTask = "Nokker Lokkal (WSL $Distro)"
  if ($Uninstall) {
    Remove-Item -Force -ErrorAction SilentlyContinue "$HOME\.config\nokker\lokkal.json"
    Unregister-ScheduledTask -TaskName $keepTask -Confirm:$false -ErrorAction SilentlyContinue
    Say 'Nokker Lokkal uninstalled.'
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
        -Description (T 'Keeps the WSL distribution of Nokker Lokkal running (otherwise Lokkal stops with the last session).') -Force `
        -ErrorAction Stop
      Start-ScheduledTask -TaskName $keepTask -ErrorAction Stop
      Say 'distribution {0} kept running (scheduled task {1}; -NoKeepAlive to do without)' $Distro $keepTask
    } catch {
      Warn 'scheduled task not set ({0}): Lokkal stops when the last WSL window closes' ($_.Exception.Message)
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
    if ((Get-FileHash $tmp -Algorithm SHA256).Hash.ToLower() -ne $want) { Die 'wrong sha256 checksum of {0}: aborted' $bin }
    Move-Item -Force $tmp (Join-Path $binDir "nokker.exe")
    $path = [Environment]::GetEnvironmentVariable("Path", "User")
    if (-not ($path -split ';' | Where-Object { $_ -eq $binDir })) {
      [Environment]::SetEnvironmentVariable("Path", ($path.TrimEnd(';') + ";" + $binDir), "User")
    }
    Say 'terminal: nokker   (new PowerShell window)'
  } catch {
    if ("$_" -eq $StopMark) { throw }
    Warn 'nokker command not available for windows-{0} in this version (the web works)' $arch
  } finally { Remove-Item -Force -ErrorAction SilentlyContinue $tmp }

  Say 'Nokker Lokkal {0} is ready: {1}/' $v $conf.web_url
  Say 'work folders in {0}, visible in File Explorer: {1}\home\{2}' $Distro $unc ($user.Trim())
  # Le jeton local ne sort JAMAIS dans une URL ni dans une ligne de commande : le noyau emet un code d'acces a usage unique
  # (POST /v1/local/session-code, 60 s), comme install.sh ; le jeton est lu DANS WSL (Invoke-WslLocal), jamais rapatrie ici.
  # Premier reglage deja fait (assistant), sinon l'etat reel du noyau : machine reliee au Router ou modele par defaut.
  $setupDone = [bool](Invoke-WslLocal $Distro $conf.install_dir 'setup')
  if (-not $setupDone) {
    $openCode = Invoke-WslLocal $Distro $conf.install_dir 'code'
    if ($openCode -match '^[A-Za-z0-9_-]{16,64}$') {
      # Le code vit 60 s : il est emis au DERNIER moment (tout est installe), puis affiche (decision de dev : on n'ouvre pas le
      # navigateur, comme install.sh sans -Open ; plus tard : nokker open /setup).
      $link = "{0}/setup#code={1}" -f ([string]$conf.web_url).TrimEnd('/'), $openCode
      Say 'first setup (choose a model): {0}' $link
      Say '  this link works once, for 60 seconds; later: nokker open /setup'
    } else { Say 'first setup (choose a model): run nokker open /setup' }
  }
  elseif (-not $Update) { Say 'Nokker opens in the browser with: nokker open /' }
}

# Jamais `exit` sous irm | iex (la fenetre se fermerait) : code de retour seulement quand le script est un fichier.
$code = 0
try { Main } catch {
  $code = 1
  if ("$_" -ne $StopMark) { Write-Host ((U "\u00d7 ") + "$_") -ForegroundColor Red }
}
if ($PSCommandPath) { exit $code }
