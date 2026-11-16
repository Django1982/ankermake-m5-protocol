import base64
import ctypes
import hashlib
import ipaddress
import json
import os
import socket


def available():
    return os.name == "nt"


def printer_address(value):
    try:
        address = ipaddress.IPv4Address(str(value or "").strip())
    except ValueError as exc:
        raise ValueError("Give this printer a LAN IP in Setup → Printer first.") from exc
    if address.is_unspecified or address.is_loopback or address.is_multicast or address.is_reserved:
        raise ValueError("Give this printer a LAN IP in Setup → Printer first.")
    return str(address)


def host_name():
    return (os.getenv("COMPUTERNAME") or socket.gethostname()).split(".", 1)[0]


def process_image():
    # Use the actual Windows image so virtualenv launchers and bundled builds match the firewall.
    buffer = ctypes.create_unicode_buffer(32768)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    length = kernel.GetModuleFileNameW(None, buffer, len(buffer))
    if not 0 < length < len(buffer):
        raise OSError("Could not identify the Windows executable running ankerctl.")
    return buffer.value


def build_helper(printer, address):
    printer_id = printer.sn or printer.p2p_duid or address
    identity = hashlib.sha256(printer_id.encode("utf-8")).hexdigest()[:20]
    data = {
        "computer": host_name(),
        "address": address,
        "program": process_image(),
        "rule_name": f"Ankerctl-LanUdp-{identity}",
    }
    encoded = base64.b64encode(json.dumps(data).encode("utf-8")).decode("ascii")
    return (_BATCH + _POWERSHELL.replace("__ANKERCTL_RULE_DATA__", encoded)).replace("\n", "\r\n")


_BATCH = r'''@echo off
setlocal DisableDelayedExpansion
set "ANKERCTL_FIREWALL_HELPER=%~f0"
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -Command "$ErrorActionPreference='Stop'; $parts=(Get-Content -LiteralPath $env:ANKERCTL_FIREWALL_HELPER -Raw) -split '(?m)^# ANKERCTL_HELPER\r?$', 2; & ([scriptblock]::Create($parts[1]))"
set "ANKERCTL_FIREWALL_EXIT=%ERRORLEVEL%"
echo(
pause
exit /b %ANKERCTL_FIREWALL_EXIT%
# ANKERCTL_HELPER
'''

_POWERSHELL = r'''$ErrorActionPreference = 'Stop'
try {
    $config = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__ANKERCTL_RULE_DATA__')) | ConvertFrom-Json
    if ($env:COMPUTERNAME -ine $config.computer) {
        throw "Run this helper on $($config.computer), the PC running ankerctl."
    }
    if (-not (Test-Path -LiteralPath $config.program -PathType Leaf)) {
        throw 'The ankerctl executable has moved. Download a fresh helper from ankerctl.'
    }
    $route = Find-NetRoute -RemoteIPAddress $config.address |
        Where-Object { $_.CimClass.CimClassName -eq 'MSFT_NetIPAddress' } | Select-Object -First 1
    if (-not $route.InterfaceAlias -or $route.InterfaceAlias -eq 'Loopback Pseudo-Interface 1') {
        throw 'Windows could not find the network adapter used to reach the printer.'
    }
    $config | Add-Member -NotePropertyName interface -NotePropertyValue $route.InterfaceAlias
    $category = Get-NetConnectionProfile -InterfaceIndex $route.InterfaceIndex -ErrorAction SilentlyContinue |
        Select-Object -First 1 -ExpandProperty NetworkCategory
    $profile = if ($category -eq 'DomainAuthenticated') { 'Domain' } else { [string]$category }

    Write-Host "Ankerctl printer firewall setup on $($config.computer)"
    Write-Host "Incoming UDP from $($config.address) on $($config.interface)"
    Write-Host "Program $($config.program)"

    $matching = Get-NetFirewallApplicationFilter -PolicyStore ActiveStore |
        Where-Object { [Environment]::ExpandEnvironmentVariables([string]$_.Program) -ieq $config.program } |
        Get-NetFirewallRule |
        Where-Object { $_.Enabled -eq 'True' -and $_.Direction -eq 'Inbound' -and $_.Action -eq 'Allow' }
    foreach ($rule in $matching) {
        $ruleProfiles = [string]$rule.Profile -split ',\s*'
        if ('Any' -notin $ruleProfiles -and $profile -notin $ruleProfiles) { continue }
        $addresses = $rule | Get-NetFirewallAddressFilter
        $ports = $rule | Get-NetFirewallPortFilter
        $interfaces = $rule | Get-NetFirewallInterfaceFilter
        if (($config.address -in $addresses.RemoteAddress -or "$($config.address)/32" -in $addresses.RemoteAddress) -and
            'Any' -in $addresses.LocalAddress -and $ports.Protocol -in @('UDP', '17') -and
            'Any' -in $ports.LocalPort -and 'Any' -in $ports.RemotePort -and
            $config.interface -in $interfaces.InterfaceAlias) {
            Write-Host 'A matching firewall rule is already active. Return to ankerctl and select Test connection.'
            exit 0
        }
    }

    $ruleData = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(($config | ConvertTo-Json -Compress)))
    $applyRule = @'
$ErrorActionPreference = 'Stop'
try {
    $data = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__RULE_DATA__')) | ConvertFrom-Json
    $options = @{
        Name = $data.rule_name; Enabled = 'True'; Direction = 'Inbound'; Action = 'Allow';
        Profile = 'Domain','Private','Public'; Program = $data.program; Protocol = 'UDP';
        LocalAddress = 'Any'; RemoteAddress = $data.address; LocalPort = 'Any'; RemotePort = 'Any';
        InterfaceAlias = [WildcardPattern]::Escape($data.interface); EdgeTraversalPolicy = 'Block'
    }
    if (Get-NetFirewallRule -Name $data.rule_name -ErrorAction SilentlyContinue) {
        Set-NetFirewallRule @options | Out-Null
    } else {
        New-NetFirewallRule @options -DisplayName "Ankerctl printer UDP ($($data.rule_name.Substring(16)))" | Out-Null
    }
    if (-not (Get-NetFirewallRule -Name $data.rule_name -PolicyStore ActiveStore -Enabled True)) { exit 2 }
    exit 0
} catch { exit 1 }
'@
    $encodedCommand = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($applyRule.Replace('__RULE_DATA__', $ruleData)))
    Write-Host 'Approve the Windows administrator prompt to apply this rule.'
    $result = Start-Process -FilePath "$PSHOME\powershell.exe" -Verb RunAs -WindowStyle Hidden -Wait -PassThru `
        -ArgumentList '-NoProfile', '-NonInteractive', '-EncodedCommand', $encodedCommand
    if ($result.ExitCode -ne 0) {
        throw 'Windows could not activate the rule. Check whether firewall rules are managed by your administrator.'
    }
    Write-Host 'Firewall rule is active. Return to ankerctl and select Test connection.'
    exit 0
} catch {
    Write-Host "Firewall setup did not finish. $($_.Exception.Message)"
    exit 1
}
'''
