# JARVIS ONBOARDING — DEPLOYMENT GUIDE

**Version:** 1.0  
**Date:** 2026-05-15  
**Status:** Ready for production deployment

---

## Overview

Universal Windows onboarding script to configure any office machine for Jarvis remote access in under 5 minutes.

**Key Facts:**
- Works on Windows 10 Home/Pro and Windows 11 Home/Pro
- Creates local account: `jarvis` / `Jarvis#2026!`
- Enables WinRM over HTTP (port 5985)
- Sets up firewall + UAC + TrustedHosts automatically
- Self-testing validation
- Zero dependencies

---

## GitHub Repository

**URL:** https://github.com/Toliccs/jarvis-tools  
**Access:** Public (anyone can download)  
**Contents:**
- `jarvis-onboard.ps1` — Main setup script
- `jarvis-onboard.bat` — Double-click launcher
- `TEST-SCRIPT.ps1` — Verification after setup
- `README.md` — User guide
- `DEPLOYMENT-GUIDE.md` — This file

---

## Deployment Options

### Option A: Direct Download (Easiest)

For any machine with internet:

```powershell
# Open PowerShell as Administrator, then paste:
iex (irm 'https://raw.githubusercontent.com/Toliccs/jarvis-tools/main/jarvis-onboard.ps1')
```

### Option B: USB Thumb Drive (Offline)

For machines without internet:

1. Copy these files to USB:
   - `jarvis-onboard.ps1`
   - `jarvis-onboard.bat`

2. On target machine:
   - Plug in USB
   - Right-click `jarvis-onboard.bat`
   - Select **Run as administrator**
   - Wait for completion

3. Check: `C:\jarvis-setup-log.txt` for results

### Option C: Network Share

For centralized deployment:

