@echo off
title XE Widgets Setup
echo Installing XE Widgets for the Corsair Xeneon Edge...
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/tkalva2-source/XE-Widgets/main/setup.ps1 | iex"
pause
