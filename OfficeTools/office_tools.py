#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PC HOUSE — Office Tools v1.0
Frontend: pywebview (HTML/CSS/JS)
Backend:  subprocess + ctypes
"""

import base64, json, os, sys, subprocess

import webview

_NWIN = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}


# ── Asset resolution (dev vs PyInstaller .exe) ────────────────────────────
def resource_path(rel):
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def get_base_path():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


# ── Logo pre-cargado a nivel módulo ───────────────────────────────────────
_logo_src = ""
try:
    with open(resource_path("assets/logo.jpg"), "rb") as _f:
        _logo_src = "data:image/jpeg;base64," + base64.b64encode(_f.read()).decode()
except Exception:
    pass


# ── Python API exposed to JS ──────────────────────────────────────────────
class Api:
    def deploy_office(self):
        try:
            base        = get_base_path()
            office_path = os.path.join(base, 'tools', 'office', 'setup.exe')
            config_path = os.path.join(base, 'tools', 'office', 'configuracion.xml')

            if not os.path.exists(office_path):
                return json.dumps({"error": "No se encontró setup.exe"})

            if not os.path.exists(config_path):
                return json.dumps({"error": "No se encontró configuracion.xml"})

            result = subprocess.run(
                [office_path, '/configure', config_path],
                capture_output=True,
                text=True,
                timeout=300
            )

            if result.returncode == 0:
                return json.dumps({"status": "ok"})
            else:
                stderr = result.stderr.strip() if result.stderr else ""
                return json.dumps({
                    "error": f"Error código {result.returncode}: {stderr}"
                })
        except subprocess.TimeoutExpired:
            return json.dumps({"error": "Tiempo de espera agotado (300 s)"})
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


# ── HTML UI ───────────────────────────────────────────────────────────────
HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body { height: 100%; }
body {
  font-family: 'Plus Jakarta Sans', -apple-system, 'Segoe UI', sans-serif;
  background: linear-gradient(135deg, #1A56C4 0%, #00C9A7 100%) fixed;
  display: flex;
  flex-direction: column;
  height: 100vh;
  overflow: hidden;
  -webkit-user-select: none;
  -webkit-font-smoothing: antialiased;
}

/* ── Header ── */
.hdr {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 14px 22px;
  background: rgba(13, 43, 107, 0.42);
  border-bottom: 1px solid rgba(255, 255, 255, 0.20);
  backdrop-filter: blur(8px);
  -webkit-backdrop-filter: blur(8px);
  flex-shrink: 0;
}
.hdr-logo  { height: 36px; width: auto; border-radius: 7px; flex-shrink: 0; }
.hdr-title { font-size: 16px; font-weight: 700; color: #ffffff; line-height: 1.2; }
.hdr-sub   { font-size: 11px; color: rgba(255, 255, 255, 0.60); font-weight: 300; margin-top: 2px; }

/* ── Main area ── */
.main {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 18px 22px;
  overflow: hidden;
}

/* ── Cards ── */
.card {
  background: rgba(255, 255, 255, 0.92);
  border: 1px solid rgba(0, 0, 0, 0.08);
  border-radius: 16px;
  padding: 18px 20px;
  display: flex;
  align-items: center;
  gap: 16px;
  box-shadow: 0 4px 18px rgba(0, 0, 0, 0.10);
  transition: background 0.15s, transform 0.12s, box-shadow 0.15s;
}
.card:hover {
  background: rgba(255, 255, 255, 0.97);
  transform: translateY(-2px);
  box-shadow: 0 8px 28px rgba(0, 0, 0, 0.14);
}
.card-icon  { font-size: 34px; line-height: 1; flex-shrink: 0; }
.card-body  { flex: 1; min-width: 0; }
.card-title { font-size: 15px; font-weight: 700; color: #0D2B6B; margin-bottom: 4px; }
.card-desc  { font-size: 12px; color: #7A90AA; font-weight: 300; line-height: 1.45; }

/* ── Buttons ── */
.btn {
  padding: 9px 22px;
  border: none;
  border-radius: 9px;
  font-family: 'Plus Jakarta Sans', sans-serif;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  transition: opacity 0.15s, transform 0.10s;
  white-space: nowrap;
  flex-shrink: 0;
}
.btn:active:not(:disabled) { transform: scale(0.96); }
.btn:disabled { opacity: 0.40; cursor: not-allowed; }
.btn-primary { background: #1A56C4; color: #ffffff; }
.btn-primary:hover:not(:disabled) { opacity: 0.88; }
.btn-cancel {
  padding: 9px 18px;
  border-radius: 9px;
  border: 1px solid rgba(0, 0, 0, 0.14);
  background: transparent;
  font-family: 'Plus Jakarta Sans', sans-serif;
  font-size: 13px;
  font-weight: 600;
  color: #7A90AA;
  cursor: pointer;
  transition: background 0.15s;
}
.btn-cancel:hover { background: rgba(0, 0, 0, 0.05); }

/* ── Footer ── */
.footer {
  text-align: center;
  padding: 8px 22px 12px;
  font-size: 11px;
  color: rgba(255, 255, 255, 0.50);
  font-weight: 300;
  flex-shrink: 0;
}

/* ── Modal internals ── */
.modal-title { font-size: 16px; font-weight: 700; color: #0D2B6B; margin-bottom: 10px; padding-right: 24px; }
.modal-desc  { font-size: 13px; color: #7A90AA; font-weight: 300; line-height: 1.65; margin-bottom: 20px; }
.modal-err   { font-size: 12px; color: #B91C1C; margin-bottom: 12px; display: none; }
.modal-btns  { display: flex; gap: 10px; justify-content: flex-end; }
.modal-x {
  position: absolute;
  top: 14px; right: 14px;
  width: 28px; height: 28px;
  border-radius: 50%;
  border: none;
  background: #F3F4F6;
  color: #374151;
  cursor: pointer;
  font-size: 14px;
  display: flex; align-items: center; justify-content: center;
  line-height: 1;
  transition: opacity 0.15s;
}
.modal-x:hover { opacity: 0.65; }
</style>
</head>
<body>

<header class="hdr">
  <img class="hdr-logo" src="__LOGO_SRC__" alt="PC House">
  <div>
    <div class="hdr-title">Office Tools</div>
    <div class="hdr-sub">Herramientas de Office 365</div>
  </div>
</header>

<div class="main">

  <div class="card">
    <div class="card-icon">&#x1F4E6;</div>
    <div class="card-body">
      <div class="card-title">Desplegar Office 365</div>
      <div class="card-desc">Instala Office 365 desde los archivos locales</div>
    </div>
    <button class="btn btn-primary" onclick="abrirModalOffice()">Instalar</button>
  </div>

  <div class="card">
    <div class="card-icon">&#x1F511;</div>
    <div class="card-body">
      <div class="card-title">Activar Office</div>
      <div class="card-desc">Abre la herramienta de activaci&oacute;n</div>
    </div>
    <button class="btn btn-primary" onclick="abrirModalActivar()">Activar</button>
  </div>

</div>

<div class="footer">PC House &copy; 2026</div>

<!-- Error toast -->
<div id="errorToast" style="display:none;position:fixed;bottom:20px;left:50%;
  transform:translateX(-50%);background:#FEE2E2;border:1px solid #FCA5A5;
  border-radius:10px;padding:12px 20px;font-size:13px;color:#B91C1C;font-weight:600;
  z-index:2000;max-width:420px;text-align:center;
  box-shadow:0 4px 20px rgba(0,0,0,0.18);cursor:pointer;"
  onclick="this.style.display='none'">
  <span id="errorToastMsg"></span>
</div>

<!-- Success toast -->
<div id="successToast" style="display:none;position:fixed;bottom:20px;left:50%;
  transform:translateX(-50%);background:#DCFCE7;border:1px solid #86EFAC;
  border-radius:10px;padding:12px 20px;font-size:13px;color:#166534;font-weight:600;
  z-index:2000;max-width:420px;text-align:center;
  box-shadow:0 4px 20px rgba(0,0,0,0.18);cursor:pointer;"
  onclick="this.style.display='none'">
  <span id="successToastMsg"></span>
</div>

<!-- MODAL: Desplegar Office -->
<div id="modal-office-overlay"
     onclick="cerrarModalOffice()"
     style="display:none;position:fixed;top:0;left:0;width:100%;height:100%;
            background:rgba(0,0,0,0.45);z-index:1000;
            align-items:center;justify-content:center;">
  <div id="modal-office-card"
       onclick="event.stopPropagation()"
       style="background:white;border-radius:16px;padding:28px;width:360px;
              max-width:90%;position:relative;z-index:1001;
              box-shadow:0 28px 80px rgba(0,0,0,0.22);">
    <button class="modal-x" onclick="cerrarModalOffice()">&#x2715;</button>
    <div class="modal-title">&#x2B07;&#xFE0F; Desplegar Office 365</div>
    <div class="modal-desc">
      Se iniciar&aacute; la instalaci&oacute;n usando los archivos locales.
      Asegur&aacute;te de que el instalador est&eacute; en la carpeta
      <strong style="color:#0D2B6B">tools/office</strong> antes de continuar.
    </div>
    <div class="modal-err" id="officeErr"></div>
    <div class="modal-btns">
      <button class="btn-cancel" onclick="cerrarModalOffice()">Cancelar</button>
      <button class="btn btn-primary" id="btnInstalar" onclick="confirmarInstalar()">Instalar</button>
    </div>
  </div>
</div>

<!-- MODAL: Activar Office -->
<div id="modal-activar-overlay"
     onclick="cerrarModalActivar()"
     style="display:none;position:fixed;top:0;left:0;width:100%;height:100%;
            background:rgba(0,0,0,0.45);z-index:1000;
            align-items:center;justify-content:center;">
  <div id="modal-activar-card"
       onclick="event.stopPropagation()"
       style="background:white;border-radius:16px;padding:28px;width:360px;
              max-width:90%;position:relative;z-index:1001;
              box-shadow:0 28px 80px rgba(0,0,0,0.22);">
    <button class="modal-x" onclick="cerrarModalActivar()">&#x2715;</button>
    <div class="modal-title">&#x1F511; Activar Office</div>
    <div class="modal-desc">
      Se abrir&aacute; la herramienta de activaci&oacute;n en una ventana de
      PowerShell como administrador. Segu&iacute; las instrucciones que
      aparezcan en pantalla.
    </div>
    <div class="modal-err" id="activarErr"></div>
    <div class="modal-btns">
      <button class="btn-cancel" onclick="cerrarModalActivar()">Cancelar</button>
      <button class="btn btn-primary" id="btnActivar" onclick="confirmarActivar()">Continuar</button>
    </div>
  </div>
</div>

<script>
/* ── Modal Desplegar Office ── */
function abrirModalOffice() {
  var e = document.getElementById('officeErr');
  e.style.display = 'none';
  e.textContent = '';
  var b = document.getElementById('btnInstalar');
  b.disabled = false;
  b.textContent = 'Instalar';
  document.getElementById('modal-office-overlay').style.display = 'flex';
}

function cerrarModalOffice() {
  document.getElementById('modal-office-overlay').style.display = 'none';
}

function confirmarInstalar() {
  var btn = document.getElementById('btnInstalar');
  btn.disabled = true;
  btn.textContent = 'Instalando...';
  window.pywebview.api.deploy_office().then(function(raw) {
    var data = JSON.parse(raw);
    if (data.error) {
      mostrarError('Error: ' + data.error);
      btn.disabled = false;
      btn.textContent = 'Reintentar';
    } else {
      cerrarModalOffice();
      mostrarExito('Office instalado correctamente');
    }
  }).catch(function(err) {
    mostrarError('Error: ' + err);
    btn.disabled = false;
    btn.textContent = 'Reintentar';
  });
}

/* ── Modal Activar Office ── */
function abrirModalActivar() {
  var e = document.getElementById('activarErr');
  e.style.display = 'none';
  e.textContent = '';
  var b = document.getElementById('btnActivar');
  b.disabled = false;
  b.textContent = 'Continuar';
  document.getElementById('modal-activar-overlay').style.display = 'flex';
}

function cerrarModalActivar() {
  document.getElementById('modal-activar-overlay').style.display = 'none';
}

function confirmarActivar() {
  var btn = document.getElementById('btnActivar');
  btn.disabled = true;
  btn.textContent = 'Iniciando...';
  window.pywebview.api.activate_office().then(function(raw) {
    var res = JSON.parse(raw);
    if (res.error) {
      var el = document.getElementById('activarErr');
      el.style.display = 'block';
      el.textContent = res.error;
      btn.disabled = false;
      btn.textContent = 'Reintentar';
    } else {
      cerrarModalActivar();
    }
  }).catch(function(err) {
    var el = document.getElementById('activarErr');
    el.style.display = 'block';
    el.textContent = 'Error: ' + err;
    btn.disabled = false;
    btn.textContent = 'Reintentar';
  });
}

function mostrarError(msg) {
  var el    = document.getElementById('errorToast');
  var msgEl = document.getElementById('errorToastMsg');
  if (!el || !msgEl) return;
  msgEl.textContent = msg;
  el.style.display = 'block';
  setTimeout(function() { el.style.display = 'none'; }, 6000);
}

function mostrarExito(msg) {
  var el    = document.getElementById('successToast');
  var msgEl = document.getElementById('successToastMsg');
  if (!el || !msgEl) return;
  msgEl.textContent = msg;
  el.style.display = 'block';
  setTimeout(function() { el.style.display = 'none'; }, 4000);
}
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
            return
        exe  = sys.executable
        args = subprocess.list2cmdline(sys.argv[1:]) if getattr(sys, "frozen", False) \
               else subprocess.list2cmdline(sys.argv)
        ret  = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, args or None, None, 1)
        if ret > 32:
            sys.exit(0)
    except Exception:
        pass


# ── Entry point ───────────────────────────────────────────────────────────
def main():
    _request_admin()
    api = Api()
    _html = HTML.replace("__LOGO_SRC__", _logo_src)
    webview.create_window(
        "PC HOUSE — Office Tools",
        html=_html,
        js_api=api,
        width=500,
        height=400,
        min_size=(500, 400),
        resizable=False,
        background_color="#1A56C4",
    )
    webview.start(debug=False)


if __name__ == "__main__":
    main()
