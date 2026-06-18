# =============================================================================
# .env.ps1 — 共享模块: 从 .env 文件读取配置
#
# 用法 (dot-source):
#   . "$PSScriptRoot\.env.ps1"
#   $val = Get-EnvValue 'KEY' 'default'
#
# 优先级: CLI 参数 > .env > 硬编码默认值
# =============================================================================

$script:__EnvPath = Join-Path $PSScriptRoot '.env'

function Get-EnvValue([string]$Key, [string]$Default) {
    if (-not (Test-Path $script:__EnvPath)) { return $Default }
    $m = Select-String -Path $script:__EnvPath -Pattern "^\s*$Key\s*=\s*(.*)" | Select-Object -First 1
    if ($m) { $v = $m.Matches.Groups[1].Value.Trim(); if ($v) { return $v } }
    return $Default
}

function Get-EnvFlag([string]$Key, [bool]$Default = $false) {
    $raw = Get-EnvValue $Key ''
    if (-not $raw) { return $Default }

    switch ($raw.Trim().ToLowerInvariant()) {
        '1'     { return $true }
        'true'  { return $true }
        'yes'   { return $true }
        'on'    { return $true }
        '0'     { return $false }
        'false' { return $false }
        'no'    { return $false }
        'off'   { return $false }
        default { return $Default }
    }
}

# ── 批量覆盖未由 CLI 显式传参的变量 ──────────────────────────────────────────
# 用法:
#   Invoke-EnvDefaults -ParamRef ([ref]$Model) -EnvKey 'WHISPER_MODEL' -Default 'large-v3-turbo'
#   或对字符串参数:
#   $Model = Merge-EnvDefault 'WHISPER_MODEL' $Model 'large-v3-turbo'
function Merge-EnvDefault {
    param([string]$EnvKey, $CurrentValue, [string]$Default)
    # 当前值非空 → 用户通过 CLI 传入或之前已设置, 保持不变
    if ($CurrentValue) { return $CurrentValue }
    # 尝试从 .env 读取
    $envVal = Get-EnvValue $EnvKey ''
    if ($envVal) { return $envVal }
    # 回退到硬编码默认
    return $Default
}
