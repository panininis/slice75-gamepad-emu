$own = (Get-NetTCPConnection -LocalPort 8321 -State Listen -ErrorAction SilentlyContinue).OwningProcess
if (-not $own) { Write-Host "no server on 8321"; exit 1 }
$b = (Get-Process -Id $own).TotalProcessorTime.TotalSeconds
Start-Sleep -Seconds 90
$a = (Get-Process -Id $own).TotalProcessorTime.TotalSeconds
$d = [math]::Round($a - $b, 2)
$pct = [math]::Round($d / 90 * 100, 1)
$msg = ("NEW-CODE pid {0}: {1} s CPU over 90 s = {2} percent of one core") -f $own, $d, $pct
Write-Host $msg
