#!/usr/bin/env python3
"""
Puente Ecowitt -> apidatos.txt (formato AMETSE)

- Recibe los datos que el gateway Ecowitt (GW3000/GW3002) envía con la
  opción "Customized" (protocolo Ecowitt, POST) o por GET.
- Convierte las unidades (imperiales -> métricas) y guarda UNA fila por minuto
  en un archivo de texto con este formato:

  'ts'; 'eid'; 'dd-mm-yyyy HH:MM'; 'temp'; 'humedad'; 'viento'; 'dirección';
  'presión'; 'lluvia'; 'racha'; 'dirección racha';

- Mantiene solo las últimas RETENTION_HOURS horas.
- Sirve el archivo en GET /apidatos.txt para que AMETSE pueda leerlo.

Solo usa la librería estándar (más tzdata para la zona horaria).
"""
import logging
import os
import sys
import threading
import time
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------- configuración
STATION_ID = os.environ.get("STATION_ID", "").strip()
if not STATION_ID:
    sys.exit("ERROR: falta la variable de entorno STATION_ID (tu eid de AMETSE)")

TZ = ZoneInfo(os.environ.get("TIMEZONE", "Europe/Madrid"))
DATA_FILE = os.environ.get("DATA_FILE", "/data/apidatos.txt")
PORT = int(os.environ.get("PORT", "8080"))
RETENTION_HOURS = float(os.environ.get("RETENTION_HOURS", "24"))
# Ruta a la que el gateway envía los datos. Usa una ruta "secreta" para que
# nadie más pueda escribir en tu archivo, p. ej. /data/report/xk29fj3
UPLOAD_PATH = "/" + os.environ.get("UPLOAD_PATH", "/data/report").strip("/")
# Campo de Ecowitt que se usa para la columna de lluvia (en pulgadas):
#   dailyrainin (acumulada del día), hourlyrainin, rainratein, eventrainin...
RAIN_FIELD = os.environ.get("RAIN_FIELD", "dailyrainin")
# URLs a las que reenviar tal cual lo que manda el gateway (separadas por comas),
# p. ej. el webhook de Home Assistant: http://192.168.1.10:8123/api/webhook/xxxx
FORWARD_URLS = [u.strip() for u in os.environ.get("FORWARD_URLS", "").split(",") if u.strip()]

lock = threading.Lock()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")


# ---------------------------------------------------------------- conversión
def num(params, *keys):
    """Primer valor numérico encontrado entre las claves indicadas."""
    for k in keys:
        v = params.get(k)
        if v not in (None, ""):
            try:
                return float(v)
            except ValueError:
                pass
    return None


def fmt(v, decimals):
    """Formatea como en el ejemplo de AMETSE: 19.0 -> '19', 19.25 -> '19.3'."""
    if v is None:
        return ""
    s = f"{v:.{decimals}f}"
    if decimals:
        s = s.rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def mul(v, k):
    return None if v is None else v * k


