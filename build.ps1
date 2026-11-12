# Baut eine einzelne FileProbe.exe (braucht pyinstaller).
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
python -m PyInstaller --noconfirm --onefile --windowed --name FileProbe `
    --exclude-module matplotlib --exclude-module pandas FileProbe.py
Write-Host "Fertig: dist\FileProbe.exe"
