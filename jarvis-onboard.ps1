# jarvis-onboard.ps1
# Universal Windows Jarvis Remote Access Onboarding Script
# Works on: Windows 10 Home/Pro, Windows 11 Home/Pro
# 
# Run as Administrator
# Usage: powershell -ExecutionPolicy Bypass -File jarvis-onboard.ps1
# Or: iex (irm 'https://raw.githubusercontent.com/toliccs/jarvis-tools/main/jarvis-onboard.ps1')

param(
    [string]$LogPath = "C:\jarvis-setup-log.txt",
    [string]$AccountName = "jarvis",
    [string]$AccountPassword = "Jarvis#2026!",
    [switch]$Verbose = $false
)

# ============================================================================
# LOGGING INFRASTRUCTURE
# ============================================================================

$script:LogFile = $LogPath
$script:PassCount = 0
$script:FailCount = 0
$script:WarnCount = 0
$script:TestResults = @()

function Initialize-Logger {
    try {
        $parent = Split-Path -Parent $LogPath
        if (-not (Test-Path $parent)) {
            New-Item -ItemType Directory -Path $parent -Force | Out-Null
        }
        Remove-Item -Path $LogPath -Force -ErrorAction SilentlyContinue
        New-Item -Path $LogPath -ItemType File | Out-Null
        return $true
    } catch {
        Write-Host "[ERROR] Failed to initialize log: $_" -ForegroundColor Red
        return $false
    }
}

function Write-Log {
    param(
        [string]$Message,
        [ValidateSet("INFO", "PASS", "FAIL", "WARN", "SECTION", "DETAIL")]
        [string]$Level = "INFO",
        [string]$Color = "White"
    )
    
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $logLine = "[$timestamp] [$Level] $Message"
    
    # Console output
    switch ($Level) {
        "SECTION" { Write-Host "`n$Message" -ForegroundColor $Color -BackgroundColor DarkGray; Write-Host ("=" * 80) -ForegroundColor $Color }
        "PASS" { Write-Host "  ✓ $Message" -ForegroundColor Green; $script:PassCount++ }
        "FAIL" { Write-Host "  ✗ $Message" -ForegroundColor Red; $script:FailCount++ }
        "WARN" { Write-Host "  ⚠ $Message" -ForegroundColor Yellow; $script:WarnCount++ }
        "DETAIL" { Write-Host "    $Message" -ForegroundColor Gray }
        default { Write-Host "  $Message" -ForegroundColor $Color }
    }
    
    # File logging
    try {
        Add-Content -Path $LogPath -Value $logLine -ErrorAction Stop
    } catch {
        # Silent fail on log write (don't break execution)
    }
}

# ============================================================================
# PREREQUISITE CHECKS
# ============================================================================

function Test-Administrator {
    $isAdmin = ([System.Security.Principal.WindowsIdentity]::GetCurrent().Groups -contains "S-1-5-32-544")
    return $isAdmin
}

function Test-WinRMInstalled {
    try {
        Get-Service WinRM -ErrorAction Stop | Out-Null
        return $true
    } catch {
        return $false
    }
}

function Get-MachineInfo {
    return @{
        ComputerName = $env:COMPUTERNAME
        OSVersion = [System.Environment]::OSVersion.VersionString
        PowerShellVersion = $PSVersionTable.PSVersion.Major
        IsAdmin = Test-Administrator
        IPv4Addresses = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | 
                         Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.*" }).IPAddress
    }
}

# ============================================================================
# MAIN SETUP FUNCTIONS
# ============================================================================

function New-JarvisAccount {
    param([string]$Name, [string]$Password)
    
    try {
        $securePass = ConvertTo-SecureString $Password -AsPlainText -Force
        $existing = Get-LocalUser $Name -ErrorAction SilentlyContinue
        
        if ($existing) {
            Set-LocalUser $Name -Password $securePass -ErrorAction Stop
            Write-Log "Reset password for existing account '$Name'" "PASS"
        } else {
            New-LocalUser $Name `
                -Password $securePass `
                -FullName "Jarvis Remote Access" `
                -Description "Automated remote access account for Jarvis" `
                -ErrorAction Stop | Out-Null
            Write-Log "Created local account '$Name'" "PASS"
        }
        
        # Ensure password never expires
        Set-LocalUser $Name -PasswordNeverExpires $true -ErrorAction Stop
        Write-Log "Password set to never expire" "DETAIL"
        
        return $true
    } catch {
        Write-Log "Account creation failed: $_" "FAIL"
        return $false
    }
}

