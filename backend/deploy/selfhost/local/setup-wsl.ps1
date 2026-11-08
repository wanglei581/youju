# 有据 2.0 本机端（Windows 主机，WSL 方式）一键安装。不需要 Docker Desktop，Windows 11 22H2 也能用。
# 先决条件（要管理员，创始人亲自做一次）：开始按钮右键「终端(管理员)」，运行 wsl --install --no-distribution，重启。
# 用法：安装包 zip 里是一个 local 文件夹，解压到数据盘的 youju 文件夹（得到 E:\youju\local），
# 在 local 里打开普通终端（不要用管理员），运行：
#   powershell -ExecutionPolicy Bypass -File .\setup-wsl.ps1
# 数据放在安装包所在盘的 \youju 下（例如 E:\youju\wsl\ext4.vhdx：数据库、原始文件都在里面），
# 和安装包解压在哪个子文件夹无关；要放别处用 -DataRoot。安装包文件夹里没有数据，升级时整个删掉换新的。
# 做这几件事（重复运行安全，已有的数据不动；中途不需要输入任何东西）：
#   1. 检查 WSL 2，确认不是在管理员窗口里运行；
#   2. 没有 youju 发行版时：从中科大镜像站下载 Ubuntu 24.04（约 370 MB，按 Ubuntu 官方校验值核对），
#      导入到 <数据盘>\youju\wsl；
#   3. 加（或更新）「登录后启动」的计划任务 youju-wsl 并马上启动，让 WSL 和里面的服务一直开着；
#   4. 把安装包拷进发行版的 /opt/youju/local，在里面运行 setup-wsl.sh：装 Docker 引擎、构建镜像、
#      生成 .env、导入起步数据（库是空的、还没配服务器、文件夹里有 youju-sync-export.jsonl.gz 时）、启动；
#   5. 检查 http://127.0.0.1:8080/health 和发行版在运行。
# 输出另存一份在 <数据盘>\youju\setup-wsl.log（出错时把它发给 Claude）。
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
if ($DataRoot -eq "") {
  try {
    $DataRoot = Join-Path ((Split-Path -Qualifier $PSScriptRoot -ErrorAction Stop) + "\") "youju"
  } catch {
    Write-Host "请把安装包放在这台电脑的数据盘上（例如 E:\youju\local），或者用 -DataRoot 指定数据位置。" -ForegroundColor Red
    exit 1
  }
}
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
function DistroNames() {
  return (& wsl.exe -l -q 2>$null | Out-String) -replace "`0", "" -split "`r?`n" |
    ForEach-Object { $_.Trim() } | Where-Object { $_ -ne "" }
}
function RunningNames() {
  return (& wsl.exe -l --running -q 2>$null | Out-String) -replace "`0", "" -split "`r?`n" |
    ForEach-Object { $_.Trim() } | Where-Object { $_ -ne "" }
}

Say ("开始：" + (Get-Date -Format "yyyy-MM-dd HH:mm:ss"))
Say ("数据位置：" + $DataRoot + "；安装包：" + $PSScriptRoot)

Step "1/5 检查 WSL 和运行身份"
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
Say ("当前用户：" + $identity.Name)
if ((New-Object Security.Principal.WindowsPrincipal($identity)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  Fail "请在普通（非管理员）终端里运行本脚本：管理员窗口里的 WSL 和平时的不是同一套，发行版和登录后启动的任务都要装在平时的身份下。"
}
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
if ((DistroNames) -contains $Distro) {
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
  New-Item -ItemType Directory -Force -Path $target | Out-Null
  $code = Run "wsl.exe" @("--import", $Distro, $target, $file, "--version", "2")
  if ($code -ne 0) { Fail "导入 WSL 发行版失败。把 $LogFile 发给 Claude。" }
  Say "已导入到 $target（数据库、原始文件都在这个文件夹的 ext4.vhdx 里）"
}

Step "3/5 登录后自动启动（计划任务 youju-wsl）"
# WSL 里没有 Windows 这边连着的进程时会自己关掉，Docker 和抓取任务也就跟着停了：登录后在隐藏窗口里
# 一直开着一个 wsl（sleep infinity），WSL 被关掉（更新、wsl --shutdown）时 30 秒后再开。
# - 用 conhost 开窗口：默认终端是 Windows Terminal 时 -WindowStyle Hidden 藏不住，窗口被关掉保活就停了；
# - 触发器写明当前用户：不写就是「任何用户登录时」，那要管理员；
# - 运行时限 0 = 不限：不设的话默认 72 小时，保活进程三天后会被停掉；
# - -Force：以后脚本改了，重跑时更新已有的任务。
# 在装 Docker 之前就启动：装完到任务启动之间发行版不会因为空闲被关掉
$taskName = "youju-wsl"
$loop = 'while ($true) {{ & wsl.exe -d {0} -u root -e sleep infinity; Start-Sleep -Seconds 30 }}' -f $Distro
$keepAlive = 'powershell.exe -NoProfile -WindowStyle Hidden -Command "' + $loop + '"'
try {
  $action = New-ScheduledTaskAction -Execute "conhost.exe" -Argument $keepAlive
  $trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity.Name
  $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
  Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Force `
    -Description "有据本机端：登录后让 WSL 发行版 $Distro 一直开着（Docker 和里面的抓取任务随它运行）" `
    -ErrorAction Stop | Out-Null
  Start-ScheduledTask -TaskName $taskName -ErrorAction Stop
  Say "计划任务 $taskName 已加好并启动（以后登录 Windows 后自动启动）。"
} catch {
  Say ("加计划任务没成功：" + $_.Exception.Message) "Yellow"
  # 这次先手动开一个保活进程（注销或重启后就没了）
  Start-Process -FilePath "conhost.exe" -ArgumentList $keepAlive
  Say "已先手动开了一个保活进程；电脑重启或注销后要重新运行本脚本。把 $LogFile 发给 Claude。" "Yellow"
}

Step "4/5 装 Docker 引擎、构建、导入起步数据、启动（第一次 15-30 分钟）"
$winDir = $PSScriptRoot -replace '\\', '/'
$src = (& wsl.exe -d $Distro -u root -e wslpath -a $winDir 2>$null | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or -not $src.StartsWith("/")) { Fail "找不到这个文件夹在 WSL 里的路径（$winDir）。" }
# 拷进发行版自己的文件系统：Windows 盘上的文件在 WSL 里是所有人可写，MySQL 会忽略这样的配置文件。
# 先删掉旧的源码：新版删掉或改名的文件（比如迁移）不能留在里面被打进镜像。
# 不拷 .env（发行版里的 .env 里是已经用着的数据库密码，不能被覆盖）
$copy = "set -euo pipefail; umask 022; rm -rf /opt/youju/local/source; mkdir -p /opt/youju/local; " +
  "tar -C '$src' --exclude=./.env --exclude=./.imported -cf - . " +
  "| tar -C /opt/youju/local --no-same-owner --no-same-permissions -xf -; sed -i 's/\r`$//' /opt/youju/local/setup-wsl.sh"
$code = Run "wsl.exe" @("-d", $Distro, "-u", "root", "-e", "bash", "-c", $copy)
if ($code -ne 0) { Fail "把安装包拷进 WSL 失败。把 $LogFile 发给 Claude。" }
$code = 0
for ($round = 1; $round -le 3; $round++) {
  $code = Run "wsl.exe" @("-d", $Distro, "-u", "root", "-e", "bash", "/opt/youju/local/setup-wsl.sh")
  if ($code -ne 10) { break }
  Say "重启发行版，让设置生效……"
  & wsl.exe --terminate $Distro | Out-Null
  Start-Sleep -Seconds 5
}
if ($code -ne 0) { Fail "安装没完成（退出码 $code）。把 $LogFile 发给 Claude。" }

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
  Say "Windows 这边连不上 http://127.0.0.1:8080/health。上面如果写了「已经配了服务器」，是在等手动起步（正常）；否则过一会儿再试，一直不通就把 $LogFile 发给 Claude。" "Yellow"
}
if ((RunningNames) -contains $Distro) { Say "发行版 $Distro 在运行。" } else { Say "发行版 $Distro 没在运行，把 $LogFile 发给 Claude。" "Yellow" }
Say "核对台：这台电脑的浏览器开 http://127.0.0.1:8080/admin，口令用这条命令看："
Say "  wsl -d $Distro -u root -e grep YOUJU_ADMIN_TOKEN /opt/youju/local/.env"
Say ("数据（数据库、原始文件）在 " + (Join-Path $DataRoot "wsl") + " 里，不要删这个文件夹。")
Say "电脑要一直开着、登录着。建议把 设置 → 系统 → 电源 里「插入电源时睡眠」设为「从不」（这一项请你自己改）。"
Say ("完成：" + (Get-Date -Format "yyyy-MM-dd HH:mm:ss"))
