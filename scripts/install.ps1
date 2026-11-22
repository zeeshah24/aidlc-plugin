# Install the aidlc CLI from this plugin checkout (Windows). Usage: scripts\install.ps1 [-Dev]
param([switch]$Dev)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
if ($Dev) {
    py -3 -m pip install -e "$Root\orchestrator[dev]"
} elseif (Get-Command pipx -ErrorAction SilentlyContinue) {
    pipx install --force "$Root\orchestrator"
} else {
    py -3 -m pip install --user "$Root\orchestrator"
}
aidlc --version
