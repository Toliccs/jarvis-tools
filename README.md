# Jarvis Tools — Universal Windows Onboarding

Configure any Windows machine for secure remote Jarvis access in under 5 minutes.

## Overview

**jarvis-onboard.ps1** is a universal PowerShell script that:
- ✅ Creates a local `jarvis` account (password: `Jarvis#2026!`)
- ✅ Configures WinRM for remote access
- ✅ Sets up firewall rules and trusted hosts
- ✅ Disables UAC remote filter (LocalAccountTokenFilterPolicy)
- ✅ Self-tests connection before completing
- ✅ Logs all output to `C:\jarvis-setup-log.txt`
- ✅ Works on Windows 10 Home/Pro, Windows 11 Home/Pro

## Quick Start

### Option 1: Download from GitHub (Recommended)

Open PowerShell as Administrator and run:

```powershell
iex (irm 'https://raw.githubusercontent.com/Toliccs/jarvis-tools/main/jarvis-onboard.ps1')
```

### Option 2: Double-Click Launcher (USB/Local)

1. Copy `jarvis-onboard.ps1` and `jarvis-onboard.bat` to your machine
2. Right-click `jarvis-onboard.bat` → **Run as administrator**
3. Wait for completion

### Option 3: Manual PowerShell

```powershell
powershell -ExecutionPolicy Bypass -File jarvis-onboard.ps1
```

## What It Does

### Phase 1: Local Account Setup
- Creates account `jarvis` with password `Jarvis#2026!`
- Prevents password expiry

### Phase 2: Group Membership
- Adds to `Administrators` (all Windows versions)
- Adds to `Remote Management Users` (Pro/Enterprise editions)

### Phase 3: WinRM Service
- Enables PSRemoting
- Starts WinRM service
- Verifies HTTP listener on port 5985

### Phase 4: Trusted Hosts
- Sets `TrustedHosts = *` (allows connections from any IP)

### Phase 5: UAC Filter
- Disables `LocalAccountTokenFilterPolicy` (allows local admin over network)

### Phase 6: Network Profile
- Changes network profiles from Public to Private (enables firewall rules)

### Phase 7: Firewall
- Enables built-in "Windows Remote Management (HTTP-In)" rule
- Creates custom rule "WinRM-HTTP-Jarvis" on TCP 5985

### Phase 8: Service Restart
- Restarts WinRM service cleanly

### Phase 9: Local Test
- Validates setup with a local WinRM connection test
- Reports success/failure before completion

## Requirements

- **Windows 10 Home/Pro** or **Windows 11 Home/Pro**
- Administrator privileges (UAC prompt will appear)
- Internet connection (optional; can run offline from USB)
- PowerShell 5.0 or later (built-in on Windows 10/11)

## Verification

After setup, verify remote access from another machine:

```powershell
# Create credential object
$cred = New-Object System.Management.Automation.PSCredential(
    "jarvis",
    (ConvertTo-SecureString "Jarvis#2026!" -AsPlainText -Force)
)

# Test connection
Invoke-Command -ComputerName <IP_OR_HOSTNAME> -Credential $cred -ScriptBlock { whoami }
```

Expected output:
```
<COMPUTERNAME>\jarvis
```

## Logs

All output is logged to: **`C:\jarvis-setup-log.txt`**

Review this file if setup fails or to verify what was configured.

## Troubleshooting

### "Not running as Administrator"
- Right-click the batch file → **Run as administrator**
- Or open PowerShell as Administrator first, then run the script

### "WinRM service not found"
- This should not happen on Windows 10/11 (WinRM is built-in)
- If it does occur, WinRM feature may need manual enablement via Windows Features

### Local test failed
- The setup completed, but local WinRM connection test failed
- This is usually safe to ignore; the account and WinRM are still configured
- Try the remote verification command above

### "TrustedHosts set to '*' is overly permissive"
- This is intentional for office automation
- If you need to restrict to specific IPs, modify the script before running

## Security Notes

- Account password `Jarvis#2026!` is standard across all office machines (by design)
- TrustedHosts allows connections from any IP (intentional for automation)
- UAC filter is disabled for remote administrative access (standard for managed networks)
- Use network firewalls and VPN for external access restrictions

## Support

Report issues or request features on GitHub:
https://github.com/Toliccs/jarvis-tools

---

**Version:** 1.0  
**Last Updated:** 2026-05-15  
**License:** Internal Use Only
