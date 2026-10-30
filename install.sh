#!/bin/sh
# Build the tray, link the tools into ~/.local/bin, add a launcher entry and
# start the tray with the session. No root needed. --uninstall undoes it all.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
bin="$HOME/.local/bin"
apps="$HOME/.local/share/applications"
autostart="$HOME/.config/autostart"

if [ "${1:-}" = "--uninstall" ]; then
    pkill -x mchose-tray 2>/dev/null || true
    rm -f "$bin/mchose" "$bin/mchose-gui" "$bin/mchose-tray" \
          "$apps/mchose.desktop" "$autostart/mchose-tray.desktop"
    echo "Désinstallé (les sauvegardes restent dans ~/.local/state/mchose)."
    exit 0
fi

cc -O2 -Wall -o "$here/mchose-tray" "$here/mchose-tray.c" \
    $(pkg-config --cflags --libs gio-2.0 cairo) -lm

mkdir -p "$bin" "$apps" "$autostart"
chmod +x "$here/mchose.py" "$here/mchose-gui.py"
ln -sf "$here/mchose.py" "$bin/mchose"
ln -sf "$here/mchose-gui.py" "$bin/mchose-gui"
ln -sf "$here/mchose-tray" "$bin/mchose-tray"

cat > "$apps/mchose.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Souris MCHOSE
Comment=DPI, polling rate, LOD et batterie de la souris MCHOSE
Exec=python3 $here/mchose-gui.py
Icon=input-mouse
Categories=Settings;HardwareSettings;
Keywords=mouse;souris;dpi;mchose;
EOF

cat > "$autostart/mchose-tray.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Batterie souris MCHOSE
Comment=Niveau de batterie de la souris dans la zone de notification
Exec=$here/mchose-tray
Icon=input-mouse
X-KDE-autostart-after=panel
EOF

pkill -x mchose-tray 2>/dev/null || true
nohup "$here/mchose-tray" >/dev/null 2>&1 &
echo "Installé. Le tray est lancé et démarrera avec la session."
case ":$PATH:" in
    *":$bin:"*) ;;
    *) echo "Note : $bin n'est pas dans le PATH (fish : fish_add_path ~/.local/bin)." ;;
esac
