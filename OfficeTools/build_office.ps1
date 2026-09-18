# PC HOUSE — Office Tools — Script de compilación
# Ejecutar desde la carpeta OfficeTools\
# Requiere: pip install pyinstaller pywebview

Set-Location $PSScriptRoot

pyinstaller `
  --noconfirm `
  --onefile `
  --windowed `
  --uac-admin `
  --name "PC House Office Tools" `
  --icon "..\assets\icon.ico" `
  --add-data "assets\logo.jpg;assets" `
  office_tools.py

Write-Host ""
Write-Host "Compilado en dist\PC House Office Tools.exe" -ForegroundColor Green
