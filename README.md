# DiagnosticoPC — PC HOUSE

Herramienta de diagnóstico portable para técnicos. Muestra métricas en vivo (CPU, RAM, almacenamiento) y genera un reporte completo del sistema exportable a PDF con logo PC HOUSE.

## Requisitos

- Windows 10/11 (x64)
- Sin instalación — corre directo desde el `.exe`
- **Se recomienda ejecutar como Administrador** para obtener datos SMART de los discos (temperatura, horas de uso, ciclos de encendido, vida útil restante). El programa solicita elevación automáticamente al iniciar mediante el diálogo UAC de Windows; si se cancela, funciona normalmente con información básica de disco.

## Uso

1. Copiar `DiagnosticoPC.exe` al pendrive (junto con la carpeta `assets/`).
2. En el PC del cliente: doble clic sobre `DiagnosticoPC.exe`.
3. Aceptar el diálogo de permisos de administrador (UAC) cuando aparezca.
4. Completar **Cliente** y **Orden N°** en el panel superior.
5. Presionar **Generar Reporte** → **Exportar PDF**.

## Módulo de almacenamiento (bahías)

Cada disco físico aparece como una bahía visual. Al hacer clic se abre un panel con:

| Dato | Fuente |
|---|---|
| Temperatura | SMART (atributo 194 / NVMe) |
| Horas de uso | Power-On Hours |
| Ciclos de encendido | Power Cycle Count |
| Vida restante | % Used NVMe / atributos SATA 231, 177 |
| Sectores reasignados | SMART atributo 5 |
| Particiones | PowerShell `Get-Volume` |

> Los datos SMART requieren privilegios de administrador. Sin ellos el panel muestra solo nombre, tipo, tamaño y estado de salud (obtenidos por Windows).

## Compilar desde fuente

```powershell
cd C:\Proyectos\DiagnosticoPC
.\build.ps1
```

Requiere Python 3.10+ con los paquetes del `requirements.txt` y PyInstaller (`pip install pyinstaller`).

## Estructura del proyecto

```
DiagnosticoPC/
├── diagnostico_pc.py       # Aplicación principal
├── build.ps1               # Script de compilación
├── assets/
│   ├── logo.jpg            # Logo PC HOUSE
│   └── personaje.png       # Mascota
└── tools/
    └── smartmontools/
        └── smartctl.exe    # smartmontools 7.5 (incluido en el .exe)
```

## Información que recopila

| Categoría | Detalles |
|---|---|
| Sistema operativo | Nombre, versión, arquitectura |
| Equipo y usuario | Hostname, usuario, dominio |
| Procesador | Modelo, núcleos, frecuencia, uso en vivo |
| Memoria RAM | Total, usado, disponible, swap |
| Almacenamiento | Discos físicos, particiones, SMART |
| Batería | Nivel, estado, tiempo restante (laptops) |
| Programas | Lista desde el registro de Windows |

## Dependencias

| Paquete | Uso |
|---|---|
| `psutil` | CPU, RAM, disco, batería |
| `reportlab` | PDF con logo |
| `pywebview` | Interfaz HTML/CSS/JS nativa (Edge WebView2) |
