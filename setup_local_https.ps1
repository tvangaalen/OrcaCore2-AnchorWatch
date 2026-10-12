param(
    # Folder holding the "Mastervolt Local CA" (ca-cert.pem + ca-key.pem) that the iPhone already trusts.
    [string]$CaDir = $(if ($env:ANCHOR_CA_DIR) { $env:ANCHOR_CA_DIR } else { "C:\Temp\mastervoltproject\certs" })
)
# Issues this app's HTTPS certificate from the existing Mastervolt Local CA, so phones that already trust that CA
# (installed for MasterVolt YMS) open the anchor watch without any change. It never creates a new CA, never writes
# into the CA folder and never touches the Windows certificate store (the CA is already trusted there).
$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$certDir = Join-Path $projectDir "certs"
New-Item -ItemType Directory -Path $certDir -Force | Out-Null

$opensslCommand = Get-Command openssl.exe -ErrorAction SilentlyContinue
if ($opensslCommand) { $opensslPath = $opensslCommand.Source }
elseif (Test-Path -LiteralPath "C:\Program Files\FireDaemon OpenSSL 3\bin\openssl.exe") { $opensslPath = "C:\Program Files\FireDaemon OpenSSL 3\bin\openssl.exe" }
else { throw "OpenSSL was not found. Install OpenSSL locally before starting the server." }

$caKey = Join-Path $CaDir "ca-key.pem"
$caPem = Join-Path $CaDir "ca-cert.pem"
if (-not (Test-Path -LiteralPath $caKey) -or -not (Test-Path -LiteralPath $caPem)) {
    throw "Mastervolt Local CA not found in $CaDir (need ca-cert.pem and ca-key.pem). Run MasterVolt YMS's setup_local_https.ps1 first, or set ANCHOR_CA_DIR. A new CA would have to be installed on the iPhone again."
}
$caSubject = & $opensslPath x509 -in $caPem -noout -subject
if ($caSubject -notmatch "Mastervolt Local CA") { throw "Unexpected CA in ${CaDir}: $caSubject" }

$serverKey = Join-Path $certDir "server-key.pem"
$serverCsr = Join-Path $certDir "server.csr"
$serverPem = Join-Path $certDir "server-cert.pem"
$serverConfig = Join-Path $certDir "server-openssl.cnf"

$localAddresses = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -ne "127.0.0.1" -and $_.IPAddress -notlike "169.254.*" } |
    Select-Object -ExpandProperty IPAddress -Unique

function Test-PrivateIPv4([string]$address) {
    if ($address -match '^10\.' -or $address -match '^192\.168\.') { return $true }
    if ($address -match '^172\.(\d+)\.') {
        $secondOctet = [int]$Matches[1]
        return $secondOctet -ge 16 -and $secondOctet -le 31
    }
    return $false
}

# Bind address: the private address of the interface with the default route (same choice as MasterVolt YMS)
$routedAddresses = Get-NetRoute -AddressFamily IPv4 -DestinationPrefix "0.0.0.0/0" -ErrorAction SilentlyContinue |
    Sort-Object RouteMetric |
    ForEach-Object {
        Get-NetIPAddress -AddressFamily IPv4 -InterfaceIndex $_.InterfaceIndex -ErrorAction SilentlyContinue |
            Where-Object { $_.AddressState -eq "Preferred" } |
            Select-Object -ExpandProperty IPAddress
    }
$privateAddress = $routedAddresses | Where-Object { Test-PrivateIPv4 $_ } | Select-Object -First 1
if (-not $privateAddress) { $privateAddress = $localAddresses | Where-Object { Test-PrivateIPv4 $_ } | Select-Object -First 1 }
if (-not $privateAddress) { $privateAddress = "127.0.0.1" }
Set-Content -LiteralPath (Join-Path $certDir "bind-address.txt") -Value $privateAddress -Encoding ascii

$altNames = @("DNS.1 = localhost", "DNS.2 = $env:COMPUTERNAME", "IP.1 = 127.0.0.1")
$ipIndex = 2
foreach ($address in $localAddresses) { $altNames += "IP.$ipIndex = $address"; $ipIndex++ }

$configText = @"
[req]
prompt = no
distinguished_name = dn
req_extensions = req_ext

[dn]
CN = $env:COMPUTERNAME Anchor Watch

[req_ext]
subjectAltName = @alt_names
basicConstraints = critical,CA:FALSE
keyUsage = critical,digitalSignature,keyEncipherment
extendedKeyUsage = serverAuth

[alt_names]
$($altNames -join "`r`n")
"@
Set-Content -LiteralPath $serverConfig -Value $configText -Encoding ascii

& $opensslPath req -new -newkey rsa:2048 -nodes -sha256 -keyout $serverKey -out $serverCsr -config $serverConfig
if ($LASTEXITCODE -ne 0) { throw "Could not create the HTTPS server request" }

# A random serial instead of the CA's serial file, so nothing is written into the Mastervolt CA folder.
# 825 days is the longest validity iOS accepts for a server certificate.
$serial = "0x" + (-join ((1..16) | ForEach-Object { "{0:x2}" -f (Get-Random -Maximum 256) }))
& $opensslPath x509 -req -in $serverCsr -CA $caPem -CAkey $caKey -set_serial $serial -out $serverPem `
    -days 825 -sha256 -extfile $serverConfig -extensions req_ext
if ($LASTEXITCODE -ne 0) { throw "Could not sign the HTTPS server certificate with the Mastervolt Local CA" }

try {
    $currentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    & icacls.exe $serverKey /inheritance:r /grant:r "${currentIdentity}:(R,W)" | Out-Null
} catch {
    Write-Warning "Could not restrict private-key file permissions: $($_.Exception.Message)"
}

Write-Host "Anchor Watch HTTPS certificate is ready (issued by the Mastervolt Local CA in $CaDir)."
Write-Host "Certificate addresses: localhost, $env:COMPUTERNAME, $($localAddresses -join ', ')"
Write-Host "Private-LAN bind address: $privateAddress"
