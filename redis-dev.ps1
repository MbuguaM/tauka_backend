<#
    Local Redis for tauka-python development.

        .\redis-dev.ps1 start     # start it (idempotent)
        .\redis-dev.ps1 stop
        .\redis-dev.ps1 status
        .\redis-dev.ps1 keys      # show rate-limit / token counters
        .\redis-dev.ps1 flush     # clear all counters

    Portable Redis 5.0.14.1 for Windows, installed under
    %LOCALAPPDATA%\redis-portable. No admin rights and no Windows service, so
    it does NOT survive a reboot — run `start` again after logging in, or see
    "Autostart" below.

    Why local rather than a shared instance: this backend's Redis keys are
    namespaced by user id (`rate:{provider}:{user_id}:…`,
    `tokens:{user_id}:month:…`) and local dev already points at the PRODUCTION
    Supabase project, so user ids are identical. Sharing one Redis would make
    local traffic consume real users' rate limits and monthly token budgets.

    Autostart at logon (no admin):
        $s = (New-Object -ComObject WScript.Shell).CreateShortcut(
                "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\redis.lnk")
        $s.TargetPath  = "$env:LOCALAPPDATA\redis-portable\redis-server.exe"
        $s.Arguments   = "--port 6379 --appendonly no"
        $s.WindowStyle = 7
        $s.Save()
#>
param([Parameter(Position = 0)][ValidateSet('start','stop','status','keys','flush')][string]$Action = 'status')

$dir = Join-Path $env:LOCALAPPDATA 'redis-portable'
$exe = Join-Path $dir 'redis-server.exe'
$cli = Join-Path $dir 'redis-cli.exe'
$port = 6379

if (-not (Test-Path $exe)) {
    Write-Error "Redis not found at $exe. Re-download from https://github.com/tporadowski/redis/releases"
    exit 1
}

function Test-Redis { ((& $cli -p $port ping 2>&1) -join '') -match 'PONG' }

switch ($Action) {
    'start' {
        if (Test-Redis) { Write-Output "already running on $port"; break }
        Start-Process -FilePath $exe -ArgumentList "--port $port --appendonly no" `
                      -WorkingDirectory $dir -WindowStyle Hidden
        Start-Sleep -Seconds 4
        if (Test-Redis) { Write-Output "started on $port" }
        else            { Write-Error "failed to start - check $dir" }
    }
    'stop' {
        $p = Get-Process redis-server -ErrorAction SilentlyContinue
        if ($p) { $p | Stop-Process -Force; Write-Output "stopped (PID $($p.Id -join ', '))" }
        else    { Write-Output "not running" }
    }
    'status' {
        if (Test-Redis) {
            $ver = (& $cli -p $port info server | Select-String 'redis_version') -join ''
            Write-Output "running on $port  |  $($ver.Trim())  |  $(& $cli -p $port dbsize) key(s)"
        } else { Write-Output "not running (rate limiting will be skipped - fails open)" }
    }
    'keys' {
        if (-not (Test-Redis)) { Write-Output 'not running'; break }
        $keys = @(& $cli -p $port keys '*' | Where-Object { $_ -and $_.Trim() })
        if (-not $keys) { Write-Output 'no counters set'; break }
        foreach ($k in $keys) {
            $k = $k.Trim()
            Write-Output ("{0}`n   value={1}  ttl={2}s" -f $k, (& $cli -p $port get $k), (& $cli -p $port ttl $k))
        }
    }
    'flush' {
        if (-not (Test-Redis)) { Write-Output 'not running'; break }
        & $cli -p $port flushall | Out-Null
        Write-Output 'all counters cleared'
    }
}
