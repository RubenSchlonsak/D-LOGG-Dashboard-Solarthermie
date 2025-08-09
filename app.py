import argparse
import os
import sys
import time
from datetime import datetime
from typing import Dict, List, Tuple

from flask import Flask, jsonify, render_template
try:
    import serial  # pyserial
except Exception as e:
    print("pySerial fehlt. Installiere mit:  pip install pyserial", file=sys.stderr)
    raise

# ---- Protokoll-Konstanten (D-LOGG / UVR) ----
CMD_MODE        = 0x81  # Modusabfrage
CMD_CURRENT     = 0xAB  # Aktuelle Daten
TYPE_UVR1611    = 0x80
TYPE_UVR61_3    = 0x90  # (unterstützen wir hier nicht im Detail)
MODE_1DL        = 0xA8
MODE_2DL        = 0xD1

# ---- Sensor-Namen (Mapping) ----
# (Nicht belegte IDs bleiben unbenannt/werden nicht angezeigt)
SENSOR_LABELS = {
    1:  "Temperature Sonnenkollektor",  # T1
    2:  "Puffer oben 1",                # T2
    3:  "Puffer unten 2",               # T3
    4:  "Warmwasser",                   # T4
    8:  "Puffer oben 2",                # T8
    9:  "Puffer mitte",                 # T9
    10: "Kessel Rücklauf",              # T10
    13: "Holzkessel",                   # T13
    14: "Kessel Vorlauf",               # T14
    16: "Puffer unten 2",               # T16 (zweiter Fühler unten)
}

# ---- Serial helper ----
def open_serial(port: str, baud: int = 115200, timeout: float = 2.0) -> serial.Serial:
    return serial.Serial(port=port, baudrate=baud, timeout=timeout)

def query_mode(ser: serial.Serial) -> int:
    ser.reset_input_buffer()
    ser.write(bytes([CMD_MODE]))
    b = ser.read(1)
    if len(b) != 1:
        raise RuntimeError("Keine Antwort auf Modusabfrage.")
    return b[0]

def request_current(ser: serial.Serial) -> bytes:
    """
    Holt einen 'Aktuelle Daten' Frame.
    Hält 1DL (57 Bytes) und 2DL (113 Bytes) für UVR1611 aus.
    """
    ser.reset_input_buffer()
    ser.write(bytes([CMD_CURRENT]))
    time.sleep(0.05)  # mini delay

    buf = ser.read(200)  # genug groß für 2DL
    # Aufräumen: nur sinnvolle Längen
    if len(buf) in (57, 113):
        return buf

    # gelegentlich liefert der D-LOGG in zwei Stücken; ein wenig nachfassen
    extra = ser.read(200)
    buf += extra
    if len(buf) >= 113:
        return buf[:113]
    if len(buf) >= 57:
        return buf[:57]

    raise RuntimeError(f"Kein valider Antwort-Frame empfangen (len={len(buf)}).")

# ---- Parsing UVR1611 ----

def _decode_sensor_value(low: int, high: int) -> Tuple[str, float]:
    """
    Decodiert einen 2-Byte Eingang Tn (UVR1611).
    Gibt (kind, value) zurück, wobei kind z.B. 'temp', 'flow', 'radiation', 'digital', 'unused' ist.
    Für das Dashboard nutzen wir nur 'temp'.
    """
    # Einheit/Typ steckt in den Bits 4..6 des high-Bytes
    etype = (high >> 4) & 0b111

    # Temp-/Flow-/Radiation-Werte basieren auf 12 Bit Nutzwert (low + high[0..3])
    raw12 = ((high & 0x0F) << 8) | low
    sign = (high & 0x80) != 0

    if etype == 0b000:
        return ("unused", float("nan"))
    if etype == 0b001:
        # digitaler Pegel (AUS/EIN)
        # wir liefern 0/1 (hier nicht angezeigt im Dashboard)
        bit = 1.0 if raw12 else 0.0
        return ("digital", bit)
    if etype == 0b010:
        # Temperatur (1/10 °C), kann negativ sein (12-bit two's complement)
        val = -(((~raw12) & 0x0FFF) + 1) if sign else raw12
        return ("temp", val / 10.0)
    if etype == 0b011:
        # Volumenstrom (4 l/h) – hier nicht genutzt
        return ("flow", raw12 * 4.0)
    if etype == 0b110:
        # Strahlung (W/m²)
        return ("radiation", float(raw12))
    if etype == 0b111:
        # Raumtemperatur (Spezialfall bei UVR): laut Referenz
        # Wenn unterstes Bit im High gesetzt, +256/10
        if (high & 0x01) != 0:
            return ("temp", (256 + low) / 10.0)
        else:
            return ("temp", low / 10.0)

    # Fallback
    return ("unknown", float("nan"))