function Add-JarvisToGroups {
    param([string]$Name)
    
    $addedGroups = @()
    
    # Administrators (required on all Windows editions)
    try {
        $adminGroup = Get-LocalGroup "Administrators"
        $isMember = $adminGroup | Get-LocalGroupMember -ErrorAction SilentlyContinue | 
                    Where-Object { $_.Name -like "*\$Name" -or $_.Name -eq $Name }
        
        if (-not $isMember) {
            Add-LocalGroupMember -Group "Administrators" -Member $Name -ErrorAction Stop
            Write-Log "Added to Administrators group" "PASS"
            $addedGroups += "Administrators"
        } else {
            Write-Log "Already member of Administrators" "DETAIL"
        }
    } catch {
        Write-Log "Failed to add to Administrators: $_" "FAIL"
        return $false
    }
    
    # Remote Management Users (optional - may not exist on Home editions)
    try {
        $rmGroup = Get-LocalGroup "Remote Management Users" -ErrorAction SilentlyContinue
        if ($rmGroup) {
            $isMember = $rmGroup | Get-LocalGroupMember -ErrorAction SilentlyContinue | 
                        Where-Object { $_.Name -like "*\$Name" -or $_.Name -eq $Name }
            
            if (-not $isMember) {
                Add-LocalGroupMember -Group "Remote Management Users" -Member $Name -ErrorAction Stop
                Write-Log "Added to Remote Management Users group" "PASS"
                $addedGroups += "Remote Management Users"
            } else {
                Write-Log "Already member of Remote Management Users" "DETAIL"
            }
        } else {
            Write-Log "Remote Management Users group not found (Home edition)" "WARN"
        }
    } catch {
        Write-Log "Note: Could not add to Remote Management Users: $_" "WARN"
    }
    
    return $true
}

function Enable-JarvisWinRM {
    # Enable PSRemoting (creates HTTP listener on 5985)
    try {
        Enable-PSRemoting -Force -SkipNetworkProfileCheck -ErrorAction Stop | Out-Null
        Write-Log "PSRemoting enabled" "PASS"
    } catch {
        Write-Log "PSRemoting enable failed: $_" "FAIL"
        return $false
    }
    
    # Start WinRM service
    try {
        $svc = Get-Service WinRM -ErrorAction Stop
        if ($svc.Status -ne "Running") {
            Start-Service WinRM -ErrorAction Stop
            Start-Sleep -Seconds 2
            Write-Log "WinRM service started" "PASS"
        } else {
            Write-Log "WinRM service already running" "DETAIL"
        }
    } catch {
        Write-Log "Failed to start WinRM service: $_" "FAIL"
        return $false
    }
    
    # Verify HTTP listener exists
    try {
        $listeners = Get-Item WSMan:\localhost\Listener -ErrorAction SilentlyContinue
        if ($listeners) {
            Write-Log "WinRM HTTP listener configured" "PASS"
        } else {
            Write-Log "WinRM listener not found (may need manual configuration)" "WARN"
        }
    } catch {
        Write-Log "Could not verify WinRM listener: $_" "WARN"
    }
    
    return $true
}

function Set-TrustedHosts {
    try {
        $current = (Get-Item WSMan:\localhost\Client\TrustedHosts -ErrorAction SilentlyContinue).Value
        if ($current -ne "*") {
            Set-Item WSMan:\localhost\Client\TrustedHosts -Value "*" -Force -ErrorAction Stop
            Write-Log "TrustedHosts set to '*' (all hosts)" "PASS"
        } else {
            Write-Log "TrustedHosts already set to '*'" "DETAIL"
        }
        return $true
    } catch {
        Write-Log "Failed to set TrustedHosts: $_" "FAIL"
        return $false
    }
}

function Disable-UACSRemoteFilter {
    try {
        $regPath = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System"
        $regName = "LocalAccountTokenFilterPolicy"
        
        $existing = Get-ItemProperty -Path $regPath -Name $regName -ErrorAction SilentlyContinue
        if ($existing.$regName -ne 1) {
            Set-ItemProperty -Path $regPath -Name $regName -Value 1 -Type DWord -Force -ErrorAction Stop
            Write-Log "UAC remote filter disabled (LocalAccountTokenFilterPolicy = 1)" "PASS"
        } else {
            Write-Log "UAC remote filter already disabled" "DETAIL"
        }
        return $true
    } catch {
        Write-Log "Failed to disable UAC filter: $_" "FAIL"
        return $false
    }
}

function Set-NetworkProfilePrivate {
    try {
        $profiles = Get-NetConnectionProfile -ErrorAction SilentlyContinue
        if ($profiles) {
            $changed = 0
            foreach ($profile in $profiles) {
                if ($profile.NetworkCategory -eq "Public") {
                    Set-NetConnectionProfile -Name $profile.Name -NetworkCategory Private -ErrorAction Stop
                    Write-Log "Network profile '$($profile.Name)' changed to Private" "PASS"
                    $changed++
                }
            }
            if ($changed -eq 0) {
                Write-Log "All network profiles already Private" "DETAIL"
            }
        }
        return $true
    } catch {
        Write-Log "Could not change network profile: $_" "WARN"
        return $true  # Non-fatal
    }
}

