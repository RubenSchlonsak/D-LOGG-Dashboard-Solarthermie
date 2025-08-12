#!/bin/bash

# Solarthermie Dashboard - Raspberry Pi Setup Script
# Führt die komplette Installation und Konfiguration durch

set -e  # Bei Fehlern abbrechen

echo "=== Solarthermie Dashboard Setup für Raspberry Pi ==="
echo

# Farben für Output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Funktionen
print_status() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNUNG]${NC} $1"
}

print_error() {
    echo -e "${RED}[FEHLER]${NC} $1"
}

# 1. System aktualisieren
print_status "System wird aktualisiert..."
sudo apt update && sudo apt upgrade -y

# 2. Abhängigkeiten installieren
print_status "Python3 und Abhängigkeiten werden installiert..."
sudo apt install python3 python3-pip git -y

# 3. USB-Port ermitteln
print_status "USB-Ports werden gesucht..."
if ls /dev/ttyUSB* >/dev/null 2>&1; then
    USB_PORT=$(ls /dev/ttyUSB* | head -n1)
    print_status "USB-Port gefunden: $USB_PORT"
else
    print_warning "Kein /dev/ttyUSB* Port gefunden. Bitte D-LOGG anschließen!"
    echo "Verfügbare Ports:"
    ls /dev/tty* | grep -E "(USB|ACM)" || echo "Keine USB/ACM Ports gefunden"
    echo
    read -p "USB-Port manuell eingeben (z.B. /dev/ttyUSB0): " USB_PORT
fi

# 4. Projektverzeichnis bestimmen und erstellen
CURRENT_USER=$(whoami)
HOME_DIR=$(eval echo ~$CURRENT_USER)
PROJECT_DIR="$HOME_DIR/solarthermie-dashboard"

print_status "Verwende User: $CURRENT_USER"
print_status "Home-Verzeichnis: $HOME_DIR"
print_status "Projekt-Verzeichnis: $PROJECT_DIR"

if [ ! -d "$PROJECT_DIR" ]; then
    print_status "Erstelle Projektverzeichnis $PROJECT_DIR"
    mkdir -p "$PROJECT_DIR"
    
    # Falls die Dateien bereits im aktuellen Verzeichnis sind
    if [ -f "app.py" ]; then
        print_status "Kopiere Projektdateien..."
        cp -r . "$PROJECT_DIR/"
    else
        print_error "app.py nicht gefunden. Bitte Projektdateien in $PROJECT_DIR ablegen."
        exit 1
    fi
fi

cd "$PROJECT_DIR"

# 5. Python-Abhängigkeiten installieren
print_status "Installiere Python-Pakete..."
pip3 install flask pyserial

# 6. User zu dialout-Gruppe hinzufügen
print_status "Füge User '$CURRENT_USER' zur dialout-Gruppe hinzu..."
sudo usermod -a -G dialout $CURRENT_USER

# 7. Systemd Service erstellen
print_status "Erstelle systemd Service..."
sudo tee /etc/systemd/system/solarthermie.service > /dev/null <<EOF
[Unit]
Description=Solarthermie Dashboard
After=multi-user.target
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=$CURRENT_USER
WorkingDirectory=$PROJECT_DIR
Environment=DLOGG_PORT=$USB_PORT
ExecStart=/usr/bin/python3 $PROJECT_DIR/app.py --host 0.0.0.0 --bind 5000
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

# 8. Service aktivieren
print_status "Aktiviere und starte Service..."
sudo systemctl daemon-reload
sudo systemctl enable solarthermie.service
sudo systemctl start solarthermie.service

# 9. Status prüfen
sleep 3
if systemctl is-active --quiet solarthermie.service; then
    print_status "Service erfolgreich gestartet!"
else
    print_error "Service konnte nicht gestartet werden. Status:"
    sudo systemctl status solarthermie.service --no-pager
fi

# 10. IP-Adresse anzeigen
print_status "Setup abgeschlossen!"
echo
echo "=== Zugriff auf Dashboard ==="
IP_ADDR=$(hostname -I | awk '{print $1}')
echo "Dashboard erreichbar unter: http://$IP_ADDR:5000"
echo "API-Endpunkt: http://$IP_ADDR:5000/api/current"
echo
echo "=== Nützliche Befehle ==="
echo "Service Status: sudo systemctl status solarthermie.service"
echo "Service Logs:   journalctl -u solarthermie.service -f"
echo "Service Stop:   sudo systemctl stop solarthermie.service"
echo "Service Start:  sudo systemctl start solarthermie.service"
echo
print_warning "Ein Neustart wird empfohlen damit alle Berechtigungen aktiv werden:"
echo "sudo reboot"