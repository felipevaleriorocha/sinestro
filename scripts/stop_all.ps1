$ErrorActionPreference = 'SilentlyContinue'

$ports = @(8787, 4096, 34872)
$currentPid = $PID

foreach ($p in $ports) {
    try {
        $lines = netstat -ano | Select-String (":{0}\s" -f $p)
        foreach ($line in $lines) {
            $parts = ($line.Line.Trim() -split '\s+')
            $pidStr = $parts[-1]
            if ($pidStr -match '^\d+$') {
                $procId = [int]$pidStr
                if ($procId -gt 0 -and $procId -ne $currentPid) {
                    Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
                    Write-Host ("Encerrado processo PID {0} na porta {1}" -f $procId, $p) -ForegroundColor Yellow
                }
            }
        }
    } catch {}
}

Write-Host "Servicos encerrados com sucesso." -ForegroundColor Green
