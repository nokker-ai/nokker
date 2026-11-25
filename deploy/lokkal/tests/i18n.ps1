# Langue des messages d'install.ps1 (anglais par defaut, francais par la regle de la commande nokker).
#   pwsh -NoProfile -File deploy/lokkal/tests/i18n.ps1      (lance aussi par tests/i18n.sh quand pwsh est la)
# install.ps1 n'est PAS execute : il est lu par l'analyseur de PowerShell. Statique : fichier ASCII sans BOM ; chaque
# Say/Warn/Die/Ask/T a pour premier argument un format anglais entre apostrophes, present dans $FR, avec les memes
# {n} ; aucune entree morte ; traductions en ASCII (accents \uXXXX). Execution : les fonctions U, Get-NokkerLang et T
# sont extraites du fichier et jouees (ordre des variables, -Lang, valeurs jamais decodees), et install.sh recoit
# NOKKER_LANG.
$ErrorActionPreference = "Stop"
$path = Join-Path $PSScriptRoot "..\install.ps1"
$fail = 0; $n = 0
function Check([bool]$cond, [string]$what) {
  $script:n++
  if (-not $cond) { $script:fail++; Write-Host "FAIL $what" }
}

# ------------------------------------------------------------------------------------------------- fichier
$bytes = [IO.File]::ReadAllBytes($path)
Check (-not ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF)) "install.ps1 sans BOM"
Check (@($bytes | Where-Object { $_ -gt 127 }).Count -eq 0) "install.ps1 100 % ASCII"

$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$errors)
Check ($errors.Count -eq 0) ("install.ps1 analysable : " + ($errors | ForEach-Object { $_.Message }) -join "; ")

# ------------------------------------------------------------------------------------------------- catalogue
$frAssign = $ast.Find({ param($a) $a -is [Management.Automation.Language.AssignmentStatementAst] -and
  $a.Left.Extent.Text -eq '$FR' }, $true)
Check ($null -ne $frAssign) "catalogue `$FR trouve"
$ht = $frAssign.Right.Expression
$cat = New-Object 'System.Collections.Generic.Dictionary[string,string]' ([StringComparer]::Ordinal)
foreach ($kv in $ht.KeyValuePairs) {
  Check ($kv.Item1 -is [Management.Automation.Language.StringConstantExpressionAst]) "cle litterale : $($kv.Item1.Extent.Text)"
  $v = $kv.Item2.PipelineElements[0].Expression
  Check ($v -is [Management.Automation.Language.StringConstantExpressionAst]) "valeur litterale : $($kv.Item2.Extent.Text)"
  $cat[$kv.Item1.Value] = $v.Value
}
Check ($cat.Count -gt 30) "catalogue rempli ($($cat.Count))"

function Holes([string]$s) { ([regex]::Matches($s, '\{(\d+)\}') | ForEach-Object { $_.Groups[1].Value } | Sort-Object -Unique) -join "," }

$used = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
$calls = $ast.FindAll({ param($a) $a -is [Management.Automation.Language.CommandAst] -and
  @("Say", "Warn", "Die", "Ask", "T") -contains $a.GetCommandName() }, $true)
foreach ($c in $calls) {
  $first = $c.CommandElements[1]
  if ($null -ne $first -and $first -is [Management.Automation.Language.VariableExpressionAst] -and $first.Splatted) { continue }  # relais @args
  $okLit = ($first -is [Management.Automation.Language.StringConstantExpressionAst]) -and
           ($first.StringConstantType -eq [Management.Automation.Language.StringConstantType]::SingleQuoted)
  Check $okLit ("ligne $($c.Extent.StartLineNumber) : format anglais entre apostrophes attendu : " + $c.Extent.Text)
  if (-not $okLit) { continue }
  $en = $first.Value; $null = $used.Add($en)
  Check $cat.ContainsKey($en) "ligne $($c.Extent.StartLineNumber) : pas de traduction : $en"
  if ($cat.ContainsKey($en)) {
    Check ((Holes $en) -eq (Holes $cat[$en])) "memes {n} : $en"
    $max = (Holes $en).Split(",") | Where-Object { $_ } | ForEach-Object { [int]$_ } | Measure-Object -Maximum
    $given = $c.CommandElements.Count - 2
    Check (([int]$max.Maximum + [int]($max.Count -gt 0)) -le $given) "ligne $($c.Extent.StartLineNumber) : valeurs fournies pour {n} : $en"
  }
}
foreach ($k in $cat.Keys) { Check $used.Contains($k) "entree morte : $k" }
foreach ($k in $cat.Keys) { Check ($cat[$k] -notmatch '[^\x00-\x7F]') "traduction ASCII : $k" }