function Configure-Firewall {
    $success = $true
    
    # Try to enable built-in WRM rule
    try {
        $ruleName = "Windows Remote Management (HTTP-In)"
        $existingRule = Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue
        
        if ($existingRule -and $existingRule.Enabled -eq $false) {
            Set-NetFirewallRule -Name $ruleName -Enabled $true -ErrorAction Stop
            Write-Log "Enabled built-in '$ruleName' rule" "PASS"
        } elseif ($existingRule) {
            Write-Log "Built-in '$ruleName' rule already enabled" "DETAIL"
        }
    } catch {
        Write-Log "Could not manage built-in WinRM rule: $_" "WARN"
    }
    
    # Create custom rule for TCP 5985 if needed
    try {
        $customRule = Get-NetFirewallRule -Name "WinRM-HTTP-Jarvis" -ErrorAction SilentlyContinue
        if (-not $customRule) {
            New-NetFirewallRule `
                -Name "WinRM-HTTP-Jarvis" `
                -DisplayName "WinRM HTTP (Jarvis)" `
                -Direction Inbound `
                -Action Allow `
                -Protocol TCP `
                -LocalPort 5985 `
                -ErrorAction Stop | Out-Null
            Write-Log "Created firewall rule for TCP 5985 (WinRM)" "PASS"
        } else {
            Write-Log "Firewall rule for TCP 5985 already exists" "DETAIL"
        }
    } catch {
        Write-Log "Could not create firewall rule: $_" "WARN"
        $success = $false
    }
    
    return $success
}

function Test-LocalWinRM {
    param([string]$AccountName, [string]$Password)
    
    try {
        $cred = New-Object System.Management.Automation.PSCredential(
            $AccountName,
            (ConvertTo-SecureString $Password -AsPlainText -Force)
        )
        
        $result = Invoke-Command `
            -ComputerName localhost `
            -Credential $cred `
            -ScriptBlock {
                @{
                    User = whoami
                    Computer = $env:COMPUTERNAME
                    Status = "CONNECTED"
                }
            } `
            -ErrorAction Stop `
            -WarningAction SilentlyContinue
        
        Write-Log "Local WinRM test PASSED" "PASS"
        Write-Log "User: $($result.User)" "DETAIL"
        Write-Log "Computer: $($result.Computer)" "DETAIL"
        
        $script:TestResults += @{ Test = "Local WinRM Connection"; Status = "PASS"; Detail = $result.User }
        return $true
    } catch {
        Write-Log "Local WinRM test FAILED: $_" "FAIL"
        $script:TestResults += @{ Test = "Local WinRM Connection"; Status = "FAIL"; Detail = $_.Exception.Message }
        return $false
    }
}

# ============================================================================
# MAIN EXECUTION
# ============================================================================

Write-Host ""
Write-Host "╔════════════════════════════════════════════════════════════════════════════════╗" -ForegroundColor Cyan
Write-Host "║                   JARVIS REMOTE ACCESS ONBOARDING                             ║" -ForegroundColor Cyan
Write-Host "║                                                                                ║" -ForegroundColor Cyan
Write-Host "║  This script will configure this machine for secure remote Jarvis access      ║" -ForegroundColor Cyan
Write-Host "╚════════════════════════════════════════════════════════════════════════════════╝" -ForegroundColor Cyan
Write-Host ""

# Initialize logging
if (-not (Initialize-Logger)) {
    Write-Host "FATAL: Could not initialize logger" -ForegroundColor Red
    exit 1
}

Write-Log "JARVIS ONBOARDING SCRIPT STARTED" "SECTION"

# Pre-flight checks
Write-Log "PRE-FLIGHT CHECKS" "SECTION"

$machineInfo = Get-MachineInfo
Write-Log "Computer: $($machineInfo.ComputerName)" "INFO"
Write-Log "OS: $($machineInfo.OSVersion)" "INFO"
Write-Log "PowerShell: v$($machineInfo.PowerShellVersion)" "INFO"
Write-Log "IPv4: $($machineInfo.IPv4Addresses -join ', ')" "DETAIL"
Write-Log ""

# Admin check
if (-not $machineInfo.IsAdmin) {
    Write-Log "FATAL: Not running as Administrator" "FAIL"
    Write-Log "Please run this script as Administrator (right-click > Run as administrator)" "INFO"
    Write-Log ""
    Write-Log "Setup failed. Check $LogPath for details." "WARN"
    Start-Sleep -Seconds 3
    exit 1
}
Write-Log "Running as Administrator" "PASS"

# WinRM check
if (-not (Test-WinRMInstalled)) {
    Write-Log "FATAL: WinRM service not found (Windows feature missing)" "FAIL"
    Write-Log "WinRM is required for remote access (should be available on all Windows 10/11 editions)" "INFO"
    exit 1
}
Write-Log "WinRM service available" "PASS"

Write-Log ""

# PHASE 1: Account Creation
Write-Log "PHASE 1: LOCAL ACCOUNT SETUP" "SECTION"
if (-not (New-JarvisAccount -Name $AccountName -Password $AccountPassword)) {
    Write-Log "Account setup FAILED - cannot continue" "FAIL"
    exit 1
}
Write-Log ""

# PHASE 2: Group Membership
Write-Log "PHASE 2: GROUP MEMBERSHIP" "SECTION"
Add-JarvisToGroups -Name $AccountName | Out-Null
Write-Log ""

# PHASE 3: WinRM Enablement
Write-Log "PHASE 3: WINRM SERVICE SETUP" "SECTION"
if (-not (Enable-JarvisWinRM)) {
    Write-Log "WinRM setup FAILED - cannot continue" "FAIL"
    exit 1
}
Write-Log ""

# PHASE 4: TrustedHosts
Write-Log "PHASE 4: TRUSTED HOSTS CONFIGURATION" "SECTION"
Set-TrustedHosts | Out-Null
Write-Log ""

# PHASE 5: UAC Filter
Write-Log "PHASE 5: UAC REMOTE FILTER" "SECTION"
Disable-UACSRemoteFilter | Out-Null
Write-Log ""

# PHASE 6: Network Profile
Write-Log "PHASE 6: NETWORK PROFILE" "SECTION"
Set-NetworkProfilePrivate | Out-Null
Write-Log ""

# PHASE 7: Firewall
Write-Log "PHASE 7: FIREWALL CONFIGURATION" "SECTION"
Configure-Firewall | Out-Null
Write-Log ""

# PHASE 8: Restart WinRM
Write-Log "PHASE 8: WINRM SERVICE RESTART" "SECTION"
try {
    Restart-Service WinRM -Force -ErrorAction Stop
    Start-Sleep -Seconds 3
    Write-Log "WinRM service restarted" "PASS"
} catch {
    Write-Log "Failed to restart WinRM: $_" "FAIL"
    exit 1
}
Write-Log ""

# PHASE 9: Local Test
Write-Log "PHASE 9: LOCAL CONNECTION TEST" "SECTION"
if (-not (Test-LocalWinRM -AccountName $AccountName -Password $AccountPassword)) {
    Write-Log "WARNING: Local WinRM test failed, but setup completed" "WARN"
    Write-Log "Some configurations may need manual verification" "WARN"
}
Write-Log ""

# ============================================================================
# SUMMARY & COMPLETION
# ============================================================================

Write-Log "FINAL SUMMARY" "SECTION"
Write-Log "Passed: $($script:PassCount)" "INFO"
Write-Log "Failed: $($script:FailCount)" "INFO"
Write-Log "Warnings: $($script:WarnCount)" "INFO"
Write-Log ""

Write-Log "REMOTE ACCESS CREDENTIALS" "SECTION"
Write-Log "Account: $AccountName" "DETAIL"
Write-Log "Password: $AccountPassword" "DETAIL"
Write-Log "Computer: $($machineInfo.ComputerName)" "DETAIL"
Write-Log "IP Addresses: $($machineInfo.IPv4Addresses -join ', ')" "DETAIL"
Write-Log "Port: 5985 (HTTP)" "DETAIL"
Write-Log ""

Write-Log "REMOTE CONNECTION TEST COMMAND" "SECTION"
Write-Log "`$cred = New-Object System.Management.Automation.PSCredential('$AccountName', (ConvertTo-SecureString '$AccountPassword' -AsPlainText -Force))" "DETAIL"
Write-Log "Invoke-Command -ComputerName <IP_OR_HOSTNAME> -Credential `$cred -ScriptBlock { whoami }" "DETAIL"
Write-Log ""

if ($script:FailCount -eq 0) {
    Write-Log "✅ SETUP COMPLETE - All systems operational" "SECTION"
    $exitCode = 0
} else {
    Write-Log "⚠️  SETUP INCOMPLETE - Review failures above" "SECTION"
    $exitCode = 1
}

Write-Log ""
Write-Log "Log file: $LogPath" "INFO"
Write-Log "Finish time: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" "INFO"

Write-Host ""
exit $exitCode
