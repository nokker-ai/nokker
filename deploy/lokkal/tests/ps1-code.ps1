# Emission du code d'acces par install.ps1 (Invoke-WslLocal), JOUEE pour de vrai sous PowerShell, avec un FAUX wsl.exe et un FAUX curl
# (aucun WSL, aucun reseau) : le script POSIX passe en base64, le code revient rogne et valide, le jeton local n'apparait dans aucune
# ligne de commande (curl la lit sur son entree standard), rien sans noyau pret.
#   docker run --rm -v "$PWD":/w -w /w mcr.microsoft.com/powershell pwsh -NoProfile -File deploy/lokkal/tests/ps1-code.ps1
# (necessite un pwsh sous Linux avec sh/curl factices : le test pose lui-meme ses faux executables dans un dossier temporaire.)
# Le comportement du script POSIX est teste a part (tests/unit/test_install_ps1_code.py) ; ici : la partie PowerShell.
$ErrorActionPreference = "Stop"
$path = Join-Path $PSScriptRoot "..\install.ps1"
$fail = 0; $n = 0
function Check([bool]$cond, [string]$what) { $script:n++; if (-not $cond) { $script:fail++; Write-Host "FAIL $what" } }

$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$errors)
Check ($errors.Count -eq 0) "install.ps1 analysable"
# On ne joue que ce qui concerne l'emission : $WslLocal, Native, Invoke-WslLocal (extraits du fichier, pas recopies).
$assign = $ast.Find({ param($a) $a -is [Management.Automation.Language.AssignmentStatementAst] -and $a.Left.Extent.Text -eq '$WslLocal' }, $true)
Check ($null -ne $assign) '$WslLocal trouve'
Invoke-Expression $assign.Extent.Text
foreach ($fn in "Native", "Invoke-WslLocal") {
  $f = $ast.Find({ param($a) $a -is [Management.Automation.Language.FunctionDefinitionAst] -and $a.Name -eq $fn }, $true)
  Check ($null -ne $f) "$fn trouve"
  Invoke-Expression $f.Extent.Text
}

$tmp = Join-Path ([IO.Path]::GetTempPath()) ("nokker-ps1code-" + [guid]::NewGuid().ToString("N"))
$bin = Join-Path $tmp "bin"; $inst = Join-Path $tmp "inst with space"
New-Item -ItemType Directory -Force -Path $bin, (Join-Path $inst "state") | Out-Null
$TOK = "tok-local-secret-0123456789abcdefghijklmnop"
$WANT = "C0deC0deC0deC0deC0de12"
[IO.File]::WriteAllText((Join-Path $inst "state/local-token"), "$TOK`n")
[IO.File]::WriteAllText((Join-Path $inst ".env"), "NOKKER_PORT=7420`nNOKKER_API_PORT=8742`n")
$log = Join-Path $tmp "log"
# faux wsl.exe : `wsl.exe -d <distro> -- sh -c <commande>` -> sh -c <commande> (comme WSL le ferait dans la distribution)
Set-Content -NoNewline -Path (Join-Path $bin "wsl.exe") -Value "#!/bin/sh`nprintf 'WSL %s\n' `"`$*`" >> `"$log`"`nshift 3; exec `"`$@`"`n"
# faux curl : healthz ok, session-code -> le code, setup -> {done:false} (script ecrit tel quel : here-string litterale)
$curlSh = @'
#!/bin/sh
printf 'ARGS %s\n' "$*" >> "__LOG__"
case "$*" in *healthz*) printf ok; exit 0;; esac
[ -t 0 ] || printf 'STDIN %s\n' "$(cat)" >> "__LOG__"
case "$*" in
  *session-code*) printf '{"code": "__CODE__", "expires_in": 60}' ;;
  *) printf '{"done": false}' ;;
esac
'@
[IO.File]::WriteAllText((Join-Path $bin "curl"), (($curlSh -replace "`r", "") -replace "__LOG__", $log -replace "__CODE__", $WANT))
chmod +x (Join-Path $bin "wsl.exe") (Join-Path $bin "curl")
$env:PATH = "$bin" + [IO.Path]::PathSeparator + $env:PATH

$got = Invoke-WslLocal "Ubuntu" $inst "code"
Check ($got -ceq $WANT) "le code revient seul, rogne : '$got'"
Check ($got -match '^[A-Za-z0-9_-]{16,64}$') "le code a la forme attendue"
$text = Get-Content -Raw $log
if ($got -cne $WANT) { Write-Host "--- log"; Write-Host $text; Write-Host "--- faux curl"; Get-Content -Raw (Join-Path $bin "curl") }
Check ($text -notmatch [regex]::Escape($TOK) -or $text -match "STDIN Authorization: Bearer $([regex]::Escape($TOK))") "jeton : seulement sur l'entree standard"
$cmdLines = ($text -split "`n") | Where-Object { $_ -match '^(WSL|ARGS) ' }
Check (@($cmdLines | Where-Object { $_ -match [regex]::Escape($TOK) }).Count -eq 0) "jeton absent de wsl.exe et de curl (arguments)"
Check ($text -match "STDIN Authorization: Bearer $([regex]::Escape($TOK))") "en-tete lu sur stdin"
Check ($text -match "session-code") "session-code appele"
# dossier refuse : apostrophe, guillemet, controle -> rien, aucun appel
$before = (Get-Content -Raw $log).Length
Check ((Invoke-WslLocal "Ubuntu" "/home/x/it's" "code") -eq "") "dossier avec apostrophe refuse"
Check ((Invoke-WslLocal "Ubuntu" "/home/x/a`"b" "code") -eq "") "dossier avec guillemet refuse"
Check ((Get-Content -Raw $log).Length -eq $before) "aucun appel pour un dossier refuse"
# setup : le noyau dit {done:false} -> vide
Check ((Invoke-WslLocal "Ubuntu" $inst "setup") -eq "") "setup non fait : vide"
Remove-Item -Recurse -Force $tmp
Write-Host "ps1-code.ps1 : $n controles, $fail en echec"
exit $(if ($fail) { 1 } else { 0 })
