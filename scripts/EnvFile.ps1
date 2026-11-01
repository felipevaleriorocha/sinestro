# Leitura segura do .env (sem ConvertFrom-StringData, que trata '\' como escape e corrompe caminhos/senhas).
function Read-EnvFile {
    param([Parameter(Mandatory)][string]$Path)
    $values = @{}
    if (!(Test-Path $Path)) { return $values }
    # .NET resolve caminhos relativos pela pasta do processo, não pela do PowerShell: converte para completo.
    $Path = (Resolve-Path -LiteralPath $Path).ProviderPath
    foreach ($line in [System.IO.File]::ReadAllLines($Path, [System.Text.Encoding]::UTF8)) {
        $t = $line.Trim()
        if ($t -eq '' -or $t.StartsWith('#') -or -not $t.Contains('=')) { continue }
        if ($t.StartsWith('export ')) { $t = $t.Substring(7).TrimStart() }
        $idx = $t.IndexOf('=')
        $key = $t.Substring(0, $idx).Trim()
        $val = $t.Substring($idx + 1).Trim()
        if ($key -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') { continue }
        if ($val.Length -ge 2 -and (($val.StartsWith('"') -and $val.EndsWith('"')) -or ($val.StartsWith("'") -and $val.EndsWith("'")))) {
            $val = $val.Substring(1, $val.Length - 2)
        } elseif ($val -match '\s#') {
            $val = ($val -split '\s#', 2)[0].TrimEnd()
        }
        $values[$key] = $val
    }
    return $values
}
