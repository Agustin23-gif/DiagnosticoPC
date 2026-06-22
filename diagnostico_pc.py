#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PC HOUSE — Diagnóstico PC v3.1
Frontend: pywebview (HTML/CSS/JS)
Backend:  psutil + winreg + reportlab + subprocess
"""

import base64, json, os, sys, platform, socket, datetime, threading, subprocess
import psutil

try:
    import winreg
    _HAS_WINREG = True
except ImportError:
    _HAS_WINREG = False

try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                    Table, TableStyle, HRFlowable, Image)
    from reportlab.lib.enums import TA_CENTER, TA_LEFT
    _HAS_RL = True
except ImportError:
    _HAS_RL = False

import webview

# Suppress console windows on Windows for all subprocess calls
_NWIN = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}


# ── Asset resolution (dev vs PyInstaller .exe) ────────────────────────────
def resource_path(rel):
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


# ── Formatting helpers ────────────────────────────────────────────────────
def _fb(n):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} PB"

def _ft(s):
    if s is None or s < 0:
        return "N/D"
    h, r = divmod(int(s), 3600)
    m, s = divmod(r, 60)
    return f"{h}h {m}m {s}s"


# ── Python API exposed to JS ──────────────────────────────────────────────
class Api:
    @staticmethod
    def _get_ram_info():
        _SMBIOS = {20: "DDR", 21: "DDR2", 24: "DDR3", 26: "DDR4", 34: "DDR5"}
        result = {"type": "N/D", "freq": "N/D", "slots": "N/D"}
        try:
            cmd = ("Get-WmiObject Win32_PhysicalMemory"
                   " | Select-Object SMBIOSMemoryType,Speed"
                   " | ConvertTo-Json -Compress")
            r = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                capture_output=True, text=True, timeout=12, **_NWIN,
            )
            chips = []
            if r.returncode == 0 and r.stdout.strip():
                raw = json.loads(r.stdout.strip())
                chips = [raw] if isinstance(raw, dict) else raw
            if chips:
                for c in chips:
                    t = int(c.get("SMBIOSMemoryType") or 0)
                    if t in _SMBIOS:
                        result["type"] = _SMBIOS[t]
                        break
                for c in chips:
                    spd = int(c.get("Speed") or 0)
                    if spd:
                        result["freq"] = str(spd)
                        break
            cmd2 = ("Get-WmiObject Win32_PhysicalMemoryArray"
                    " | Select-Object -ExpandProperty MemoryDevices")
            r2 = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd2],
                capture_output=True, text=True, timeout=10, **_NWIN,
            )
            total = 0
            if r2.returncode == 0 and r2.stdout.strip():
                try:
                    total = int(r2.stdout.strip().splitlines()[0].strip())
                except ValueError:
                    pass
            used = len(chips)
            if total > 0:
                result["slots"] = f"{used} de {total} slots usados"
            elif used > 0:
                result["slots"] = f"{used} módulo(s)"
        except Exception:
            pass
        return result

    @staticmethod
    def _get_cpu_name():
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                 r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
            name, _ = winreg.QueryValueEx(key, "ProcessorNameString")
            winreg.CloseKey(key)
            name = name.strip()
            if name:
                return name
        except Exception:
            pass
        return platform.processor() or "N/D"

    def __init__(self):
        self._cpu_value     = 0.0
        self._disk_activity = {}
        self._cpu_history   = [0.0] * 60
        self._lock          = threading.Lock()
        self._cpu_model = Api._get_cpu_name()
        self._ram_info  = Api._get_ram_info()
        self._hostname = socket.gethostname()
        self._disk_health_cache = None
        self._chk_lock     = threading.Lock()
        self._chkdsk_lines = []
        self._chkdsk_done  = False
        self._wh_lock      = threading.Lock()
        self._wh_lines     = []
        self._wh_done      = True
        self._wh_rc        = 0
        threading.Thread(target=self._cpu_monitor_thread,  daemon=True).start()
        threading.Thread(target=self._disk_monitor_thread, daemon=True).start()

    def _cpu_monitor_thread(self):
        import time
        try:
            import wmi
            w = wmi.WMI()
            while True:
                cpu = w.Win32_Processor()[0]
                with self._lock:
                    self._cpu_value = float(cpu.LoadPercentage or 0)
                time.sleep(1)
        except Exception:
            # Fallback a psutil si WMI falla
            psutil.cpu_percent(interval=None)
            time.sleep(1)
            while True:
                v = psutil.cpu_percent(interval=1)
                with self._lock:
                    self._cpu_value = v

    def _disk_monitor_thread(self):
        import time
        while True:
            try:
                before = psutil.disk_io_counters(perdisk=True)
                time.sleep(1)
                after = psutil.disk_io_counters(perdisk=True)
                activity = {}
                for disk in after:
                    if disk in before:
                        rb = after[disk].read_bytes - before[disk].read_bytes
                        wb = after[disk].write_bytes - before[disk].write_bytes
                        total_mb = (rb + wb) / (1024 * 1024)
                        pct = min(100.0, total_mb / 5.0)  # 500 MB/s = 100 %
                        activity[disk] = round(pct, 1)
                with self._lock:
                    self._disk_activity = activity
            except Exception:
                time.sleep(1)

    def get_disk_activity(self):
        with self._lock:
            return json.dumps(self._disk_activity)

    def get_metrics(self):
        with self._lock:
            cpu = self._cpu_value
        ram = psutil.virtual_memory()
        with self._lock:
            self._cpu_history.append(cpu)
            self._cpu_history = self._cpu_history[-60:]
            hist = list(self._cpu_history)
        return json.dumps({
            "cpu": cpu,
            "cpu_history": hist,
            "cpu_model": self._cpu_model[:50],
            "ram_pct": ram.percent,
            "ram_used": _fb(ram.used),
            "ram_total": _fb(ram.total),
            "ram_info": self._ram_info,
            "hostname": self._hostname,
        })

    def get_assets(self):
        logo_b64 = ""
        personaje_b64 = ""
        try:
            with open(resource_path("assets/logo.jpg"), "rb") as f:
                logo_b64 = base64.b64encode(f.read()).decode()
        except Exception:
            pass
        try:
            with open(resource_path("assets/personaje.png"), "rb") as f:
                personaje_b64 = base64.b64encode(f.read()).decode()
        except Exception:
            pass
        ventanas_b64 = ""
        try:
            with open(resource_path("assets/ventanas.png.png"), "rb") as f:
                ventanas_b64 = base64.b64encode(f.read()).decode()
        except Exception:
            pass
        return json.dumps({
            "logo": logo_b64,
            "logo_mime": "image/jpeg",
            "personaje": personaje_b64,
            "ventanas": ventanas_b64,
            "hostname": self._hostname,
            "cpu_model": self._cpu_model[:50],
        })

    def get_disk_health(self):
        self._disk_health_cache = self._get_disk_health()
        return json.dumps(self._disk_health_cache)

    def get_disk_detail(self, disk_name):
        return json.dumps(self._build_disk_detail(disk_name, self._disk_health_cache))

    @staticmethod
    def _build_disk_detail(disk_name, health_cache):
        disk_num = disk_type = disk_health = disk_size = None
        if health_cache:
            for d in health_cache:
                if d.get("name") == disk_name:
                    disk_num    = d.get("disk_num", "")
                    disk_type   = d.get("type",   "N/D")
                    disk_health = d.get("health", "N/D")
                    disk_size   = d.get("size",   "N/D")
                    break
        smart_raw  = Api._run_smartctl(str(disk_num)) if disk_num not in (None, "") else None
        scsi_only  = bool(smart_raw and smart_raw.get('_scsi_only'))
        smart_info = Api._parse_smart(smart_raw) if smart_raw and not scsi_only else None
        scsi_info  = Api._parse_scsi_basic(smart_raw) if scsi_only else None
        partitions = Api._get_disk_partitions(disk_num)
        return {
            "name":            disk_name or "—",
            "type":            disk_type  or "N/D",
            "health":          disk_health or "N/D",
            "size":            disk_size  or "N/D",
            "smart_available": smart_info is not None,
            "smart":           smart_info,
            "scsi_info":       scsi_info,
            "partitions":      partitions,
        }

    @staticmethod
    def _get_volume_letters(disk_num):
        """Return drive letters (no colon) for partitions on disk_num. No admin needed."""
        try:
            cmd = (
                f"Get-Disk -Number {disk_num} | Get-Partition"
                f" | Where-Object {{ $_.DriveLetter -ne $null -and $_.DriveLetter -ne '' }}"
                " | Select-Object -ExpandProperty DriveLetter"
            )
            r = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                capture_output=True, text=True, timeout=10, **_NWIN,
            )
            return [l for l in (ln.strip() for ln in r.stdout.strip().splitlines())
                    if len(l) == 1 and l.isalpha()]
        except Exception:
            return []

    @staticmethod
    def _run_smartctl(disk_num):
        device   = f"\\\\.\\PhysicalDrive{disk_num}"
        candidates = []
        rel = os.path.join("tools", "smartmontools", "smartctl.exe")
        if getattr(sys, 'frozen', False):
            candidates.append(os.path.join(os.path.dirname(sys.executable), rel))
            if hasattr(sys, '_MEIPASS'):
                candidates.append(os.path.join(sys._MEIPASS, rel))
        else:
            candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), rel))
        exe_list = [c for c in candidates if os.path.exists(c)]
        exe_list += ["smartctl", r"C:\Program Files\smartmontools\bin\smartctl.exe"]

        # Volume letters work without admin for ATA/SATA; PhysicalDrive needs admin
        vol_letters = Api._get_volume_letters(disk_num)

        def _has_data(data):
            return bool(
                data.get("temperature") or data.get("power_on_time")
                or data.get("ata_smart_attributes")
                or data.get("nvme_smart_health_information_log")
            )

        # Generic flags first, then USB bridge chips (JMicron, Cypress, Sunplus, Prolific)
        _PHYS_FLAGS = (
            [], ["-d", "sat"], ["-d", "nvme"], ["-d", "scsi"], ["-d", "ata"],
            ["-d", "usbjmicron"], ["-d", "usbcypress"],
            ["-d", "usbsunplus"], ["-d", "usbprolific"],
        )
        _VOL_FLAGS = (
            [], ["-d", "sat"],
            ["-d", "usbjmicron"], ["-d", "usbcypress"],
            ["-d", "usbsunplus"], ["-d", "usbprolific"],
        )

        for exe in exe_list:
            exe_found = False

            # 1) \\.\PhysicalDriveN — needs admin but covers NVMe and USB bridges
            for extra in _PHYS_FLAGS:
                try:
                    r = subprocess.run(
                        [exe] + extra + ["-a", "-j", device],
                        capture_output=True, text=True, timeout=15, **_NWIN,
                    )
                    exe_found = True
                    txt = r.stdout.strip()
                    if txt:
                        data = json.loads(txt)
                        if _has_data(data):
                            return data
                except FileNotFoundError:
                    break
                except Exception:
                    exe_found = True

            if not exe_found:
                continue  # this exe binary doesn't exist, try next

            # 2) Volume letters — no admin needed; try USB flags for external drives
            for letter in vol_letters:
                for extra in _VOL_FLAGS:
                    try:
                        r = subprocess.run(
                            [exe] + extra + ["-a", "-j", f"{letter}:"],
                            capture_output=True, text=True, timeout=15, **_NWIN,
                        )
                        txt = r.stdout.strip()
                        if txt:
                            data = json.loads(txt)
                            if _has_data(data):
                                return data
                    except Exception:
                        continue

            break  # found a valid exe; tried all device paths; stop

        # SCSI fallback — get basic device info when SMART isn't accessible
        for exe in exe_list:
            try:
                dev_letter = chr(ord('a') + int(disk_num)) if str(disk_num).isdigit() else 'a'
                r = subprocess.run(
                    [exe, '-d', 'scsi', '-a', '-j', f'/dev/sd{dev_letter}'],
                    capture_output=True, text=True, timeout=15, **_NWIN,
                )
                txt = r.stdout.strip()
                if txt:
                    data = json.loads(txt)
                    if data.get('model_name') or data.get('scsi_product') or data.get('scsi_vendor'):
                        data['_scsi_only'] = True
                        return data
            except Exception:
                pass
            break  # only try first valid exe

        return None

    @staticmethod
    def _parse_smart(raw):
        try:
            s = {}
            try:    s["temperature"]    = raw["temperature"]["current"]
            except: s["temperature"]    = None
            try:    s["power_on_hours"] = raw["power_on_time"]["hours"]
            except: s["power_on_hours"] = None
            try:    s["power_cycles"]   = raw.get("power_cycle_count")
            except: s["power_cycles"]   = None
            try:    s["passed"]         = raw["smart_status"]["passed"]
            except: s["passed"]         = None
            try:    s["serial"]         = raw.get("serial_number", "") or ""
            except: s["serial"]         = ""
            # NVMe life
            try:
                pct_used = (raw.get("nvme_smart_health_information_log") or {}).get("percentage_used")
                s["percentage_used"] = pct_used
                s["life_remaining"]  = max(0, 100 - pct_used) if pct_used is not None else None
            except:
                s["percentage_used"] = None
                s["life_remaining"]  = None
            # SATA reallocated sectors (id=5)
            try:
                attrs = ((raw.get("ata_smart_attributes") or {}).get("table") or [])
                rs = next((a for a in attrs if a.get("id") == 5), None)
                s["reallocated_sectors"] = int(((rs or {}).get("raw") or {}).get("value", 0)) if rs else 0
            except:
                s["reallocated_sectors"] = None
            # SATA SSD life (attrs 231=SSD life left, 177=wear leveling, 202=percent lifetime)
            if s["life_remaining"] is None:
                try:
                    attrs = ((raw.get("ata_smart_attributes") or {}).get("table") or [])
                    for tid, invert in [(231, False), (177, True), (202, True)]:
                        attr = next((a for a in attrs if a.get("id") == tid), None)
                        if attr:
                            val = int(((attr.get("raw") or {}).get("value")) or 0)
                            s["life_remaining"]  = 100 - val if invert else val
                            s["percentage_used"] = val if invert else 100 - val
                            break
                except:
                    pass
            return s
        except Exception:
            return None

    @staticmethod
    def _parse_scsi_basic(raw):
        try:
            return {
                "model":          raw.get("model_name") or raw.get("scsi_product") or "",
                "serial":         raw.get("serial_number") or "",
                "capacity_bytes": (raw.get("user_capacity") or {}).get("bytes"),
                "rotation_rate":  raw.get("rotation_rate"),
            }
        except Exception:
            return None

    @staticmethod
    def _get_disk_partitions(disk_num):
        partitions = []
        if disk_num not in (None, ""):
            try:
                cmd = (
                    f"Get-Disk -Number {disk_num} | Get-Partition | Get-Volume"
                    " | Select-Object DriveLetter,FileSystemType,Size,SizeRemaining"
                    " | ConvertTo-Json -Compress"
                )
                r = subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                    capture_output=True, text=True, timeout=12, **_NWIN,
                )
                if r.returncode == 0 and r.stdout.strip():
                    vols = json.loads(r.stdout.strip())
                    if isinstance(vols, dict):
                        vols = [vols]
                    for v in vols:
                        letter = str(v.get("DriveLetter") or "").strip()
                        if not letter or letter in ("None", ""):
                            continue
                        total     = int(v.get("Size") or 0)
                        remaining = int(v.get("SizeRemaining") or 0)
                        used      = total - remaining
                        pct       = round(used / total * 100, 1) if total else 0
                        partitions.append({
                            "mount":  letter + ":\\",
                            "fstype": str(v.get("FileSystemType") or "").strip() or "N/D",
                            "total":  _fb(total)     if total     else "N/D",
                            "used":   _fb(used)      if total     else "N/D",
                            "free":   _fb(remaining) if remaining else "N/D",
                            "pct":    pct,
                        })
                    return partitions
            except Exception:
                pass
        # Fallback: all psutil partitions
        try:
            for p in psutil.disk_partitions(all=False):
                try:
                    u = psutil.disk_usage(p.mountpoint)
                    partitions.append({
                        "mount":  p.mountpoint,
                        "fstype": p.fstype or "N/D",
                        "total":  _fb(u.total),
                        "used":   _fb(u.used),
                        "free":   _fb(u.free),
                        "pct":    round(u.percent, 1),
                    })
                except Exception:
                    pass
        except Exception:
            pass
        return partitions

    def generate_report(self, cliente, orden):
        try:
            lines = []
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            lines += [
                "=" * 72,
                "  PC HOUSE — REPORTE DE DIAGNÓSTICO",
                "=" * 72,
                f"  Fecha       : {now}",
                f"  Cliente     : {cliente or '(no especificado)'}",
                f"  Orden N°    : {orden or '(no especificado)'}",
                "=" * 72,
                "",
            ]

            lines += [
                "── SISTEMA OPERATIVO ─────────────────────────────────────────────────",
                f"  OS          : {platform.system()} {platform.release()}",
                f"  Versión     : {platform.version()[:80]}",
                f"  Arquitectura: {platform.machine()}",
                f"  Hostname    : {self._hostname}",
                f"  Usuario     : {os.environ.get('USERNAME', 'N/D')}",
                f"  Dominio     : {os.environ.get('USERDOMAIN', 'N/D')}",
                "",
            ]

            freq = psutil.cpu_freq()
            lines += [
                "── PROCESADOR ────────────────────────────────────────────────────────",
                f"  Modelo      : {Api._get_cpu_name()}",
                f"  Núcleos físicos: {psutil.cpu_count(logical=False)}",
                f"  Núcleos lógicos: {psutil.cpu_count(logical=True)}",
                f"  Frecuencia  : {freq.current:.0f} MHz  (max {freq.max:.0f} MHz)" if freq else "  Frecuencia  : N/D",
                f"  Uso actual  : {psutil.cpu_percent(interval=1):.1f}%",
                "",
            ]

            vm = psutil.virtual_memory()
            sw = psutil.swap_memory()
            ri = self._ram_info
            lines += [
                "── MEMORIA RAM ───────────────────────────────────────────────────────",
                f"  Total       : {_fb(vm.total)}",
                f"  Usado       : {_fb(vm.used)}  ({vm.percent:.1f}%)",
                f"  Disponible  : {_fb(vm.available)}",
                f"  Tipo        : {ri.get('type', 'N/D')}",
                f"  Frecuencia  : {ri.get('freq', 'N/D') + ' MHz' if ri.get('freq', 'N/D') != 'N/D' else 'N/D'}",
                f"  Slots       : {ri.get('slots', 'N/D')}",
                f"  Swap Total  : {_fb(sw.total)}",
                f"  Swap Usado  : {_fb(sw.used)}  ({sw.percent:.1f}%)",
                "",
            ]

            # Use cached disk health from startup; fetch only if not yet loaded
            disk_health = (self._disk_health_cache
                           if self._disk_health_cache is not None
                           else self._get_disk_health())
            lines.append("── ESTADO DE DISCOS FÍSICOS ──────────────────────────────────────────")
            if disk_health:
                for dh in disk_health:
                    h = dh["health"]
                    estado = ("BUENO"     if h == "Healthy"   else
                              "EN RIESGO" if h == "Warning"   else
                              "DAÑADO"    if h == "Unhealthy" else h or "N/D")
                    lines.append(
                        f"  [{dh['type']:<4}] {dh['name']:<42} {dh['size']:>10}   {estado}"
                    )
            else:
                lines.append("  No se pudo obtener información de salud de discos físicos")
            lines.append("")

            disk_data = []
            lines.append("── ALMACENAMIENTO — PARTICIONES ──────────────────────────────────────")
            for p in psutil.disk_partitions(all=False):
                try:
                    u = psutil.disk_usage(p.mountpoint)
                    lines.append(
                        f"  {p.mountpoint:<6} {p.fstype:<6}  Total:{_fb(u.total):>9}  "
                        f"Usado:{_fb(u.used):>9}  Libre:{_fb(u.free):>9}  ({u.percent:.1f}%)"
                    )
                    disk_data.append({
                        "mount": p.mountpoint,
                        "total": _fb(u.total),
                        "used": _fb(u.used),
                        "free": _fb(u.free),
                        "pct": u.percent,
                    })
                except PermissionError:
                    lines.append(f"  {p.mountpoint:<6} (sin acceso)")
            lines.append("")

            bat = psutil.sensors_battery()
            lines.append("── BATERÍA ───────────────────────────────────────────────────────────")
            if bat:
                st = "Cargando" if bat.power_plugged else "Descargando"
                lines += [
                    f"  Nivel       : {bat.percent:.1f}%",
                    f"  Estado      : {st}",
                    f"  Tiempo rest.: {_ft(bat.secsleft)}",
                ]
            else:
                lines.append("  No se detectó batería (equipo de escritorio o sin sensor)")
            lines.append("")

            lines.append("── PROGRAMAS INSTALADOS ──────────────────────────────────────────────")
            progs = self._get_programas()
            if progs:
                for p in progs[:150]:
                    lines.append(f"  {p}")
                if len(progs) > 150:
                    lines.append(f"  ... y {len(progs) - 150} más")
            else:
                lines.append("  (winreg no disponible)")
            lines.append("")
            lines.append("=" * 72)
            lines.append("  Reporte generado por PC HOUSE — Diagnóstico PC v3.1")
            lines.append("=" * 72)

            return json.dumps({"report": "\n".join(lines), "disks": disk_data})
        except Exception as e:
            return json.dumps({"error": str(e)})

    def export_pdf(self, cliente, orden, report_text):
        if not _HAS_RL:
            return json.dumps({"error": "reportlab no está instalado"})
        try:
            win = webview.windows[0]
            result = win.create_file_dialog(
                webview.SAVE_DIALOG,
                directory=os.path.expanduser("~\\Desktop"),
                save_filename=f"Diagnostico_{(cliente or 'PC').replace(' ', '_')}.pdf",
                file_types=("PDF (*.pdf)",),
            )
            if not result:
                return json.dumps({"status": "cancel"})
            path = result[0] if isinstance(result, (list, tuple)) else result
            if not path.lower().endswith(".pdf"):
                path += ".pdf"
            self._build_pdf(path, cliente, orden, report_text)
            return json.dumps({"status": "ok", "path": path})
        except Exception as e:
            return json.dumps({"error": str(e)})

    def _build_pdf(self, path, cliente, orden, report_text):
        doc = SimpleDocTemplate(
            path, pagesize=A4,
            leftMargin=2*cm, rightMargin=2*cm,
            topMargin=2*cm, bottomMargin=2*cm,
        )
        styles = getSampleStyleSheet()
        brand_blue = colors.HexColor("#0039A6")
        silver     = colors.HexColor("#D9E3F0")

        h1 = ParagraphStyle("H1", parent=styles["Heading1"],
                             fontSize=16, textColor=colors.white,
                             backColor=brand_blue, spaceAfter=0,
                             spaceBefore=0, leading=22,
                             leftIndent=8, rightIndent=8)
        body = ParagraphStyle("Body", parent=styles["Normal"],
                              fontSize=8.5, fontName="Courier",
                              textColor=colors.HexColor("#1A1A2E"),
                              leading=13)
        meta = ParagraphStyle("Meta", parent=styles["Normal"],
                              fontSize=9, textColor=colors.HexColor("#444"),
                              spaceAfter=2)

        story = []
        logo_path = resource_path("assets/logo.jpg")
        if os.path.exists(logo_path):
            try:
                logo_img = Image(logo_path, width=3.5*cm, height=1.4*cm, kind="proportional")
                now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
                hdr_data = [[logo_img,
                              Paragraph("PC HOUSE<br/><font size='9' color='#D9E3F0'>Diagnóstico PC v3.1</font>",
                                        ParagraphStyle("HdrTitle", fontSize=14,
                                                       textColor=colors.white,
                                                       fontName="Helvetica-Bold", leading=18)),
                              Paragraph(f"<font size='8'>{now_str}</font>",
                                        ParagraphStyle("HdrDate", fontSize=8,
                                                       textColor=silver, alignment=2))]]
                hdr_tbl = Table(hdr_data, colWidths=[3.8*cm, 10*cm, 3.2*cm])
                hdr_tbl.setStyle(TableStyle([
                    ("BACKGROUND", (0,0), (-1,-1), brand_blue),
                    ("VALIGN",     (0,0), (-1,-1), "MIDDLE"),
                    ("LEFTPADDING",  (0,0), (-1,-1), 8),
                    ("RIGHTPADDING", (0,0), (-1,-1), 8),
                    ("TOPPADDING",   (0,0), (-1,-1), 8),
                    ("BOTTOMPADDING",(0,0), (-1,-1), 8),
                ]))
                story.append(hdr_tbl)
            except Exception:
                story.append(Paragraph("PC HOUSE — Diagnóstico PC", h1))
        else:
            story.append(Paragraph("PC HOUSE — Diagnóstico PC", h1))

        story.append(Spacer(1, 10))
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        story.append(Paragraph(f"<b>Cliente:</b> {cliente or '(no especificado)'}", meta))
        story.append(Paragraph(f"<b>Orden N°:</b> {orden or '(no especificado)'}", meta))
        story.append(Paragraph(f"<b>Fecha:</b> {now_str}", meta))
        story.append(HRFlowable(width="100%", thickness=1, color=brand_blue, spaceAfter=8))

        for line in report_text.split("\n"):
            safe = (line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
            if safe.startswith("─") or safe.startswith("="):
                story.append(Paragraph(f"<b>{safe}</b>",
                                       ParagraphStyle("Sep", parent=body,
                                                      textColor=brand_blue, fontSize=8)))
            else:
                story.append(Paragraph(safe or " ", body))

        story.append(Spacer(1, 12))
        story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#CCC")))
        story.append(Paragraph("Generado por PC HOUSE — Diagnóstico PC v3.1",
                                ParagraphStyle("Footer", parent=styles["Normal"],
                                               fontSize=7, textColor=colors.grey, alignment=1)))
        doc.build(story)

    def generate_visual_report(self):
        try:
            import io, base64 as _b64, tempfile, shutil, math
            from PIL import Image

            now       = datetime.datetime.now()
            fname     = now.strftime("Reporte_PCHouse_%Y-%m-%d_%H-%M.jpg")
            downloads = os.path.join(os.path.expanduser("~"), "Downloads")
            if not os.path.exists(downloads):
                downloads = os.path.join(os.path.expanduser("~"), "Descargas")
            if not os.path.exists(downloads):
                downloads = os.path.expanduser("~")
            os.makedirs(downloads, exist_ok=True)
            out_path = os.path.join(downloads, fname)

            # ── wkhtmltoimage ─────────────────────────────────────────────────
            rel = os.path.join("tools", "wkhtmltopdf", "wkhtmltoimage.exe")
            candidates = []
            if getattr(sys, 'frozen', False):
                candidates.append(os.path.join(os.path.dirname(sys.executable), rel))
                if hasattr(sys, '_MEIPASS'):
                    candidates.append(os.path.join(sys._MEIPASS, rel))
            else:
                candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), rel))
            candidates += [
                r"C:\Program Files\wkhtmltopdf\bin\wkhtmltoimage.exe",
                r"C:\Program Files (x86)\wkhtmltopdf\bin\wkhtmltoimage.exe",
            ]
            exe = next((c for c in candidates if os.path.exists(c)), None)
            if not exe:
                return json.dumps({"error": "No se encontro wkhtmltoimage.exe en tools/wkhtmltopdf/"})

            # ── Datos ─────────────────────────────────────────────────────────
            with self._lock:
                cpu_pct     = self._cpu_value
                cpu_history = list(self._cpu_history)
            cpu_model   = (self._cpu_model or "N/D").strip()
            vm          = psutil.virtual_memory()
            ri          = self._ram_info
            ram_pct     = vm.percent
            username    = os.environ.get("USERNAME", "N/D")
            os_name     = "{} {}".format(platform.system(), platform.release())
            disk_health = (self._disk_health_cache if self._disk_health_cache is not None
                           else self._get_disk_health()) or []
            parts = []
            for _p in psutil.disk_partitions(all=False):
                try:
                    _u = psutil.disk_usage(_p.mountpoint)
                    parts.append({"mp": _p.mountpoint, "pct": _u.percent,
                                  "used": _fb(_u.used), "total": _fb(_u.total)})
                except Exception:
                    pass

            logo_src = ""
            try:
                with open(resource_path("assets/logo.jpg"), "rb") as f:
                    logo_src = "data:image/jpeg;base64," + base64.b64encode(f.read()).decode()
            except Exception:
                pass

            # ── Escalares ─────────────────────────────────────────────────────
            cpu_i   = int(round(cpu_pct))
            ram_i   = int(round(ram_pct))
            ram_ug  = round(vm.used      / (1024 ** 3), 1)
            ram_tg  = round(vm.total     / (1024 ** 3), 1)
            ram_ag  = round(vm.available / (1024 ** 3), 1)
            ram_typ = ri.get("type",  "N/D")
            ram_frq = ri.get("freq",  "N/D")
            ram_slt = ri.get("slots", "N/D")
            fecha_s = now.strftime("%d/%m/%Y  %H:%M")
            mod_s   = cpu_model[:54]

            parts_ltr = {pt["mp"][0].upper(): pt for pt in parts if pt.get("mp")}

            def _dpct(dnum):
                for ltr in {"0": ["C"], "1": ["D","E"], "2": ["E","F"]}.get(str(dnum), ["C"]):
                    if ltr in parts_ltr:
                        return parts_ltr[ltr]["pct"]
                return max((p["pct"] for p in parts), default=0.0)

            def _dusage(dnum):
                for ltr in {"0": ["C"], "1": ["D","E"], "2": ["E","F"]}.get(str(dnum), ["C"]):
                    if ltr in parts_ltr:
                        p = parts_ltr[ltr]
                        return p["used"], p["total"]
                if parts:
                    p = max(parts, key=lambda x: x["pct"])
                    return p["used"], p["total"]
                return "N/D", "N/D"

            # ── Helpers de color ──────────────────────────────────────────────
            def _clr(p):
                return "#EF4444" if p > 90 else ("#F59E0B" if p >= 70 else "#10B981")

            def _lbl(p):
                return "CRITICO" if p > 90 else ("MODERADO" if p >= 70 else "OPTIMO")

            def _badge(lbl, p):
                bg  = "#FEE2E2" if p > 90 else ("#FEF3C7" if p >= 70 else "#D1FAE5")
                clr = "#DC2626"  if p > 90 else ("#D97706"  if p >= 70 else "#059669")
                return (
                    '<span style="background:' + bg + ';color:' + clr + ';'
                    'padding:5px 18px;border-radius:99px;font-size:14px;font-weight:700;">'
                    + lbl + '</span>'
                )

            def _bar(pct, h=20):
                c = _clr(pct)
                return (
                    '<div style="background:#E5E7EB;border-radius:99px;'
                    'overflow:hidden;height:' + str(h) + 'px;">'
                    '<div style="width:' + '{:.0f}'.format(min(pct, 100)) + '%;'
                    'height:100%;background:' + c + ';border-radius:99px;"></div>'
                    '</div>'
                )

            # Encabezado de tarjeta: tabla icono+titulo izq, badge der
            def _card_hdr(icon, title, pct):
                return (
                    '<table width="100%" style="border-collapse:collapse;margin-bottom:16px;"><tr>'
                    '<td style="font-size:15px;font-weight:700;color:#6B7280;'
                    'letter-spacing:1px;text-transform:uppercase;vertical-align:middle;">'
                    + icon + '&nbsp;&nbsp;' + title + '</td>'
                    '<td style="text-align:right;vertical-align:middle;">'
                    + _badge(_lbl(pct), pct) + '</td>'
                    '</tr></table>'
                )

            # ── Estado general ────────────────────────────────────────────────
            # BUENO: CPU<70 Y RAM<80 Y discos ok
            # ATENCION: RAM>=70 O cualquier disco>85 O disco en warning
            # CRITICO: CPU>90 O RAM>85 O disco>95 O disco unhealthy
            max_dpct = max((_dpct(dh.get("disk_num","")) for dh in disk_health), default=0.0)
            any_unhealthy = any(str(dh.get("health","")) == "Unhealthy" for dh in disk_health)
            any_warning   = any(str(dh.get("health","")) == "Warning"   for dh in disk_health)

            if cpu_pct > 90 or ram_pct > 85 or max_dpct > 95 or any_unhealthy:
                estado = "critico"
            elif ram_pct >= 70 or max_dpct > 85 or any_warning:
                estado = "atencion"
            else:
                estado = "bueno"

            if estado == "critico":
                st_bg   = "#FEE2E2"
                st_bdr  = "#EF4444"
                st_cbg  = "#EF4444"  # color del circulo
                st_ctxt = "&#10007;"  # ✗
                st_txt  = "Tu equipo necesita mantenimiento urgente"
                if cpu_pct > 90:
                    st_sub = "El procesador esta funcionando al limite (" + str(cpu_i) + "%)"
                elif ram_pct > 85:
                    st_sub = "La memoria RAM esta casi llena (" + str(ram_i) + "%)"
                elif max_dpct > 95:
                    st_sub = "El disco principal esta casi lleno (" + "{:.0f}".format(max_dpct) + "%)"
                else:
                    st_sub = "Se detecto un disco con fallas"
            elif estado == "atencion":
                st_bg   = "#FEF3C7"
                st_bdr  = "#F59E0B"
                st_cbg  = "#F59E0B"
                st_ctxt = "&#33;"   # !
                st_txt  = "Tu equipo necesita atencion"
                if ram_pct >= 70:
                    st_sub = "La memoria RAM esta siendo muy utilizada (" + str(ram_i) + "%)"
                elif max_dpct > 85:
                    st_sub = "El disco tiene poco espacio libre (" + "{:.0f}".format(max_dpct) + "%)"
                else:
                    st_sub = "Un disco muestra senales de desgaste"
            else:
                st_bg   = "#D1FAE5"
                st_bdr  = "#10B981"
                st_cbg  = "#10B981"
                st_ctxt = "&#10003;"  # ✓
                st_txt  = "Tu equipo esta funcionando bien"
                st_sub  = "No se detectaron problemas criticos en este equipo"

            # ── Recomendaciones ───────────────────────────────────────────────
            recs = []
            if cpu_pct > 90:
                recs.append(("red",
                    "El procesador esta al limite (" + str(cpu_i) + "%). "
                    "Revisa que programas estan usando demasiados recursos "
                    "para evitar lentitud y sobrecalentamiento."))
            elif cpu_pct >= 70:
                recs.append(("yellow",
                    "El procesador tiene carga moderada (" + str(cpu_i) + "%). "
                    "Considera cerrar programas que no estes usando."))
            if ram_pct > 85:
                recs.append(("red",
                    "La memoria RAM esta casi llena (" + str(ram_i) + "%). "
                    "Se recomienda ampliar de " + str(int(ram_tg)) + " GB a "
                    + str(int(ram_tg) * 2) + " GB para mejor rendimiento."))
            elif ram_pct >= 70:
                recs.append(("yellow",
                    "La RAM esta siendo muy utilizada (" + str(ram_i) + "%). "
                    "Cierra aplicaciones en segundo plano para liberar memoria."))
            for dh in disk_health:
                dpct  = _dpct(dh.get("disk_num", ""))
                hlth  = str(dh.get("health", "") or "")
                dname = str(dh.get("name", "Disco") or "Disco")
                if hlth == "Unhealthy":
                    recs.append(("red",
                        "El disco " + dname + " presenta fallas. "
                        "Respalda los datos inmediatamente y reemplaza el disco."))
                elif hlth == "Warning":
                    recs.append(("yellow",
                        "El disco " + dname + " muestra senales de desgaste. "
                        "Haz un respaldo de los datos a la brevedad."))
                if dpct > 90:
                    recs.append(("red",
                        "El disco " + dname + " esta casi lleno ("
                        + "{:.0f}".format(dpct) + "%). "
                        "Libera espacio o amplia el almacenamiento para evitar problemas."))
                elif dpct > 75:
                    recs.append(("yellow",
                        "El disco " + dname + " tiene poco espacio ("
                        + "{:.0f}".format(dpct) + "%). "
                        "Elimina archivos o programas que ya no necesites."))
            if not recs:
                recs.append(("green",
                    "No se detectaron problemas criticos. "
                    "El sistema esta funcionando correctamente."))

            # ── Logos ─────────────────────────────────────────────────────────
            logo_h  = ('<img style="height:70px;border-radius:10px;vertical-align:middle;" src="'
                       + logo_src + '">') if logo_src else ""
            logo_f  = ('<img style="height:38px;border-radius:7px;vertical-align:middle;" src="'
                       + logo_src + '">') if logo_src else ""

            # ═══════════════════════════════════════════════════════════════════
            # SECCION 1 — ENCABEZADO
            # ═══════════════════════════════════════════════════════════════════
            s1 = (
                '<div class="hdr" style="padding:0 28px;">'
                '<table width="100%" style="height:150px;"><tr>'

                '<td style="vertical-align:middle;width:55%;">'
                + logo_h +
                '<span style="display:inline-block;vertical-align:middle;margin-left:14px;">'
                '<div style="font-size:40px;font-weight:800;line-height:1;color:#ffffff !important;">'
                'PC HOUSE</div>'
                '<div style="font-size:18px;margin-top:5px;opacity:0.85;color:#ffffff !important;">'
                'Reporte de Diagn&#243;stico T&#233;cnico</div>'
                '</span></td>'

                '<td style="vertical-align:middle;text-align:right;">'
                '<div style="font-size:36px;font-weight:700;line-height:1;color:#ffffff !important;">'
                + self._hostname + '</div>'
                '<div style="font-size:16px;margin-top:7px;opacity:0.85;color:#ffffff !important;">'
                'Usuario: ' + username + '</div>'
                '<div style="font-size:16px;margin-top:4px;opacity:0.85;color:#ffffff !important;">'
                + os_name + '</div>'
                '<div style="font-size:16px;margin-top:4px;opacity:0.85;color:#ffffff !important;">'
                + fecha_s + '</div>'
                '</td>'

                '</tr></table>'
                '</div>'
            )

            # ═══════════════════════════════════════════════════════════════════
            # SECCION 2 — ESTADO GENERAL
            # ═══════════════════════════════════════════════════════════════════
            s2 = (
                '<div style="background:' + st_bg + ';border:3px solid ' + st_bdr + ';'
                'border-radius:16px;box-shadow:0 2px 16px rgba(0,0,0,0.08);'
                'margin:16px;padding:32px 24px;text-align:center;">'

                # Circulo icono (CSS puro, no emoji)
                '<div style="width:90px;height:90px;border-radius:50%;background:' + st_cbg + ';'
                'margin:0 auto 16px;font-size:48px;font-weight:900;color:#fff;'
                'text-align:center;line-height:90px;">'
                + st_ctxt + '</div>'

                '<div style="font-size:32px;font-weight:800;color:#1F2937;'
                'line-height:1.2;margin-bottom:12px;">'
                + st_txt + '</div>'

                '<div style="font-size:18px;color:#374151;">'
                + st_sub + '</div>'

                '</div>'
            )

            # ═══════════════════════════════════════════════════════════════════
            # SECCION 3 — PROCESADOR (columna unica)
            # ═══════════════════════════════════════════════════════════════════
            cpu_c = _clr(cpu_pct)
            s3 = (
                '<div style="background:#fff;border-radius:16px;'
                'box-shadow:0 2px 16px rgba(0,0,0,0.08);margin:16px;padding:24px;">'

                + _card_hdr("&#128421;", "PROCESADOR", cpu_pct) +

                '<div style="font-size:22px;font-weight:700;color:#1F2937;margin-bottom:20px;">'
                + mod_s + '</div>'

                # Numero grande centrado
                '<div style="text-align:center;margin-bottom:6px;">'
                '<span style="font-size:72px;font-weight:800;color:' + cpu_c + ';line-height:1;">'
                + str(cpu_i) + '%</span>'
                '</div>'
                '<div style="text-align:center;font-size:16px;color:#9CA3AF;margin-bottom:20px;">'
                'de uso actual</div>'

                # Barra horizontal ancho completo
                + _bar(cpu_pct, h=22) +

                '</div>'
            )

            # ═══════════════════════════════════════════════════════════════════
            # SECCION 4 — MEMORIA RAM
            # ═══════════════════════════════════════════════════════════════════
            ram_c = _clr(ram_pct)
            # 3 sub-tarjetas: (900 - 32 - 48 - 2*10) / 3 = 800/3 ~ 266px cada una
            ST = ('display:inline-block;vertical-align:top;background:#F5F8FF;'
                  'border-radius:12px;padding:14px 16px;width:265px;')
            LB = ('font-size:12px;color:#9CA3AF;font-weight:600;letter-spacing:1px;'
                  'text-transform:uppercase;margin-bottom:6px;')
            NB = 'font-size:28px;font-weight:800;color:#1F2937;line-height:1;'
            UB = 'font-size:15px;font-weight:600;color:#9CA3AF;'

            ram_rec = ""
            if ram_pct > 80:
                ram_rec = (
                    '<div style="background:#FEF3C7;border-radius:10px;'
                    'padding:12px 16px;margin-top:14px;font-size:16px;color:#92400E;">'
                    '! Se recomienda ampliar la RAM</div>'
                )

            s4 = (
                '<div style="background:#fff;border-radius:16px;'
                'box-shadow:0 2px 16px rgba(0,0,0,0.08);margin:16px;padding:24px;">'

                + _card_hdr("&#129504;", "MEMORIA RAM", ram_pct) +

                # 3 sub-tarjetas (se permite horizontal dentro de tarjeta)
                '<div style="margin-bottom:16px;font-size:0;">'
                '<div style="' + ST + 'margin-right:10px;">'
                '<div style="' + LB + '">Usada</div>'
                '<div style="' + NB + '">' + str(ram_ug)
                + '<span style="' + UB + '"> GB</span></div>'
                '</div>'
                '<div style="' + ST + 'margin-right:10px;">'
                '<div style="' + LB + '">Disponible</div>'
                '<div style="' + NB + '">' + str(ram_ag)
                + '<span style="' + UB + '"> GB</span></div>'
                '</div>'
                '<div style="' + ST + '">'
                '<div style="' + LB + '">Tipo / Freq.</div>'
                '<div style="font-size:22px;font-weight:800;color:#1F2937;line-height:1;">'
                + str(ram_typ) + '</div>'
                '<div style="font-size:14px;color:#9CA3AF;margin-top:4px;">'
                + str(ram_frq) + ' MHz</div>'
                '</div>'
                '</div>'

                # Barra de progreso con % al costado
                '<table width="100%" style="border-collapse:collapse;"><tr>'
                '<td style="vertical-align:middle;">' + _bar(ram_pct, h=20) + '</td>'
                '<td style="width:64px;text-align:right;vertical-align:middle;'
                'font-size:24px;font-weight:800;color:' + ram_c + ';padding-left:10px;">'
                + str(ram_i) + '%</td>'
                '</tr></table>'

                + ram_rec +
                '</div>'
            )

            # ═══════════════════════════════════════════════════════════════════
            # SECCION 5 — ALMACENAMIENTO
            # ═══════════════════════════════════════════════════════════════════
            def _disk_block(dh):
                dname = str(dh.get("name",    "Disco") or "Disco").strip()
                dtype = str(dh.get("type",    "N/D")   or "N/D")
                dsize = str(dh.get("size",    "N/D")   or "N/D")
                dnum  = dh.get("disk_num", "")
                dhlth = str(dh.get("health",  "")      or "")
                dpct  = _dpct(dnum)
                du, dt = _dusage(dnum)
                bc    = _clr(dpct)

                # Badge tipo
                if "NVME" in dtype.upper():
                    tb, tc = "#EDE9FE", "#5B21B6"
                elif "SSD" in dtype.upper():
                    tb, tc = "#DBEAFE", "#1E40AF"
                else:
                    tb, tc = "#FEF3C7", "#92400E"
                t_badge = (
                    '<span style="background:' + tb + ';color:' + tc + ';'
                    'padding:4px 14px;border-radius:99px;font-size:14px;font-weight:700;'
                    'margin-right:8px;">' + (dtype.upper() if dtype != "N/D" else "DISCO")
                    + '</span>'
                )

                # Badge salud
                if dhlth == "Unhealthy":
                    hb, hc, ht = "#FEE2E2", "#DC2626", "FALLAS"
                elif dhlth == "Warning":
                    hb, hc, ht = "#FEF3C7", "#D97706", "ATENCION"
                else:
                    hb, hc, ht = "#D1FAE5", "#059669", "SALUDABLE"
                h_badge = (
                    '<span style="background:' + hb + ';color:' + hc + ';'
                    'padding:4px 14px;border-radius:99px;font-size:14px;font-weight:700;">'
                    + ht + '</span>'
                )

                # Caja de recomendacion
                if dhlth == "Unhealthy":
                    drec = ('<div style="background:#FEE2E2;border-radius:10px;'
                            'padding:12px 16px;margin-top:10px;font-size:16px;color:#991B1B;">'
                            '! Respaldá los datos y reemplazá este disco</div>')
                elif dhlth == "Warning":
                    drec = ('<div style="background:#FEF3C7;border-radius:10px;'
                            'padding:12px 16px;margin-top:10px;font-size:16px;color:#92400E;">'
                            '! Hacé un respaldo de los datos de este disco</div>')
                elif dpct > 90:
                    drec = ('<div style="background:#FEE2E2;border-radius:10px;'
                            'padding:12px 16px;margin-top:10px;font-size:16px;color:#991B1B;">'
                            '! Disco casi lleno &mdash; liberá espacio urgente</div>')
                elif dpct > 75:
                    drec = ('<div style="background:#FEF3C7;border-radius:10px;'
                            'padding:12px 16px;margin-top:10px;font-size:16px;color:#92400E;">'
                            '! Poco espacio libre &mdash; consider&#225; liberar archivos</div>')
                else:
                    drec = ('<div style="background:#D1FAE5;border-radius:10px;'
                            'padding:12px 16px;margin-top:10px;font-size:16px;color:#065F46;">'
                            'Disco en buen estado</div>')

                return (
                    '<div style="background:#F8FAFF;border-radius:12px;'
                    'padding:18px 20px;margin-bottom:16px;">'

                    '<table width="100%" style="border-collapse:collapse;margin-bottom:12px;"><tr>'
                    '<td style="font-size:18px;font-weight:700;color:#1F2937;vertical-align:middle;">'
                    + dname + '</td>'
                    '<td style="text-align:right;vertical-align:middle;">'
                    + t_badge + h_badge + '</td>'
                    '</tr></table>'

                    '<div style="font-size:36px;font-weight:800;color:#1F2937;margin-bottom:12px;">'
                    + dsize + '</div>'

                    + _bar(dpct, h=24) +

                    '<table width="100%" style="border-collapse:collapse;margin-top:8px;"><tr>'
                    '<td style="font-size:16px;color:#6B7280;">'
                    + str(du) + ' usados de ' + str(dt) + ' ('
                    + '{:.0f}'.format(dpct) + '%)</td>'
                    '<td style="text-align:right;font-size:24px;font-weight:800;color:'
                    + bc + ';">' + '{:.0f}'.format(dpct) + '%</td>'
                    '</tr></table>'

                    + drec +
                    '</div>'
                )

            disk_ov  = max((_dpct(dh.get("disk_num","")) for dh in disk_health), default=0.0)
            s5 = ""
            if disk_health:
                s5 = (
                    '<div style="background:#fff;border-radius:16px;'
                    'box-shadow:0 2px 16px rgba(0,0,0,0.08);margin:16px;padding:24px;">'
                    + _card_hdr("&#128190;", "ALMACENAMIENTO", disk_ov)
                    + "".join(_disk_block(dh) for dh in disk_health[:3])
                    + '</div>'
                )

            # ═══════════════════════════════════════════════════════════════════
            # SECCION 6 — RECOMENDACIONES
            # ═══════════════════════════════════════════════════════════════════
            def _rec(level, text):
                if level == "red":
                    rb, rd, ri2 = "#FEE2E2", "#FCA5A5", "!"
                elif level == "yellow":
                    rb, rd, ri2 = "#FEF3C7", "#FCD34D", "!"
                else:
                    rb, rd, ri2 = "#D1FAE5", "#6EE7B7", "OK"
                return (
                    '<div style="background:' + rb + ';border:1.5px solid ' + rd + ';'
                    'border-radius:12px;padding:18px 20px;margin-bottom:12px;">'
                    '<span style="font-size:20px;font-weight:900;vertical-align:middle;'
                    'margin-right:10px;color:#1F2937;">[' + ri2 + ']</span>'
                    '<span style="font-size:17px;color:#1F2937;vertical-align:middle;'
                    'line-height:1.5;">' + text + '</span>'
                    '</div>'
                )

            s6 = (
                '<div style="background:#fff;border-radius:16px;'
                'box-shadow:0 2px 16px rgba(0,0,0,0.08);margin:16px;padding:24px;">'
                '<div style="font-size:16px;font-weight:700;color:#6B7280;letter-spacing:1px;'
                'text-transform:uppercase;margin-bottom:16px;">RECOMENDACIONES</div>'
                + "".join(_rec(lvl, txt) for lvl, txt in recs)
                + '</div>'
            )

            # ═══════════════════════════════════════════════════════════════════
            # PIE DE PAGINA
            # ═══════════════════════════════════════════════════════════════════
            footer = (
                '<div style="background:#0039A6;padding:0 28px;margin-top:8px;">'
                '<table width="100%" style="height:80px;border-collapse:collapse;"><tr>'
                '<td style="vertical-align:middle;">'
                + logo_f +
                '<span style="color:#fff;font-size:20px;font-weight:700;'
                'vertical-align:middle;margin-left:12px;">'
                'Diagn&#243;stico por PC House</span>'
                '</td>'
                '<td style="vertical-align:middle;text-align:right;'
                'color:rgba(255,255,255,.75);font-size:14px;">'
                + fecha_s + '</td>'
                '</tr></table>'
                '</div>'
            )

            # ═══════════════════════════════════════════════════════════════════
            # HTML FINAL
            # ═══════════════════════════════════════════════════════════════════
            css = (
                "* { margin:0; padding:0; box-sizing:border-box; }\n"
                "html { width:900px; }\n"
                "body { width:900px; background:#F0F4F8; "
                "font-family:'Segoe UI',-apple-system,Arial,sans-serif; }\n"
                ".hdr {\n"
                "  background-color: #0039A6 !important;\n"
                "  background-image: -webkit-linear-gradient(315deg, #0039A6 0%, #00B4D8 100%) !important;\n"
                "  background-image: linear-gradient(135deg, #0039A6 0%, #00B4D8 100%) !important;\n"
                "  color: #ffffff !important;\n"
                "}\n"
                ".hdr td, .hdr div, .hdr span { color: #ffffff !important; }\n"
                ".hdr table { border-collapse: collapse; }\n"
            )
            html = (
                '<!DOCTYPE html><html lang="es"><head>'
                '<meta charset="UTF-8">'
                '<style>' + css + '</style>'
                '</head><body>'
                + s1 + s2 + s3 + s4 + s5 + s6 + footer
                + '<div style="height:20px;"></div>'
                '</body></html>'
            )

            # ── Debug HTML ────────────────────────────────────────────────────
            _proj = (os.path.dirname(sys.executable)
                     if getattr(sys, 'frozen', False)
                     else os.path.dirname(os.path.abspath(__file__)))
            _dbg  = os.path.join(_proj, "reporte_test.html")
            with open(_dbg, "w", encoding="utf-8") as fh:
                fh.write(html)

            tmp_dir   = tempfile.mkdtemp()
            html_path = os.path.join(tmp_dir, "report.html")
            jpg_path  = os.path.join(tmp_dir, "report.jpg")
            shutil.copy2(_dbg, html_path)

            result = subprocess.run(
                [exe,
                 "--width",               "900",
                 "--disable-smart-width",
                 "--zoom",                "1.0",
                 "--quality",             "95",
                 "--encoding",            "UTF-8",
                 "--enable-local-file-access",
                 "--javascript-delay",    "300",
                 html_path, jpg_path],
                capture_output=True, text=True, timeout=40, **_NWIN,
            )
            if not os.path.exists(jpg_path):
                shutil.rmtree(tmp_dir, ignore_errors=True)
                return json.dumps({"error": (
                    "wkhtmltoimage no genero imagen. "
                    "HTML: " + _dbg + " | stderr: "
                    + (result.stderr or "").strip()[:300]
                )})

            shutil.copy2(jpg_path, out_path)

            img_pil = Image.open(jpg_path)
            prev    = img_pil.copy()
            prev.thumbnail((450, 900), Image.LANCZOS)
            buf = io.BytesIO()
            prev.save(buf, "JPEG", quality=78)
            b64 = _b64.b64encode(buf.getvalue()).decode()

            shutil.rmtree(tmp_dir, ignore_errors=True)
            return json.dumps({"status": "ok", "path": out_path, "preview_b64": b64})

        except Exception as e:
            import traceback
            return json.dumps({"error": str(e), "trace": traceback.format_exc()})
    def open_report_folder(self, path):
        try:
            os.startfile(path)
            return json.dumps({"status": "ok"})
        except Exception as e:
            return json.dumps({"error": str(e)})

    def get_temperatures(self):
        try:
            import os as _os, sys as _sys

            if getattr(_sys, 'frozen', False):
                base_path = _sys._MEIPASS
            else:
                base_path = _os.path.dirname(_os.path.abspath(__file__))
            lhm_dir = _os.path.join(base_path, 'tools', 'LibreHardwareMonitor')

            if not _os.path.isdir(lhm_dir):
                return json.dumps({"error": "Carpeta tools/LibreHardwareMonitor no encontrada"})

            # Keep Computer object alive on self to avoid re-init overhead every 2 s
            if not getattr(self, '_lhm_computer', None):
                import clr as _clr
                if lhm_dir not in _sys.path:
                    _sys.path.append(lhm_dir)
                _clr.AddReference('LibreHardwareMonitorLib')
                from LibreHardwareMonitor.Hardware import Computer as _Computer
                comp = _Computer()
                comp.IsCpuEnabled = True
                comp.IsGpuEnabled = True
                comp.Open()
                self._lhm_computer = comp

            cpu_max_val = None
            cpu_avg_val = None
            cpu_cores   = []
            gpu_sensors = []

            for hw in self._lhm_computer.Hardware:
                hw.Update()
                for s in hw.Sensors:
                    if str(s.SensorType) != 'Temperature':
                        continue
                    try:
                        val = round(float(s.Value), 1)
                    except (TypeError, Exception):
                        continue
                    name = str(s.Name)
                    if name == "Core Max":
                        cpu_max_val = val
                    elif name == "Core Average":
                        cpu_avg_val = val
                    elif "CPU Core #" in name and "Distance" not in name:
                        cpu_cores.append({"name": name, "value": val})
                    elif "GPU" in name:
                        gpu_sensors.append({"name": name, "value": val})

            cpu_data = None
            if cpu_max_val is not None or cpu_cores:
                if cpu_max_val is None and cpu_cores:
                    cpu_max_val = max(c["value"] for c in cpu_cores)
                if cpu_avg_val is None and cpu_cores:
                    cpu_avg_val = round(sum(c["value"] for c in cpu_cores) / len(cpu_cores), 1)
                cpu_data = {"max": cpu_max_val, "avg": cpu_avg_val, "cores": cpu_cores}

            return json.dumps({
                "starting": False,
                "cpu": cpu_data,
                "gpu": gpu_sensors if gpu_sensors else None,
            })
        except Exception as e:
            self._lhm_computer = None  # reset so next call retries init
            import traceback
            return json.dumps({"error": str(e), "trace": traceback.format_exc()})

    # ── Pulso de Red ──────────────────────────────────────────────────────────
    def get_net_info(self):
        try:
            import socket as _sk, subprocess as _sp, json as _j, re as _re
            import sys as _sys
            _nw = {"creationflags": 0x08000000} if _sys.platform == "win32" else {}
            result = {}

            # IP local
            try:
                s = _sk.socket(_sk.AF_INET, _sk.SOCK_DGRAM)
                s.settimeout(2)
                s.connect(("8.8.8.8", 80))
                result['local_ip'] = s.getsockname()[0]
                s.close()
            except Exception:
                result['local_ip'] = "No disponible"

            # Estado de conexión
            try:
                _sk.create_connection(("8.8.8.8", 53), timeout=2).close()
                result['connected'] = True
            except Exception:
                result['connected'] = False

            # IP pública
            if result['connected']:
                try:
                    import urllib.request as _ur
                    result['public_ip'] = _ur.urlopen(
                        'https://api.ipify.org', timeout=5).read().decode()
                except Exception:
                    result['public_ip'] = "Sin acceso"
            else:
                result['public_ip'] = "Sin conexión"

            # Red / SSID
            try:
                p = _sp.run(['netsh', 'wlan', 'show', 'interfaces'],
                            capture_output=True, text=True, timeout=5, **_nw)
                ssid = None
                for line in p.stdout.splitlines():
                    stripped = line.strip()
                    if stripped.startswith('SSID') and 'BSSID' not in stripped:
                        ssid = stripped.split(':', 1)[-1].strip()
                        break
                result['network'] = ssid if ssid else 'Ethernet'
            except Exception:
                result['network'] = 'Ethernet'

            # DNS
            try:
                p = _sp.run(['ipconfig', '/all'],
                            capture_output=True, text=True, timeout=5, **_nw)
                dns_found = _re.findall(
                    r'DNS[^\n:]*:\s*([\d]{1,3}\.[\d]{1,3}\.[\d]{1,3}\.[\d]{1,3})',
                    p.stdout, _re.IGNORECASE)
                valid = [ip for ip in dns_found if ip and not ip.startswith('0.')]
                result['dns_primary']   = valid[0] if len(valid) > 0 else 'N/A'
                result['dns_secondary'] = valid[1] if len(valid) > 1 else 'N/A'
            except Exception:
                result['dns_primary']   = 'N/A'
                result['dns_secondary'] = 'N/A'

            return _j.dumps(result)
        except Exception as e:
            import json as _j2
            return _j2.dumps({"error": str(e)})

    def start_net_test(self):
        import threading as _thr
        self._net_cancel = False
        if getattr(self, '_net_thread', None) and self._net_thread.is_alive():
            return json.dumps({"error": "Test ya en curso"})
        self._net_thread = _thr.Thread(target=self._net_test_worker, daemon=True)
        self._net_thread.start()
        return json.dumps({"ok": True})

    def cancel_net_test(self):
        self._net_cancel = True
        return json.dumps({"ok": True})

    # ── Limpiar Sistema ───────────────────────────────────────────────────
    def analyze_cleanup(self):
        try:
            result = {}

            temp_user = os.environ.get("TEMP", "")
            size = 0
            if temp_user and os.path.exists(temp_user):
                for dp, dn, fn in os.walk(temp_user):
                    for f in fn:
                        try: size += os.path.getsize(os.path.join(dp, f))
                        except Exception: pass
            result["temp_user"] = size

            size = 0
            win_temp = r"C:\Windows\Temp"
            if os.path.exists(win_temp):
                for dp, dn, fn in os.walk(win_temp):
                    for f in fn:
                        try: size += os.path.getsize(os.path.join(dp, f))
                        except Exception: pass
            result["temp_win"] = size

            size = 0
            try:
                r = subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                     "$shell = New-Object -ComObject Shell.Application; "
                     "$rb = $shell.Namespace(0xA); "
                     "($rb.Items() | Measure-Object -Property Size -Sum).Sum"],
                    capture_output=True, text=True, timeout=10, **_NWIN
                )
                if r.returncode == 0 and r.stdout.strip():
                    size = int(float(r.stdout.strip()))
            except Exception:
                pass
            result["recycle"] = size

            result["total"] = result["temp_user"] + result["temp_win"] + result["recycle"]
            return json.dumps(result)
        except Exception as e:
            return json.dumps({"error": str(e), "temp_user": 0, "temp_win": 0, "recycle": 0, "total": 0})

    def run_cleanup(self):
        import os as _os, shutil as _sh
        freed = 0

        def _del_dir(path):
            total = 0
            try:
                for item in _os.listdir(path):
                    ip = _os.path.join(path, item)
                    try:
                        if _os.path.isfile(ip):
                            total += _os.path.getsize(ip)
                            _os.remove(ip)
                        elif _os.path.isdir(ip):
                            for dp, dn, fn in _os.walk(ip):
                                for f in fn:
                                    try:
                                        total += _os.path.getsize(_os.path.join(dp, f))
                                    except Exception:
                                        pass
                            _sh.rmtree(ip, ignore_errors=True)
                    except Exception:
                        pass
            except Exception:
                pass
            return total

        # 1. %TEMP% del usuario
        temp_user = _os.environ.get("TEMP", "")
        if temp_user and _os.path.exists(temp_user):
            freed += _del_dir(temp_user)

        # 2. C:\Windows\Temp
        win_temp = r"C:\Windows\Temp"
        if _os.path.exists(win_temp):
            freed += _del_dir(win_temp)

        # 3. Papelera de reciclaje
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 "Clear-RecycleBin -Force -ErrorAction SilentlyContinue"],
                capture_output=True, timeout=30, **_NWIN
            )
        except Exception:
            pass

        return json.dumps({"freed": freed, "freed_fmt": _fb(freed)})

    def get_system_info(self):
        try:
            result = {}

            # Placa madre
            r = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 "Get-WmiObject Win32_BaseBoard | Select-Object Manufacturer,Product | ConvertTo-Json -Compress"],
                capture_output=True, text=True, timeout=10, **_NWIN
            )
            if r.returncode == 0 and r.stdout.strip():
                result["motherboard"] = json.loads(r.stdout.strip())

            # BIOS
            r = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 "Get-WmiObject Win32_BIOS | Select-Object SMBIOSBIOSVersion,ReleaseDate | ConvertTo-Json -Compress"],
                capture_output=True, text=True, timeout=10, **_NWIN
            )
            if r.returncode == 0 and r.stdout.strip():
                result["bios"] = json.loads(r.stdout.strip())

            # GPU
            r = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 "Get-WmiObject Win32_VideoController | Select-Object Name,AdapterRAM,CurrentHorizontalResolution,CurrentVerticalResolution | ConvertTo-Json -Compress"],
                capture_output=True, text=True, timeout=10, **_NWIN
            )
            if r.returncode == 0 and r.stdout.strip():
                raw = json.loads(r.stdout.strip())
                result["gpu"] = [raw] if isinstance(raw, dict) else raw

            # SO
            r = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 "Get-WmiObject Win32_OperatingSystem | Select-Object Caption,BuildNumber,Version,OSArchitecture | ConvertTo-Json -Compress"],
                capture_output=True, text=True, timeout=10, **_NWIN
            )
            if r.returncode == 0 and r.stdout.strip():
                result["os"] = json.loads(r.stdout.strip())

            # Activación Windows
            try:
                r = subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                     "$p = Get-WmiObject SoftwareLicensingProduct | Where-Object {$_.Name -like '*Windows*' -and $_.PartialProductKey}; if($p) { $p[0].LicenseStatus } else { 0 }"],
                    capture_output=True, text=True, timeout=10, **_NWIN
                )
                status = r.stdout.strip()
                result["activation"] = "Activado ✅" if status == "1" else "No activado ❌"
            except Exception:
                result["activation"] = "N/D"

            return json.dumps(result)
        except Exception as e:
            return json.dumps({"error": str(e)})

    def _analyze_cleanup_worker(self):
        import json as _j, os as _os, subprocess as _sp, sys as _sys
        _nw = {"creationflags": 0x08000000} if _sys.platform == "win32" else {}

        def push(js):
            try:
                import webview as _wv
                _wv.windows[0].evaluate_js(js)
            except Exception:
                pass

        def dir_size(path):
            total = 0
            try:
                for root, dirs, files in _os.walk(path):
                    for f in files:
                        try:
                            total += _os.path.getsize(_os.path.join(root, f))
                        except Exception:
                            pass
            except Exception:
                pass
            return total

        def fmt(b):
            if b >= 1_073_741_824: return f"{b/1_073_741_824:.1f} GB"
            if b >= 1_048_576:     return f"{b/1_048_576:.0f} MB"
            if b >= 1024:          return f"{b/1024:.0f} KB"
            return f"{b} B"

        try:
            results = []

            temp_user = _os.environ.get('TEMP', _os.path.join(
                _os.environ.get('USERPROFILE', 'C:\\Users\\Default'), 'AppData', 'Local', 'Temp'))
            sz = dir_size(temp_user)
            results.append({'key': 'temp_user', 'label': 'Temporales del usuario (%TEMP%)',
                            'icon': '\U0001f5c2', 'bytes': sz, 'size': fmt(sz)})
            push('cleanOnCategoryResult(' + _j.dumps(results[-1]) + ')')

            sz = dir_size(r'C:\Windows\Temp')
            results.append({'key': 'temp_win', 'label': 'Temporales de Windows',
                            'icon': '\U0001f5c2', 'bytes': sz, 'size': fmt(sz)})
            push('cleanOnCategoryResult(' + _j.dumps(results[-1]) + ')')

            recycle_bytes = 0
            try:
                ps = _sp.run(
                    ['powershell', '-NoProfile', '-NonInteractive', '-Command',
                     "(New-Object -ComObject Shell.Application).Namespace(0xA).Items() | "
                     "Measure-Object -Property Size -Sum | Select-Object -ExpandProperty Sum"],
                    capture_output=True, text=True, timeout=15, **_nw)
                val = ps.stdout.strip()
                if val and val.replace('.', '', 1).isdigit():
                    recycle_bytes = int(float(val))
            except Exception:
                pass
            results.append({'key': 'recycle', 'label': 'Papelera de reciclaje',
                            'icon': '\U0001f5d1', 'bytes': recycle_bytes, 'size': fmt(recycle_bytes)})
            push('cleanOnCategoryResult(' + _j.dumps(results[-1]) + ')')

            sz = dir_size(r'C:\Windows\SoftwareDistribution\Download')
            results.append({'key': 'wupdate', 'label': 'Caché Windows Update',
                            'icon': '\U0001f504', 'bytes': sz, 'size': fmt(sz)})
            push('cleanOnCategoryResult(' + _j.dumps(results[-1]) + ')')

            total = sum(r['bytes'] for r in results)
            push('cleanOnAnalysisDone(' + _j.dumps(
                {'total_bytes': total, 'total': fmt(total), 'categories': results}) + ')')

        except Exception as e:
            push('cleanOnAnalysisDone(' + _j.dumps({'error': str(e)}) + ')')

    def _run_cleanup_worker(self):
        import json as _j, os as _os, shutil as _sh, subprocess as _sp, sys as _sys, time as _t
        _nw = {"creationflags": 0x08000000} if _sys.platform == "win32" else {}

        def push(js):
            try:
                import webview as _wv
                _wv.windows[0].evaluate_js(js)
            except Exception:
                pass

        def progress(pct, label):
            push('cleanUpdateProgress(' + str(pct) + ',' + _j.dumps(label) + ')')

        def on_step(data):
            push('cleanOnStep(' + _j.dumps(data) + ')')

        def fmt(b):
            if b >= 1_073_741_824: return f"{b/1_073_741_824:.1f} GB"
            if b >= 1_048_576:     return f"{b/1_048_576:.0f} MB"
            if b >= 1024:          return f"{b/1024:.0f} KB"
            return f"{b} B"

        def del_dir_contents(path):
            freed = 0
            try:
                for name in _os.listdir(path):
                    fp = _os.path.join(path, name)
                    try:
                        if _os.path.isdir(fp):
                            for root, dirs, files in _os.walk(fp):
                                for f in files:
                                    try: freed += _os.path.getsize(_os.path.join(root, f))
                                    except Exception: pass
                            _sh.rmtree(fp, ignore_errors=True)
                        else:
                            freed += _os.path.getsize(fp)
                            _os.remove(fp)
                    except Exception:
                        pass
            except Exception:
                pass
            return freed

        total_freed = 0
        try:
            # Temporales usuario
            progress(5, 'Limpiando temporales del usuario...')
            temp_user = _os.environ.get('TEMP', '')
            f1 = del_dir_contents(temp_user) if temp_user else 0
            total_freed += f1
            on_step({'label': 'Temporales del usuario', 'freed': fmt(f1)})
            progress(25, 'Limpiando temporales de Windows...')

            # Temporales Windows
            f2 = del_dir_contents(r'C:\Windows\Temp')
            total_freed += f2
            on_step({'label': 'Temporales de Windows', 'freed': fmt(f2)})
            progress(50, 'Vaciando papelera...')

            # Papelera
            try:
                _sp.run(['powershell', '-NoProfile', '-NonInteractive', '-Command',
                         'Clear-RecycleBin -Force -ErrorAction SilentlyContinue'],
                        capture_output=True, timeout=30, **_nw)
                on_step({'label': 'Papelera de reciclaje', 'freed': 'Vaciada'})
            except Exception:
                on_step({'label': 'Papelera de reciclaje', 'freed': 'Omitida'})
            progress(70, 'Limpiando caché Windows Update...')

            # Windows Update
            try:
                _sp.run(['net', 'stop', 'wuauserv'], capture_output=True, timeout=20, **_nw)
                _t.sleep(1)
                f4 = del_dir_contents(r'C:\Windows\SoftwareDistribution\Download')
                total_freed += f4
                _sp.run(['net', 'start', 'wuauserv'], capture_output=True, timeout=20, **_nw)
                on_step({'label': 'Caché Windows Update', 'freed': fmt(f4)})
            except Exception:
                on_step({'label': 'Caché Windows Update', 'freed': 'Omitida'})
            progress(100, 'Completado')

            push('cleanOnDone(' + _j.dumps({'total': fmt(total_freed), 'total_bytes': total_freed}) + ')')

        except Exception as e:
            push('cleanOnDone(' + _j.dumps({'error': str(e)}) + ')')

    def _net_test_worker(self):
        import subprocess as _sp, json as _j, re as _re, time as _t, os as _os
        import sys as _sys
        _nw = {"creationflags": 0x08000000} if _sys.platform == "win32" else {}

        def push(js):
            try:
                import webview as _wv
                _wv.windows[0].evaluate_js(js)
            except Exception:
                pass

        def progress(pct, label):
            push('netUpdateProgress(' + str(pct) + ',' + _j.dumps(label) + ')')

        def on_step(data):
            push('netOnStep(' + _j.dumps(data) + ')')

        all_times = []
        res = {}
        try:
            # ── PASO 1: PING ──
            progress(5, 'Midiendo latencia...')
            servers = [('8.8.8.8', 'Google'), ('1.1.1.1', 'Cloudflare'), ('208.67.222.222', 'OpenDNS')]
            srv_res = []
            for ip, name in servers:
                if getattr(self, '_net_cancel', False):
                    return
                try:
                    p = _sp.run(['ping', '-n', '4', ip],
                                capture_output=True, text=True, timeout=20, **_nw)
                    times = [int(m) for m in _re.findall(
                        r'(?:tiempo|time)[=<](\d+)\s*ms', p.stdout, _re.IGNORECASE)]
                    if times:
                        avg = round(sum(times) / len(times))
                        all_times.extend(times)
                        srv_res.append({'server': name, 'ip': ip, 'ms': avg, 'ok': True})
                    else:
                        srv_res.append({'server': name, 'ip': ip, 'ms': None, 'ok': False})
                except Exception:
                    srv_res.append({'server': name, 'ip': ip, 'ms': None, 'ok': False})

            ok_pings = [r['ms'] for r in srv_res if r['ms'] is not None]
            avg_ping = round(sum(ok_pings) / len(ok_pings)) if ok_pings else None
            jitter   = (max(all_times) - min(all_times)) if len(all_times) >= 2 else 0
            res['ping_avg'] = avg_ping
            res['jitter']   = jitter
            on_step({'type': 'ping', 'servers': srv_res, 'avg_ms': avg_ping, 'jitter_ms': jitter})
            progress(30, 'Midiendo velocidad de descarga...')

            # ── PASO 2: DESCARGA ──
            if getattr(self, '_net_cancel', False):
                return
            import urllib.request as _ur, ssl as _ssl
            _ctx = _ssl.create_default_context()
            _ctx.check_hostname = False
            _ctx.verify_mode = _ssl.CERT_NONE
            dl = None
            dl_error = None
            _dl_urls = [
                'https://speed.cloudflare.com/__down?bytes=10000000',
                'http://ipv4.download.thinkbroadband.com/10MB.zip',
                'https://proof.ovh.net/files/10Mb.dat',
            ]
            for _url in _dl_urls:
                if getattr(self, '_net_cancel', False):
                    return
                try:
                    t0 = _t.time()
                    with _ur.urlopen(_url, timeout=30, context=_ctx if _url.startswith('https') else None) as resp:
                        datos = resp.read()
                    fin = _t.time()
                    segundos = fin - t0
                    if segundos > 0 and len(datos) > 0:
                        dl = round((len(datos) * 8) / (segundos * 1_000_000), 1)
                        dl_error = None
                        break
                except Exception as _e:
                    dl_error = str(_e)
                    continue
            res['download_mbps'] = dl
            on_step({'type': 'download', 'mbps': dl, 'error': dl_error})
            progress(65, 'Midiendo velocidad de subida...')

            # ── PASO 3: SUBIDA ──
            if getattr(self, '_net_cancel', False):
                return
            ul = None
            ul_error = None
            _ul_urls = [
                'https://speed.cloudflare.com/__up',
                'https://httpbin.org/post',
            ]
            datos_subida = _os.urandom(5 * 1024 * 1024)  # 5MB random
            for _url in _ul_urls:
                if getattr(self, '_net_cancel', False):
                    return
                try:
                    req = _ur.Request(_url, data=datos_subida, method='POST')
                    req.add_header('Content-Type', 'application/octet-stream')
                    t0 = _t.time()
                    with _ur.urlopen(req, timeout=30, context=_ctx if _url.startswith('https') else None) as resp:
                        resp.read()
                    fin = _t.time()
                    segundos = fin - t0
                    if segundos > 0:
                        ul = round((len(datos_subida) * 8) / (segundos * 1_000_000), 1)
                        ul_error = None
                        break
                except Exception as _e:
                    ul_error = str(_e)
                    continue
            res['upload_mbps'] = ul
            on_step({'type': 'upload', 'mbps': ul, 'error': ul_error})
            progress(90, 'Calculando jitter...')

            # ── PASO 4: JITTER (ya calculado del ping) ──
            on_step({'type': 'jitter', 'ms': jitter})
            progress(100, 'Completado')

            # Calidad — solo con datos reales disponibles
            data_points = 0
            score = 0
            if dl is not None:
                data_points += 1
                if dl > 50: score += 3
                elif dl > 20: score += 2
                elif dl > 5:  score += 1
            if avg_ping is not None:
                data_points += 1
                if avg_ping < 20: score += 2
                elif avg_ping < 50: score += 1
            if data_points == 0:
                quality = 'slow'
            elif score >= 4:
                quality = 'excellent'
            elif score >= 2:
                quality = 'good'
            else:
                quality = 'slow'
            if jitter < 5: score += 1  # bonus no penaliza, solo mejora

            push('netOnDone(' + _j.dumps({**res, 'quality': quality}) + ')')

        except Exception as e:
            push('netOnDone(' + _j.dumps({'error': str(e)}) + ')')

    @staticmethod
    def _get_programas():
        if not _HAS_WINREG:
            return []
        keys = [
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
            r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
        ]
        progs = []
        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for key_path in keys:
                try:
                    with winreg.OpenKey(root, key_path) as k:
                        for i in range(winreg.QueryInfoKey(k)[0]):
                            try:
                                with winreg.OpenKey(k, winreg.EnumKey(k, i)) as sk:
                                    name = winreg.QueryValueEx(sk, "DisplayName")[0]
                                    ver = ""
                                    try:
                                        ver = winreg.QueryValueEx(sk, "DisplayVersion")[0]
                                    except OSError:
                                        pass
                                    entry = name + (f"  v{ver}" if ver else "")
                                    if entry not in progs:
                                        progs.append(entry)
                            except OSError:
                                pass
                except OSError:
                    pass
        return sorted(progs, key=str.lower)

    @staticmethod
    def _get_disk_health():
        # Attempt 1: Get-PhysicalDisk (most info, but fails on some systems)
        try:
            cmd = (
                "Get-PhysicalDisk | Select-Object FriendlyName,MediaType,HealthStatus,Size,DeviceId"
                " | ConvertTo-Json -Compress"
            )
            r = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                capture_output=True, text=True, timeout=12, **_NWIN,
            )
            if r.returncode == 0 and r.stdout.strip():
                raw = json.loads(r.stdout.strip())
                if isinstance(raw, dict):
                    raw = [raw]
                mtype_map  = {"3": "HDD", "4": "SSD", "0": "N/D", "Unspecified": "N/D"}
                health_map = {"0": "Healthy", "1": "Warning", "2": "Unhealthy"}
                result = []
                for d in raw:
                    health   = str(d.get("HealthStatus", "") or "").strip()
                    mtype    = str(d.get("MediaType",    "") or "").strip()
                    name     = str(d.get("FriendlyName", "Disco") or "Disco").strip()
                    size_b   = d.get("Size") or 0
                    disk_num = str(d.get("DeviceId", "") or "").strip()
                    health = health_map.get(health, health)
                    mtype  = mtype_map.get(mtype, mtype) or "N/D"
                    result.append({
                        "name":     name,
                        "type":     mtype,
                        "health":   health,
                        "size":     _fb(int(size_b)) if size_b else "N/D",
                        "disk_num": disk_num,
                    })
                if result:
                    return result
        except Exception:
            pass
        # Attempt 2: Get-Disk + Get-Partition (more compatible, Windows 8+)
        result = Api._try_get_disk_fallback()
        if result:
            return result
        # Attempt 3: psutil (always works if drives are mounted)
        return Api._psutil_disk_fallback()

    @staticmethod
    def _try_get_disk_fallback():
        try:
            cmd = (
                "$d=Get-Disk|Select-Object Number,MediaType,HealthStatus,Size;"
                "$p=Get-Partition|Where-Object{$_.DriveLetter -match '[A-Z]'}"
                "|Select-Object DriveLetter,DiskNumber;"
                "[PSCustomObject]@{disks=$d;partitions=$p}|ConvertTo-Json -Depth 4 -Compress"
            )
            r = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                capture_output=True, text=True, timeout=15, **_NWIN,
            )
            if r.returncode != 0 or not r.stdout.strip():
                return []
            raw = json.loads(r.stdout.strip())
            disks_raw = raw.get("disks", []) or []
            parts_raw = raw.get("partitions", []) or []
            if isinstance(disks_raw, dict): disks_raw = [disks_raw]
            if isinstance(parts_raw, dict): parts_raw = [parts_raw]
            num_to_letters = {}
            for p in parts_raw:
                dl = str(p.get("DriveLetter") or "").strip()
                dn = str(p.get("DiskNumber")  or "").strip()
                if dl and dn:
                    num_to_letters.setdefault(dn, []).append(dl)
            mtype_map  = {"0": "Disco", "3": "HDD", "4": "SSD",
                          "Unspecified": "Disco", "HDD": "HDD", "SSD": "SSD"}
            health_map = {"0": "Healthy", "1": "Warning", "2": "Unhealthy",
                          "Healthy": "Healthy", "Warning": "Warning", "Unhealthy": "Unhealthy"}
            result = []
            for d in disks_raw:
                mtype    = str(d.get("MediaType",    "") or "").strip()
                health   = str(d.get("HealthStatus", "") or "").strip()
                size_b   = d.get("Size") or 0
                num      = str(d.get("Number", "") or "").strip()
                letters  = num_to_letters.get(num, [])
                name     = ("Disco " + " / ".join(f"{l}:" for l in letters)
                            if letters else f"Disco {num}")
                mtype    = mtype_map.get(mtype, mtype) or "Disco"
                health   = health_map.get(health, health) or "Healthy"
                result.append({
                    "name":     name,
                    "type":     mtype,
                    "health":   health,
                    "size":     _fb(int(size_b)) if size_b else "N/D",
                    "disk_num": num,
                })
            return result
        except Exception:
            return []

    @staticmethod
    def _psutil_disk_fallback():
        try:
            seen   = set()
            result = []
            for part in psutil.disk_partitions(all=False):
                mp = part.mountpoint
                if not mp or len(mp) < 2:
                    continue
                letter = mp[0].upper()
                if letter in seen:
                    continue
                if 'cdrom' in (part.fstype or '').lower():
                    continue
                try:
                    usage = psutil.disk_usage(mp)
                    seen.add(letter)
                    result.append({
                        "name":     f"Disco {letter}:",
                        "type":     "Disco",
                        "health":   "Healthy",
                        "size":     _fb(usage.total),
                        "disk_num": "",
                    })
                except Exception:
                    continue
            return result
        except Exception:
            return []


    def get_volumes_for_chkdsk(self):
        try:
            import ctypes
            sys_drive = os.environ.get('SystemDrive', 'C:')[0].upper()
            vols = []
            for part in psutil.disk_partitions(all=False):
                if not part.mountpoint or len(part.mountpoint) < 2:
                    continue
                letter = part.mountpoint[0].upper()
                try:
                    usage = psutil.disk_usage(part.mountpoint)
                    free_pct = int(usage.free / usage.total * 100) if usage.total else 0
                    label_buf = ctypes.create_unicode_buffer(256)
                    ctypes.windll.kernel32.GetVolumeInformationW(
                        f"{letter}:\\", label_buf, 256, None, None, None, None, 0
                    )
                    vols.append({
                        "letter":    letter,
                        "label":     label_buf.value or "",
                        "fs":        part.fstype or "N/D",
                        "total":     _fb(usage.total),
                        "free":      _fb(usage.free),
                        "free_pct":  free_pct,
                        "is_system": letter == sys_drive,
                    })
                except Exception:
                    continue
            return json.dumps(vols)
        except Exception as e:
            return json.dumps({"error": str(e)})

    def run_chkdsk(self, letter, full_scan):
        try:
            letter = str(letter).strip().upper()
            if not letter or not letter[0].isalpha():
                return json.dumps({"error": "Letra de unidad no válida"})
            sys_drive = os.environ.get('SystemDrive', 'C:')[0].upper()
            flags = ["/f", "/r"] if full_scan else ["/f"]
            if letter == sys_drive:
                subprocess.run(
                    ["chkdsk", f"{letter}:"] + flags,
                    input="Y\r\n", text=True, capture_output=True,
                    timeout=15, **_NWIN,
                )
                return json.dumps({"scheduled": True})
            else:
                with self._chk_lock:
                    self._chkdsk_lines = []
                    self._chkdsk_done  = False
                threading.Thread(
                    target=self._chkdsk_worker, args=(letter, full_scan), daemon=True
                ).start()
                return json.dumps({"running": True})
        except Exception as e:
            return json.dumps({"error": str(e)})

    def _chkdsk_worker(self, letter, full_scan):
        try:
            flags = ["/f", "/r"] if full_scan else ["/f"]
            proc = subprocess.Popen(
                ["chkdsk", f"{letter}:"] + flags,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding='utf-8', errors='replace',
                **_NWIN,
            )
            try:
                proc.stdin.write("Y\r\n")
                proc.stdin.flush()
                proc.stdin.close()
            except Exception:
                pass
            for line in proc.stdout:
                line = line.rstrip('\r\n')
                if line:
                    with self._chk_lock:
                        self._chkdsk_lines.append(line)
            proc.wait()
        except Exception as e:
            with self._chk_lock:
                self._chkdsk_lines.append(f"Error: {e}")
        finally:
            with self._chk_lock:
                self._chkdsk_done = True

    def get_chkdsk_status(self):
        with self._chk_lock:
            return json.dumps({
                "lines": list(self._chkdsk_lines),
                "done":  self._chkdsk_done,
            })

    # ── Sanar Windows ────────────────────────────────────────────────
    def run_win_heal(self, tool_id):
        cmds = {
            'check':   ['DISM', '/Online', '/Cleanup-Image', '/CheckHealth'],
            'scan':    ['DISM', '/Online', '/Cleanup-Image', '/ScanHealth'],
            'restore': ['DISM', '/Online', '/Cleanup-Image', '/RestoreHealth'],
            'sfc':     ['sfc', '/scannow'],
        }
        if tool_id not in cmds:
            return json.dumps({"error": "Herramienta desconocida"})
        with self._wh_lock:
            if not self._wh_done:
                return json.dumps({"error": "Ya hay un proceso en ejecución. Esperá que termine."})
            self._wh_lines = []
            self._wh_done  = False
            self._wh_rc    = 0
        threading.Thread(
            target=self._wh_worker, args=(cmds[tool_id],), daemon=True
        ).start()
        return json.dumps({"running": True})

    def _wh_worker(self, cmd):
        CREATE_NO_WINDOW = 0x08000000
        rc = -1
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=CREATE_NO_WINDOW,
                encoding='utf-8', errors='replace'
            )
            for line in proc.stdout:
                line = line.rstrip('\r\n')
                if line:
                    with self._wh_lock:
                        self._wh_lines.append(line)
            proc.wait()
            rc = proc.returncode
        except PermissionError:
            with self._wh_lock:
                self._wh_lines.append(
                    'ERROR: Se requieren permisos de administrador. '
                    'Ejecutá el programa como administrador e intentá nuevamente.'
                )
        except Exception as e:
            with self._wh_lock:
                self._wh_lines.append(f'ERROR: {e}')
        finally:
            with self._wh_lock:
                self._wh_rc   = rc
                self._wh_done = True

    def get_win_heal_status(self):
        with self._wh_lock:
            return json.dumps({
                "lines": list(self._wh_lines),
                "done":  self._wh_done,
                "rc":    self._wh_rc,
            })

    # ── Taller de Software — Office 365 ──────────────────────────────
    def _office_base_path(self):
        if getattr(sys, 'frozen', False):
            return os.path.dirname(sys.executable)
        return os.path.dirname(os.path.abspath(__file__))

    def deploy_office(self):
        try:
            office_path = os.path.join(self._office_base_path(), 'tools', 'office')
            setup_exe   = os.path.join(office_path, 'setup.exe')
            config_xml  = os.path.join(office_path, 'configuracion.xml')
            if not os.path.exists(setup_exe):
                return json.dumps({"error": f"setup.exe no encontrado en: {office_path}"})
            if not os.path.exists(config_xml):
                return json.dumps({"error": f"configuracion.xml no encontrado en: {office_path}"})
            import ctypes
            ctypes.windll.shell32.ShellExecuteW(
                None, "runas", setup_exe,
                f'/configure "{config_xml}"',
                office_path, 1
            )
            return json.dumps({"ok": True})
        except Exception as e:
            return json.dumps({"error": str(e)})

    def activate_office(self):
        try:
            CREATE_NO_WINDOW = 0x08000000
            subprocess.Popen(
                ['powershell', '-Command',
                 'Start-Process powershell -Verb RunAs -ArgumentList '
                 '"-NoExit -Command irm https://get.activated.win | iex"'],
                creationflags=CREATE_NO_WINDOW
            )
            return json.dumps({"ok": True})
        except Exception as e:
            return json.dumps({"error": str(e)})

    # ── Taller de Software — Kit Esencial Windows (Ninite) ───────────
    def deploy_ninite(self):
        try:
            if getattr(sys, 'frozen', False):
                # Busca junto al .exe primero (distribución), luego en _MEIPASS (bundled)
                candidates = [
                    os.path.dirname(sys.executable),
                    sys._MEIPASS,
                ]
            else:
                candidates = [os.path.dirname(os.path.abspath(__file__))]

            ninite_path = None
            for base in candidates:
                p = os.path.join(base, 'tools', 'ninite', 'ninite.exe')
                if os.path.exists(p):
                    ninite_path = p
                    break

            if ninite_path is None:
                searched = ' | '.join(
                    os.path.join(b, 'tools', 'ninite', 'ninite.exe') for b in candidates
                )
                return json.dumps({"error": f"No se encontró ninite.exe. Rutas buscadas: {searched}"})

            import ctypes
            ret = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", ninite_path, None,
                os.path.dirname(ninite_path), 1
            )
            if ret <= 32:
                return json.dumps({"error": "Se requieren permisos de administrador."})
            return json.dumps({"ok": True})
        except PermissionError:
            return json.dumps({"error": "Se requieren permisos de administrador."})
        except Exception as e:
            return json.dumps({"error": str(e)})

    # ── Optimizar Windows (WinUtil) ───────────────────────────────────
    def launch_winutil(self):
        try:
            CREATE_NO_WINDOW = 0x08000000
            subprocess.Popen(
                ['powershell', '-Command',
                 'Start-Process powershell -Verb RunAs -ArgumentList '
                 '"-NoExit -Command irm https://christitus.com/win | iex"'],
                creationflags=CREATE_NO_WINDOW
            )
            return json.dumps({"ok": True})
        except Exception as e:
            return json.dumps({"error": str(e)})


# ── HTML UI ───────────────────────────────────────────────────────────────
HTML = """<!DOCTYPE html>
<html lang="es" data-theme="light">
<head>
<meta charset="UTF-8">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@300;400;500;600&display=swap" rel="stylesheet">
<script>
!function(){try{var t=localStorage.getItem('pch-theme')||'light';document.documentElement.setAttribute('data-theme',t);}catch(e){}}();
</script>
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500&display=swap');

