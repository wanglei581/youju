# 有据 2.0 本机端（Windows 主机，WSL 方式）一键安装。不需要 Docker Desktop，Windows 11 22H2 也能用。
# 先决条件（要管理员，创始人亲自做一次）：开始按钮右键「终端(管理员)」，运行 wsl --install --no-distribution，重启。
# 用法：把安装包解压到数据盘（例如 E:\youju\local），在这个文件夹里打开终端（不用管理员），运行：
#   powershell -ExecutionPolicy Bypass -File .\setup-wsl.ps1
# 做这几件事（重复运行安全，已有的数据不动；中途不需要输入任何东西）：
#   1. 检查 WSL 2；
#   2. 没有 youju 发行版时：从中科大镜像站下载 Ubuntu 24.04（约 370 MB，按 Ubuntu 官方校验值核对），
#      导入到安装包上一级文件夹的 wsl 子文件夹（例如 E:\youju\wsl；数据库、原始文件都在这里面）；
#   3. 把这个文件夹拷进发行版的 /opt/youju/local，在里面运行 setup-wsl.sh：
#      装 Docker 引擎、构建镜像、生成 .env、导入起步数据（文件夹里有 youju-sync-export.jsonl.gz 时）、启动；
#   4. 加一个「登录后启动」的计划任务 youju-wsl，让 WSL 和里面的服务一直开着；
#   5. 检查 http://127.0.0.1:8080/health。
# 输出另存一份在上一级文件夹的 setup-wsl.log（出错时把它发给 Claude）。
param(
  [string]$Distro = "youju",
  [string]$DataRoot = ""
)
# 不用 Stop：Windows PowerShell 5.1 在输出被转走时（例如由别的程序启动）会把外部命令的标准错误
# 当成报错直接中止脚本。每个外部命令都看退出码
$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:WSL_UTF8 = "1"
Set-Location -Path $PSScriptRoot
if ($DataRoot -eq "") { $DataRoot = Split-Path -Parent $PSScriptRoot }
New-Item -ItemType Directory -Force -Path $DataRoot | Out-Null
$LogFile = Join-Path $DataRoot "setup-wsl.log"

# Ubuntu 24.04.5 的 WSL 包；校验值来自 https://releases.ubuntu.com/noble/SHA256SUMS
$ImageName = "ubuntu-24.04.5-wsl-amd64.wsl"
$ImageSha256 = "bb415d824822c4b878125729af451a5d18fb13d1cf5cbed9a7393ad64ac6039e"
$ImageUrls = @(
  "https://mirrors.ustc.edu.cn/ubuntu-releases/noble/$ImageName",
  "https://releases.ubuntu.com/noble/$ImageName"
)

function Say([string]$Text, [string]$Color = "Gray") {
  Write-Host $Text -ForegroundColor $Color
  Add-Content -Path $LogFile -Value $Text -Encoding UTF8
}
function Step([string]$Text) { Say ""; Say "== $Text" "Cyan" }
function Fail([string]$Text) { Say $Text "Red"; exit 1 }
# 运行外部命令：输出同时打到屏幕和日志，返回退出码
function Run([string]$Exe, [string[]]$Arguments) {
  & $Exe @Arguments 2>&1 | ForEach-Object {
    $line = "$_"
    Write-Host $line
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
  }
  return $LASTEXITCODE
}
function HashOk([string]$Path) {
  return (Test-Path $Path) -and ((Get-FileHash -Algorithm SHA256 $Path).Hash.ToLower() -eq $ImageSha256)
}

Say ("开始：" + (Get-Date -Format "yyyy-MM-dd HH:mm:ss"))

Step "1/5 检查 WSL"
$verText = (& wsl.exe --version 2>&1 | Out-String)
if ($LASTEXITCODE -ne 0 -or $verText -notmatch '(\d+)\.(\d+)\.(\d+)') {
  Fail "WSL 还没装好。请用管理员终端运行 wsl --install --no-distribution，重启电脑后再运行本脚本。"
}
$wslVersion = [version]("{0}.{1}.{2}" -f $Matches[1], $Matches[2], $Matches[3])
if ($wslVersion -lt [version]"2.1.5") {
  Fail "WSL 版本 $wslVersion 太旧（要 2.1.5 以上）。请用管理员终端运行 wsl --update，再运行本脚本。"
}
Say "WSL $wslVersion"

Step "2/5 Ubuntu 24.04（WSL 发行版 $Distro）"
$names = (& wsl.exe -l -q 2>$null | Out-String) -replace "`0", "" -split "`r?`n" |
  ForEach-Object { $_.Trim() } | Where-Object { $_ -ne "" }