# ------------------------------------------------------------------------------------------------- execution
foreach ($name in "U", "Get-NokkerLang", "T") {
  $fn = $ast.Find({ param($a) $a -is [Management.Automation.Language.FunctionDefinitionAst] -and $a.Name -eq $name }, $true)
  Check ($null -ne $fn) "fonction $name"
  . ([scriptblock]::Create($fn.Extent.Text))
}
$FR = @{}; foreach ($k in $cat.Keys) { $FR[$k] = $cat[$k] }

$keep = @{}; foreach ($v in "NOKKER_LANG", "LC_ALL", "LC_MESSAGES", "LANG") { $keep[$v] = [Environment]::GetEnvironmentVariable($v) }
function SetEnv($h) { foreach ($v in "NOKKER_LANG", "LC_ALL", "LC_MESSAGES", "LANG") { [Environment]::SetEnvironmentVariable($v, $h[$v]) } }
$cases = @(
  @{ env = @{}; opt = ""; want = "en"; what = "rien : anglais" },
  @{ env = @{ LANG = "C" }; opt = ""; want = "en"; what = "LANG=C" },
  @{ env = @{ LANG = "fr_CH.UTF-8" }; opt = ""; want = "fr"; what = "LANG=fr_CH" },
  @{ env = @{ LANG = "fr_CH.UTF-8"; LC_ALL = "en_US.UTF-8" }; opt = ""; want = "en"; what = "LC_ALL prime" },
  @{ env = @{ LANG = "en_US.UTF-8"; LC_MESSAGES = "fr_FR.UTF-8" }; opt = ""; want = "fr"; what = "LC_MESSAGES prime sur LANG" },
  @{ env = @{ NOKKER_LANG = "en"; LANG = "fr_FR.UTF-8" }; opt = ""; want = "en"; what = "NOKKER_LANG prime" },
  @{ env = @{ NOKKER_LANG = "french" }; opt = ""; want = "fr"; what = "NOKKER_LANG=french" },
  @{ env = @{ NOKKER_LANG = "fr" }; opt = "en"; want = "en"; what = "-Lang en prime" },
  @{ env = @{}; opt = "fr-CH"; want = "fr"; what = "-Lang fr-CH" },
  @{ env = @{}; opt = "de"; want = ""; what = "-Lang de refuse" }
)
foreach ($c in $cases) { SetEnv $c.env; Check ((Get-NokkerLang $c.opt) -eq $c.want) ("Get-NokkerLang : " + $c.what) }
SetEnv $keep

$script:L = "en"
Check ((T 'WSL 2: {0} (account {1})' "Ubuntu" "alice") -eq "WSL 2: Ubuntu (account alice)") "T en"
$script:L = "fr"
Check ((T 'WSL 2: {0} (account {1})' "Ubuntu" "alice") -eq "WSL 2 : Ubuntu (compte alice)") "T fr"
Check ((T 'Nokker Lokkal uninstalled.') -eq ("Nokker Lokkal d" + [char]0xE9 + "sinstall" + [char]0xE9 + ".")) "T fr : accents decodes"
Check ((T 'work folders in {0}, visible in File Explorer: {1}\home\{2}' "Ubuntu" "\\wsl.localhost\Ubuntu" "u1234") -like "*\\wsl.localhost\Ubuntu\home\u1234") "T : valeurs jamais decodees (\u dans un chemin)"
Check ((T ' [y/N]') -eq " [o/N]") "T fr : [o/N]"
$script:L = "en"

# install.sh recoit la langue choisie (NOKKER_LANG, pas --lang)
$src = [IO.File]::ReadAllText($path)
Check ($src.Contains('$envs += "NOKKER_LANG=$L "')) "NOKKER_LANG transmis a install.sh"
Check (-not ($src -match '\$shArgs \+= .*--lang')) "pas de --lang vers install.sh"

Write-Host ("i18n.ps1 : {0} controles, {1} en echec" -f $n, $fail)
if ($fail -gt 0) { exit 1 }
