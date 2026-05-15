# TEST-SCRIPT.ps1
# Verify Jarvis remote access on a machine

param(
    [string]$ComputerName = "192.168.1.233",
    [string]$AccountName = "jarvis",
    [string]$Password = "Jarvis#2026!"
)

Write-Host ""
Write-Host "JARVIS REMOTE ACCESS VERIFICATION TEST" -ForegroundColor Cyan -BackgroundColor DarkGray
Write-Host ""
Write-Host "Target: $ComputerName" -ForegroundColor Yellow
Write-Host "Account: $AccountName" -ForegroundColor Yellow
Write-Host ""

# Step 1: Network connectivity
Write-Host "[1/4] Testing network connectivity..." -ForegroundColor Cyan
$pingTest = $null
try {
    $pingTest = Test-Connection -ComputerName $ComputerName -Count 1 -ErrorAction Stop
    Write-Host "  OK - Reachable (latency: $($pingTest.ResponseTime)ms)" -ForegroundColor Green
} catch {
    Write-Host "  FAIL - Not reachable" -ForegroundColor Red
    Write-Host "  Error: $_" -ForegroundColor Red
    exit 1
}

# Step 2: WinRM connectivity
Write-Host ""
Write-Host "[2/4] Testing WinRM port (5985)..." -ForegroundColor Cyan
$tcpTest = $null
try {
    $tcpTest = Test-NetConnection -ComputerName $ComputerName -Port 5985 -ErrorAction Stop
    if ($tcpTest.TcpTestSucceeded) {
        Write-Host "  OK - Port 5985 is accessible" -ForegroundColor Green
    } else {
        Write-Host "  FAIL - Port 5985 not accessible" -ForegroundColor Red
        exit 1
    }
} catch {
    Write-Host "  FAIL - Port test error" -ForegroundColor Red
    Write-Host "  Error: $_" -ForegroundColor Red
    exit 1
}

# Step 3: Create credentials
Write-Host ""
Write-Host "[3/4] Creating credential object..." -ForegroundColor Cyan
$securePass = ConvertTo-SecureString $Password -AsPlainText -Force
$cred = New-Object System.Management.Automation.PSCredential($AccountName, $securePass)
Write-Host "  OK - Credentials ready" -ForegroundColor Green

# Step 4: Remote test
Write-Host ""
Write-Host "[4/4] Testing remote command execution..." -ForegroundColor Cyan
try {
    $result = Invoke-Command -ComputerName $ComputerName -Credential $cred -ScriptBlock {
        @{
            User = whoami
            Computer = $env:COMPUTERNAME
            PSVersion = $PSVersionTable.PSVersion.Major
            Time = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
        }
    } -ErrorAction Stop -WarningAction SilentlyContinue
    
    Write-Host "  OK - Remote command succeeded" -ForegroundColor Green
    Write-Host ""
    Write-Host "  Remote System Info:" -ForegroundColor Green
    Write-Host "    User: $($result.User)" -ForegroundColor Green
    Write-Host "    Computer: $($result.Computer)" -ForegroundColor Green
    Write-Host "    PowerShell: v$($result.PSVersion)" -ForegroundColor Green
    Write-Host "    Time: $($result.Time)" -ForegroundColor Green
} catch {
    Write-Host "  FAIL - Remote command failed" -ForegroundColor Red
    Write-Host "  Error: $_" -ForegroundColor Red
    Write-Host ""
    Write-Host "  Possible causes:" -ForegroundColor Yellow
    Write-Host "    1. Account doesn't exist (run jarvis-onboard.ps1 first)" -ForegroundColor Yellow
    Write-Host "    2. Incorrect password" -ForegroundColor Yellow
    Write-Host "    3. WinRM not configured" -ForegroundColor Yellow
    Write-Host "    4. Network/firewall blocking" -ForegroundColor Yellow
    exit 1
}

# Success
Write-Host ""
Write-Host "ALL TESTS PASSED - Remote access working!" -ForegroundColor Green -BackgroundColor DarkGreen
Write-Host ""
exit 0
