#!/bin/sh
# Installs the catprinter CUPS backend and creates two print queues:
#
#   CatPrinter      regular printer: anything CUPS can print (PDF, images, text,
#                   jobs from other computers), rendered for the 48 mm head.
#   CatPrinter-ZPL  raw queue for ZPL labels (e.g. Mercado Libre's), as if it
#                   were a Zebra printer.
#
# Usage (as root, from the repository):
#   sudo ./cups/install.sh [PRINTER_MAC|auto] [--share]
#   sudo ./cups/install.sh --uninstall
#
# PRINTER_MAC pins the queues to one printer (recommended); "auto" (default)
# uses the first cat printer found. --share publishes both queues on the local
# network so other computers (Windows, macOS, phones) can print to them.
set -eu

PREFIX=/opt/catprinter
BACKEND=/usr/lib/cups/backend/catprinter
QUEUE=CatPrinter
ZPL_QUEUE=CatPrinter-ZPL
REPO=$(cd "$(dirname "$0")/.." && pwd)

if [ "$(id -u)" -ne 0 ]; then
    echo "Run it as root: sudo $0 $*" >&2
    exit 1
fi

if [ "${1:-}" = "--uninstall" ]; then
    lpadmin -x "$QUEUE" 2>/dev/null || true
    lpadmin -x "$ZPL_QUEUE" 2>/dev/null || true
    rm -f "$BACKEND"
    rm -rf "$PREFIX"
    echo "Removed the $QUEUE and $ZPL_QUEUE queues, the backend and $PREFIX."
    exit 0
fi

DEVICE=auto
SHARE=no
for arg in "$@"; do
    case "$arg" in
        --share) SHARE=yes ;;
        *) DEVICE=$arg ;;
    esac
done
# One slash: with "//" CUPS parses the address as host:port and rejects the URI.
URI="catprinter:/$DEVICE"

echo "Installing the catprinter package in $PREFIX..."
mkdir -p "$PREFIX"
rm -rf "$PREFIX/catprinter"
cp -r "$REPO/catprinter" "$PREFIX/catprinter"
find "$PREFIX/catprinter" -name __pycache__ -prune -exec rm -rf {} +
cp "$REPO/requirements.txt" "$PREFIX/"
if [ ! -x "$PREFIX/venv/bin/python" ]; then
    python3 -m venv "$PREFIX/venv"
fi
"$PREFIX/venv/bin/pip" install --quiet --upgrade pip
"$PREFIX/venv/bin/pip" install --quiet -r "$PREFIX/requirements.txt"

echo "Installing the backend in $BACKEND..."
printf '#!%s\nimport sys\nsys.path.insert(0, "%s")\nfrom catprinter.cups_backend import main\nsys.exit(main())\n' \
    "$PREFIX/venv/bin/python" "$PREFIX" > "$BACKEND"
# Mode 0700 makes CUPS run the backend as root, which it needs for Bluetooth.
chown root:root "$BACKEND"
chmod 0700 "$BACKEND"

echo "Creating the $QUEUE and $ZPL_QUEUE queues ($URI)..."
lpadmin -p "$QUEUE" -E -v "$URI" -P "$REPO/cups/catprinter.ppd" \
    -D "Cat Printer (48 mm)" -L "Bluetooth" \
    -o printer-error-policy=retry-job \
    -o print-scaling-default=fit
lpadmin -p "$ZPL_QUEUE" -E -v "$URI" -m raw \
    -D "Cat Printer - ZPL labels" -L "Bluetooth" \
    -o printer-error-policy=retry-job 2>/dev/null

if [ "$SHARE" = yes ]; then
    lpadmin -p "$QUEUE" -o printer-is-shared=true
    lpadmin -p "$ZPL_QUEUE" -o printer-is-shared=true
    cupsctl --share-printers
    echo "Shared on the local network:"
    echo "  http://$(hostname).local:631/printers/$QUEUE"
    echo "  http://$(hostname).local:631/printers/$ZPL_QUEUE"
    if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
        echo "The ufw firewall is active: allow CUPS with 'sudo ufw allow 631/tcp'."
    fi
fi

echo "Done. Test it with:"
echo "  lp -d $QUEUE some-image.png"
echo "  lp -d $ZPL_QUEUE label.txt"
