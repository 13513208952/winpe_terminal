@echo off
rem SPDX-License-Identifier: MIT
rem Console is EMS/SAC on COM1 (enabled in BCD by the image builder).
set "WPT_BOOTLOG=%SystemRoot%\System32\wpt-startup.log"
echo [%date% %time%] startnet begin > "%WPT_BOOTLOG%"
echo [WPT] Initializing WinPE. Please wait...
wpeinit >> "%WPT_BOOTLOG%" 2>&1
echo [%date% %time%] wpeinit exit=%errorlevel% >> "%WPT_BOOTLOG%"
echo [WPT] WinPE ready. EMS/SAC is configured on COM1.
echo [WPT] Host terminal attaches through SAC; this window stays for local use.
cmd.exe /k
