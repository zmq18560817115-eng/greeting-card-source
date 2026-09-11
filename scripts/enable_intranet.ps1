param(
    [Parameter(Mandatory=$true)][string]$Address,
    [ValidateRange(1,65535)][int]$Port = 8848,
    [string]$Python = 'python',
    [switch]$CheckOnly
)
$ErrorActionPreference = 'Stop'
$workspace = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$actual = @(Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -eq $Address })
if ($actual.Count -eq 0) { throw 'This address does not belong to this computer. Run this script on the actual backend host.' }
Write-Host ('Backend: http://{0}:{1}/' -f $Address,$Port)
Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue | Select-Object LocalAddress,LocalPort,OwningProcess | Format-Table
if ($CheckOnly) {
    Get-NetFirewallRule -Name ('GreetingCard-LAN-' + $Port) -ErrorAction SilentlyContinue | Select-Object DisplayName,Enabled,Direction,Action | Format-Table
    Write-Host 'If the service listens on 127.0.0.1, configure LAN access. If TCP works here but fails on a colleague computer, ask IT to check VLAN/VPN routing and access rules.'
    exit 0
}
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { throw 'Run PowerShell as Administrator to add the scoped inbound firewall rule. No firewall settings have been changed.' }
Push-Location -LiteralPath $workspace
try {
    & $Python (Join-Path $PSScriptRoot 'configure_intranet.py') --address $Address --port $Port
    if ($LASTEXITCODE -ne 0) { throw 'Configuration failed; firewall unchanged.' }
} finally { Pop-Location }
$ruleName = 'GreetingCard-LAN-' + $Port
if (Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue) {
    Set-NetFirewallRule -Name $ruleName -Enabled True -Direction Inbound -Action Allow -Profile Any -RemoteAddress LocalSubnet -LocalAddress $Address
    Get-NetFirewallRule -Name $ruleName | Get-NetFirewallPortFilter | Set-NetFirewallPortFilter -Protocol TCP -LocalPort $Port
} else {
    New-NetFirewallRule -Name $ruleName -DisplayName 'Employee greeting admin - company LAN' -Direction Inbound -Action Allow -Enabled True -Profile Any -Protocol TCP -LocalPort $Port -LocalAddress $Address -RemoteAddress LocalSubnet | Out-Null
}
Write-Host 'Configuration complete. Restart the existing backend service. The rule allows only the selected port from the local subnet; other subnets/VPN require company IT routing and scoped rules.'