1. Copy scripts to network drive (e.g., `\\fileserver\share\jarvis\`)
2. Run from share: `\\fileserver\share\jarvis\jarvis-onboard.bat`

---

## Deployment Checklist

### BEFORE Deployment

- [ ] Test script verified on GPC (192.168.1.212) — ✅ DONE 2026-05-15
- [ ] GitHub repo created and files pushed — ✅ DONE 2026-05-15
- [ ] README documentation complete — ✅ DONE 2026-05-15
- [ ] Test verification script working — ✅ DONE 2026-05-15

### Deployment Sequence

1. **Son's PC (192.168.1.233) — Priority**
   - Method: USB thumb drive (offline)
   - Steps:
     - [ ] Copy files to USB
     - [ ] Run `jarvis-onboard.bat` as Administrator
     - [ ] Wait for "Setup Complete" message
     - [ ] Check `C:\jarvis-setup-log.txt`
     - [ ] Test: Run `TEST-SCRIPT.ps1 -ComputerName 192.168.1.233`

2. **Vaun's PC (192.168.1.71)**
   - Method: Download from GitHub or USB
   - Steps:
     - [ ] Run `jarvis-onboard.ps1` (or `.bat` from USB)
     - [ ] Verify via `TEST-SCRIPT.ps1`

3. **ADMIN PC (192.168.1.74)**
   - Method: Same as above

4. **Any Future Machines**
   - Method: USB or GitHub download
   - Steps: Same as above

### AFTER Each Deployment

1. Verify with test script:
   ```powershell
   & "C:\path\to\TEST-SCRIPT.ps1" -ComputerName <IP>
   ```

2. Confirm output shows:
   ```
   ALL TESTS PASSED - Remote access working!
   ```

3. Log results to deployment tracker

---

## Testing Procedure

### Local Test (on target machine after setup)

The script automatically runs a local test at the end. Look for:

```
[4/4] Testing local command execution...
  ✓ Local WinRM test PASSED as jarvis
```

If this fails but setup completed, manual verification may be needed.

### Remote Test (from any machine)

Use `TEST-SCRIPT.ps1`:

```powershell
# From any machine in the office, test a recently deployed machine:

# Test Son's PC (192.168.1.233)
& .\TEST-SCRIPT.ps1 -ComputerName "192.168.1.233"

# Test Vaun's PC (192.168.1.71)
& .\TEST-SCRIPT.ps1 -ComputerName "192.168.1.71"
```

Expected output:
```
[1/4] Testing network connectivity...
  OK - Reachable (latency: Xms)

[2/4] Testing WinRM port (5985)...
  OK - Port 5985 is accessible

[3/4] Creating credential object...
  OK - Credentials ready

[4/4] Testing remote command execution...
  OK - Remote command succeeded
  
  Remote System Info:
    User: <COMPUTERNAME>\jarvis
    Computer: <COMPUTERNAME>
    PowerShell: v5
    Time: 2026-05-15 HH:MM:SS

ALL TESTS PASSED - Remote access working!
```

---

## What Gets Installed

### Local Account
- **Name:** jarvis
- **Password:** Jarvis#2026!
- **Groups:** Administrators, Remote Management Users (if available)
- **Password expires:** Never

### WinRM Configuration
- **Service:** Enabled and running
- **Listener:** HTTP on port 5985
- **TrustedHosts:** * (all IP addresses)
- **UAC Filter:** Disabled (LocalAccountTokenFilterPolicy = 1)

### Firewall Rules
- Built-in "Windows Remote Management (HTTP-In)" rule enabled
- Custom "WinRM-HTTP-Jarvis" rule created for TCP 5985

### Network
- Network profiles set to Private (enables firewall access)

---

## Troubleshooting

### Setup Fails with "Not running as Administrator"

**Fix:** Right-click `.bat` file → "Run as administrator"

### Port 5985 Not Accessible

**Check:**
1. Firewall rules enabled: `netsh advfirewall firewall show rule name="*WinRM*"`
2. WinRM service running: `Get-Service WinRM` (should show "Running")
3. Network profile set to Private: `Get-NetConnectionProfile`

**Fix:**
```powershell
# Re-run setup as Administrator
# Or manually enable rules:
netsh advfirewall firewall set rule name="Windows Remote Management (HTTP-In)" new enable=yes
```

### WinRM Test Fails After Setup

**Cause:** Usually one of:
1. Account not created (check: `Get-LocalUser jarvis`)
2. WinRM not started (check: `Get-Service WinRM`)
3. Firewall still blocking (check firewall rules)

**Fix:**
1. Check `C:\jarvis-setup-log.txt` for specific failure
2. Re-run setup script as Administrator
3. Try manually enabling PSRemoting:
   ```powershell
   Enable-PSRemoting -Force -SkipNetworkProfileCheck
   Restart-Service WinRM
   ```

### Remote Connection Fails: "Access Denied"

**Cause:** Usually UAC filter issue

**Fix:**
```powershell
# Check if UAC filter is set:
Get-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System" -Name "LocalAccountTokenFilterPolicy"

# Should show: LocalAccountTokenFilterPolicy = 1
# If not, run setup again or set manually:
Set-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System" `
    -Name "LocalAccountTokenFilterPolicy" -Value 1 -Type DWord -Force
Restart-Service WinRM
```

---

## Deployment Status

| Machine | IP | Status | Date | Notes |
|---------|----|----|------|-------|
| GPC (Baseline) | 192.168.1.212 | ✅ VERIFIED | 2026-05-15 | Script tested & working |
| Son's PC | 192.168.1.233 | ⏳ PENDING | — | Awaiting USB deployment |
| Vaun's PC | 192.168.1.71 | ⏳ PENDING | — | After Son's PC |
| ADMIN PC | 192.168.1.74 | ⏳ PENDING | — | After Vaun's PC |
| Future machines | — | ⏳ PENDING | — | As needed |

---

## Support & Updates

**Repository:** https://github.com/Toliccs/jarvis-tools  
**Issues:** Report via GitHub Issues  
**Updates:** Pull latest from repo (script is backward compatible)

**Credentials:**
- Account: `jarvis`
- Password: `Jarvis#2026!`
- ⚠️ Same on all machines (by design for office automation)

---

## Security Notes

**Intentional Design Choices:**

1. **Same password across machines** — This is intentional for office automation. Use network firewalls to restrict external access.

2. **TrustedHosts = \*** — Allows WinRM from any IP on office network. Same security model as RDP.

3. **UAC filter disabled** — Required for local admin accounts to work over WinRM. Standard for managed networks.

4. **No HTTPS** — HTTP only (port 5985). Use network-level firewalls for external security. If external access needed, escalate to Nick.

---

_Document created 2026-05-15 by Jarvis_
