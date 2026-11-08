# 有据 2.0 本机（Windows 主机）一键安装。用法（在这个文件所在的文件夹里，右键"在终端中打开"）：
#   powershell -ExecutionPolicy Bypass -File .\setup.ps1
# 做这几件事，每一步都会先说明：
#   1. 检查 Docker Desktop（WSL2）装好、正在运行；
#   2. 第一次运行时生成 .env（数据库密码、核对台口令随机生成；服务器地址和同步密钥要你粘贴）；
#   3. 载入镜像（同一文件夹里有 youju-selfhost.tar 就载入，否则从 .env 里的镜像仓库拉）；
#   4. 启动（MySQL + 抓取处理任务），等它健康；
#   5. 可选：把电脑设成插着电源时不睡眠（会先问你）。
# 不会删除任何已有数据；重复运行是安全的。
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Step($text) { Write-Host ""; Write-Host "== $text" -ForegroundColor Cyan }
function Fail($text) { Write-Host $text -ForegroundColor Red; Read-Host "按回车键退出"; exit 1 }
function RandomHex([int]$bytes) {
  $buffer = New-Object byte[] $bytes
  [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($buffer)
  return ($buffer | ForEach-Object { $_.ToString("x2") }) -join ""
}

Step "1/5 检查 Docker Desktop"
# PowerShell 5.1 里外部命令失败不会抛异常，要看退出码
$dockerOk = $false
try { docker version --format "{{.Server.Version}}" | Out-Null; $dockerOk = ($LASTEXITCODE -eq 0) } catch { $dockerOk = $false }
if (-not $dockerOk) {
  Fail "没有找到正在运行的 Docker Desktop。请先安装 Docker Desktop（选 WSL2），打开它、等左下角显示 Engine running，再运行本脚本。`n安装后在 Docker Desktop 设置里：General 勾选 Start Docker Desktop when you sign in；Resources → Advanced 把 Disk image location 改到空间大的盘（例如 D:\DockerData），内存给 8 GB。"
}
Write-Host "Docker 正在运行。"

Step "2/5 准备配置文件 .env"
if (Test-Path ".env") {
  Write-Host "已经有 .env，不改它。（要改就用记事本打开 .env）"
} else {
  $syncUrl = (Read-Host "粘贴服务器地址（例如 https://api.example.com；服务器还没买就直接回车，先只在本机跑）").Trim().TrimEnd("/")
  $syncSecret = ""
  if ($syncUrl -ne "") {
    $syncSecret = (Read-Host "粘贴同步密钥（和服务器 .env 里的 YOUJU_SYNC_SECRET 一样）").Trim()
    if ($syncSecret.Length -lt 32) { Fail "同步密钥至少 32 位，请向 Claude 要服务器上生成的那一个。" }
  }
  $dbPassword = RandomHex 24
  $text = Get-Content "env.example" -Raw -Encoding UTF8
  $text = $text -replace "MYSQL_ROOT_PASSWORD=换成随机串", ("MYSQL_ROOT_PASSWORD=" + (RandomHex 24))
  $text = $text -replace "MYSQL_PASSWORD=换成随机串", ("MYSQL_PASSWORD=" + $dbPassword)
  $text = $text -replace "换成上面的MYSQL_PASSWORD", $dbPassword
  $text = $text -replace "YOUJU_ADMIN_TOKEN=换成随机串", ("YOUJU_ADMIN_TOKEN=" + (RandomHex 24))
  # 没填服务器：这两项留空，本机先单独跑（推送、看服务器健康、拉异地备份都安静跳过）
  $text = $text -replace "YOUJU_SYNC_URL=https://api.example.com", ("YOUJU_SYNC_URL=" + $syncUrl)
  $text = $text -replace "YOUJU_SYNC_SECRET=换成和服务器一样的随机串", ("YOUJU_SYNC_SECRET=" + $syncSecret)
  [System.IO.File]::WriteAllText((Join-Path $PSScriptRoot ".env"), $text, (New-Object System.Text.UTF8Encoding $false))
  Write-Host "已生成 .env。核对台口令在 .env 的 YOUJU_ADMIN_TOKEN 一行。"
}

Step "3/5 准备程序（镜像）"
# 三种来源，按顺序：同文件夹里的镜像文件 → 同文件夹里的程序源码（在这台电脑上构建，
# 用国内镜像源装依赖） → .env 里写的镜像仓库
$image = (Select-String -Path ".env" -Pattern "^YOUJU_IMAGE=([^\r\n]+)").Matches[0].Groups[1].Value.Trim()
if (Test-Path "youju-selfhost.tar") {
  docker load -i youju-selfhost.tar
  if ($LASTEXITCODE -ne 0) { Fail "载入镜像文件失败，把上面的报错截图发给 Claude。" }
} elseif (Test-Path "source\Dockerfile") {
  Write-Host "用 source 文件夹里的程序源码在这台电脑上构建（第一次要 10-20 分钟，下载基础镜像比较慢）……"
  docker build -t youju-base:local -f source\Dockerfile source
  if ($LASTEXITCODE -ne 0) { Fail "构建基础镜像失败，把上面的报错截图发给 Claude。" }
  docker build -t $image --build-arg BASE=youju-base:local -f source\backend\deploy\selfhost\Dockerfile source\backend\deploy\selfhost
  if ($LASTEXITCODE -ne 0) { Fail "构建镜像失败，把上面的报错截图发给 Claude。" }
} else {
  docker pull $image
  if ($LASTEXITCODE -ne 0) { Fail "拉不下镜像 $image。把 Claude 给的安装包（含 source 文件夹）解压到这个文件夹再运行。" }
}

Step "4/5 启动"
docker compose up -d
if ($LASTEXITCODE -ne 0) { Fail "启动失败，把上面的报错截图发给 Claude。" }
Write-Host "等服务准备好（第一次要建表，约 1-3 分钟）……"
$ok = $false
for ($i = 0; $i -lt 60; $i++) {
  Start-Sleep -Seconds 5
  try {
    $r = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:8080/health" -TimeoutSec 5
    if ($r.StatusCode -eq 200) { $ok = $true; break }
  } catch { }
}
if (-not $ok) { Fail "5 分钟还没准备好。运行 docker compose logs worker --tail 100，把输出截图发给 Claude。" }
Write-Host "本机端已启动。核对台：http://127.0.0.1:8080/admin" -ForegroundColor Green

Step "5/5 电源设置（可选）"
$answer = Read-Host "把这台电脑设成插着电源时不睡眠、不关硬盘吗？抓取要一直开着。输入 y 同意，直接回车跳过"
if ($answer -eq "y") {
  powercfg /change standby-timeout-ac 0
  $ok1 = ($LASTEXITCODE -eq 0)
  powercfg /change hibernate-timeout-ac 0
  $ok2 = ($LASTEXITCODE -eq 0)
  powercfg /change disk-timeout-ac 0
  $ok3 = ($LASTEXITCODE -eq 0)
  if ($ok1 -and $ok2 -and $ok3) {
    Write-Host "已设好（只改了插电源时的设置，屏幕关闭不受影响）。"
  } else {
    Write-Host "没设成功（可能要管理员权限）。请手动设：设置 → 系统 → 电源 → 屏幕和睡眠 → 插入电源时，睡眠设为'从不'。" -ForegroundColor Yellow
  }
}

Write-Host ""
$hasServer = (Select-String -Path ".env" -Pattern "^YOUJU_SYNC_URL=\S").Matches.Count -gt 0
Write-Host "装好了。下一步导入公开数据（导出文件 youju-sync-export.jsonl.gz 放在这个文件夹里）：" -ForegroundColor Green
Write-Host "导入时先停定时任务，用一次性容器做，做完再启动："
Write-Host "  docker compose stop worker"
Write-Host "  docker compose run --rm -v ${PWD}:/move worker youju sync-import --file /move/youju-sync-export.jsonl.gz"
Write-Host "  docker compose run --rm worker youju sync-setup"
if ($hasServer) {
  Write-Host "  docker compose run --rm worker youju sync-push --mark-synced"
} else {
  Write-Host "  （还没配服务器：先只在本机跑，以后配好服务器按 Claude 的说明重新起步）"
}
Write-Host "  docker compose start worker"
Read-Host "按回车键结束"