def _parse_uvr1611_block(dev_type: int, data55: bytes) -> Dict:
    """
    Parsen eines UVR1611-Blocks innerhalb 'Aktuelle Daten'.
    data55: genau 55 Bytes (alles nach dem Typbyte, ohne Checksum), Reihenfolge wie im D-LOGG.
    Rückgabe: {"type": "UVR1611", "temps": {"T1":..}, "outputs": {...}}
    """
    if dev_type != TYPE_UVR1611 or len(data55) != 55:
        raise ValueError("Block ist nicht UVR1611 oder falsche Länge.")

    # Sensoren T1..T16: 32 Byte => 16 * (low, high)
    temps: Dict[str, float] = {}
    for i in range(16):
        low = data55[2*i + 0]
        high = data55[2*i + 1]
        kind, val = _decode_sensor_value(low, high)
        if kind == "temp":
            temps[f"T{i+1}"] = val

    # Ausgänge (optional, falls später benötigt)
    ausgbyte1 = data55[32]   # A1..A8
    ausgbyte2 = data55[33]   # A9..A13 in unteren Bits
    outputs = {}
    for bit in range(8):
        outputs[f"A{bit+1}"] = 1 if (ausgbyte1 >> bit) & 1 else 0
    for bit in range(5):
        outputs[f"A{bit+9}"] = 1 if (data55[33] >> bit) & 1 else 0

    return {
        "type": "UVR1611",
        "temps": temps,
        "outputs": outputs,
    }

def parse_current_frame(buf: bytes) -> List[Dict]:
    """
    Nimmt einen 'Aktuelle Daten' Puffer (1DL=57B, 2DL=113B) und liefert eine Liste von Geräten.
    Jedes Gerät als Dict mit 'type', 'temps', 'outputs'.
    """
    devices: List[Dict] = []

    if len(buf) == 57 and buf[0] == TYPE_UVR1611:
        # 1DL, 1 Gerät (UVR1611): [type][55 data][checksum]
        data55 = buf[1:56]
        devices.append(_parse_uvr1611_block(TYPE_UVR1611, data55))
        return devices

    if len(buf) == 113 and buf[0] in (TYPE_UVR1611, TYPE_UVR61_3):
        # 2DL: layout (UVR1611/61-3 gemischt möglich):
        # [type1][55 bytes dev1][type2][55 bytes dev2][checksum]
        t1 = buf[0]
        d1 = buf[1:56]
        t2 = buf[56]
        d2 = buf[57:112]
        # checksum = buf[112]  # (hier nicht geprüft)

        if t1 == TYPE_UVR1611:
            try:
                devices.append(_parse_uvr1611_block(t1, d1))
            except Exception:
                pass
        # UVR61_3 lassen wir (fürs Temperaturlayout) weg oder später ergänzen.
        if t2 == TYPE_UVR1611:
            try:
                devices.append(_parse_uvr1611_block(t2, d2))
            except Exception:
                pass

        return devices

    # Fallback: unbekannt – keine Geräte
    return devices

# ---- Flask App ----
app = Flask(__name__)

# Konfiguration über Env/Args
SERIAL_PORT = os.environ.get("DLOGG_PORT", "COM4")  # auf dem Pi z.B. /dev/ttyUSB0

def read_all_devices() -> Dict:
    """
    Öffnet seriell kurz, fragt Modus & aktuelle Daten ab, parst.
    Liefert ein JSON-geeignetes Dict mit gemappten Namen.
    """
    with open_serial(SERIAL_PORT) as ser:
        mode = query_mode(ser)  # 0xA8 (1DL) oder 0xD1 (2DL)
        _ = mode  # aktuell nicht benötigt
        buf = request_current(ser)
        devices = parse_current_frame(buf)

    # Mappen: gewünschte Labels auf Basis T-Nummer
    # Alle Temps beider Geräte in ein gemeinsames Feld (bei Dubletten gewinnt Gerät1).
    merged: Dict[str, float] = {}
    for dev in devices:
        for t_key, val in dev.get("temps", {}).items():
            try:
                t_num = int(t_key[1:])  # "T7" -> 7
            except Exception:
                continue
            if t_num in SENSOR_LABELS and not (SENSOR_LABELS[t_num] in merged):
                merged[SENSOR_LABELS[t_num]] = val

    # Zusätzlich (optional) alle übrigen T-Kanäle sichtbar machen:
    # for dev in devices:
    #     for t_key, val in dev.get("temps", {}).items():
    #         name = t_key
    #         if name not in merged:
    #             merged[name] = val

    return {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "port": SERIAL_PORT,
        "devices_found": len(devices),
        "values": merged,  # { "Warmwasser": 52.3, ... }
    }

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/current")
def api_current():
    try:
        data = read_all_devices()
        return jsonify({"ok": True, **data})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

def parse_args():
    p = argparse.ArgumentParser(description="D-LOGG → Web-Dashboard")
    p.add_argument("--port", "-p", default=os.environ.get("DLOGG_PORT", SERIAL_PORT),
                   help="Serieller Port (Windows: COM4, Linux: /dev/ttyUSB0)")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--bind", type=int, default=5000, help="HTTP Port")
    return p.parse_args()

if __name__ == "__main__":
    args = parse_args()
    SERIAL_PORT = args.port
    print(f"* Starte Dashboard auf http://{args.host}:{args.bind}  (Port: {SERIAL_PORT})")
    app.run(host=args.host, port=args.bind, debug=False)