def build_row(p, now):
    """Devuelve (ts, línea) o None si no hay datos útiles."""
    temp_f = num(p, "tempf")
    hum = num(p, "humidity")
    wind_mph = num(p, "windspeedmph")
    wind_dir = num(p, "winddir")
    gust_mph = num(p, "windgustmph")
    gust_dir = num(p, "windgustdir", "winddir")  # Ecowitt no da dirección de racha
    baro_in = num(p, "baromrelin", "baromin", "baromabsin")
    rain_in = num(p, RAIN_FIELD)

    if temp_f is None and hum is None and baro_in is None:
        return None

    ts = int(now // 60 * 60)  # redondeado al minuto, como en el ejemplo
    fecha = datetime.fromtimestamp(ts, TZ).strftime("%d-%m-%Y %H:%M")

    campos = [
        str(ts),
        STATION_ID,
        fecha,
        fmt(None if temp_f is None else (temp_f - 32) * 5 / 9, 1),  # °C
        fmt(hum, 0),                                                # %
        fmt(mul(wind_mph, 1.609344), 0),                            # km/h
        fmt(wind_dir, 0),                                           # grados
        fmt(mul(baro_in, 33.8639), 1),                              # hPa
        fmt(mul(rain_in, 25.4), 1),                                 # mm
        fmt(mul(gust_mph, 1.609344), 0),                            # km/h
        fmt(gust_dir, 0),                                           # grados
    ]
    return ts, "; ".join(f"'{c}'" for c in campos) + ";"


# ---------------------------------------------------------------- almacenamiento
def save_row(ts, line):
    with lock:
        rows = {}
        try:
            with open(DATA_FILE, encoding="utf-8") as f:
                for l in f:
                    l = l.strip()
                    if not l:
                        continue
                    try:
                        rows[int(l.split(";")[0].strip("' "))] = l
                    except ValueError:
                        continue
        except FileNotFoundError:
            pass

        rows[ts] = line  # si ya había fila de ese minuto, se sustituye
        cutoff = ts - RETENTION_HOURS * 3600
        keep = [rows[t] for t in sorted(rows) if t >= cutoff]

        os.makedirs(os.path.dirname(DATA_FILE) or ".", exist_ok=True)
        tmp = DATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(keep) + "\n")
        os.replace(tmp, DATA_FILE)  # escritura atómica: nadie lee un archivo a medias


def read_file():
    with lock:
        try:
            with open(DATA_FILE, "rb") as f:
                return f.read()
        except FileNotFoundError:
            return b""


# ---------------------------------------------------------------- reenvío
def forward(raw, ctype):
    """Reenvía el POST original a otros destinos (Home Assistant, etc.)."""
    for url in FORWARD_URLS:
        try:
            req = urllib.request.Request(url, data=raw, method="POST",
                                         headers={"Content-Type": ctype})
            with urllib.request.urlopen(req, timeout=5) as r:
                r.read()
        except Exception as e:  # un fallo aquí no debe afectar al resto
            logging.warning("No se pudo reenviar a %s: %s", url, e)


# ---------------------------------------------------------------- servidor HTTP
def flatten(qs):
    return {k: v[0] for k, v in qs.items() if v}


class Handler(BaseHTTPRequestHandler):
    server_version = "ecowitt-ametse"

    def log_message(self, format, *args):  # noqa: A002
        logging.info("%s - %s", self.address_string(), format % args)

    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _handle_upload(self, params):
        row = build_row(params, time.time())
        if row is None:
            logging.warning("Datos recibidos sin temperatura/humedad/presión: %s", list(params))
            return self._send(400, "datos insuficientes\n")
        ts, line = row
        save_row(ts, line)
        logging.info("Fila guardada: %s", line)
        self._send(200, "OK\n")

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        if path == "/apidatos.txt":
            self._send(200, read_file())
        elif path == "/health":
            self._send(200, "ok\n")
        elif path.rstrip("/") == UPLOAD_PATH.rstrip("/"):
            self._handle_upload(flatten(parse_qs(u.query)))
        elif path == "/":
            self._send(200, f"Puente Ecowitt -> AMETSE ({STATION_ID}). Datos en /apidatos.txt\n")
        else:
            self._send(404, "not found\n")

    def do_POST(self):
        u = urlparse(self.path)
        if u.path.rstrip("/") != UPLOAD_PATH.rstrip("/"):
            return self._send(404, "not found\n")
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(min(length, 65536))
        body = raw.decode("utf-8", "replace")
        if FORWARD_URLS:
            ctype = self.headers.get("Content-Type", "application/x-www-form-urlencoded")
            threading.Thread(target=forward, args=(raw, ctype), daemon=True).start()
        params = flatten(parse_qs(u.query))
        params.update(flatten(parse_qs(body)))
        self._handle_upload(params)


if __name__ == "__main__":
    logging.info("Estación %s | escuchando en :%s | subida en %s | archivo %s | reenvío a %d destino(s)",
                 STATION_ID, PORT, UPLOAD_PATH, DATA_FILE, len(FORWARD_URLS))
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
