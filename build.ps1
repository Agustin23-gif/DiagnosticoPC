# build.ps1 — Genera DiagnosticoPC.exe portable
# Uso: cd C:\Proyectos\DiagnosticoPC && .\build.ps1

$env:PATH = [System.Environment]::GetEnvironmentVariable("PATH","Machine") + ";" +
            [System.Environment]::GetEnvironmentVariable("PATH","User")

Write-Host ""
Write-Host "  [1/3] Instalando PyInstaller..." -ForegroundColor Cyan
python -m pip install pyinstaller --quiet

Write-Host "  [2/3] Compilando ejecutable (puede tardar 2-3 minutos)..." -ForegroundColor Cyan
python -m PyInstaller `
    --onefile `
    --windowed `
    --name "PC House Diagnostico" `
    --icon "assets/icon.ico" `
    --collect-all reportlab `
    --collect-all webview `
    --collect-all PIL `
    --collect-all pythonnet `
    --hidden-import wmi `
    --hidden-import psutil `
    --hidden-import winreg `
    --hidden-import clr `
    --add-data "assets;assets" `
    --add-data "tools;tools" `
    --add-data "tools/LibreHardwareMonitor;tools/LibreHardwareMonitor" `
    --clean `
    --noconfirm `
    "diagnostico_pc.py"

Write-Host "  [3/3] Verificando resultado..." -ForegroundColor Cyan
$exe = "dist\PC House Diagnostico.exe"
if (Test-Path $exe) {
    $mb = [math]::Round((Get-Item $exe).Length / 1MB, 1)
    Write-Host ""
    Write-Host "  Listo!  $exe  ($mb MB)" -ForegroundColor Green
    Write-Host "  Copie ese archivo (y la carpeta assets/) a su pendrive." -ForegroundColor Green
    Write-Host ""
} else {
    Write-Host ""
    Write-Host "  ERROR: no se genero el ejecutable. Revise el log arriba." -ForegroundColor Red
    Write-Host ""
}
