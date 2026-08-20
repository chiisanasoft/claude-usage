# Install the Claude Usage VS Code extension on Windows.
# Usage:  .\scripts\install.ps1 [path\to\file.vsix]
# Picks the first .vsix in the extension root, or builds one if none exists.

[CmdletBinding()]
param(
    [string]$Vsix = ""
)

$ErrorActionPreference = "Stop"
$ExtRoot = Split-Path -Parent $PSScriptRoot

function Find-CodeCli {
    foreach ($name in @("code.cmd", "code-insiders.cmd", "code", "code-insiders")) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }
    }
    foreach ($candidate in @(
        "$env:LOCALAPPDATA\Programs\Microsoft VS Code\bin\code.cmd",
        "$env:LOCALAPPDATA\Programs\Microsoft VS Code Insiders\bin\code-insiders.cmd"
    )) {
        if (Test-Path $candidate) { return $candidate }
    }
    throw "Could not find VS Code CLI. Install VS Code or add 'code' to PATH."
}

if (-not $Vsix) {
    Set-Location -LiteralPath $ExtRoot
    if (-not (Test-Path "node_modules")) { npm install }
    Get-ChildItem -Path $ExtRoot -Filter "*.vsix" | Remove-Item -Force -ErrorAction SilentlyContinue
    npm run package
    $Vsix = (Get-ChildItem -Path $ExtRoot -Filter "*.vsix" | Sort-Object LastWriteTime -Descending | Select-Object -First 1).FullName
}

if (-not $Vsix -or -not (Test-Path $Vsix)) {
    throw "No .vsix file found and packaging failed."
}

$CodeCli = Find-CodeCli
Write-Output "Installing $Vsix via $CodeCli ..."
& $CodeCli --install-extension $Vsix --force
Write-Output "Done. Reload VS Code (Ctrl+Shift+P -> Reload Window) to see the Claude Usage sidebar."
