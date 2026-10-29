# LoRA Captioner - 停止服务
# 逻辑都在这里，stop_webui.bat 只是双击入口。
#
# PORT 必须和 run_webui.bat 里的一致；改端口时两处都要改。

$ErrorActionPreference = 'SilentlyContinue'

# Windows PowerShell 5.1 默认按系统编码处理控制台输出和 JSON 响应，
# 中文会变乱码，这里统一切到 UTF-8。
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch { }

$Port = 7860
$Base = "http://127.0.0.1:$Port"

# 取 JSON 时手动按 UTF-8 解码：Invoke-RestMethod 在 PS 5.1 上会把
# 不带 charset 的 application/json 当成 Latin-1，中文全乱。
function Get-JsonUtf8($uri) {
    try {
        $resp = Invoke-WebRequest -Uri $uri -TimeoutSec 4 -UseBasicParsing
        $bytes = $resp.RawContentStream.ToArray()
        $text = [System.Text.Encoding]::UTF8.GetString($bytes)
        return $text | ConvertFrom-Json
    } catch {
        return $null
    }
}

Write-Host ""
Write-Host "  LoRA Captioner - 停止服务" -ForegroundColor Cyan
Write-Host "  ====================================" -ForegroundColor DarkGray
Write-Host ""

# ---------- 1. 有没有在跑 ----------
$alive = $false
try {
    Invoke-WebRequest -Uri "$Base/" -TimeoutSec 3 -UseBasicParsing | Out-Null
    $alive = $true
} catch { }

if (-not $alive) {
    # 端口没响应，但可能有僵尸进程还占着
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($conn) {
        Write-Host "  [!] 端口 $Port 被占用但服务无响应，清理残留进程" -ForegroundColor Yellow
    } else {
        Write-Host "  [!] 端口 $Port 上没有运行中的服务" -ForegroundColor Yellow
        Write-Host ""
        exit 0
    }
}

# ---------- 2. 看有没有任务在跑 ----------
$busy = $false
if ($alive) {
    $m = Get-JsonUtf8 "$Base/api/executor/monitor"
    if ($m -and $m.current) {
        Write-Host "  正在运行  $($m.current.label)" -ForegroundColor Yellow
        $prog = $m.current.progress
        if ($prog -and $prog.total) {
            Write-Host "            $($prog.done)/$($prog.total)" -ForegroundColor DarkGray
        }
        $busy = $true
    }
    if ($m -and $m.queue -and $m.queue.Count -gt 0) {
        Write-Host "  排队中    $($m.queue.Count) 个任务" -ForegroundColor Yellow
        $busy = $true
    }
}

$forceQs = ""
if ($busy) {
    Write-Host ""
    Write-Host "  任务没跑完，强制停止会中断当前进度（已完成的图不受影响）。" -ForegroundColor Yellow
    $ans = Read-Host "  确定要停? [y/N]"
    if ($ans -notmatch '^[Yy]') {
        Write-Host ""
        Write-Host "  已取消" -ForegroundColor DarkGray
        Write-Host ""
        exit 0
    }
    $forceQs = "?force=true"
}

# ---------- 3. 请求关闭 ----------
if ($alive) {
    Write-Host ""
    Write-Host "  正在停止..." -ForegroundColor Cyan
    try {
        Invoke-RestMethod -Uri "$Base/api/shutdown$forceQs" -Method Post -TimeoutSec 6 | Out-Null
    } catch {
        # 服务可能在响应前就退出了，属于正常情况
    }
}

# ---------- 4. 等端口释放 ----------
$freed = $false
for ($i = 0; $i -lt 25; $i++) {
    Start-Sleep -Milliseconds 300
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if (-not $conn) { $freed = $true; break }
}

# ---------- 5. 兜底：直接结束进程 ----------
if (-not $freed) {
    Write-Host "  接口关闭未生效，改为结束进程" -ForegroundColor Yellow
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($conn) {
        $procIds = $conn | Select-Object -ExpandProperty OwningProcess -Unique
        foreach ($procId in $procIds) {
            $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
            if ($proc) {
                Write-Host "    结束 $($proc.ProcessName) (PID $procId)" -ForegroundColor DarkGray
                Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
            }
        }
        Start-Sleep -Seconds 1
        $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
        if (-not $conn) { $freed = $true }
    } else {
        $freed = $true
    }
}

Write-Host ""
if ($freed) {
    Write-Host "  [OK] 服务已停止" -ForegroundColor Green
} else {
    Write-Host "  [X] 停止失败，端口 $Port 仍被占用" -ForegroundColor Red
    Write-Host "      可以手动结束进程：任务管理器里找 python.exe" -ForegroundColor DarkGray
}
Write-Host ""
