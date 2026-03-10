param(
  [string]$Config = "reference_tool.config.json",
  [string]$Python = "python"
)

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

if (-Not (Test-Path $Config)) {
  if (Test-Path "reference_tool.config.example.json") {
    Copy-Item "reference_tool.config.example.json" $Config
    Write-Host "已生成 $Config，请先编辑 input/output 等参数后再运行。"
    exit 0
  }
}

& $Python "reference_tool.py" --config $Config @args