if ($names -contains $Distro) {
  Say "已经有 $Distro，不重装。"
} else {
  $download = Join-Path $DataRoot "download"
  New-Item -ItemType Directory -Force -Path $download | Out-Null
  # 存成 .tar.gz：.wsl 包就是 gzip 压缩的 tar，wsl --import 认这个扩展名
  $file = Join-Path $download ($ImageName -replace '\.wsl$', '.tar.gz')
  $ok = HashOk $file
  foreach ($url in $ImageUrls) {
    if ($ok) { break }
    Say "下载 $url（约 370 MB）……"
    $code = Run "curl.exe" @("-sS", "--fail", "--location", "--retry", "5", "--retry-delay", "5", "-C", "-", "-o", $file, $url)
    $ok = ($code -eq 0) -and (HashOk $file)
    if (-not $ok) {
      Say "这个地址没下成，或者校验值对不上，换下一个" "Yellow"
      Remove-Item -Force -ErrorAction SilentlyContinue $file
    }
  }
  if (-not $ok) { Fail "Ubuntu 没下载成（或校验值对不上）。把 $LogFile 发给 Claude。" }
  $target = Join-Path $DataRoot "wsl"
  $code = Run "wsl.exe" @("--import", $Distro, $target, $file, "--version", "2")
  if ($code -ne 0) { Fail "导入 WSL 发行版失败。把 $LogFile 发给 Claude。" }
  Say "已导入到 $target（数据库、原始文件都在这个文件夹的 ext4.vhdx 里）"
}

Step "3/5 装 Docker 引擎、构建、导入起步数据、启动（第一次 15-30 分钟）"
$winDir = $PSScriptRoot -replace '\\', '/'
$src = (& wsl.exe -d $Distro -u root -e wslpath -a $winDir 2>$null | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or -not $src.StartsWith("/")) { Fail "找不到这个文件夹在 WSL 里的路径（$winDir）。" }
# 拷进发行版自己的文件系统：Windows 盘上的文件在 WSL 里是所有人可写，MySQL 会忽略这样的配置文件。
# 不拷 .env（发行版里的 .env 里是已经用着的数据库密码，不能被覆盖）
$copy = "umask 022 && mkdir -p /opt/youju/local && tar -C '$src' --exclude=./.env --exclude=./.imported -cf - . " +
  "| tar -C /opt/youju/local --no-same-owner --no-same-permissions -xf - && sed -i 's/\r`$//' /opt/youju/local/setup-wsl.sh"
$code = Run "wsl.exe" @("-d", $Distro, "-u", "root", "-e", "sh", "-c", $copy)
if ($code -ne 0) { Fail "把安装包拷进 WSL 失败。把 $LogFile 发给 Claude。" }
$code = 0
for ($round = 1; $round -le 2; $round++) {
  $code = Run "wsl.exe" @("-d", $Distro, "-u", "root", "-e", "bash", "/opt/youju/local/setup-wsl.sh")
  if ($code -ne 10) { break }
  Say "重启发行版，让 systemd 生效……"
  & wsl.exe --terminate $Distro | Out-Null
  Start-Sleep -Seconds 3
}
if ($code -ne 0) { Fail "安装没完成（退出码 $code）。把 $LogFile 发给 Claude。" }

Step "4/5 登录后自动启动（计划任务 youju-wsl）"
$taskName = "youju-wsl"
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
  Say "已经有计划任务 $taskName。"
} else {
  # 隐藏的 PowerShell 窗口里让发行版一直开着（sleep infinity）：WSL 里没有进程时会自己关掉，
  # Docker 和抓取任务也就跟着停了。WSL 被关掉时这个任务算失败，1 分钟后自动再开
  $arg = "-NoProfile -WindowStyle Hidden -Command `"& wsl.exe -d $Distro -u root -e sleep infinity`""
  $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arg
  $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
  $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
  try {
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
      -Description "有据本机端：登录后启动 WSL 发行版 $Distro（Docker 和里面的抓取任务随它运行）" `
      -ErrorAction Stop | Out-Null
  } catch {
    Fail ("加计划任务失败：" + $_.Exception.Message)
  }
  Say "已加计划任务 $taskName（登录 Windows 后自动启动）。"
}
Start-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue

Step "5/5 检查"
$health = ""
for ($i = 0; $i -lt 12; $i++) {
  $health = (& curl.exe -s --max-time 5 http://127.0.0.1:8080/health 2>$null | Out-String).Trim()
  if ($health -match '"ok"') { break }
  Start-Sleep -Seconds 5
}
if ($health -match '"ok"') {
  Say "本机端已启动。" "Green"
} else {
  Say "Windows 这边还连不上 http://127.0.0.1:8080/health（WSL 里已经启动了）。过一会儿再试；一直不通就把 $LogFile 发给 Claude。" "Yellow"
}
Say "核对台：这台电脑的浏览器开 http://127.0.0.1:8080/admin，口令用这条命令看："
Say "  wsl -d $Distro -u root -e grep YOUJU_ADMIN_TOKEN /opt/youju/local/.env"
Say ("数据（数据库、原始文件）在 " + (Join-Path $DataRoot "wsl") + " 里，不要删这个文件夹。")
Say "电脑要一直开着、登录着。建议把 设置 → 系统 → 电源 里「插入电源时睡眠」设为「从不」（这一项请你自己改）。"
Say ("完成：" + (Get-Date -Format "yyyy-MM-dd HH:mm:ss"))