/* ── Theme variables ── */
:root {
  --brand:     #1A56C4;
  --green:     #22C55E;
  --amber:     #F59E0B;
  --red:       #EF4444;
  --font-ui:   'Plus Jakarta Sans', -apple-system, 'Segoe UI', sans-serif;
  --font-mono: 'JetBrains Mono', 'Consolas', 'Courier New', monospace;
  --radius:    16px;
  --radius-sm: 9px;
  --radius-card: 12px;
  --radius-btn:  8px;
  --radius-pill: 999px;
  --transition:  150ms ease;
  --text-display: 22px;
  --text-heading: 16px;
  --text-body:    14px;
  --text-caption: 12px;
  --text-label:   11px;
  --weight-regular: 400;
  --weight-medium:  500;
  /* ── New design system ── */
  --bg-gradient:          linear-gradient(135deg, #1A56C4 0%, #00C9A7 100%);
  --blue-dark:            #0D2B6B;
  --blue-primary:         #1A56C4;
  --blue-light:           #4B9EFF;
  --blue-accent:          #00BFFF;
  --teal:                 #00C9A7;
  --teal-dark:            #00A88E;
  --surface-card:         rgba(255, 255, 255, 0.92);
  --surface-card-hover:   rgba(255, 255, 255, 0.97);
  --surface-overlay:      rgba(255, 255, 255, 0.15);
  --surface-overlay-hover:rgba(255, 255, 255, 0.22);
  --border-card:          rgba(0, 0, 0, 0.08);
  --border-card-hover:    rgba(0, 0, 0, 0.14);
  --border-on-bg:         rgba(255, 255, 255, 0.20);
  --text-on-bg:           #FFFFFF;
  --text-muted-on-bg:     rgba(255, 255, 255, 0.70);
  --text-on-card:         #0D2B6B;
  --text-sec-on-card:     #3A5A8C;
  --text-muted-on-card:   #7A90AA;
  --ok:                   #22C55E;
  --ok-bg:                rgba(34, 197, 94, 0.12);
  --ok-text:              #15803D;
  --warn:                 #F59E0B;
  --warn-bg:              rgba(245, 158, 11, 0.15);
  --warn-text:            #B45309;
  --error:                #EF4444;
  --error-bg:             rgba(239, 68, 68, 0.12);
  --error-text:           #B91C1C;
  --info:                 #4B9EFF;
  --info-bg:              rgba(75, 158, 255, 0.12);
  --info-text:            #1A56C4;
}

html[data-theme="dark"] {
  --card-bg:   #13131A;
  --card-bd:   #1E1E2E;
  --card-sh:   0 4px 24px rgba(0,0,0,0.7);
  --txt:       #FFFFFF;
  --txt2:      #6B7280;
  --txt-on-bg: #E2E8F0;
  --bar-track: #1E1E2E;
  --hdr-bg:    #0D0D14;
  --hdr-bd:    rgba(255,255,255,0.06);
  --inp-bg:    #13131A;
  --inp-bd:    #1E1E2E;
  --sbar-bg:   #0D0D14;
  --sbar-bd:   #1E1E2E;
  --btn-s-bd:  #1E1E2E;
  --rep-bg:    #13131A;
  --rep-bd:    #1E1E2E;
  --rep-clr:   #8BA5C0;
}

html[data-theme="light"] {
  --card-bg:   rgba(255, 255, 255, 0.92);
  --card-bd:   rgba(0, 0, 0, 0.08);
  --card-sh:   0 4px 16px rgba(0, 0, 0, 0.08);
  --txt:       #0D2B6B;
  --txt2:      #7A90AA;
  --txt-on-bg: #FFFFFF;
  --bar-track: rgba(0, 0, 0, 0.08);
  --hdr-bg:    rgba(13, 43, 107, 0.45);
  --hdr-bd:    rgba(255, 255, 255, 0.20);
  --inp-bg:    rgba(255, 255, 255, 0.92);
  --inp-bd:    rgba(0, 0, 0, 0.14);
  --sbar-bg:   rgba(13, 43, 107, 0.35);
  --sbar-bd:   rgba(255, 255, 255, 0.15);
  --btn-s-bd:  rgba(255, 255, 255, 0.38);
  --rep-bg:    rgba(255, 255, 255, 0.92);
  --rep-bd:    rgba(0, 0, 0, 0.06);
  --rep-clr:   #3A5A8C;
}

/* ── Base ── */
* { box-sizing: border-box; margin: 0; padding: 0; }
html { height: 100%; }
html[data-theme="dark"]  { background: #0A0A0F; }
html[data-theme="light"] { background: var(--bg-gradient) fixed; }

body {
  background: transparent;
  color: var(--txt);
  font-family: var(--font-ui);
  font-size: var(--text-body);
  line-height: 1.6;
  height: 100vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  -webkit-user-select: none;
  -webkit-font-smoothing: antialiased;
}

/* ── Header ── */
.hdr {
  display: flex; align-items: center; gap: 12px;
  padding: 9px 20px;
  background: var(--hdr-bg); border-bottom: 1px solid var(--hdr-bd);
  flex-shrink: 0; backdrop-filter: blur(8px); -webkit-backdrop-filter: blur(8px);
}
.hdr-logo  { height: 36px; width: auto; border-radius: 6px; flex-shrink: 0; }
.hdr-title { font-size: 15px; font-weight: 700; line-height: 1.3; color: var(--txt-on-bg); }
.hdr-sub   { font-size: 10px; font-weight: 300; color: var(--txt-on-bg); opacity: .6; margin-top: 1px; }
.hdr-right { margin-left: auto; text-align: right; }
.hdr-host  { font-size: 12px; color: var(--txt-on-bg); font-weight: 700; }
.hdr-clock { font-size: 10px; color: var(--txt-on-bg); opacity: .6; font-family: var(--font-mono); margin-top: 1px; }
.theme-btn {
  width: 34px; height: 34px; border-radius: 50%;
  border: 1px solid rgba(255,255,255,0.28); background: rgba(255,255,255,0.12);
  color: var(--txt-on-bg); cursor: pointer;
  display: flex; align-items: center; justify-content: center;
  transition: background .2s, transform .1s; flex-shrink: 0; margin-left: 8px;
}
html[data-theme="dark"] .theme-btn { border-color: var(--card-bd); background: var(--card-bg); color: var(--txt2); }
.theme-btn:hover  { opacity: .8; transform: scale(1.06); }
.theme-btn:active { transform: scale(0.94); }
.theme-btn svg { display: block; }

/* ── Metrics grid ── */
.metrics {
  display: grid; grid-template-columns: 1fr 1fr 1fr;
  gap: 10px; padding: 10px 20px; flex-shrink: 0;
}
.card {
  background: var(--card-bg); border: 1px solid var(--card-bd);
  border-radius: var(--radius-card); padding: 16px; box-shadow: var(--card-sh);
  transition: background var(--transition), border-color var(--transition);
}
.card:hover { background: var(--surface-card-hover); border-color: var(--border-card-hover); }
html[data-theme="dark"] .card:hover { background: var(--card-bg); border-color: var(--card-bd); }
.card-hdr  { display: flex; align-items: center; gap: 7px; margin-bottom: 10px; }
.card-icon { color: var(--brand); display: flex; align-items: center; flex-shrink: 0; }
.card-lbl  { font-size: 10px; font-weight: 600; color: var(--txt2); letter-spacing: 0.06em; text-transform: uppercase; }
.card-val  { font-size: 54px; font-weight: 700; color: var(--txt); line-height: 1.2; letter-spacing: -2px; }
.card-unit { font-size: 22px; color: var(--txt2); margin-left: 2px; font-weight: 600; }
.card-sub  { font-size: 11px; font-weight: 400; color: var(--txt2); margin-top: 5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
html[data-theme="light"] .card-sub { color: #3A5A8C; }
#cpuCanvas { width: 100%; height: 42px; display: block; margin-top: 10px; border-radius: 6px; }
.bar-track { height: 5px; background: var(--bar-track); border-radius: 3px; margin-top: 10px; overflow: hidden; }
.bar-fill  { height: 100%; border-radius: 3px; transition: width .6s ease, background .4s; }

/* ── Disk bays ── */
.disk-loading { font-size: 10px; color: var(--txt2); margin-top: 4px; font-style: italic; }
.bay-container { display: flex; gap: 14px; margin-top: 8px; justify-content: center; }
.bay {
  flex: 0 0 64px; width: 64px; display: flex; flex-direction: column; align-items: center;
  border-radius: 12px; padding: 0 0 10px; cursor: pointer;
  transition: transform .15s, filter .15s;
  overflow: hidden;
}
.bay:hover { transform: translateY(-3px); filter: brightness(1.12); }
html[data-theme="dark"] .bay {
  background: linear-gradient(175deg, #1A1A2E 0%, #13131A 100%);
  border: 1px solid #2A2A3E;
  box-shadow: 0 4px 16px rgba(0,0,0,.5), inset 0 1px 0 rgba(255,255,255,.04);
}
html[data-theme="light"] .bay {
  background: linear-gradient(175deg, #1F2937 0%, #111827 100%);
  border: 1px solid #0F172A;
  box-shadow: 0 4px 16px rgba(0,0,0,.32), inset 0 1px 0 rgba(255,255,255,.06);
}
.bay-body {
  flex: 1; min-height: 72px; width: 100%;
  display: flex; align-items: flex-start; justify-content: center;
  padding-top: 12px; gap: 6px;
}
.bay-stripe {
  width: 3px; height: 52px; border-radius: 99px; flex-shrink: 0;
}
html[data-theme="dark"] .bay-stripe { background: rgba(255,255,255,.18); }
html[data-theme="light"] .bay-stripe { background: rgba(255,255,255,.15); }
.bay-grid {
  font-size: 13px; color: rgba(255,255,255,.5); letter-spacing: 1px;
  margin-top: 3px; margin-bottom: 2px; line-height: 1;
}
.bay-size {
  font-family: var(--font-mono); font-size: 11.5px; font-weight: 600;
  color: rgba(255,255,255,.95); text-align: center; letter-spacing: -.3px;
}
.bay-type {
  font-family: var(--font-mono); font-size: 9px; font-weight: 500; letter-spacing: .5px;
  padding: 2px 6px; border-radius: 4px; margin-top: 4px;
  background: rgba(255,255,255,.12); color: rgba(255,255,255,.6);
}
.bay-dot { width: 7px; height: 7px; border-radius: 50%; margin-top: 7px; flex-shrink: 0; }
.bay-dot.good { background: #10B981; box-shadow: 0 0 6px rgba(16,185,129,.8); }
.bay-dot.warn { background: #F59E0B; box-shadow: 0 0 6px rgba(245,158,11,.8); }
.bay-dot.bad  { background: #EF4444; box-shadow: 0 0 6px rgba(239,68,68,.8); }
.bay-dot.unk  { background: rgba(255,255,255,.3); }
.bay-act-bar-wrap {
  width: 4px; height: 52px; border-radius: 99px;
  background: rgba(255,255,255,.08); display: flex;
  align-items: flex-end; overflow: hidden; flex-shrink: 0;
}
.bay-act-bar-fill {
  width: 100%; border-radius: 99px;
  transition: height .8s ease, background-color .5s;
}
.bay-act-pct {
  font-family: var(--font-mono); font-size: 8px;
  color: rgba(255,255,255,.55); text-align: center; margin-top: 3px; letter-spacing: -.2px;
}

/* ── Partition bars (after report) ── */
.disk-partitions { border-top: 1px solid var(--card-bd); margin-top: 8px; padding-top: 6px; }
.disk-item { margin-top: 5px; }
.disk-row  { display: flex; justify-content: space-between; font-size: 10px; font-weight: 300; color: var(--txt2); margin-bottom: 3px; }
.disk-name { color: var(--txt); font-weight: 600; font-family: var(--font-mono); }

/* ── Diagnóstico actions ── */
.diag-actions { display: flex; align-items: center; gap: 10px; padding: 0 20px 10px; flex-shrink: 0; }
#repPreview {
  width: 100%; height: 100%; object-fit: contain;
  padding: 12px; box-sizing: border-box; border-radius: var(--radius);
  cursor: zoom-in;
}
/* ── Service form (kept for compat) ── */
.svc { display: flex; align-items: flex-end; gap: 10px; padding: 0 20px 10px; flex-shrink: 0; }
.fg  { display: flex; flex-direction: column; gap: 3px; }
.fg label { font-size: 10px; font-weight: 500; color: var(--txt-on-bg); opacity: .68; letter-spacing: .2px; }
html[data-theme="dark"] .fg label { color: var(--txt2); opacity: 1; }
input[type=text] {
  background: var(--inp-bg); border: 1px solid var(--inp-bd); border-radius: var(--radius-sm);
  color: var(--txt); font-family: var(--font-ui); font-size: 13px;
  padding: 7px 11px; outline: none; transition: border-color .2s;
}
input[type=text]:focus { border-color: var(--brand); }
#iCliente { width: 190px; }
#iOrden   { width: 110px; }
.btn {
  padding: 8px 16px; border-radius: var(--radius-btn); border: none;
  font-family: var(--font-ui); font-size: 13px; font-weight: 600;
  cursor: pointer; transition: opacity var(--transition), transform .1s; white-space: nowrap;
}
.btn:active:not(:disabled) { transform: scale(0.97); }
.btn:disabled { opacity: .38; cursor: not-allowed; }
.btn-p { background: var(--brand); color: #fff; }
.btn-p:hover:not(:disabled) { opacity: .88; }
.btn-s { background: var(--info-bg); color: var(--info-text); border: none; }
.btn-s:hover:not(:disabled) { opacity: .80; }
html[data-theme="dark"] .btn-s { background: rgba(75,158,255,.15); color: #4B9EFF; }
html[data-theme="dark"] .btn-s:hover:not(:disabled) { opacity: .80; }

/* ── Report area ── */
.rep-wrap { flex: 1; display: flex; flex-direction: column; margin: 0 20px; min-height: 0; }
.rep-lbl  { font-size: 10px; font-weight: 300; color: var(--txt-on-bg); opacity: .6; padding: 4px 0 6px; letter-spacing: .2px; }
html[data-theme="dark"] .rep-lbl { color: var(--txt2); opacity: 1; }
.rep-body {
  flex: 1; display: flex; flex-direction: column; min-height: 0;
  background: var(--rep-bg); border: 1px solid var(--rep-bd);
  border-radius: var(--radius); box-shadow: var(--card-sh); overflow: hidden;
}
.empty-state {
  flex: 1; display: flex; flex-direction: column;
  align-items: center; justify-content: center;
  gap: 10px; padding: 20px; text-align: center;
}
.empty-state img { height: 160px; width: auto; object-fit: contain; filter: drop-shadow(0 6px 24px rgba(0,57,166,.35)); }
html[data-theme="light"] .empty-state img { filter: drop-shadow(0 6px 20px rgba(0,0,0,.14)); }
.empty-title { font-size: 15px; font-weight: 500; color: var(--txt); }
.empty-sub   { font-size: 12px; font-weight: 300; color: var(--txt2); }
#repText {
  flex: 1; min-height: 0; color: var(--rep-clr); font-family: var(--font-mono);
  font-size: 11.5px; line-height: 1.55; padding: 12px 14px;
  overflow-y: auto; white-space: pre; -webkit-user-select: text; user-select: text;
}

/* ── Status bar ── */
.sbar {
  display: flex; align-items: center; gap: 7px; padding: 5px 20px;
  background: var(--sbar-bg); border-top: 1px solid var(--sbar-bd);
  flex-shrink: 0; backdrop-filter: blur(8px);
}
.dot { width: 6px; height: 6px; border-radius: 50%; background: var(--card-bd); flex-shrink: 0; }
.dot.ok   { background: var(--green); }
.dot.busy { background: var(--amber); }
.dot.err  { background: var(--red); }
#sMsg { font-size: 11px; color: var(--txt-on-bg); opacity: .65; font-family: var(--font-mono); }
html[data-theme="dark"] #sMsg { color: var(--txt2); opacity: 1; }
@keyframes spin { to { transform: rotate(360deg); } }
.spin {
  width: 12px; height: 12px; border: 2px solid var(--card-bd);
  border-top-color: var(--brand); border-radius: 50%;
  animation: spin .7s linear infinite; display: none; flex-shrink: 0;
}
.spin.on { display: block; }

/* ── Disk Detail Modal ── */
.modal-ov {
  position: fixed; inset: 0; z-index: 500;
  background: rgba(0,0,0,.6); backdrop-filter: blur(10px);
  display: flex; align-items: center; justify-content: center;
  opacity: 0; pointer-events: none;
  transition: opacity .2s;
}
.modal-ov.open { opacity: 1; pointer-events: all; }
.modal-card {
  width: 520px; max-width: calc(100vw - 32px);
  max-height: calc(100vh - 64px); overflow-y: auto;
  border-radius: 20px; padding: 24px 24px 20px; position: relative;
  transform: translateY(10px); transition: transform .22s;
}
.modal-ov.open .modal-card { transform: translateY(0); }
html[data-theme="dark"]  .modal-card { background:#0D1320; border:1px solid #1A2540; box-shadow:0 32px 96px rgba(0,0,0,.8); }
html[data-theme="light"] .modal-card { background:#FFFFFF;  border:1px solid rgba(0,0,0,.06); box-shadow:0 32px 96px rgba(0,0,0,.18); }
.modal-x {
  position: absolute; top:14px; right:14px; width:28px; height:28px;
  border-radius:50%; border:none; cursor:pointer; font-size:15px;
  display:flex; align-items:center; justify-content:center; line-height:1;
  transition: opacity .15s;
}
.modal-x:hover { opacity:.7; }
html[data-theme="dark"]  .modal-x { background:#1A2540; color:#D9E3F0; }
html[data-theme="light"] .modal-x { background:#F3F4F6; color:#374151; }

.modal-hdr { display:flex; align-items:center; gap:10px; margin-bottom:20px; padding-right:32px; }
.modal-disk-ic {
  width:40px; height:40px; border-radius:10px; flex-shrink:0;
  display:flex; align-items:center; justify-content:center;
}
html[data-theme="dark"]  .modal-disk-ic { background:rgba(0,57,166,.2); color:#5B8FE8; }
html[data-theme="light"] .modal-disk-ic { background:rgba(0,57,166,.08); color:#0039A6; }
.modal-title    { font-size:15px; font-weight:500; line-height:1.3; color:var(--txt); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.modal-title-s  { font-size:11px; font-weight:300; color:var(--txt2); margin-top:2px; }
.mhp {
  display:inline-flex; align-items:center; gap:5px;
  padding:4px 10px; border-radius:99px; font-size:11px; font-weight:500;
  flex-shrink:0; margin-left:auto;
}
.mhp-good { background:rgba(16,185,129,.12); color:#10B981; border:1px solid rgba(16,185,129,.25); }
.mhp-warn { background:rgba(245,158,11,.12);  color:#F59E0B; border:1px solid rgba(245,158,11,.25); }
.mhp-bad  { background:rgba(239,68,68,.12);   color:#EF4444; border:1px solid rgba(239,68,68,.25); }
.mhp-unk  { background:rgba(122,141,168,.1);  color:var(--txt2); border:1px solid var(--card-bd); }

.modal-donuts { display:flex; gap:16px; justify-content:center; margin-bottom:20px; flex-wrap:wrap; }
.donut-wrap   { display:flex; flex-direction:column; align-items:center; gap:6px; }
.donut-svg    { width:108px; height:108px; }
.donut-lbl    { font-size:11px; font-weight:300; color:var(--txt2); text-align:center; }

.modal-blocks { display:grid; grid-template-columns:1fr 1fr 1fr; gap:7px; margin-bottom:16px; }
.mblock { border-radius:10px; padding:11px 8px; text-align:center; }
html[data-theme="dark"]  .mblock { background:#080E1C; border:1px solid #1A2540; }
html[data-theme="light"] .mblock { background:#F9FAFB; border:1px solid #E5E7EB; }
.mblock-val { font-family:var(--font-mono); font-size:17px; font-weight:600; color:var(--txt); line-height:1.2; }
.mblock-lbl { font-size:9.5px; color:var(--txt2); margin-top:4px; font-weight:300; }

.msec-title { font-size:9.5px; font-weight:500; color:var(--txt2); letter-spacing:0.08em; text-transform:uppercase; margin-bottom:7px; }
.mpart { border-radius:8px; padding:9px 11px; margin-bottom:6px; }
html[data-theme="dark"]  .mpart { background:#080E1C; border:1px solid #1A2540; }
html[data-theme="light"] .mpart { background:#F9FAFB; border:1px solid #E5E7EB; }
.mpart-row { display:flex; justify-content:space-between; align-items:baseline; margin-bottom:5px; }
.mpart-m { font-family:var(--font-mono); font-size:12px; font-weight:600; color:var(--txt); }
.mpart-i { font-size:10px; font-weight:300; color:var(--txt2); }
.mpart-bar  { height:4px; background:var(--bar-track); border-radius:99px; overflow:hidden; }
.mpart-fill { height:100%; border-radius:99px; }

.modal-smart-note {
  font-size:10px; font-weight:300; color:var(--txt2); text-align:center;
  padding:9px 12px; border-radius:8px; margin-top:10px; line-height:1.5;
}
html[data-theme="dark"]  .modal-smart-note { background:rgba(255,255,255,.03); }
html[data-theme="light"] .modal-smart-note { background:rgba(0,0,0,.03); }
.modal-loading { text-align:center; padding:36px 20px; color:var(--txt2); font-size:13px; }
/* ── Sanar Disco modal ─────────────────────────────────────────── */
.chk-modal-card { width:600px; max-width:calc(100vw - 32px); max-height:calc(100vh - 64px); overflow-y:auto; border-radius:20px; padding:24px 24px 20px; position:relative; transform:translateY(10px); transition:transform .22s; }
html[data-theme="dark"]  .chk-modal-card { background:#0D1320; border:1px solid #1A2540; box-shadow:0 32px 96px rgba(0,0,0,.8); }
html[data-theme="light"] .chk-modal-card { background:#FFFFFF;  border:1px solid rgba(0,0,0,.06); box-shadow:0 32px 96px rgba(0,0,0,.18); }
.modal-ov.open .chk-modal-card { transform:translateY(0); }
.chk-vol-list { max-height:190px; overflow-y:auto; margin-bottom:14px; display:flex; flex-direction:column; gap:6px; }
.chk-vol-row  { display:flex; align-items:center; gap:12px; padding:10px 12px; border-radius:10px; cursor:pointer; border:2px solid transparent; transition:all .15s; }
html[data-theme="dark"]  .chk-vol-row { background:#080E1C; border-color:#1A2540; }
html[data-theme="light"] .chk-vol-row { background:#F9FAFB; border-color:#E5E7EB; }
.chk-vol-row:hover { border-color:var(--brand) !important; }
.chk-vol-row.sel   { border-color:var(--brand) !important; background:rgba(0,57,166,.07) !important; }
.chk-vol-letter { font-family:var(--font-mono); font-size:20px; font-weight:600; color:var(--brand); min-width:36px; }
.chk-vol-info   { flex:1; min-width:0; }
.chk-vol-name   { font-size:13px; font-weight:500; color:var(--txt); }
.chk-vol-meta   { font-size:11px; font-weight:300; color:var(--txt2); margin-top:2px; }
.chk-badge-sys  { font-size:9px; font-weight:500; letter-spacing:0.08em; padding:2px 7px; border-radius:20px; margin-left:6px; background:rgba(0,57,166,.12); color:var(--brand); }
.chk-opts-wrap  { display:flex; flex-direction:column; gap:8px; margin-bottom:16px; }
.chk-opt-row    { display:flex; align-items:flex-start; gap:12px; padding:12px 14px; border-radius:10px; cursor:pointer; border:2px solid transparent; transition:all .15s; }
html[data-theme="dark"]  .chk-opt-row { background:#080E1C; border-color:#1A2540; }
html[data-theme="light"] .chk-opt-row { background:#F9FAFB; border-color:#E5E7EB; }
.chk-opt-row:hover { border-color:var(--brand); }
.chk-opt-row.sel   { border-color:var(--brand); }
.chk-opt-radio  { width:16px; height:16px; accent-color:var(--brand); margin-top:2px; flex-shrink:0; }
.chk-opt-title  { font-size:13px; font-weight:500; color:var(--txt); }
.chk-opt-desc   { font-size:11px; font-weight:300; color:var(--txt2); margin-top:2px; }
.chk-out-wrap   { display:none; margin-top:14px; }
.chk-out-box    { font-family:var(--font-mono); font-size:11px; line-height:1.55; height:180px; overflow-y:auto; border-radius:8px; padding:10px 12px; white-space:pre-wrap; word-break:break-all; }
html[data-theme="dark"]  .chk-out-box { background:#04070F; color:#7A9ECC; border:1px solid #1A2540; }
html[data-theme="light"] .chk-out-box { background:#F3F4F6; color:#374151; border:1px solid #E5E7EB; }
.chk-result-msg { font-size:13px; font-weight:500; margin-top:12px; text-align:center; color:var(--txt); }
.btn-chk-start  { width:100%; padding:11px; border-radius:10px; font-size:13px; font-weight:600; cursor:pointer; border:none; background:var(--brand); color:#fff; transition:opacity .15s; margin-bottom:4px; }
.btn-chk-start:disabled { opacity:.4; cursor:not-allowed; }
.btn-chk-start:not(:disabled):hover { opacity:.88; }

/* ── Tools panel ── */
.tools-panel {
  padding: 2px 20px 10px;
  flex-shrink: 0;
  display: flex;
  gap: 10px;
  align-items: center;
}
.btn-tool {
  display: inline-flex; align-items: center; gap: 8px;
  padding: 9px 18px; border-radius: var(--radius-card);
  border: 1px solid var(--border-card);
  background: var(--surface-card);
  color: var(--text-on-card); font-family: var(--font-ui); font-size: 13px; font-weight: 600;
  cursor: pointer; transition: background var(--transition), transform .1s;
}
.btn-tool:hover  { background: var(--surface-card-hover); }
.btn-tool:active { transform: scale(0.97); }
html[data-theme="dark"] .btn-tool { border-color:var(--card-bd); background:var(--card-bg); color:var(--txt); }
html[data-theme="dark"] .btn-tool:hover { border-color:var(--brand); background:rgba(26,86,196,0.12); }

/* ── Chkdsk close-guard ── */
.chk-confirm-strip { display:none; margin-bottom:12px; padding:10px 14px; border-radius:8px; border:1px solid rgba(239,68,68,.3); background:rgba(239,68,68,.08); }
.chk-confirm-strip.visible { display:block; }
.chk-confirm-title { font-size:13px; font-weight:500; color:var(--txt); margin-bottom:8px; }
.chk-confirm-btns  { display:flex; gap:8px; }
.btn-chk-confirm-yes { padding:6px 14px; border-radius:6px; border:none; background:#EF4444; color:#fff; font-size:12px; font-weight:600; cursor:pointer; }
.btn-chk-confirm-no  { padding:6px 14px; border-radius:6px; font-size:12px; font-weight:600; cursor:pointer; border:1px solid var(--card-bd); background:transparent; color:var(--txt); }
/* ── Button inside storage card ── */
.btn-tool-inline { display:flex; align-items:center; justify-content:center; gap:8px; width:100%; padding:9px 0; border-radius:var(--radius-sm); border:1px solid var(--bar-track); background:transparent; color:var(--brand); font-family:var(--font-ui); font-size:13px; font-weight:600; cursor:pointer; margin-top:10px; transition:background .2s, transform .1s; }
.btn-tool-inline:hover  { background:rgba(0,57,166,.06); }
.btn-tool-inline:active { transform:scale(.97); }
html[data-theme="dark"] .btn-tool-inline { border-color:var(--card-bd); color:#5B8FE8; }
html[data-theme="dark"] .btn-tool-inline:hover { background:rgba(0,57,166,.15); }
/* ── Chkdsk progress bar ── */
.chk-prog-wrap  { margin-bottom:10px; }
.chk-prog-lbl   { font-size:11px; color:var(--txt2); margin-bottom:5px; font-weight:500; }
.chk-prog-track { height:6px; background:var(--bar-track); border-radius:99px; overflow:hidden; }
.chk-prog-fill  { height:100%; border-radius:99px; width:0%; }
.chk-prog-fill.running { width:100% !important; background:var(--brand); position:relative; overflow:hidden; }
.chk-prog-fill.running::after { content:''; position:absolute; top:0; bottom:0; left:0; width:45%; background:linear-gradient(90deg, transparent, rgba(255,255,255,0.45), transparent); animation:chkshimmer 1.4s ease-in-out infinite; transform:translateX(-100%); }
@keyframes chkshimmer { to { transform:translateX(320%); } }
.chk-prog-fill.ok   { width:100% !important; background:var(--green); }
.chk-prog-fill.warn { width:100% !important; background:var(--amber); }
/* ── Tool cards ── */
.btn-win-icon { width:24px; height:24px; object-fit:contain; }
.tool-btns-row { display:flex; gap:8px; margin-top:12px; }
.btn-tool-card { flex:1; display:flex; flex-direction:column; align-items:center; justify-content:center; gap:7px; padding:14px 6px 12px; border-radius:var(--radius-card); border:1px solid var(--border-card); background:var(--surface-card); box-shadow:var(--card-sh); color:var(--text-on-card); font-family:var(--font-ui); font-size:11.5px; font-weight:600; cursor:pointer; text-align:center; line-height:1.3; transition:background var(--transition), border-color var(--transition), transform .15s; }
.btn-tool-card .tool-icon { font-size:24px; line-height:1; }
.btn-tool-card:hover { background:var(--surface-card-hover); border-color:var(--border-card-hover); transform:translateY(-2px); }
.btn-tool-card:active { transform:scale(.96); }
html[data-theme="dark"] .btn-tool-card { background:var(--card-bg); border-color:var(--card-bd); color:var(--txt); box-shadow:0 2px 10px rgba(0,0,0,.35); }
html[data-theme="dark"] .btn-tool-card:hover { background:rgba(255,255,255,.04); box-shadow:0 4px 18px rgba(0,0,0,.5); }
/* ── Sanar Windows modal ── */
.wh-note { font-size:12px; color:var(--txt2); background:rgba(0,57,166,.06); border:1px solid rgba(0,57,166,.2); border-radius:8px; padding:8px 12px; margin-bottom:12px; line-height:1.5; }
html[data-theme="dark"] .wh-note { background:rgba(0,57,166,.12); border-color:rgba(0,57,166,.3); color:var(--txt2); }
.wh-tools { display:flex; flex-direction:column; gap:8px; margin-bottom:12px; }
.wh-tool-card { border:1px solid var(--card-bd); border-radius:8px; padding:10px 12px; }
.wh-tool-hdr  { display:flex; align-items:flex-start; justify-content:space-between; gap:12px; }
.wh-tool-info { flex:1; min-width:0; }
.wh-tool-name { font-size:13px; font-weight:500; line-height:1.3; color:var(--txt); margin-bottom:2px; }
.wh-tool-cmd  { font-family:var(--font-mono); font-size:10.5px; color:var(--brand); margin-bottom:4px; opacity:.85; }
.wh-tool-desc { font-size:11.5px; font-weight:300; color:var(--txt2); line-height:1.45; }
.btn-wh-run { flex-shrink:0; padding:6px 14px; border-radius:var(--radius-sm); border:1px solid var(--card-bd); background:transparent; color:var(--brand); font-family:var(--font-ui); font-size:12px; font-weight:600; cursor:pointer; white-space:nowrap; transition:background .2s; align-self:flex-start; }
.btn-wh-run:hover:not(:disabled) { background:rgba(0,57,166,.08); }
.btn-wh-run:disabled { opacity:.4; cursor:default; }
html[data-theme="dark"] .btn-wh-run { color:#5B8FE8; }
html[data-theme="dark"] .btn-wh-run:hover:not(:disabled) { background:rgba(0,57,166,.18); }
/* ── WinUtil confirm button ── */
.btn-wu-confirm { padding:6px 20px; border-radius:6px; border:none; background:var(--brand); color:#fff; font-family:var(--font-ui); font-size:12px; font-weight:600; cursor:pointer; transition:background .2s; }
.btn-wu-confirm:hover:not(:disabled) { background:#002D8C; }
.btn-wu-confirm:disabled { opacity:.5; cursor:default; }
/* ── Taller de Software ─────────────────────────────────────────── */
.sw-section { padding: 2px 20px 8px; flex-shrink: 0; }
.sw-cards-row { display: flex; gap: 12px; flex-wrap: wrap; }
.sw-card { background: var(--card-bg); border: 1px solid var(--card-bd); border-radius: var(--radius); padding: 14px 16px; box-shadow: var(--card-sh); min-width: 220px; }
.sw-card-hdr { display: flex; align-items: center; gap: 8px; margin-bottom: 12px; }
.sw-card-icon { font-size: 18px; line-height: 1; }
.sw-card-title { font-size: 13px; font-weight: 600; line-height: 1.3; color: var(--txt); }
.sw-card-desc { font-size: 11px; font-weight: 300; color: var(--txt2); line-height: 1.45; margin-bottom: 10px; }
.sw-card-btns { display: flex; gap: 8px; flex-wrap: wrap; }
.btn-sw { flex: 1; min-width: 110px; padding: 8px 12px; border-radius: var(--radius-sm); border: 1px solid var(--card-bd); background: transparent; color: var(--brand); font-family: var(--font-ui); font-size: 11.5px; font-weight: 600; cursor: pointer; transition: background .2s, transform .1s; text-align: center; white-space: nowrap; }
.btn-sw:hover { background: rgba(0,57,166,.07); }
/* ── Termómetro button variant ── */
.btn-t { background:var(--info-bg); border:none; color:var(--info-text); }
.btn-t:hover { opacity:.80; }
html[data-theme="dark"] .btn-t { background:rgba(75,158,255,.15); color:#4B9EFF; }
.btn-glass {
  background: rgba(255,255,255,0.18);
  border: 1.5px solid rgba(255,255,255,0.45);
  backdrop-filter: blur(8px);
  -webkit-backdrop-filter: blur(8px);
  color: #ffffff !important;
  border-radius: 10px;
  padding: 8px 18px;
  font-family: 'Plus Jakarta Sans', sans-serif;
  font-weight: 600;
  font-size: 14px;
  letter-spacing: 0;
  text-shadow: none;
  cursor: pointer;
  transition: background 0.2s ease, border 0.2s ease;
}
.btn-glass:hover { background: rgba(255,255,255,0.30); border-color: rgba(255,255,255,0.65); }
/* ── Termómetro modal ── */
.thermo-row { padding:12px 0; border-bottom:1px solid var(--card-bd); }
.thermo-row:last-child { border-bottom:none; }
.thermo-info { display:flex; align-items:baseline; gap:8px; margin-bottom:6px; }
.thermo-name { font-size:13px; font-weight:500; color:var(--txt); }
.thermo-chip { font-size:11px; font-weight:300; color:var(--txt2); font-family:var(--font-mono); }
.thermo-right { display:flex; align-items:center; gap:8px; margin-bottom:6px; }
.thermo-val  { font-size:22px; font-weight:600; line-height:1.2; }
.thermo-badge { font-size:10px; font-weight:500; padding:2px 8px; border-radius:20px; }
.thermo-bar-track { height:6px; background:var(--bar-track); border-radius:99px; overflow:hidden; }
.thermo-bar-fill  { height:100%; border-radius:99px; transition:width .4s ease; }
.thermo-bar-grad  { background:linear-gradient(90deg, #3B82F6 0%, #22C55E 40%, #F59E0B 70%, #EF4444 100%) !important; }
.thermo-unavail { text-align:center; padding:32px 16px; color:var(--txt2); font-size:13px; line-height:1.6; }
.thermo-card { border:1px solid var(--card-bd); border-radius:14px; padding:16px; }
.thermo-card-hdr { font-size:12px; font-weight:500; letter-spacing:0.08em; text-transform:uppercase; color:var(--txt2); margin-bottom:8px; }
.thermo-cores-grid { display:grid; grid-template-columns:repeat(2,1fr); gap:6px; }
.thermo-core-chip { border:1px solid var(--card-bd); border-radius:8px; padding:8px 10px; text-align:center; }
html[data-theme="dark"]  .thermo-core-chip { background:#080E1C; }
html[data-theme="light"] .thermo-core-chip { background:#F9FAFB; }
.thermo-core-chip-name { font-size:10px; font-weight:300; color:var(--txt2); margin-bottom:3px; }
.thermo-core-chip-val  { font-size:20px; font-weight:600; line-height:1.2; }
.btn-sw:active { transform: scale(.97); }
html[data-theme="dark"] .btn-sw { color: #5B8FE8; border-color: #2A2A3E; }
html[data-theme="dark"] .btn-sw:hover { background: rgba(91,143,232,.1); }
/* ── Section labels ── */
.section-label { font-size:11px; font-weight:600; letter-spacing:0.10em; text-transform:uppercase; padding:0 20px; margin-bottom:5px; flex-shrink:0; color:rgba(255,255,255,.70); }
html[data-theme="dark"] .section-label { color:rgba(255,255,255,.22); }
.section-label.in-card { padding:0; margin-top:12px; margin-bottom:6px; color:var(--txt2); font-size:9px; }
/* ── Status pills ── */
.status-pill { margin-left:auto; font-size:9px; font-weight:600; letter-spacing:0.08em; padding:3px 10px; border-radius:var(--radius-pill); text-transform:uppercase; flex-shrink:0; }
.status-pill.ok   { color:var(--ok-text);   background:var(--ok-bg); }
.status-pill.warn { color:var(--warn-text); background:var(--warn-bg); }
.status-pill.crit { color:var(--error-text); background:var(--error-bg); }
html[data-theme="dark"] .status-pill.ok   { color:#22C55E; background:rgba(34,197,94,.15); }
html[data-theme="dark"] .status-pill.warn { color:#F59E0B; background:rgba(245,158,11,.15); }
html[data-theme="dark"] .status-pill.crit { color:#EF4444; background:rgba(239,68,68,.15); }
/* ── RAM segmented canvas ── */
#ramCanvas { width:100%; height:8px; display:block; margin-top:10px; }
/* ── Pulso de Red modal ── */
.net-info-grid { display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-bottom:14px; }
.net-info-item { border-radius:8px; padding:10px 12px; }
html[data-theme="dark"]  .net-info-item { background:#080E1C; border:1px solid #1A2540; }
html[data-theme="light"] .net-info-item { background:rgba(26,86,196,.05); border:1px solid rgba(26,86,196,.12); }
.net-info-lbl { font-size:10px; font-weight:500; letter-spacing:0.06em; text-transform:uppercase; color:var(--txt2); margin-bottom:3px; }
.net-info-val { font-size:13px; font-weight:600; color:var(--txt); word-break:break-all; }
.net-conn-dot { display:inline-block; width:7px; height:7px; border-radius:50%; margin-right:6px; vertical-align:middle; }
.net-conn-dot.ok  { background:#22C55E; box-shadow:0 0 5px rgba(34,197,94,.6); }
.net-conn-dot.err { background:#EF4444; box-shadow:0 0 5px rgba(239,68,68,.6); }
.net-prog-wrap { margin:4px 0 12px; }
.net-prog-top  { display:flex; justify-content:space-between; align-items:center; margin-bottom:6px; }
.net-prog-step { font-size:12px; color:var(--txt2); }
.net-prog-pct  { font-size:12px; font-weight:700; color:var(--brand); }
.net-prog-track { height:6px; background:var(--bar-track); border-radius:3px; overflow:hidden; }
.net-prog-fill  { height:100%; border-radius:3px; background:var(--brand); width:0%; transition:width .5s ease; }
.net-step-card  { border-radius:10px; padding:12px 14px; margin-bottom:8px; }
html[data-theme="dark"]  .net-step-card { background:#080E1C; border:1px solid #1A2540; }
html[data-theme="light"] .net-step-card { background:rgba(26,86,196,.04); border:1px solid rgba(26,86,196,.10); }
.net-step-hdr  { display:flex; align-items:center; justify-content:space-between; margin-bottom:6px; }
.net-step-name { font-size:10px; font-weight:600; letter-spacing:0.08em; text-transform:uppercase; color:var(--txt2); }
.net-step-badge { font-size:10px; font-weight:600; padding:2px 9px; border-radius:999px; }
.net-step-main  { font-size:38px; font-weight:700; line-height:1.1; }
.net-step-unit  { font-size:16px; font-weight:400; }
.net-step-sub   { font-size:11px; color:var(--txt2); margin-top:3px; }
.net-ping-grid  { display:grid; grid-template-columns:repeat(3,1fr); gap:5px; margin-top:8px; }
.net-ping-item  { text-align:center; padding:6px 4px; border-radius:6px; }
html[data-theme="dark"]  .net-ping-item { background:rgba(255,255,255,.04); }
html[data-theme="light"] .net-ping-item { background:rgba(26,86,196,.06); }
.net-ping-srv   { font-size:9px; font-weight:500; color:var(--txt2); margin-bottom:2px; }
.net-ping-ms    { font-size:15px; font-weight:700; }
.net-summary-wrap { border-radius:12px; padding:16px; margin-top:4px; }
html[data-theme="dark"]  .net-summary-wrap { background:#080E1C; border:1px solid #1A2540; }
html[data-theme="light"] .net-summary-wrap { background:rgba(26,86,196,.05); border:1px solid rgba(26,86,196,.14); }
.net-sum-speeds { display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-bottom:12px; }
.net-sum-side   { text-align:center; padding:10px; border-radius:8px; }
html[data-theme="dark"]  .net-sum-side { background:rgba(255,255,255,.03); }
html[data-theme="light"] .net-sum-side { background:rgba(255,255,255,.6); }
.net-sum-arrow  { font-size:16px; margin-bottom:3px; }
.net-sum-big    { font-size:30px; font-weight:700; line-height:1; }
.net-sum-lbl    { font-size:10px; color:var(--txt2); margin-top:4px; }
.net-sum-stats  { display:grid; grid-template-columns:1fr 1fr; gap:6px; margin-bottom:12px; }
.net-sum-stat   { text-align:center; padding:8px; border-radius:8px; }
html[data-theme="dark"]  .net-sum-stat { background:rgba(255,255,255,.03); }
html[data-theme="light"] .net-sum-stat { background:rgba(255,255,255,.6); }
.net-sum-sv     { font-size:20px; font-weight:700; }
.net-sum-sl     { font-size:10px; color:var(--txt2); margin-top:2px; }
.net-badge-glob { text-align:center; font-size:13px; font-weight:600; padding:9px; border-radius:8px; margin-bottom:12px; }

/* ── Limpiar Sistema ───────────────────────────────────── */
.clean-cat-row  { display:flex; align-items:center; gap:10px; padding:9px 12px; border-radius:9px; margin-bottom:6px; }
html[data-theme="dark"]  .clean-cat-row { background:#080E1C; border:1px solid #1A2540; }
html[data-theme="light"] .clean-cat-row { background:rgba(26,86,196,.04); border:1px solid rgba(26,86,196,.10); }
.clean-cat-icon { font-size:16px; flex-shrink:0; }
.clean-cat-lbl  { flex:1; font-size:13px; color:var(--txt); }
.clean-cat-size { font-size:13px; font-weight:700; color:var(--brand); white-space:nowrap; }
.clean-total-row { display:flex; align-items:center; justify-content:space-between; padding:10px 12px; border-radius:9px; margin-top:4px; }
html[data-theme="dark"]  .clean-total-row { background:rgba(26,86,196,.12); border:1px solid rgba(26,86,196,.25); }
html[data-theme="light"] .clean-total-row { background:rgba(26,86,196,.08); border:1px solid rgba(26,86,196,.18); }
.clean-total-size { font-size:15px; font-weight:700; color:var(--brand); }
.clean-step-row { display:flex; align-items:center; justify-content:space-between; padding:7px 12px; border-radius:8px; margin-bottom:5px; font-size:13px; }
html[data-theme="dark"]  .clean-step-row { background:#080E1C; border:1px solid #1A2540; }
html[data-theme="light"] .clean-step-row { background:rgba(34,197,94,.05); border:1px solid rgba(34,197,94,.15); }
.clean-step-lbl { color:var(--txt); }
.clean-step-val { font-weight:700; color:var(--ok-text); }
.clean-done-wrap  { text-align:center; padding:24px 16px; border-radius:12px; }
html[data-theme="dark"]  .clean-done-wrap { background:rgba(34,197,94,.08); border:1px solid rgba(34,197,94,.20); }
html[data-theme="light"] .clean-done-wrap { background:rgba(34,197,94,.06); border:1px solid rgba(34,197,94,.18); }
.clean-done-icon  { font-size:36px; margin-bottom:8px; }
.clean-done-title { font-size:16px; font-weight:700; color:var(--txt); margin-bottom:4px; }
.clean-done-freed { font-size:13px; color:var(--txt2); }

/* ── Fix pointer-events cleanModal (EdgeWebView2) ─────── */
#cleanModal.open                { pointer-events: none; }
#cleanModal .chk-modal-card     { pointer-events: auto !important; position: relative; z-index: 600; }
#cleanModal .btn,
#cleanModal .modal-x            { position: relative; z-index: 99999; pointer-events: auto !important; }

/* ── Fix pointer-events adnModal (EdgeWebView2) ──────── */
#adnModal.open                  { pointer-events: none; }
#adnModal .chk-modal-card       { pointer-events: auto !important; position: relative; z-index: 600; }
#adnModal .modal-x              { position: relative; z-index: 99999; pointer-events: auto !important; }

/* ── Estilos internos modal ADN ──────────────────────── */
.adn-section { margin-bottom: 18px; }
.adn-section-title {
  font-size: 11px; font-weight: 700; text-transform: uppercase;
  letter-spacing: 0.08em; color: #1E3A8A; margin-bottom: 8px;
  padding-bottom: 4px; border-bottom: 1px solid rgba(30,58,138,0.18);
}
html[data-theme="dark"] .adn-section-title { color: #93c5fd; border-color: rgba(147,197,253,0.2); }
.adn-row {
  display: flex; justify-content: space-between; align-items: baseline;
  gap: 12px; padding: 4px 0; font-size: 12px;
}
.adn-label { color: var(--txt2); flex-shrink: 0; }
.adn-value { color: #374151; font-weight: 600; text-align: right; word-break: break-word; }
html[data-theme="dark"] .adn-value { color: #e2e8f0; }
.adn-gpu-block { padding: 6px 0; }
.adn-gpu-block + .adn-gpu-block { border-top: 1px dashed rgba(128,128,128,0.2); padding-top: 10px; margin-top: 4px; }
.btn-hdr-adn {
  background: rgba(255,255,255,0.12);
  border: 1px solid rgba(255,255,255,0.30);
  color: #ffffff;
  border-radius: 8px;
  padding: 4px 12px;
  font-size: 12px;
  font-weight: 600;
  font-family: 'Plus Jakarta Sans', sans-serif;
  cursor: pointer;
  transition: background 0.2s ease;
  margin-top: 4px;
}
.btn-hdr-adn:hover { background: rgba(255,255,255,0.22); }
</style>
</head>
<body>

<header class="hdr">
  <img id="logo" class="hdr-logo" src="" alt="PC HOUSE">
  <div>
    <div class="hdr-title">PC HOUSE &mdash; Diagn&oacute;stico PC</div>
    <div class="hdr-sub">Herramienta de diagn&oacute;stico t&eacute;cnico profesional</div>
  </div>
  <div class="hdr-right">
    <div class="hdr-host" id="host">—</div>
    <div class="hdr-clock" id="clock">—</div>
    <button class="btn-hdr-adn" onclick="abrirModalADN()">&#x1F9EC; ADN del Equipo</button>
  </div>
  <button class="theme-btn" id="themeBtn" onclick="toggleTheme()" title="Cambiar tema">
    <span id="themeIcon"></span>
  </button>
</header>

<div class="section-label">MONITOREO EN TIEMPO REAL</div>
<div class="metrics">
  <div class="card">
    <div class="card-hdr"><span class="card-icon" id="iconCPU"></span><span class="card-lbl">Procesador</span><span class="status-pill ok" id="cpuStatus">&#x25CF; &Oacute;PTIMO</span></div>
    <div style="display:flex;align-items:baseline;gap:3px">
      <span class="card-val" id="cpuPct">0</span><span class="card-unit">%</span>
    </div>
    <div class="card-sub" id="cpuMod">—</div>
    <canvas id="cpuCanvas"></canvas>
  </div>
  <div class="card">
    <div class="card-hdr"><span class="card-icon" id="iconRAM"></span><span class="card-lbl">Memoria RAM</span><span class="status-pill ok" id="ramStatus">&#x25CF; &Oacute;PTIMO</span></div>
    <div style="display:flex;align-items:baseline;gap:3px">
      <span class="card-val" id="ramPct">0</span><span class="card-unit">%</span>
    </div>
    <div class="card-sub" id="ramSub">—</div>
    <canvas id="ramCanvas"></canvas>
    <div class="card-sub" id="ramInfo" style="margin-top:6px;font-size:11.5px;opacity:.8">—</div>
  </div>
  <div class="card">
    <div class="card-hdr"><span class="card-icon" id="iconHDD"></span><span class="card-lbl">Almacenamiento</span><span class="status-pill ok">&#x25CF; SALUDABLE</span></div>
    <div id="diskHealth"><div class="disk-loading">Consultando estado de discos&hellip;</div></div>
    <div id="diskList" class="disk-partitions" style="display:none"></div>
    <div class="section-label in-card">HERRAMIENTAS R&Aacute;PIDAS</div>
    <div class="tool-btns-row">
      <button class="btn-tool-card" onclick="openChkdskModal()">
        <span class="tool-icon">&#x1F527;</span>
        <span>Reparar Unidad</span>
      </button>
      <button class="btn-tool-card" onclick="openWinHealModal()">
        <img id="winLogoIcon" class="btn-win-icon" src="" alt="&#x229E;">
        <span>Sanar Windows</span>
      </button>
      <button class="btn-tool-card" onclick="openWinUtilModal()">
        <span class="tool-icon">&#x26A1;</span>
        <span>Optimizar Windows</span>
      </button>
    </div>
  </div>
</div>

<div class="section-label">TALLER DE SOFTWARE</div>
<div class="sw-section">
  <div class="sw-cards-row">
    <div class="sw-card">
      <div class="sw-card-hdr">
        <span class="sw-card-icon">&#x1F4E6;</span>
        <span class="sw-card-title">Office 365</span>
      </div>
      <div class="sw-card-btns">
        <button class="btn-sw" onclick="openOfficeDeployModal()">&#x2B07;&#xFE0F; Desplegar Office</button>
        <button class="btn-sw" onclick="openOfficeActivateModal()">&#x1F511; Activar Office</button>
      </div>
    </div>
    <div class="sw-card">
      <div class="sw-card-hdr">
        <span class="sw-card-icon">&#x1F680;</span>
        <span class="sw-card-title">Kit Esencial Windows</span>
      </div>
      <div class="sw-card-desc">Instalador de programas b&aacute;sicos para equipos nuevos o reci&eacute;n formateados</div>
      <div class="sw-card-btns">
        <button class="btn-sw" onclick="openNiniteModal()">&#x26A1; Instalar Programas Esenciales</button>
      </div>
    </div>
  </div>
</div>

<div class="section-label">DIAGN&Oacute;STICO</div>
<div class="diag-actions">
  <button class="btn btn-p" id="btnGenVisual" onclick="doGenVisual()">&#x1F4CA; Generar Reporte Visual</button>
  <button class="btn btn-s" id="btnOpenFolder" onclick="doOpenFolder()" style="display:none">&#x1F5BC;&#xFE0F; Abrir Reporte</button>
  <button class="btn-glass" onclick="openThermoModal()">&#x1F321;&#xFE0F; Term&oacute;metro</button>
  <button class="btn-glass" onclick="openNetModal()">&#x26A1; Pulso de Red</button>
  <button class="btn-glass" onclick="limpiarSistema()">&#x1F9F9; Limpiar Sistema</button>
</div>
<div class="rep-wrap">
  <div class="rep-body">
    <div id="emptyState" class="empty-state">
      <img id="mascotMain" src="" alt="">
      <p class="empty-title">Listo para diagnosticar</p>
      <p class="empty-sub">Presion&aacute; &#x1F4CA; Generar Reporte Visual para crear la imagen</p>
    </div>
    <img id="repPreview" style="display:none" alt="Vista previa del reporte">
  </div>
</div>

<div class="sbar">
  <div class="spin" id="spin"></div>
  <div class="dot" id="dot"></div>
  <span id="sMsg">Listo &mdash; presion&aacute; Generar Reporte Visual para comenzar</span>
</div>

<div id="diskModal" class="modal-ov" onclick="closeModalOv(event)">
  <div class="modal-card">
    <button class="modal-x" onclick="closeDiskModal()">&#x2715;</button>
    <div id="modalContent"></div>
  </div>
</div>

<div id="chkdskModal" class="modal-ov" onclick="closeChkdskOv(event)">
  <div class="chk-modal-card">
    <button class="modal-x" onclick="closeChkdskModal()">&#x2715;</button>
    <div class="chk-confirm-strip" id="chkConfirmStrip">
      <div class="chk-confirm-title">Hay una revisi&oacute;n en curso. &iquest;Cerrar de todas formas?</div>
      <div class="chk-confirm-btns">
        <button class="btn-chk-confirm-yes" onclick="_doCloseChkdsk()">S&iacute;, cerrar</button>
        <button class="btn-chk-confirm-no" onclick="document.getElementById('chkConfirmStrip').classList.remove('visible')">Cancelar</button>
      </div>
    </div>
    <div style="font-size:16px;font-weight:700;margin-bottom:4px;color:var(--txt)">&#x1F527; Sanar Disco</div>
    <div style="font-size:12px;color:var(--txt2);margin-bottom:16px">Selecion&aacute; una partici&oacute;n y el tipo de revisi&oacute;n</div>
    <div style="font-size:11px;font-weight:600;letter-spacing:.5px;text-transform:uppercase;color:var(--txt2);margin-bottom:8px">Particiones disponibles</div>
    <div class="chk-vol-list" id="chkVolList"><div class="modal-loading">Detectando particiones&hellip;</div></div>
    <div style="font-size:11px;font-weight:600;letter-spacing:.5px;text-transform:uppercase;color:var(--txt2);margin-bottom:8px">Tipo de revisi&oacute;n</div>
    <div class="chk-opts-wrap">
      <div class="chk-opt-row sel" id="chkOptQuick" onclick="selectChkMode('quick')">
        <input class="chk-opt-radio" type="radio" name="chkMode" checked>
        <div><div class="chk-opt-title">Revisi&oacute;n r&aacute;pida</div>
             <div class="chk-opt-desc">Repara errores de sistema de archivos &mdash; <code>chkdsk /f</code></div></div>
      </div>
      <div class="chk-opt-row" id="chkOptFull" onclick="selectChkMode('full')">
        <input class="chk-opt-radio" type="radio" name="chkMode">
        <div><div class="chk-opt-title">Revisi&oacute;n completa</div>
             <div class="chk-opt-desc">Repara errores y busca sectores defectuosos &mdash; m&aacute;s lento &mdash; <code>chkdsk /f /r</code></div></div>
      </div>
    </div>
    <button class="btn-chk-start" id="btnChkStart" onclick="startChkdsk()" disabled>Iniciar revisi&oacute;n</button>
    <div class="chk-out-wrap" id="chkOutWrap">
      <div class="chk-prog-wrap">
        <div class="chk-prog-lbl" id="chkProgLbl">Procesando...</div>
        <div class="chk-prog-track"><div class="chk-prog-fill" id="chkProgFill"></div></div>
      </div>
      <div style="font-size:11px;font-weight:600;letter-spacing:.5px;text-transform:uppercase;color:var(--txt2);margin-bottom:6px">Salida del proceso</div>
      <div class="chk-out-box" id="chkOutBox"></div>
    </div>
    <div class="chk-result-msg" id="chkResultMsg"></div>
  </div>
</div>

<div id="winHealModal" class="modal-ov" onclick="closeWinHealOv(event)">
  <div class="chk-modal-card">
    <button class="modal-x" onclick="closeWinHealModal()">&#x2715;</button>
    <div class="chk-confirm-strip" id="whConfirmStrip">
      <div class="chk-confirm-title">Hay un proceso en curso. ¿Cerrar de todas formas?</div>
      <div class="chk-confirm-btns">
        <button class="btn-chk-confirm-yes" onclick="_doCloseWinHeal()">S&iacute;, cerrar</button>
        <button class="btn-chk-confirm-no" onclick="document.getElementById('whConfirmStrip').classList.remove('visible')">Cancelar</button>
      </div>
    </div>
    <div style="font-size:16px;font-weight:700;margin-bottom:4px;color:var(--txt)">&#x229E; Sanar Windows</div>
    <div style="font-size:12px;color:var(--txt2);margin-bottom:12px">Herramientas de reparaci&oacute;n del sistema operativo</div>
    <div class="wh-note">&#x1F4A1; Orden recomendado: ejecut&aacute; primero las herramientas DISM en orden (1&rarr;2&rarr;3) y luego SFC al final para mejores resultados.</div>
    <div class="wh-tools">
      <div class="wh-tool-card">
        <div class="wh-tool-hdr">
          <div class="wh-tool-info">
            <div class="wh-tool-name">1 &mdash; DISM: Verificaci&oacute;n r&aacute;pida</div>
            <div class="wh-tool-cmd">DISM /Online /Cleanup-Image /CheckHealth</div>
            <div class="wh-tool-desc">Verifica si la imagen de Windows tiene alg&uacute;n problema registrado. Es instant&aacute;neo y no modifica nada.</div>
          </div>
          <button class="btn-wh-run" id="btnWh0" onclick="runWinHeal('check', this)">Ejecutar</button>
        </div>
      </div>
      <div class="wh-tool-card">
        <div class="wh-tool-hdr">
          <div class="wh-tool-info">
            <div class="wh-tool-name">2 &mdash; DISM: An&aacute;lisis profundo</div>
            <div class="wh-tool-cmd">DISM /Online /Cleanup-Image /ScanHealth</div>
            <div class="wh-tool-desc">Analiza en detalle si hay archivos corruptos en la imagen de Windows. Puede tardar varios minutos.</div>
          </div>
          <button class="btn-wh-run" id="btnWh1" onclick="runWinHeal('scan', this)">Ejecutar</button>
        </div>
      </div>
      <div class="wh-tool-card">
        <div class="wh-tool-hdr">
          <div class="wh-tool-info">
            <div class="wh-tool-name">3 &mdash; DISM: Reparaci&oacute;n completa</div>
            <div class="wh-tool-cmd">DISM /Online /Cleanup-Image /RestoreHealth</div>
            <div class="wh-tool-desc">Descarga y repara autom&aacute;ticamente los archivos corruptos de Windows desde los servidores de Microsoft. Requiere internet y puede tardar bastante.</div>
          </div>
          <button class="btn-wh-run" id="btnWh2" onclick="runWinHeal('restore', this)">Ejecutar</button>
        </div>
      </div>
      <div class="wh-tool-card">
        <div class="wh-tool-hdr">
          <div class="wh-tool-info">
            <div class="wh-tool-name">4 &mdash; SFC: Verificar archivos del sistema</div>
            <div class="wh-tool-cmd">sfc /scannow</div>
            <div class="wh-tool-desc">Escanea y repara archivos protegidos del sistema operativo. Se recomienda ejecutar despu&eacute;s de DISM Reparaci&oacute;n completa.</div>
          </div>
          <button class="btn-wh-run" id="btnWh3" onclick="runWinHeal('sfc', this)">Ejecutar</button>
        </div>
      </div>
    </div>
    <div class="chk-out-wrap" id="whOutWrap" style="display:none">
      <div class="chk-prog-wrap">
        <div class="chk-prog-lbl" id="whProgLbl">Procesando...</div>
        <div class="chk-prog-track"><div class="chk-prog-fill" id="whProgFill"></div></div>
      </div>
      <div style="font-size:11px;font-weight:600;letter-spacing:.5px;text-transform:uppercase;color:var(--txt2);margin-bottom:6px">Salida del proceso</div>
      <div class="chk-out-box" id="whOutBox"></div>
    </div>
    <div class="chk-result-msg" id="whResultMsg"></div>
  </div>
</div>

<div id="winUtilModal" class="modal-ov" onclick="closeWinUtilOv(event)">
  <div class="chk-modal-card" style="max-width:420px">
    <button class="modal-x" onclick="closeWinUtilModal()">&#x2715;</button>
    <div style="font-size:16px;font-weight:700;margin-bottom:10px;color:var(--txt)">&#x26A1; Optimizar Windows</div>
    <div style="font-size:13px;color:var(--txt2);line-height:1.65;margin-bottom:18px">Se abrir&aacute; <strong style="color:var(--txt)">WinUtil</strong> de Chris Titus Tech en una ventana de PowerShell como administrador. &iquest;Des&eacute;as continuar?</div>
    <div id="wuErrorMsg" style="font-size:12px;color:var(--red);margin-bottom:10px;display:none"></div>
    <div style="display:flex;gap:10px;justify-content:flex-end">
      <button class="btn-chk-confirm-no" onclick="closeWinUtilModal()">Cancelar</button>
      <button class="btn-wu-confirm" id="btnWuConfirm" onclick="confirmWinUtil()">Continuar</button>
    </div>
  </div>
</div>

<div id="officeDeployModal" class="modal-ov" onclick="closeOfficeDeployOv(event)">
  <div class="chk-modal-card" style="max-width:440px">
    <button class="modal-x" onclick="closeOfficeDeployModal()">&#x2715;</button>
    <div style="font-size:16px;font-weight:700;margin-bottom:10px;color:var(--txt)">&#x2B07;&#xFE0F; Desplegar Office 365</div>
    <div style="font-size:13px;color:var(--txt2);line-height:1.65;margin-bottom:18px">Se iniciar&aacute; la instalaci&oacute;n de Office 365 usando los archivos locales. Aseg&uacute;rate de que el instalador est&eacute; en la carpeta <strong style="color:var(--txt)">tools/office</strong> antes de continuar.</div>
    <div id="officeDeployErr" style="font-size:12px;color:var(--red);margin-bottom:10px;display:none"></div>
    <div style="display:flex;gap:10px;justify-content:flex-end">
      <button class="btn-chk-confirm-no" onclick="closeOfficeDeployModal()">Cancelar</button>
      <button class="btn-wu-confirm" id="btnOfficeDeploy" onclick="confirmOfficeDeploy()">Instalar</button>
    </div>
  </div>
</div>

<div id="officeActivateModal" class="modal-ov" onclick="closeOfficeActivateOv(event)">
  <div class="chk-modal-card" style="max-width:440px">
    <button class="modal-x" onclick="closeOfficeActivateModal()">&#x2715;</button>
    <div style="font-size:16px;font-weight:700;margin-bottom:10px;color:var(--txt)">&#x1F511; Activar Office</div>
    <div style="font-size:13px;color:var(--txt2);line-height:1.65;margin-bottom:18px">Se abrir&aacute; la herramienta de activaci&oacute;n en una ventana de PowerShell como administrador. Segu&iacute; las instrucciones que aparezcan en pantalla.</div>
    <div id="officeActivateErr" style="font-size:12px;color:var(--red);margin-bottom:10px;display:none"></div>
    <div style="display:flex;gap:10px;justify-content:flex-end">
      <button class="btn-chk-confirm-no" onclick="closeOfficeActivateModal()">Cancelar</button>
      <button class="btn-wu-confirm" id="btnOfficeActivate" onclick="confirmOfficeActivate()">Continuar</button>
    </div>
  </div>
</div>

<div id="niniteModal" class="modal-ov" onclick="closeNiniteOv(event)">
  <div class="chk-modal-card" style="max-width:440px">
    <button class="modal-x" onclick="closeNiniteModal()">&#x2715;</button>
    <div style="font-size:16px;font-weight:700;margin-bottom:10px;color:var(--txt)">&#x1F680; Kit Esencial Windows</div>
    <div style="font-size:13px;color:var(--txt2);line-height:1.65;margin-bottom:18px">Se ejecutar&aacute; el instalador de programas esenciales. Este proceso puede tardar varios minutos dependiendo de tu conexi&oacute;n a internet. &iquest;Deseas continuar?</div>
    <div id="niniteErr" style="font-size:12px;color:var(--red);margin-bottom:10px;display:none"></div>
    <div style="display:flex;gap:10px;justify-content:flex-end">
      <button class="btn-chk-confirm-no" onclick="closeNiniteModal()">Cancelar</button>
      <button class="btn-wu-confirm" id="btnNinite" onclick="confirmNinite()">Iniciar</button>
    </div>
  </div>
</div>

<div id="thermoModal" class="modal-ov" onclick="closeThermoOv(event)">
  <div class="chk-modal-card" style="max-width:480px">
    <button class="modal-x" onclick="closeThermoModal()">&#x2715;</button>
    <div style="font-size:16px;font-weight:700;margin-bottom:4px;color:var(--txt)">&#x1F321;&#xFE0F; Term&oacute;metro del Sistema</div>
    <div style="font-size:12px;color:var(--txt2);margin-bottom:16px">Temperaturas en tiempo real &mdash; actualizaci&oacute;n cada 2 s</div>
    <div id="thermoList"><div class="modal-loading">Leyendo sensores&hellip;</div></div>
  </div>
</div>

<div id="netModal" class="modal-ov" onclick="closeNetOv(event)">
  <div class="chk-modal-card" style="max-width:500px">
    <button class="modal-x" onclick="closeNetModal()">&#x2715;</button>
    <div style="font-size:16px;font-weight:700;margin-bottom:4px;color:var(--txt)">&#x26A1; Pulso de Red</div>
    <div style="font-size:12px;color:var(--txt2);margin-bottom:16px">An&aacute;lisis completo de tu conexi&oacute;n a internet</div>

    <!-- Info section (visible al abrir) -->
    <div id="netInfoSection">
      <div class="net-info-grid">
        <div class="net-info-item"><div class="net-info-lbl">IP Local</div><div class="net-info-val" id="netLocalIP">&hellip;</div></div>
        <div class="net-info-item"><div class="net-info-lbl">IP P&uacute;blica</div><div class="net-info-val" id="netPublicIP">&hellip;</div></div>
        <div class="net-info-item"><div class="net-info-lbl">Red</div><div class="net-info-val" id="netSSID">&hellip;</div></div>
        <div class="net-info-item"><div class="net-info-lbl">DNS</div><div class="net-info-val" id="netDNS">&hellip;</div></div>
      </div>
      <div style="display:flex;align-items:center;margin-bottom:16px;font-size:13px;font-weight:600;color:var(--txt)">
        <span class="net-conn-dot" id="netDot"></span><span id="netStatusTxt">Verificando&hellip;</span>
      </div>
    </div>

    <!-- Botón iniciar (visible al abrir) -->
    <div id="netStartSection">
      <button class="btn btn-p" style="width:100%;padding:11px;border-radius:10px;font-size:14px" onclick="startNetTest()">Iniciar Test</button>
    </div>

    <!-- Progreso + resultados (oculto al inicio) -->
    <div id="netTestBody" style="display:none">
      <div class="net-prog-wrap">
        <div class="net-prog-top">
          <span class="net-prog-step" id="netStepLabel">Iniciando&hellip;</span>
          <span class="net-prog-pct"  id="netPctLabel">0%</span>
        </div>
        <div class="net-prog-track"><div class="net-prog-fill" id="netProgFill"></div></div>
      </div>
      <div id="netResults"></div>
    </div>

    <!-- Resumen final (oculto al inicio) -->
    <div id="netSummaryEl" style="display:none"></div>
    <div id="netRepeatSection" style="display:none;margin-top:12px">
      <button class="btn btn-s" style="width:100%;padding:10px;border-radius:10px" onclick="startNetTest()">&#x1F504; Repetir Test</button>
    </div>
  </div>
</div>

<div id="cleanModal" class="modal-ov">
  <div class="chk-modal-card" style="max-width:460px">
    <button class="modal-x" onclick="closeCleanModal()">&#x2715;</button>
    <div style="font-size:16px;font-weight:700;margin-bottom:4px;color:var(--txt)">&#x1F9F9; Limpiar Sistema</div>
    <div style="font-size:12px;color:var(--txt2);margin-bottom:16px">Libera espacio eliminando archivos temporales y cach&eacute;</div>

    <!-- Categorías del análisis (spinner → filas) -->
    <div id="cleanAnalyzeBody">
      <div class="modal-loading">&#x1F50D; Analizando sistema&hellip;</div>
    </div>

    <!-- Total (oculto hasta que termina el análisis) -->
    <div id="cleanTotalRow" class="clean-total-row" style="display:none;margin-top:8px">
      <span style="font-weight:700;color:var(--txt)">Total a liberar</span>
      <span id="cleanTotalVal" class="clean-total-size">—</span>
    </div>

    <!-- Botón Limpiar Ahora — siempre en el DOM, solo se muestra/oculta -->
    <button id="cleanLimpiarBtn"
            class="btn btn-p"
            style="display:none;width:100%;margin-top:14px;padding:11px;border-radius:10px;font-size:14px"
            onclick="limpiarAhora()">
      &#x1F9F9; Limpiar Ahora
    </button>

    <!-- Barra de progreso (oculta hasta que empieza la limpieza) -->
    <div id="cleanup-progress" style="display:none;margin-top:20px;">
      <div id="cleanup-status-text"
           style="font-size:13px;color:var(--txt2);margin-bottom:10px;text-align:center;">
        Limpiando&hellip;
      </div>
      <div style="background:var(--bar-track);border-radius:99px;overflow:hidden;height:10px;">
        <div id="cleanup-bar"
             style="width:0%;height:100%;border-radius:99px;
                    background:linear-gradient(90deg,#3B5BDB,#1E3A8A);
                    transition:width 0.4s ease;">
        </div>
      </div>
    </div>

    <!-- Mensaje final de éxito (oculto hasta que termina) -->
    <div id="cleanup-final" style="display:none;text-align:center;padding:20px 0;">
      <div style="width:72px;height:72px;border-radius:50%;
                  background:linear-gradient(135deg,#3B5BDB,#1E3A8A);
                  margin:0 auto 16px;display:flex;align-items:center;
                  justify-content:center;font-size:36px;">
        &#x1F4A5;
      </div>
      <div style="font-size:22px;font-weight:800;color:#1E3A8A;margin-bottom:8px;">
        &iexcl;Archivos basura&hellip; desintegrados!
      </div>
      <div style="font-size:15px;color:var(--txt2);margin-bottom:6px;">
        Limpieza completada exitosamente
      </div>
      <div style="font-size:28px;font-weight:700;color:#3B5BDB;margin:12px 0;">
        <span id="cleanup-freed-amount">0 MB</span> liberados
      </div>
      <div style="font-size:13px;color:var(--txt2);margin-bottom:20px;">
        Tu equipo opera al m&aacute;ximo ahora &#x1F680;
      </div>
      <button onclick="cerrarModalLimpieza()"
              style="background:linear-gradient(135deg,#3B5BDB,#1E3A8A);
                     color:#fff;border:none;border-radius:12px;
                     padding:12px 32px;font-size:15px;font-weight:600;
                     cursor:pointer;position:relative;z-index:99999;
                     pointer-events:auto !important;">
        Cerrar
      </button>
    </div>
  </div>
</div>

<div id="adnModal" class="modal-ov">
  <div class="chk-modal-card" style="max-width:520px">
    <button class="modal-x" onclick="cerrarModalADN()">&#x2715;</button>
    <div style="font-size:16px;font-weight:700;margin-bottom:4px;color:var(--txt)">&#x1F9EC; ADN del Equipo</div>
    <div style="font-size:12px;color:var(--txt2);margin-bottom:16px">Informaci&oacute;n detallada del hardware y sistema</div>
    <div id="adnBody">
      <div class="modal-loading">&#x1F50D; Leyendo ADN del sistema&hellip;</div>
    </div>
  </div>
</div>

<script>
const IC = {
  cpu:  `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><line x1="9" y1="1" x2="9" y2="4"/><line x1="15" y1="1" x2="15" y2="4"/><line x1="9" y1="20" x2="9" y2="23"/><line x1="15" y1="20" x2="15" y2="23"/><line x1="20" y1="9" x2="23" y2="9"/><line x1="20" y1="14" x2="23" y2="14"/><line x1="1" y1="9" x2="4" y2="9"/><line x1="1" y1="14" x2="4" y2="14"/></svg>`,
  ram:  `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M6 19v-3"/><path d="M10 19v-3"/><path d="M14 19v-3"/><path d="M18 19v-3"/><path d="M8 11V9"/><path d="M16 11V9"/><path d="M12 11V9"/><path d="M2 15h20"/><path d="M2 7a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v1.1a2 2 0 0 0 0 3.837V17a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2v-5.1a2 2 0 0 0 0-3.837Z"/></svg>`,
  hdd:  `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14a9 3 0 0 0 18 0V5"/><path d="M3 12a9 3 0 0 0 18 0"/></svg>`,
  sun:  `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/></svg>`,
  moon: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>`,
};
document.getElementById('iconCPU').innerHTML = IC.cpu;
document.getElementById('iconRAM').innerHTML = IC.ram;
document.getElementById('iconHDD').innerHTML = IC.hdd;

function updateThemeIcon(t) {
  document.getElementById('themeIcon').innerHTML = t === 'light' ? IC.moon : IC.sun;
}
function toggleTheme() {
  const next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem('pch-theme', next);
  updateThemeIcon(next);
  drawCPU(cpuHist);
}
updateThemeIcon(document.documentElement.getAttribute('data-theme') || 'light');

const cpuHist = new Array(60).fill(0);
let _cpuDisp   = new Array(60).fill(0);
let _cpuAnim   = null;
let reportReady = false, lastReport = '', lastReportPath = null;

function tickClock() {
  const n = new Date();
  document.getElementById('clock').textContent =
    n.toLocaleDateString('es-AR') + '  ' + n.toLocaleTimeString('es-AR');
}
setInterval(tickClock, 1000); tickClock();

function drawCPU(hist) {
  if (!_cpuAnim) _cpuAnim = requestAnimationFrame(_cpuStep);
}
function _cpuStep() {
  let dirty = false;
  for (let i = 0; i < 60; i++) {
    const d = cpuHist[i] - _cpuDisp[i];
    if (Math.abs(d) > 0.05) { _cpuDisp[i] += d * 0.14; dirty = true; }
    else _cpuDisp[i] = cpuHist[i];
  }
  _drawCPUFrame(_cpuDisp);
  _cpuAnim = dirty ? requestAnimationFrame(_cpuStep) : null;
}
function _drawCPUFrame(hist) {
  const c = document.getElementById('cpuCanvas');
  const dpr = window.devicePixelRatio || 1;
  const r = c.getBoundingClientRect();
  if (!r.width) return;
  c.width = r.width * dpr; c.height = r.height * dpr;
  const ctx = c.getContext('2d');
  ctx.scale(dpr, dpr);
  const W = r.width, H = r.height, n = hist.length, sx = W / (n - 1);
  const light = document.documentElement.getAttribute('data-theme') === 'light';
  ctx.fillStyle = light ? '#EEF2FF' : '#0A0A12';
  ctx.fillRect(0, 0, W, H);
  const pts = hist.map((v, i) => [i * sx, H - (v / 100) * (H - 4) - 2]);
  const gFill = ctx.createLinearGradient(0, 0, 0, H);
  if (light) {
    gFill.addColorStop(0,   'rgba(0,57,166,0.35)');
    gFill.addColorStop(0.6, 'rgba(0,180,216,0.12)');
    gFill.addColorStop(1,   'rgba(0,57,166,0.02)');
  } else {
    gFill.addColorStop(0,   'rgba(123,47,190,0.72)');
    gFill.addColorStop(0.55,'rgba(200,64,180,0.28)');
    gFill.addColorStop(1,   'rgba(255,107,53,0.05)');
  }
  ctx.fillStyle = gFill;
  ctx.beginPath();
  ctx.moveTo(0, H);
  ctx.lineTo(pts[0][0], pts[0][1]);
  for (let i = 1; i < pts.length; i++) {
    const cpx = (pts[i-1][0] + pts[i][0]) / 2;
    ctx.bezierCurveTo(cpx, pts[i-1][1], cpx, pts[i][1], pts[i][0], pts[i][1]);
  }
  ctx.lineTo((n-1)*sx, H);
  ctx.closePath();
  ctx.fill();
  const gLine = ctx.createLinearGradient(0, 0, W, 0);
  if (light) {
    gLine.addColorStop(0, '#0039A6');
    gLine.addColorStop(1, '#00B4D8');
  } else {
    gLine.addColorStop(0, '#FF6B35');
    gLine.addColorStop(0.5, '#E040FB');
    gLine.addColorStop(1, '#7B2FBE');
  }
  ctx.strokeStyle = gLine;
  ctx.lineWidth = 2;
  ctx.lineJoin = 'round';
  ctx.beginPath();
  ctx.moveTo(pts[0][0], pts[0][1]);
  for (let i = 1; i < pts.length; i++) {
    const cpx = (pts[i-1][0] + pts[i][0]) / 2;
    ctx.bezierCurveTo(cpx, pts[i-1][1], cpx, pts[i][1], pts[i][0], pts[i][1]);
  }
  ctx.stroke();
}

let _lastRamPct = 0;
function drawRAM(pct) {
  const c = document.getElementById('ramCanvas');
  if (!c) return;
  const dpr = window.devicePixelRatio || 1;
  const r = c.getBoundingClientRect();
  if (!r.width) return;
  c.width = r.width * dpr; c.height = r.height * dpr;
  const ctx = c.getContext('2d');
  ctx.scale(dpr, dpr);
  const W = r.width, H = r.height;
  const light = document.documentElement.getAttribute('data-theme') === 'light';
  const N = 28, gap = 4;
  const segW = (W - gap * (N - 1)) / N;
  const segR = 3;
  const filled = Math.round(pct / 100 * N);
  const g = ctx.createLinearGradient(0, 0, W, 0);
  if (light) {
    g.addColorStop(0, '#0039A6');
    g.addColorStop(1, '#00B4D8');
  } else {
    g.addColorStop(0,    '#FF6B35');
    g.addColorStop(0.5,  '#E040FB');
    g.addColorStop(1,    '#7B2FBE');
  }
  const inactiveColor = light ? '#E0E0E0' : '#2A2A3E';
  for (let i = 0; i < N; i++) {
    const x = i * (segW + gap);
    ctx.fillStyle = i < filled ? g : inactiveColor;
    ctx.beginPath();
    if (ctx.roundRect) { ctx.roundRect(x, 0, segW, H, segR); } else { ctx.rect(x, 0, segW, H); }
    ctx.fill();
  }
}
function setRAM(pct) {
  _lastRamPct = pct;
  drawRAM(pct);
}

function setStatus(msg, state) {
  document.getElementById('sMsg').textContent = msg;
  document.getElementById('dot').className  = 'dot'+(state?' '+state:'');
  document.getElementById('spin').className = 'spin'+(state==='busy'?' on':'');
}

function applyMetrics(raw) {
  const d = typeof raw==='string'?JSON.parse(raw):raw;
  document.getElementById('cpuPct').textContent = Math.round(d.cpu);
  if (d.cpu_model) document.getElementById('cpuMod').textContent = d.cpu_model;
  if (d.hostname)  document.getElementById('host').textContent  = d.hostname;
  cpuHist.splice(0,cpuHist.length,...(d.cpu_history||cpuHist));
  drawCPU(cpuHist);
  document.getElementById('ramPct').textContent = Math.round(d.ram_pct);
  document.getElementById('ramSub').textContent = d.ram_used+' / '+d.ram_total;
  setRAM(d.ram_pct);
  if (d.ram_info) {
    const ri = d.ram_info;
    const parts = [];
    if (ri.type && ri.type !== 'N/D') parts.push(ri.type);
    if (ri.freq && ri.freq !== 'N/D') parts.push(ri.freq + ' MHz');
    if (ri.slots && ri.slots !== 'N/D') parts.push(ri.slots);
    document.getElementById('ramInfo').textContent = parts.join('  ·  ') || '—';
  }
  // Status pills
  const _sc = (v, w, c) => v >= c ? 'crit' : v >= w ? 'warn' : 'ok';
  const _sl = {ok: '● ÓPTIMO', warn: '● MODERADO', crit: '● CRÍTICO'};
  const cpuEl = document.getElementById('cpuStatus');
  if (cpuEl) { const cls = _sc(d.cpu, 70, 90); cpuEl.className = 'status-pill '+cls; cpuEl.textContent = _sl[cls]; }
  const ramEl = document.getElementById('ramStatus');
  if (ramEl) { const cls = _sc(d.ram_pct, 70, 85); ramEl.className = 'status-pill '+cls; ramEl.textContent = _sl[cls]; }
}
function pollMetrics() {
  if (!window.pywebview||!window.pywebview.api) return;
  window.pywebview.api.get_metrics().then(applyMetrics).catch(()=>{});
}
setInterval(pollMetrics, 2000);

function pollDiskActivity() {
  if (!window.pywebview||!window.pywebview.api) return;
  window.pywebview.api.get_disk_activity().then(raw => {
    const act = JSON.parse(raw);
    document.querySelectorAll('.bay[data-disk-num]').forEach(bay => {
      const num = bay.dataset.diskNum;
      const key = 'PhysicalDrive' + num;
      const pct = act[key] !== undefined ? act[key] : 0;
      const fill  = document.getElementById('bay-act-' + num);
      const label = document.getElementById('bay-pct-' + num);
      if (fill) {
        fill.style.height = pct + '%';
        fill.style.background = pct >= 80 ? '#EF4444' : pct >= 50 ? '#F59E0B' : '#10B981';
      }
      if (label) label.textContent = Math.round(pct) + '%';
    });
  }).catch(()=>{});
}
setInterval(pollDiskActivity, 1000);

function renderDiskHealth(disks) {
  const el = document.getElementById('diskHealth');
  if (!disks||!disks.length){el.innerHTML='<div class="disk-loading">No se detectaron unidades. Verificá los permisos del programa.</div>';return;}
  const bays = disks.map((d,i) => {
    const dotCls =
      d.health==='Healthy'   ? 'good' :
      d.health==='Warning'   ? 'warn' :
      d.health==='Unhealthy' ? 'bad'  : 'unk';
    const label =
      d.health==='Healthy'   ? 'Bueno'    :
      d.health==='Warning'   ? 'En Riesgo':
      d.health==='Unhealthy' ? 'Dañado'   : (d.health||'N/D');
    const esc = s => String(s).replace(/&/g,'&amp;').replace(/"/g,'&quot;');
    const dnum = (d.disk_num!==undefined&&d.disk_num!==''&&d.disk_num!==null) ? String(d.disk_num) : String(i);
    return `<div class="bay"
  title="${esc(d.name)}"
  data-disk-name="${esc(d.name)}"
  data-disk-type="${esc(d.type)}"
  data-disk-health="${esc(d.health)}"
  data-disk-size="${esc(d.size)}"
  data-disk-num="${esc(dnum)}"
  onclick="openDiskModal(this.dataset.diskName,this.dataset.diskType,this.dataset.diskHealth,this.dataset.diskSize)">
  <div class="bay-body">
    <div class="bay-stripe"></div>
    <div class="bay-act-bar-wrap"><div class="bay-act-bar-fill" id="bay-act-${esc(dnum)}" style="height:0%;background:#10B981"></div></div>
    <div class="bay-stripe"></div>
  </div>
  <div class="bay-grid">&#x2807;&#x2807;</div>
  <div class="bay-size">${d.size}</div>
  <div class="bay-type">${d.type}</div>
  <div class="bay-act-pct" id="bay-pct-${esc(dnum)}">—%</div>
  <div class="bay-dot ${dotCls}"></div>
</div>`;
  }).join('');
  el.innerHTML = `<div class="bay-container">${bays}</div>`;
}

function renderDisks(disks) {
  if (!disks||!disks.length) return;
  const el = document.getElementById('diskList');
  el.style.display = 'block';
  el.innerHTML = disks.map(d => {
    const pct = Math.round(d.pct);
    const clr = pct>90?'var(--red)':pct>75?'var(--amber)':'var(--brand)';
    return `<div class="disk-item"><div class="disk-row"><span class="disk-name">${d.mount}</span><span>${d.used} / ${d.total} (${pct}%)</span></div><div class="bar-track"><div class="bar-fill" style="width:${pct}%;background:${clr}"></div></div></div>`;
  }).join('');
}

function doGenVisual() {
  if (!window.pywebview||!window.pywebview.api) return;
  document.getElementById('btnGenVisual').disabled = true;
  document.getElementById('btnOpenFolder').style.display = 'none';
  document.getElementById('emptyState').style.display = 'none';
  document.getElementById('repPreview').style.display  = 'none';
  setStatus('Generando imagen del reporte…', 'busy');
  window.pywebview.api.generate_visual_report().then(raw => {
    const d = JSON.parse(raw);
    if (d.error) {
      setStatus('Error al generar: ' + d.error, 'err');
      document.getElementById('emptyState').style.display = 'flex';
    } else {
      const prev = document.getElementById('repPreview');
      prev.src = 'data:image/jpeg;base64,' + d.preview_b64;
      prev.style.display = 'block';
      lastReportPath = d.path;
      document.getElementById('btnOpenFolder').style.display = 'inline-block';
      setStatus('Reporte guardado en: ' + d.path, 'ok');
    }
    document.getElementById('btnGenVisual').disabled = false;
  }).catch(()=>{
    setStatus('Error inesperado al generar el reporte', 'err');
    document.getElementById('emptyState').style.display = 'flex';
    document.getElementById('btnGenVisual').disabled = false;
  });
}

function doOpenFolder() {
  if (!lastReportPath||!window.pywebview||!window.pywebview.api) return;
  window.pywebview.api.open_report_folder(lastReportPath).catch(()=>{});
}

window.addEventListener('pywebviewready', function() {
  window.pywebview.api.get_assets().then(raw => {
    const a = JSON.parse(raw);
    if (a.logo)      document.getElementById('logo').src        = 'data:'+a.logo_mime+';base64,'+a.logo;
    if (a.personaje) document.getElementById('mascotMain').src  = 'data:image/png;base64,'+a.personaje;
    if (a.ventanas)  document.getElementById('winLogoIcon').src = 'data:image/png;base64,'+a.ventanas;
    if (a.hostname)  document.getElementById('host').textContent = a.hostname;
    if (a.cpu_model) document.getElementById('cpuMod').textContent = a.cpu_model;
  }).catch(()=>{});
  window.pywebview.api.get_disk_health().then(raw=>{
    renderDiskHealth(JSON.parse(raw));
  }).catch(()=>{document.getElementById('diskHealth').innerHTML='<div class="disk-loading">No se pudo obtener estado de discos</div>';});
  pollMetrics();
});

// ── Disk detail modal ──────────────────────────────────────────────────────
function openDiskModal(name, type, health, size) {
  const ov = document.getElementById('diskModal');
  ov.classList.add('open');
  _renderModalLoading(name, type, health, size);
  if (window.pywebview && window.pywebview.api) {
    window.pywebview.api.get_disk_detail(name)
      .then(raw => _renderModalDetail(JSON.parse(raw)))
      .catch(() => _renderModalDetail({name,type,health,size,smart_available:false,smart:null,partitions:[]}));
  }
}
function closeDiskModal() { document.getElementById('diskModal').classList.remove('open'); }
function closeModalOv(e)  { if (e.target===document.getElementById('diskModal')) closeDiskModal(); }

function _healthPill(health) {
  const map = {
    'Healthy':   ['mhp-good','● Saludable'],
    'Warning':   ['mhp-warn','▲ Atención'],
    'Unhealthy': ['mhp-bad', '✕ Crítico'],
  };
  const [cls,lbl] = map[health] || ['mhp-unk','? Desconocido'];
  return `<span class="mhp ${cls}">${lbl}</span>`;
}

function _renderModalLoading(name, type, health, size) {
  document.getElementById('modalContent').innerHTML = `
    <div class="modal-hdr">
      <div class="modal-disk-ic">${IC.hdd}</div>
      <div style="flex:1;min-width:0"><div class="modal-title">${name}</div><div class="modal-title-s">${type} · ${size}</div></div>
      ${_healthPill(health)}
    </div>
    <div class="modal-loading">Obteniendo datos del disco&hellip;</div>`;
}

function _donut(pct, main, sub, color, label) {
  const r=38, circ=2*Math.PI*r, filled=Math.max(0,Math.min(pct,100))/100*circ;
  const light=document.documentElement.getAttribute('data-theme')==='light';
  const track=light?'#E5E7EB':'#1E1E2E';
  const tf=light?'#111827':'#FFFFFF', tf2=light?'#6B7280':'#6B7280';
  return `<div class="donut-wrap">
    <svg viewBox="0 0 100 100" class="donut-svg">
      <circle cx="50" cy="50" r="${r}" fill="none" stroke="${track}" stroke-width="5" stroke-dasharray="2.5 3"/>
      <circle cx="50" cy="50" r="${r}" fill="none" stroke="${color}" stroke-width="5.5"
        stroke-dasharray="${filled.toFixed(1)} ${(circ-filled).toFixed(1)}"
        stroke-linecap="round" transform="rotate(-90 50 50)"/>
      <text x="50" y="44" text-anchor="middle" font-family="'JetBrains Mono',Consolas,monospace"
        font-size="20" font-weight="700" fill="${tf}">${main}</text>
      <text x="50" y="60" text-anchor="middle" font-family="'Inter','Segoe UI',sans-serif"
        font-size="9" fill="${tf2}">${sub}</text>
    </svg>
    <div class="donut-lbl">${label}</div>
  </div>`;
}

function _renderModalDetail(data) {
  const health=data.health||'N/D';
  const smart=data.smart||null;
  const scsi=data.scsi_info||null;
  let donuts='', blocks=[], parts='';

  if (smart) {
    // Temperature donut
    if (smart.temperature!=null) {
      const t=smart.temperature, pct=Math.min(t/70*100,100);
      const clr=t<40?'#10B981':t<55?'#F59E0B':'#EF4444';
      donuts+=_donut(pct,`${t}°`,'Celsius',clr,'Temperatura');
    }
    // Life remaining donut
    if (smart.life_remaining!=null) {
      const l=smart.life_remaining;
      const clr=l>50?'#10B981':l>20?'#F59E0B':'#EF4444';
      donuts+=_donut(l,`${l}%`,'vida útil',clr,'Vida restante');
    }
    // Power-on hours donut
    if (smart.power_on_hours!=null) {
      const h=smart.power_on_hours;
      const disp=h>=10000?`${(h/1000).toFixed(1)}k`:h.toString();
      donuts+=_donut(Math.min(h/50000*100,100),disp,'horas','#0039A6','Encendido');
    }
    // Blocks
    if (smart.power_on_hours!=null) blocks.push({v:smart.power_on_hours.toLocaleString(), l:'Horas de uso'});
    if (smart.power_cycles!=null)   blocks.push({v:smart.power_cycles.toLocaleString(), l:'Arranques'});
    if (smart.reallocated_sectors!=null) {
      const rs=smart.reallocated_sectors;
      blocks.push({v:rs, l:'Sect. reasignados', c:rs>0?'#F59E0B':'#10B981'});
    }
    if (smart.percentage_used!=null) blocks.push({v:`${smart.percentage_used}%`, l:'Desgaste acum.'});
    if (smart.serial) blocks.push({v:smart.serial, l:'Número de serie', small:true});
  }
  if (!smart && scsi) {
    if (scsi.model)  blocks.push({v:scsi.model,  l:'Modelo', small:true});
    if (scsi.serial) blocks.push({v:scsi.serial, l:'Número de serie', small:true});
  }
  blocks.push({v:data.type||'—', l:'Tipo'});
  blocks.push({v:data.size||'—', l:'Capacidad', small:true});

  const blocksHtml=blocks.map(b=>`<div class="mblock">
    <div class="mblock-val" style="${b.c?`color:${b.c}`:''}${b.small?';font-size:13px':''}">${b.v}</div>
    <div class="mblock-lbl">${b.l}</div>
  </div>`).join('');

  if (data.partitions && data.partitions.length) {
    const rows=data.partitions.map(p=>{
      const pct=Math.round(p.pct);
      const clr=pct>90?'#EF4444':pct>75?'#F59E0B':'#0039A6';
      return `<div class="mpart">
        <div class="mpart-row"><span class="mpart-m">${p.mount}</span><span class="mpart-i">${p.fstype} &middot; ${p.used}/${p.total} (${pct}%)</span></div>
        <div class="mpart-bar"><div class="mpart-fill" style="width:${pct}%;background:${clr}"></div></div>
      </div>`;
    }).join('');
    parts=`<div class="msec-title">Particiones</div>${rows}`;
  }

  const note=!data.smart_available
    ?(scsi
      ?`<div class="modal-smart-note"><small>ℹ️ Datos SMART no disponibles en este controlador</small></div>`
      :`<div class="modal-smart-note">Datos SMART detallados no disponibles en este equipo<br><small>Instale <b>smartmontools</b> y ejecútelo con privilegios para ver temperatura, horas de uso y estado avanzado</small></div>`)
    :'';

  document.getElementById('modalContent').innerHTML=`
    <div class="modal-hdr">
      <div class="modal-disk-ic">${IC.hdd}</div>
      <div style="flex:1;min-width:0"><div class="modal-title">${data.name}</div><div class="modal-title-s">${data.type} &middot; ${data.size}</div></div>
      ${_healthPill(health)}
    </div>
    ${donuts?`<div class="modal-donuts">${donuts}</div>`:''}
    ${blocksHtml?`<div class="modal-blocks">${blocksHtml}</div>`:''}
    ${parts}
    ${note}`;
}

// ── Sanar Disco ──────────────────────────────────────────────────
let _chkSelLetter = null, _chkFullScan = false, _chkPollTmr = null;

function openChkdskModal() {
  _chkSelLetter = null; _chkFullScan = false;
  document.getElementById('chkVolList').innerHTML = '<div class="modal-loading">Detectando particiones…</div>';
  document.getElementById('btnChkStart').disabled = true;
  document.getElementById('btnChkStart').textContent = 'Iniciar revisión';
  document.getElementById('chkOutWrap').style.display = 'none';
  document.getElementById('chkOutBox').textContent = '';
  document.getElementById('chkResultMsg').textContent = '';
  document.getElementById('chkProgFill').className = 'chk-prog-fill';
  document.getElementById('chkProgLbl').textContent = 'Procesando...';
  selectChkMode('quick');
  document.getElementById('chkdskModal').classList.add('open');
  window.pywebview.api.get_volumes_for_chkdsk().then(raw => {
    const d = JSON.parse(raw);
    if (d.error) { document.getElementById('chkVolList').innerHTML = '<div class="modal-loading">Error: '+d.error+'</div>'; return; }
    renderChkVols(d);
  }).catch(() => {
    document.getElementById('chkVolList').innerHTML = '<div class="modal-loading">No se pudieron detectar las particiones.</div>';
  });
}
function _doCloseChkdsk() {
  document.getElementById('chkdskModal').classList.remove('open');
  document.getElementById('chkConfirmStrip').classList.remove('visible');
  if (_chkPollTmr) { clearInterval(_chkPollTmr); _chkPollTmr = null; }
}
function closeChkdskModal() {
  if (_chkPollTmr) { document.getElementById('chkConfirmStrip').classList.add('visible'); return; }
  _doCloseChkdsk();
}
function closeChkdskOv(e) {
  if (e.target === document.getElementById('chkdskModal')) closeChkdskModal();
}
function renderChkVols(vols) {
  const el = document.getElementById('chkVolList');
  if (!vols.length) { el.innerHTML = '<div class="modal-loading">No se encontraron particiones accesibles.</div>'; return; }
  el.innerHTML = vols.map(v => {
    const badge = v.is_system ? '<span class="chk-badge-sys">SISTEMA</span>' : '';
    const name  = v.label ? v.letter+': '+v.label+badge : v.letter+':'+badge;
    return `<div class="chk-vol-row" data-letter="${v.letter}" onclick="selectChkVol(this,'${v.letter}')"><div class="chk-vol-letter">${v.letter}:</div><div class="chk-vol-info"><div class="chk-vol-name">${name}</div><div class="chk-vol-meta">${v.fs} &middot; ${v.total} total &middot; ${v.free} libre (${v.free_pct}%)</div></div></div>`;
  }).join('');
}
function selectChkVol(el, letter) {
  document.querySelectorAll('.chk-vol-row').forEach(r => r.classList.remove('sel'));
  el.classList.add('sel');
  _chkSelLetter = letter;
  document.getElementById('btnChkStart').disabled = false;
}
function selectChkMode(mode) {
  _chkFullScan = (mode === 'full');
  ['quick','full'].forEach(m => {
    const row = document.getElementById('chkOpt' + m[0].toUpperCase() + m.slice(1));
    const radio = row.querySelector('input');
    if (m === mode) { row.classList.add('sel'); radio.checked = true; }
    else            { row.classList.remove('sel'); radio.checked = false; }
  });
}
function startChkdsk() {
  if (!_chkSelLetter) return;
  document.getElementById('btnChkStart').disabled = true;
  document.getElementById('chkResultMsg').textContent = '';
  document.getElementById('chkOutWrap').style.display = 'block';
  document.getElementById('chkOutBox').textContent = 'Iniciando…';
  document.getElementById('chkProgFill').className = 'chk-prog-fill running';
  document.getElementById('chkProgLbl').textContent = 'Procesando...';
  window.pywebview.api.run_chkdsk(_chkSelLetter, _chkFullScan).then(raw => {
    const res = JSON.parse(raw);
    if (res.error) {
      document.getElementById('chkOutWrap').style.display = 'none';
      document.getElementById('chkResultMsg').textContent = '⚠️ ' + res.error;
      document.getElementById('btnChkStart').disabled = false;
      return;
    }
    if (res.scheduled) {
      document.getElementById('chkOutWrap').style.display = 'none';
      document.getElementById('chkResultMsg').textContent = '✅ Revisión programada para el próximo reinicio';
      document.getElementById('btnChkStart').textContent = 'Programar de nuevo';
      document.getElementById('btnChkStart').disabled = false;
      return;
    }
    if (res.running) {
      _chkPollTmr = setInterval(pollChkdskStatus, 1000);
    }
  }).catch(err => {
    document.getElementById('chkOutWrap').style.display = 'none';
    document.getElementById('chkResultMsg').textContent = '⚠️ Error: ' + err;
    document.getElementById('btnChkStart').disabled = false;
  });
}
function pollChkdskStatus() {
  window.pywebview.api.get_chkdsk_status().then(raw => {
    const s = JSON.parse(raw);
    const box = document.getElementById('chkOutBox');
    if (s.lines.length) { box.textContent = s.lines.join('\\n'); box.scrollTop = box.scrollHeight; }
    if (s.done) {
      clearInterval(_chkPollTmr); _chkPollTmr = null;
      const out = s.lines.join(' ').toLowerCase();
      const hasWarn = /error|bad sector|problem|incorrect|corrupt|damage|dañ|sector defect/.test(out)
                   && !/no (error|problem|issue|encontr)/.test(out);
      document.getElementById('chkProgFill').className = 'chk-prog-fill ' + (hasWarn ? 'warn' : 'ok');
      document.getElementById('chkProgLbl').textContent = hasWarn ? 'Completado con advertencias' : 'Completado';
      document.getElementById('chkResultMsg').textContent = hasWarn
        ? '⚠️ Revisión completada con advertencias — revisa el detalle arriba'
        : '✅ Revisión completada correctamente';
      document.getElementById('btnChkStart').textContent = 'Iniciar otra revisión';
      document.getElementById('btnChkStart').disabled = false;
    }
  }).catch(() => {});
}

// ── Sanar Windows ──────────────────────────────────────────────────
let _whPollTmr = null, _whActiveBtn = null;
const WH_BTNS = ['btnWh0','btnWh1','btnWh2','btnWh3'];

function openWinHealModal() {
  document.getElementById('whOutWrap').style.display = 'none';
  document.getElementById('whOutBox').textContent = '';
  document.getElementById('whResultMsg').textContent = '';
  document.getElementById('whProgFill').className = 'chk-prog-fill';
  document.getElementById('whProgLbl').textContent = 'Procesando...';
  document.getElementById('whConfirmStrip').classList.remove('visible');
  WH_BTNS.forEach(id => { const b = document.getElementById(id); b.disabled = false; b.textContent = 'Ejecutar'; });
  document.getElementById('winHealModal').classList.add('open');
}
function _doCloseWinHeal() {
  document.getElementById('winHealModal').classList.remove('open');
  document.getElementById('whConfirmStrip').classList.remove('visible');
  if (_whPollTmr) { clearInterval(_whPollTmr); _whPollTmr = null; }
  _whActiveBtn = null;
}
function closeWinHealModal() {
  if (_whPollTmr) { document.getElementById('whConfirmStrip').classList.add('visible'); return; }
  _doCloseWinHeal();
}
function closeWinHealOv(e) {
  if (e.target === document.getElementById('winHealModal')) closeWinHealModal();
}
function runWinHeal(toolId, btn) {
  if (_whPollTmr) return;
  WH_BTNS.forEach(id => { document.getElementById(id).disabled = true; });
  _whActiveBtn = btn;
  btn.textContent = 'Ejecutando...';
  document.getElementById('whResultMsg').textContent = '';
  document.getElementById('whOutWrap').style.display = 'block';
  document.getElementById('whOutBox').textContent = 'Iniciando...';
  document.getElementById('whProgFill').className = 'chk-prog-fill running';
  document.getElementById('whProgLbl').textContent = 'Procesando...';
  window.pywebview.api.run_win_heal(toolId).then(raw => {
    const res = JSON.parse(raw);
    if (res.error) {
      document.getElementById('whOutWrap').style.display = 'none';
      document.getElementById('whResultMsg').textContent = '⚠️ ' + res.error;
      WH_BTNS.forEach(id => { document.getElementById(id).disabled = false; });
      if (_whActiveBtn) { _whActiveBtn.textContent = 'Ejecutar'; _whActiveBtn = null; }
      return;
    }
    if (res.running) { _whPollTmr = setInterval(pollWinHealStatus, 1000); }
  }).catch(err => {
    document.getElementById('whOutWrap').style.display = 'none';
    document.getElementById('whResultMsg').textContent = '⚠️ Error: ' + err;
    WH_BTNS.forEach(id => { document.getElementById(id).disabled = false; });
    if (_whActiveBtn) { _whActiveBtn.textContent = 'Ejecutar'; _whActiveBtn = null; }
  });
}
function pollWinHealStatus() {
  window.pywebview.api.get_win_heal_status().then(raw => {
    const s = JSON.parse(raw);
    const box = document.getElementById('whOutBox');
    if (s.lines.length) { box.textContent = s.lines.join('\\n'); box.scrollTop = box.scrollHeight; }
    if (s.done) {
      clearInterval(_whPollTmr); _whPollTmr = null;
      const ok = s.rc === 0;
      document.getElementById('whProgFill').className = 'chk-prog-fill ' + (ok ? 'ok' : 'warn');
      document.getElementById('whProgLbl').textContent = ok ? 'Completado' : 'Completado con advertencias';
      document.getElementById('whResultMsg').textContent = ok
        ? '✅ Proceso completado correctamente'
        : '⚠️ Proceso completado con advertencias — revisá el detalle arriba';
      WH_BTNS.forEach(id => { document.getElementById(id).disabled = false; });
      if (_whActiveBtn) { _whActiveBtn.textContent = 'Ejecutar de nuevo'; _whActiveBtn = null; }
    }
  }).catch(() => {});
}

// ── Optimizar Windows (WinUtil) ────────────────────────────────────
function openWinUtilModal() {
  const errEl = document.getElementById('wuErrorMsg');
  errEl.style.display = 'none'; errEl.textContent = '';
  const btn = document.getElementById('btnWuConfirm');
  btn.disabled = false; btn.textContent = 'Continuar';
  document.getElementById('winUtilModal').classList.add('open');
}
function closeWinUtilModal() {
  document.getElementById('winUtilModal').classList.remove('open');
}
function closeWinUtilOv(e) {
  if (e.target === document.getElementById('winUtilModal')) closeWinUtilModal();
}
function confirmWinUtil() {
  const btn = document.getElementById('btnWuConfirm');
  btn.disabled = true; btn.textContent = 'Iniciando...';
  window.pywebview.api.launch_winutil().then(raw => {
    const res = JSON.parse(raw);
    if (res.error) {
      const errEl = document.getElementById('wuErrorMsg');
      errEl.style.display = 'block';
      errEl.textContent = '⚠️ ' + res.error;
      btn.disabled = false; btn.textContent = 'Reintentar';
    } else {
      closeWinUtilModal();
    }
  }).catch(err => {
    const errEl = document.getElementById('wuErrorMsg');
    errEl.style.display = 'block';
    errEl.textContent = '⚠️ Error: ' + err;
    btn.disabled = false; btn.textContent = 'Reintentar';
  });
}

// ── Taller de Software — Office 365 ────────────────────────────────
function openOfficeDeployModal() {
  document.getElementById('officeDeployModal').classList.add('open');
  const e = document.getElementById('officeDeployErr'); e.style.display='none'; e.textContent='';
  const b = document.getElementById('btnOfficeDeploy'); b.disabled=false; b.textContent='Instalar';
}
function closeOfficeDeployModal() { document.getElementById('officeDeployModal').classList.remove('open'); }
function closeOfficeDeployOv(e) { if (e.target === document.getElementById('officeDeployModal')) closeOfficeDeployModal(); }
function confirmOfficeDeploy() {
  const btn = document.getElementById('btnOfficeDeploy');
  btn.disabled = true; btn.textContent = 'Iniciando...';
  window.pywebview.api.deploy_office().then(raw => {
    const res = JSON.parse(raw);
    if (res.error) {
      const el = document.getElementById('officeDeployErr');
      el.style.display = 'block'; el.textContent = '⚠️ ' + res.error;
      btn.disabled = false; btn.textContent = 'Reintentar';
    } else { closeOfficeDeployModal(); }
  }).catch(err => {
    const el = document.getElementById('officeDeployErr');
    el.style.display = 'block'; el.textContent = '⚠️ Error: ' + err;
    btn.disabled = false; btn.textContent = 'Reintentar';
  });
}
function openOfficeActivateModal() {
  document.getElementById('officeActivateModal').classList.add('open');
  const e = document.getElementById('officeActivateErr'); e.style.display='none'; e.textContent='';
  const b = document.getElementById('btnOfficeActivate'); b.disabled=false; b.textContent='Continuar';
}
function closeOfficeActivateModal() { document.getElementById('officeActivateModal').classList.remove('open'); }
function closeOfficeActivateOv(e) { if (e.target === document.getElementById('officeActivateModal')) closeOfficeActivateModal(); }
function confirmOfficeActivate() {
  const btn = document.getElementById('btnOfficeActivate');
  btn.disabled = true; btn.textContent = 'Iniciando...';
  window.pywebview.api.activate_office().then(raw => {
    const res = JSON.parse(raw);
    if (res.error) {
      const el = document.getElementById('officeActivateErr');
      el.style.display = 'block'; el.textContent = '⚠️ ' + res.error;
      btn.disabled = false; btn.textContent = 'Reintentar';
    } else { closeOfficeActivateModal(); }
  }).catch(err => {
    const el = document.getElementById('officeActivateErr');
    el.style.display = 'block'; el.textContent = '⚠️ Error: ' + err;
    btn.disabled = false; btn.textContent = 'Reintentar';
  });
}

// ── Taller de Software — Kit Esencial Windows ───────────────────────
function openNiniteModal() {
  document.getElementById('niniteModal').classList.add('open');
  const e = document.getElementById('niniteErr'); e.style.display='none'; e.textContent='';
  const b = document.getElementById('btnNinite'); b.disabled=false; b.textContent='Iniciar';
}
function closeNiniteModal() { document.getElementById('niniteModal').classList.remove('open'); }
function closeNiniteOv(e) { if (e.target === document.getElementById('niniteModal')) closeNiniteModal(); }
function confirmNinite() {
  const btn = document.getElementById('btnNinite');
  btn.disabled = true; btn.textContent = 'Iniciando...';
  window.pywebview.api.deploy_ninite().then(raw => {
    const res = JSON.parse(raw);
    if (res.error) {
      const el = document.getElementById('niniteErr');
      el.style.display = 'block'; el.textContent = '⚠️ ' + res.error;
      btn.disabled = false; btn.textContent = 'Reintentar';
    } else { closeNiniteModal(); }
  }).catch(err => {
    const el = document.getElementById('niniteErr');
    el.style.display = 'block'; el.textContent = '⚠️ Error: ' + err;
    btn.disabled = false; btn.textContent = 'Reintentar';
  });
}

// ── Termómetro modal ──────────────────────────────────────────────────────
var _thermoInterval = null;

function openThermoModal() {
  document.getElementById('thermoModal').classList.add('open');
  updateTemps();
  _thermoInterval = setInterval(updateTemps, 2000);
}

function closeThermoModal() {
  document.getElementById('thermoModal').classList.remove('open');
  if (_thermoInterval) { clearInterval(_thermoInterval); _thermoInterval = null; }
}

function closeThermoOv(e) {
  if (e.target === document.getElementById('thermoModal')) closeThermoModal();
}

function _tempColor(v) {
  if (v < 50) return '#0039A6';
  if (v < 70) return '#10B981';
  if (v < 85) return '#F59E0B';
  return '#EF4444';
}
function _tempLabel(v) {
  if (v < 50) return '&#x2744;&#xFE0F; Fr&iacute;o';
  if (v < 70) return '&#x2705; Normal';
  if (v < 85) return '&#x1F536; Caliente';
  return '&#x1F534; Cr&iacute;tico &mdash; Revisar ventilaci&oacute;n';
}

function updateTemps() {
  if (!window.pywebview || !window.pywebview.api) return;
  window.pywebview.api.get_temperatures().then(function(raw) {
    var d = JSON.parse(raw);
    var el = document.getElementById('thermoList');
    if (!el) return;

    if (d.error) {
      el.innerHTML = '<div class="thermo-unavail"><div style="font-size:28px;margin-bottom:8px">&#x26A0;&#xFE0F;</div>'
        + '<div style="font-weight:600;margin-bottom:4px">Error al leer sensores</div>'
        + '<div style="font-size:11px;opacity:.7;margin-top:4px">' + d.error + '</div></div>';
      return;
    }
    if (d.starting) {
      el.innerHTML = '<div class="thermo-unavail">'
        + '<div style="font-size:28px;margin-bottom:8px">&#x23F3;</div>'
        + '<div style="font-weight:600;margin-bottom:4px">Iniciando sensores&hellip;</div>'
        + '<div style="font-size:11px;opacity:.7">Intent&aacute; abrir el modal de nuevo en unos segundos.</div>'
        + '</div>';
      return;
    }

    var html = '';

    // ── CPU card ──
    if (d.cpu) {
      var mx = d.cpu.max, avg = d.cpu.avg;
      var cl = _tempColor(mx), lbl = _tempLabel(mx);
      var pct = Math.min(100, Math.max(0, mx));
      // [1] °C integrado al número grande — mismo tamaño y color
      html += '<div class="thermo-card">'
            +   '<div class="thermo-card-hdr">&#x1F5A5;&#xFE0F; CPU</div>'
            +   '<div style="text-align:center;margin:4px 0 2px">'
            +     '<span style="font-size:72px;font-weight:800;line-height:1;color:' + cl + '">' + mx + '&deg;C</span>'
            +   '</div>'
            +   '<div style="text-align:center;font-size:13px;font-weight:700;color:' + cl + ';margin-bottom:3px">' + lbl + '</div>'
            +   '<div style="text-align:center;font-size:11px;color:var(--txt2);margin-bottom:4px">Temperatura m&aacute;xima de los n&uacute;cleos</div>'
            +   '<div style="text-align:center;font-size:11px;color:var(--txt2);margin-bottom:10px">'
            +     'M&aacute;x: ' + mx + '&deg;C &nbsp;&middot;&nbsp; Promedio: ' + avg + '&deg;C'
            +   '</div>'
            +   '<div class="thermo-bar-track" style="margin-bottom:14px">'
            +     '<div class="thermo-bar-fill thermo-bar-grad" style="width:' + pct + '%"></div>'
            +   '</div>';

      // [2] Cores: cada uno con su propio color según temperatura
      if (d.cpu.cores && d.cpu.cores.length) {
        html += '<div class="thermo-cores-grid">';
        d.cpu.cores.forEach(function(c) {
          var cc = _tempColor(c.value);
          html += '<div class="thermo-core-chip">'
                +   '<div class="thermo-core-chip-name">' + c.name + '</div>'
                +   '<div style="font-size:20px;font-weight:700;line-height:1;color:' + cc + '">' + c.value + '&deg;C</div>'
                + '</div>';
        });
        html += '</div>';
      }
      html += '</div>';
    }

    // [3] GPU card — mismo estilo que CPU
    html += '<div class="thermo-card" style="margin-top:10px">'
          +   '<div class="thermo-card-hdr">&#x1F4BB; GPU</div>';
    if (d.gpu && d.gpu.length) {
      // Agrupar: GPU Core (principal), GPU Hot Spot (secundario), resto
      var gpuCore    = null, gpuHotSpot = null, gpuOther = [];
      d.gpu.forEach(function(g) {
        if (g.name === 'GPU Core')     gpuCore    = g;
        else if (g.name === 'GPU Hot Spot') gpuHotSpot = g;
        else gpuOther.push(g);
      });

      if (gpuCore) {
        var gc = _tempColor(gpuCore.value), gl = _tempLabel(gpuCore.value);
        var gp = Math.min(100, Math.max(0, gpuCore.value));
        html += '<div style="text-align:center;margin:4px 0 2px">'
              +   '<span style="font-size:72px;font-weight:800;line-height:1;color:' + gc + '">' + gpuCore.value + '&deg;C</span>'
              + '</div>'
              + '<div style="text-align:center;font-size:13px;font-weight:700;color:' + gc + ';margin-bottom:3px">' + gl + '</div>';

        if (gpuHotSpot) {
          var hc = _tempColor(gpuHotSpot.value);
          html += '<div style="text-align:center;font-size:11px;color:var(--txt2);margin-bottom:4px">Temperatura del n&uacute;cleo GPU</div>'
                + '<div style="text-align:center;font-size:11px;color:var(--txt2);margin-bottom:10px">'
                +   'Core: ' + gpuCore.value + '&deg;C &nbsp;&middot;&nbsp; Hot Spot: '
                +   '<span style="color:' + hc + ';font-weight:700">' + gpuHotSpot.value + '&deg;C</span>'
                + '</div>';
        } else {
          html += '<div style="margin-bottom:14px"></div>';
        }

        html += '<div class="thermo-bar-track" style="margin-bottom:' + (gpuOther.length ? 14 : 0) + 'px">'
              +   '<div class="thermo-bar-fill thermo-bar-grad" style="width:' + gp + '%"></div>'
              + '</div>';
      }

      // Sensores GPU adicionales (otras GPUs, p.ej. integrada)
      gpuOther.forEach(function(g) {
        var gc = _tempColor(g.value), gl = _tempLabel(g.value);
        var gp = Math.min(100, Math.max(0, g.value));
        html += '<div style="margin-top:12px;padding-top:10px;border-top:1px solid var(--card-bd)">'
              +   '<div style="font-size:11px;font-weight:700;letter-spacing:.5px;text-transform:uppercase;color:var(--txt2);margin-bottom:6px">' + g.name + '</div>'
              +   '<div style="text-align:center;margin:2px 0">'
              +     '<span style="font-size:30px;font-weight:800;line-height:1;color:' + gc + '">' + g.value + '&deg;C</span>'
              +   '</div>'
              +   '<div style="text-align:center;font-size:11px;font-weight:600;color:' + gc + ';margin-bottom:8px">' + gl + '</div>'
              +   '<div class="thermo-bar-track"><div class="thermo-bar-fill thermo-bar-grad" style="width:' + gp + '%"></div></div>'
              + '</div>';
      });

      if (!gpuCore && gpuOther.length === 0) {
        html += '<div style="font-size:12px;color:var(--txt2);padding:6px 0">GPU integrada &mdash; sin sensor de temperatura disponible</div>';
      }
    } else {
      html += '<div style="font-size:12px;color:var(--txt2);padding:6px 0">GPU integrada &mdash; sin sensor de temperatura disponible</div>';
    }
    html += '</div>';

    el.innerHTML = html;
  }).catch(function() {});
}

// ── Pulso de Red modal ────────────────────────────────────────────────────────
function openNetModal() {
  document.getElementById('netModal').classList.add('open');
  document.getElementById('netInfoSection').style.display   = '';
  document.getElementById('netStartSection').style.display  = '';
  document.getElementById('netTestBody').style.display      = 'none';
  document.getElementById('netSummaryEl').style.display     = 'none';
  document.getElementById('netRepeatSection').style.display = 'none';
  document.getElementById('netResults').innerHTML           = '';
  document.getElementById('netLocalIP').textContent   = '…';
  document.getElementById('netPublicIP').textContent  = '…';
  document.getElementById('netSSID').textContent      = '…';
  document.getElementById('netDNS').textContent       = '…';
  document.getElementById('netDot').className         = 'net-conn-dot';
  document.getElementById('netStatusTxt').textContent = 'Verificando…';
  if (!window.pywebview || !window.pywebview.api) return;
  window.pywebview.api.get_net_info().then(function(raw) {
    try {
      var d = JSON.parse(raw);
      if (d.error) return;
      document.getElementById('netLocalIP').textContent  = d.local_ip  || '—';
      document.getElementById('netPublicIP').textContent = d.public_ip || '—';
      document.getElementById('netSSID').textContent     = d.network   || '—';
      var dns2 = (d.dns_secondary && d.dns_secondary !== 'N/A') ? ' / ' + d.dns_secondary : '';
      document.getElementById('netDNS').textContent      = (d.dns_primary || 'N/A') + dns2;
      if (d.connected) {
        document.getElementById('netDot').className         = 'net-conn-dot ok';
        document.getElementById('netStatusTxt').textContent = 'Conectado';
      } else {
        document.getElementById('netDot').className         = 'net-conn-dot err';
        document.getElementById('netStatusTxt').textContent = 'Sin conexión';
      }
    } catch(e) {}
  }).catch(function() {});
}

function closeNetModal() {
  document.getElementById('netModal').classList.remove('open');
  if (window.pywebview && window.pywebview.api) window.pywebview.api.cancel_net_test();
}
function closeNetOv(e) {
  if (e.target === document.getElementById('netModal')) closeNetModal();
}

function startNetTest() {
  if (!window.pywebview || !window.pywebview.api) return;
  document.getElementById('netInfoSection').style.display   = 'none';
  document.getElementById('netStartSection').style.display  = 'none';
  document.getElementById('netRepeatSection').style.display = 'none';
  document.getElementById('netSummaryEl').style.display     = 'none';
  document.getElementById('netTestBody').style.display      = '';
  document.getElementById('netResults').innerHTML           = '';
  document.getElementById('netProgFill').style.width        = '0%';
  document.getElementById('netStepLabel').textContent       = 'Iniciando…';
  document.getElementById('netPctLabel').textContent        = '0%';
  window.pywebview.api.start_net_test();
}

function netUpdateProgress(pct, label) {
  var f = document.getElementById('netProgFill');
  var l = document.getElementById('netStepLabel');
  var p = document.getElementById('netPctLabel');
  if (f) f.style.width  = pct + '%';
  if (l) l.textContent  = label;
  if (p) p.textContent  = pct + '%';
}

function _nspC(v) {
  if (v == null) return '#6B7280';
  if (v > 50) return '#22C55E';
  if (v > 20) return '#1A56C4';
  if (v > 10) return '#F59E0B';
  if (v > 5)  return '#F97316';
  return '#EF4444';
}
function _nspL(v) {
  if (v == null) return 'Sin datos';
  if (v > 50) return 'Excelente';
  if (v > 20) return 'Bueno';
  if (v > 10) return 'Aceptable';
  if (v > 5)  return 'Lento';
  return 'Muy lento';
}
function _npC(v) {
  if (v == null) return '#6B7280';
  if (v < 20)  return '#22C55E';
  if (v < 50)  return '#1A56C4';
  if (v < 100) return '#F59E0B';
  if (v < 200) return '#F97316';
  return '#EF4444';
}
function _npL(v) {
  if (v == null) return 'Sin datos';
  if (v < 20)  return 'Excelente';
  if (v < 50)  return 'Muy bueno';
  if (v < 100) return 'Aceptable';
  if (v < 200) return 'Lento';
  return 'Muy lento';
}
function _njC(v) {
  if (v < 5)  return '#22C55E';
  if (v < 20) return '#F59E0B';
  return '#EF4444';
}

function netOnStep(data) {
  var el = document.getElementById('netResults');
  if (!el) return;
  var card = document.createElement('div');
  card.className = 'net-step-card';
  var h = '';
  if (data.type === 'ping') {
    var col = _npC(data.avg_ms), lbl = _npL(data.avg_ms);
    var srvHtml = (data.servers || []).map(function(s) {
      var c = s.ok ? _npC(s.ms) : '#6B7280';
      return '<div class="net-ping-item">'
           + '<div class="net-ping-srv">' + s.server + '</div>'
           + '<div class="net-ping-ms" style="color:' + c + '">' + (s.ok ? s.ms + ' ms' : '—') + '</div>'
           + '</div>';
    }).join('');
    h = '<div class="net-step-hdr">'
      + '<span class="net-step-name">Latencia / Ping</span>'
      + '<span class="net-step-badge" style="background:' + col + '22;color:' + col + '">' + lbl + '</span>'
      + '</div>'
      + '<div class="net-step-main" style="color:' + col + '">'
      + (data.avg_ms != null ? data.avg_ms : '—')
      + '<span class="net-step-unit"> ms</span></div>'
      + '<div class="net-ping-grid">' + srvHtml + '</div>';
  } else if (data.type === 'download') {
    if (data.mbps != null) {
      var col = _nspC(data.mbps), lbl = _nspL(data.mbps);
      h = '<div class="net-step-hdr">'
        + '<span class="net-step-name">&#x2193; Descarga</span>'
        + '<span class="net-step-badge" style="background:' + col + '22;color:' + col + '">' + lbl + '</span>'
        + '</div>'
        + '<div class="net-step-main" style="color:' + col + '">'
        + data.mbps + '<span class="net-step-unit"> Mbps</span></div>'
        + '<div class="net-step-sub">10 MB descargados</div>';
    } else {
      h = '<div class="net-step-hdr">'
        + '<span class="net-step-name">&#x2193; Descarga</span>'
        + '<span class="net-step-badge" style="background:#EF444422;color:#B91C1C">Sin datos</span>'
        + '</div>'
        + '<div class="net-step-main" style="color:#EF4444">Error de conexión</div>'
        + '<div class="net-step-sub" style="color:#B91C1C;font-size:11px">' + (data.error || 'No se pudo conectar') + '</div>';
    }
  } else if (data.type === 'upload') {
    if (data.mbps != null) {
      var col = _nspC(data.mbps), lbl = _nspL(data.mbps);
      h = '<div class="net-step-hdr">'
        + '<span class="net-step-name">&#x2191; Subida</span>'
        + '<span class="net-step-badge" style="background:' + col + '22;color:' + col + '">' + lbl + '</span>'
        + '</div>'
        + '<div class="net-step-main" style="color:' + col + '">'
        + data.mbps + '<span class="net-step-unit"> Mbps</span></div>'
        + '<div class="net-step-sub">5 MB enviados</div>';
    } else {
      h = '<div class="net-step-hdr">'
        + '<span class="net-step-name">&#x2191; Subida</span>'
        + '<span class="net-step-badge" style="background:#EF444422;color:#B91C1C">Sin datos</span>'
        + '</div>'
        + '<div class="net-step-main" style="color:#EF4444">Error de conexión</div>'
        + '<div class="net-step-sub" style="color:#B91C1C;font-size:11px">' + (data.error || 'No se pudo conectar') + '</div>';
    }
  } else if (data.type === 'jitter') {
    var col = _njC(data.ms);
    var lbl = data.ms < 5 ? 'Estable' : (data.ms < 20 ? 'Moderado' : 'Inestable');
    h = '<div class="net-step-hdr">'
      + '<span class="net-step-name">Jitter</span>'
      + '<span class="net-step-badge" style="background:' + col + '22;color:' + col + '">' + lbl + '</span>'
      + '</div>'
      + '<div class="net-step-main" style="color:' + col + '">'
      + data.ms + '<span class="net-step-unit"> ms</span></div>'
      + '<div class="net-step-sub">Variación entre pings (máx − mín)</div>';
  }
  card.innerHTML = h;
  el.appendChild(card);
}

function netOnDone(data) {
  document.getElementById('netTestBody').style.display      = 'none';
  document.getElementById('netSummaryEl').style.display     = '';
  document.getElementById('netRepeatSection').style.display = '';
  var sum = document.getElementById('netSummaryEl');
  if (data.error) {
    sum.innerHTML = '<div class="thermo-unavail">&#x26A0;&#xFE0F; Error: ' + data.error + '</div>';
    return;
  }
  var dl = data.download_mbps, ul = data.upload_mbps;
  var ping = data.ping_avg,    jitter = data.jitter;
  var dlC = _nspC(dl), ulC = _nspC(ul), pC = _npC(ping), jC = _njC(jitter != null ? jitter : 0);
  var qMap = {
    excellent: { e: '🟢', t: 'Conexión Excelente', bg: 'rgba(34,197,94,.12)',  c: '#15803D' },
    good:      { e: '🟡', t: 'Conexión Aceptable', bg: 'rgba(245,158,11,.15)', c: '#B45309' },
    slow:      { e: '🔴', t: 'Conexión Lenta',      bg: 'rgba(239,68,68,.12)',  c: '#B91C1C' },
  };
  var q = qMap[data.quality] || qMap.slow;
  sum.innerHTML = '<div class="net-summary-wrap">'
    + '<div class="net-badge-glob" style="background:' + q.bg + ';color:' + q.c + '">' + q.e + ' ' + q.t + '</div>'
    + '<div class="net-sum-speeds">'
    + '<div class="net-sum-side"><div class="net-sum-arrow" style="color:' + dlC + '">&#x2193;</div>'
    + '<div class="net-sum-big" style="color:' + dlC + '">' + (dl != null ? dl : '—') + '</div>'
    + '<div class="net-sum-lbl">Mbps descarga</div></div>'
    + '<div class="net-sum-side"><div class="net-sum-arrow" style="color:' + ulC + '">&#x2191;</div>'
    + '<div class="net-sum-big" style="color:' + ulC + '">' + (ul != null ? ul : '—') + '</div>'
    + '<div class="net-sum-lbl">Mbps subida</div></div>'
    + '</div>'
    + '<div class="net-sum-stats">'
    + '<div class="net-sum-stat"><div class="net-sum-sv" style="color:' + pC + '">' + (ping != null ? ping + ' ms' : '—') + '</div><div class="net-sum-sl">Latencia</div></div>'
    + '<div class="net-sum-stat"><div class="net-sum-sv" style="color:' + jC + '">' + (jitter != null ? jitter + ' ms' : '—') + '</div><div class="net-sum-sl">Jitter</div></div>'
    + '</div>'
    + '</div>';
}

// ── Limpiar Sistema ──────────────────────────────────────────────────────
function _fmtCleanBytes(b) {
  b = b || 0;
  var u = ['B','KB','MB','GB','TB'], i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return b.toFixed(1) + ' ' + u[i];
}

function cerrarModalLimpieza() {
  document.getElementById('cleanModal').classList.remove('open');
  // Reset para la próxima apertura
  document.getElementById('cleanup-progress').style.display        = 'none';
  document.getElementById('cleanup-final').style.display           = 'none';
  document.getElementById('cleanup-bar').style.width               = '0%';
  document.getElementById('cleanup-status-text').textContent       = 'Limpiando…';
}

function closeCleanModal() { cerrarModalLimpieza(); }

function limpiarSistema() {
  // Reset completo
  document.getElementById('cleanAnalyzeBody').innerHTML    = '<div class="modal-loading">&#x1F50D; Analizando sistema&hellip;</div>';
  document.getElementById('cleanTotalRow').style.display   = 'none';
  document.getElementById('cleanTotalVal').textContent     = '—';
  document.getElementById('cleanLimpiarBtn').style.display = 'none';
  document.getElementById('cleanLimpiarBtn').disabled      = false;
  document.getElementById('cleanLimpiarBtn').innerHTML     = '&#x1F9F9; Limpiar Ahora';
  document.getElementById('cleanup-progress').style.display = 'none';
  document.getElementById('cleanup-final').style.display   = 'none';
  document.getElementById('cleanup-bar').style.width       = '0%';
  // Abrir modal
  document.getElementById('cleanModal').classList.add('open');
  // Análisis
  if (!window.pywebview || !window.pywebview.api) { return; }
  window.pywebview.api.analyze_cleanup().then(function(raw) {
    var d;
    try { d = JSON.parse(raw); } catch(e) { d = {error: 'Error al parsear respuesta'}; }
    if (d.error) {
      document.getElementById('cleanAnalyzeBody').innerHTML =
        '<div class="modal-loading" style="color:var(--error)">&#x26A0;&#xFE0F; ' + d.error + '</div>';
      return;
    }
    var html = '';
    html += '<div class="clean-cat-row"><span class="clean-cat-icon">&#x1F5C2;</span><span class="clean-cat-lbl">Temporales del usuario (%TEMP%)</span><span class="clean-cat-size">' + _fmtCleanBytes(d.temp_user) + '</span></div>';
    html += '<div class="clean-cat-row"><span class="clean-cat-icon">&#x1F5C2;</span><span class="clean-cat-lbl">Temporales de Windows</span><span class="clean-cat-size">'                + _fmtCleanBytes(d.temp_win)  + '</span></div>';
    html += '<div class="clean-cat-row"><span class="clean-cat-icon">&#x1F5D1;</span><span class="clean-cat-lbl">Papelera de reciclaje</span><span class="clean-cat-size">'               + _fmtCleanBytes(d.recycle)   + '</span></div>';
    document.getElementById('cleanAnalyzeBody').innerHTML    = html;
    document.getElementById('cleanTotalVal').textContent     = _fmtCleanBytes(d.total || 0);
    document.getElementById('cleanTotalRow').style.display   = '';
    document.getElementById('cleanLimpiarBtn').style.display = '';
  });
}

function limpiarAhora() {
  document.getElementById('cleanLimpiarBtn').style.display    = 'none';
  document.getElementById('cleanTotalRow').style.display      = 'none';
  document.getElementById('cleanAnalyzeBody').style.display   = 'none';
  document.getElementById('cleanup-progress').style.display   = 'block';
  document.getElementById('cleanup-bar').style.width          = '0%';

  var etapas = [
    { texto: '🗂 Desintegrando temporales del usuario...', pct: 25 },
    { texto: '🗂 Desintegrando temporales de Windows...', pct: 55 },
    { texto: '🗑 Desintegrando papelera de reciclaje...', pct: 80 },
    { texto: '💥 Confirmando desintegración total...',     pct: 95 }
  ];
  var i = 0;
  function avanzarEtapa() {
    if (i < etapas.length) {
      document.getElementById('cleanup-status-text').textContent = etapas[i].texto;
      document.getElementById('cleanup-bar').style.width         = etapas[i].pct + '%';
      i++;
      setTimeout(avanzarEtapa, 900);
    }
  }
  avanzarEtapa();

  if (!window.pywebview || !window.pywebview.api) { return; }
  window.pywebview.api.run_cleanup().then(function(raw) {
    var d;
    try { d = JSON.parse(raw); } catch(e) { d = {}; }
    document.getElementById('cleanup-bar').style.width = '100%';
    setTimeout(function() {
      mostrarResultadoLimpieza(d.freed || 0);
    }, 500);
  });
}

function mostrarResultadoLimpieza(bytesFreed) {
  var freed = bytesFreed || 0;
  var texto;
  if      (freed >= 1073741824) texto = (freed / 1073741824).toFixed(1) + ' GB';
  else if (freed >= 1048576)    texto = (freed / 1048576).toFixed(1)    + ' MB';
  else if (freed >= 1024)       texto = (freed / 1024).toFixed(1)       + ' KB';
  else                          texto = freed + ' B';

  document.getElementById('cleanup-progress').style.display      = 'none';
  document.getElementById('cleanup-final').style.display         = 'block';
  document.getElementById('cleanup-freed-amount').textContent    = texto;
}

/* ── ADN del Equipo ──────────────────────────────────── */
function _adnFmtBiosDate(raw) {
  if (!raw) return 'N/D';
  var m = String(raw).match(/^(\\d{4})(\\d{2})(\\d{2})/);
  if (!m) return raw;
  return m[3] + '/' + m[2] + '/' + m[1];
}

function _adnFmtVRAM(bytes) {
  if (!bytes || bytes <= 0) return 'Compartida (integrada)';
  var gb = bytes / 1073741824;
  return gb >= 1 ? gb.toFixed(1) + ' GB' : (bytes / 1048576).toFixed(0) + ' MB';
}

function cerrarModalADN() {
  document.getElementById('adnModal').classList.remove('open');
  document.getElementById('adnBody').innerHTML = '<div class="modal-loading">&#x1F50D; Leyendo ADN del sistema&hellip;</div>';
}

function abrirModalADN() {
  document.getElementById('adnBody').innerHTML = '<div class="modal-loading">&#x1F50D; Leyendo ADN del sistema&hellip;</div>';
  document.getElementById('adnModal').classList.add('open');
  if (!window.pywebview || !window.pywebview.api) { return; }
  window.pywebview.api.get_system_info().then(function(raw) {
    var d;
    try { d = JSON.parse(raw); } catch(e) { d = { error: 'Error al parsear respuesta' }; }
    if (d.error) {
      document.getElementById('adnBody').innerHTML = '<div style="color:#ef4444;font-size:13px;padding:12px 0;">Error: ' + d.error + '</div>';
      return;
    }

    var html = '';

    /* ── Sección 1: Placa Madre ── */
    html += '<div class="adn-section">';
    html += '<div class="adn-section-title">&#x1F4CB; Placa Madre</div>';
    var mb = d.motherboard || {};
    var bios = d.bios || {};
    html += '<div class="adn-row"><span class="adn-label">Fabricante</span><span class="adn-value">' + (mb.Manufacturer || 'N/D') + '</span></div>';
    html += '<div class="adn-row"><span class="adn-label">Modelo</span><span class="adn-value">' + (mb.Product || 'N/D') + '</span></div>';
    html += '<div class="adn-row"><span class="adn-label">Versi&oacute;n BIOS</span><span class="adn-value">' + (bios.SMBIOSBIOSVersion || 'N/D') + '</span></div>';
    html += '<div class="adn-row"><span class="adn-label">Fecha BIOS</span><span class="adn-value">' + _adnFmtBiosDate(bios.ReleaseDate) + '</span></div>';
    html += '</div>';

    /* ── Sección 2: GPU ── */
    html += '<div class="adn-section">';
    html += '<div class="adn-section-title">&#x1F5A5;&#xFE0F; Tarjeta de Video</div>';
    var gpus = d.gpu || [];
    if (gpus.length === 0) {
      html += '<div class="adn-row"><span class="adn-label">GPU</span><span class="adn-value">N/D</span></div>';
    } else {
      gpus.forEach(function(g, i) {
        if (gpus.length > 1) html += '<div class="adn-gpu-block">';
        if (gpus.length > 1) html += '<div style="font-size:11px;color:var(--txt2);margin-bottom:4px;">GPU ' + (i + 1) + '</div>';
        html += '<div class="adn-row"><span class="adn-label">Nombre</span><span class="adn-value">' + (g.Name || 'N/D') + '</span></div>';
        html += '<div class="adn-row"><span class="adn-label">VRAM</span><span class="adn-value">' + _adnFmtVRAM(g.AdapterRAM) + '</span></div>';
        var res = (g.CurrentHorizontalResolution && g.CurrentVerticalResolution)
          ? g.CurrentHorizontalResolution + ' × ' + g.CurrentVerticalResolution
          : 'N/D';
        html += '<div class="adn-row"><span class="adn-label">Resoluci&oacute;n</span><span class="adn-value">' + res + '</span></div>';
        if (gpus.length > 1) html += '</div>';
      });
    }
    html += '</div>';

    /* ── Sección 3: Sistema Operativo ── */
    html += '<div class="adn-section">';
    html += '<div class="adn-section-title">&#x1F4BB; Sistema Operativo</div>';
    var os = d.os || {};
    html += '<div class="adn-row"><span class="adn-label">Nombre</span><span class="adn-value">' + (os.Caption || 'N/D') + '</span></div>';
    html += '<div class="adn-row"><span class="adn-label">Build</span><span class="adn-value">' + (os.BuildNumber || 'N/D') + '</span></div>';
    html += '<div class="adn-row"><span class="adn-label">Versi&oacute;n</span><span class="adn-value">' + (os.Version || 'N/D') + '</span></div>';
    html += '<div class="adn-row"><span class="adn-label">Arquitectura</span><span class="adn-value">' + (os.OSArchitecture || 'N/D') + '</span></div>';
    html += '<div class="adn-row"><span class="adn-label">Activaci&oacute;n</span><span class="adn-value">' + (d.activation || 'N/D') + '</span></div>';
    html += '</div>';

    document.getElementById('adnBody').innerHTML = html;
  });
}

window.addEventListener('resize', () => { _drawCPUFrame(_cpuDisp); drawRAM(_lastRamPct); });
</script>
</body>
</html>
"""


# ── UAC elevation ─────────────────────────────────────────────────────────
def _request_admin():
    """Re-launch the process elevated via UAC. Exits this process if a new one starts."""
    try:
        import ctypes
        if ctypes.windll.shell32.IsUserAnAdmin():
            return  # Already admin
        exe  = sys.executable
        # When frozen by PyInstaller sys.executable IS the .exe; pass no extra argv
        args = subprocess.list2cmdline(sys.argv[1:]) if getattr(sys, "frozen", False) \
               else subprocess.list2cmdline(sys.argv)
        ret  = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, args or None, None, 1)
        if ret > 32:
            sys.exit(0)   # Elevated copy launched → quit this non-admin instance
        # ret ≤ 32 means UAC was cancelled or failed → continue without admin
    except Exception:
        pass


# ── Entry point ───────────────────────────────────────────────────────────
def main():
    _request_admin()
    api = Api()
    window = webview.create_window(
        "PC HOUSE — Diagnóstico PC",
        html=HTML,
        js_api=api,
        width=1100,
        height=720,
        min_size=(900, 580),
        background_color="#FFFFFF",
    )
    webview.start(debug=False)


if __name__ == "__main__":
    main()
