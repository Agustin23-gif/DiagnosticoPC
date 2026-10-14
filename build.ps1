# build.ps1 — Genera DiagnosticoPC.exe portable
# Uso: cd C:\Proyectos\DiagnosticoPC && .\build.ps1

$env:PATH = [System.Environment]::GetEnvironmentVariable("PATH","Machine") + ";" +
            [System.Environment]::GetEnvironmentVariable("PATH","User")

Write-Host ""
Write-Host "  [1/3] Instalando PyInstaller..." -ForegroundColor Cyan
python -m pip install pyinstaller --quiet

# Bootstrapper de WebView2: se descarga ANTES de compilar para que quede
# embebido en el .exe (tools\ va por --add-data) y además se copia al lado
# del .exe en dist\tools\webview2 (carpeta que va al pendrive).
$webview2Path = "tools\webview2\MicrosoftEdgeWebview2Setup.exe"
if (-not (Test-Path $webview2Path)) {
    New-Item -ItemType Directory -Force -Path "tools\webview2" | Out-Null
    Write-Host "  Descargando WebView2 bootstrapper..." -ForegroundColor Cyan
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri "https://go.microsoft.com/fwlink/p/?LinkId=2124703" `
            -OutFile $webview2Path -UseBasicParsing
        Write-Host "  WebView2 bootstrapper descargado en $webview2Path" -ForegroundColor Green
    } catch {
        Remove-Item $webview2Path -ErrorAction SilentlyContinue
        Write-Host "  AVISO: no se pudo descargar WebView2 ($($_.Exception.Message)). Se compila sin el." -ForegroundColor Yellow
    }
}

# Instalador offline de WebView2 (~200 MB, se agrega a mano): NO se embebe,
# --onefile lo extraería a %TEMP% en cada arranque. Se aparta mientras compila
# PyInstaller y después se copia solo al lado del .exe (dist\tools\webview2).
$wv2Offline    = "tools\webview2\MicrosoftEdgeWebView2RuntimeInstallerX64.exe"
$wv2OfflineTmp = "_wv2_offline_tmp"
if (Test-Path $wv2Offline) {
    New-Item -ItemType Directory -Force -Path $wv2OfflineTmp | Out-Null
    Move-Item $wv2Offline $wv2OfflineTmp -Force
}

Write-Host "  [2/3] Compilando ejecutable (puede tardar 2-3 minutos)..." -ForegroundColor Cyan
try {
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
} finally {
    # Devolver el instalador offline a tools\webview2 aunque la compilación falle
    $tmpFile = Join-Path $wv2OfflineTmp "MicrosoftEdgeWebView2RuntimeInstallerX64.exe"
    if (Test-Path $tmpFile) {
        Move-Item $tmpFile $wv2Offline -Force
        Remove-Item $wv2OfflineTmp -Force -ErrorAction SilentlyContinue
    }
}

Write-Host "  [3/3] Verificando resultado..." -ForegroundColor Cyan
$exe = "dist\PC House Diagnostico.exe"
if (Test-Path $exe) {
    if (Test-Path $webview2Path) {
        New-Item -ItemType Directory -Force -Path "dist\tools\webview2" | Out-Null
        Copy-Item $webview2Path "dist\tools\webview2\" -Force
        Write-Host "  WebView2 bootstrapper copiado a dist\tools\webview2" -ForegroundColor Green
    }
    if (Test-Path $wv2Offline) {
        New-Item -ItemType Directory -Force -Path "dist\tools\webview2" | Out-Null
        Copy-Item $wv2Offline "dist\tools\webview2\" -Force
        Write-Host "  WebView2 instalador offline copiado a dist\tools\webview2" -ForegroundColor Green
    }
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
