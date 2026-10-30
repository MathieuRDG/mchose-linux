# MCHOSE A7 V2 Ultra sous Linux

Batterie dans la zone de notification, et réglages de la souris (DPI, polling
rate, LOD, mode, options capteur, anti-rebond, veille) sans le driver web M HUB.

| Fichier | Rôle | Dépendances |
|---|---|---|
| `mchose-tray` (`mchose-tray.c`) | icône de batterie, tourne en permanence | GIO + cairo (déjà là) |
| `mchose.py` | bibliothèque + ligne de commande | Python seul |
| `mchose-gui.py` | fenêtre de réglages, lancée à la demande | GTK 4 + libadwaita (déjà là) |
| `70-mchose.rules` | règle udev corrigée (optionnelle) | — |

Testé sur une A7 V2 Ultra : dongle `3837:100b`, firmware 5.44.2.4, en 2.4 GHz.

*Projet non officiel, sans lien avec MCHOSE. Les noms MCHOSE et M HUB
appartiennent à leur propriétaire et ne servent ici qu'à désigner le matériel.*

## Installer

```sh
./install.sh            # compile le tray, liens dans ~/.local/bin, lanceur, démarrage auto
./install.sh --uninstall
```

Pas de root. Le démarrage automatique passe par `~/.config/autostart`, donc il se
désactive aussi depuis *Paramètres système → Démarrage automatique*.

## L'icône

Le pourcentage est écrit dans l'icône : vert ≥ 50 %, orange ≥ 20 %, rouge en
dessous, bleu en charge, gris quand la souris dort (elle ne répond pas en
veille, la dernière valeur reste affichée). Notification à 15 % puis 5 %.
Clic gauche : réglages. Clic droit : menu. Clic milieu : relire tout de suite.

Une lecture par minute (`mchose-tray --interval 30` pour plus souvent) :
quelques échanges de 21 octets sur l'interface de configuration. Ça ne touche
pas l'interface sur laquelle passent les mouvements, et les feature reports
passent par l'endpoint de contrôle, pas par celui des mouvements : le pointeur
n'est pas ralenti. Environ 3 Mo de RAM, 0 % de CPU.

## Ligne de commande

```sh
mchose battery                     # 60 %  [2.4G]
mchose show                        # toute la configuration
mchose dpi 800                     # DPI du palier courant
mchose dpi --stages 400,800,1600 --active 2
mchose stage 3
mchose rate 1000                   # 125 500 1000 2000 4000 8000
mchose lod 1                       # 0.7, 1 ou 2 mm
mchose mode gaming                 # performance, gaming, extreme
mchose motion-sync off             # ripple / angle / motion-sync on|off
mchose debounce 4                  # 0-20 ms
mchose sleep 10                    # minutes, 0 = jamais
mchose backup                      # copie dans ~/.local/state/mchose/
mchose restore FICHIER
mchose measure                     # polling réellement mesuré (bouge la souris)
mchose --debug show                # chaque trame envoyée et reçue
```

Chaque écriture est relue dans la souris : une commande qui affiche un résultat
a bien changé la souris, sinon c'est une erreur. Avant la toute première
modification, la configuration d'origine est copiée dans
`~/.local/state/mchose/config-original.bin` (bouton « Restaurer » dans la
fenêtre, ou `mchose restore` sur ce fichier).

## Sécurité

Rien n'écrit de firmware. Les commandes envoyées sont exactement celles de M HUB,
avec une seule différence volontaire : en changeant le polling d'un lien, l'outil
renvoie la valeur actuelle de l'autre lien au lieu de 0, pour ne jamais le toucher.
Un verrou commun (`$XDG_RUNTIME_DIR/mchose.lock`) empêche le tray et la fenêtre de
parler à la souris en même temps.

## Règle udev (demande sudo)

Sans règle, seul root peut parler à la souris. `70-mchose.rules` donne l'accès à
la personne connectée, pour le dongle (`100b`) comme pour la souris en câble
(`4019`, nécessaire pour lire la batterie pendant la charge), et garde les deux
hors de la mise en veille USB :

```sh
sudo install -m 644 70-mchose.rules /etc/udev/rules.d/
sudo udevadm control --reload && sudo udevadm trigger
```

Si une ancienne règle existe déjà pour `3837:100b` (par exemple installée pour
le driver web), vérifie-la avec `udevadm verify` : une règle en `MODE="0666"`
ouvre la souris à tous les comptes de la machine, `TAG+="uaccess"` suffit.

## Le protocole, en bref

Interface USB 2, collection vendeur `0xFF01`, feature reports `0x11` (20 octets)
et `0x12` (64). Commande + arguments, chaque octet XOR `0xFF`, bourrage compris.
La réponse renvoie `cmd ^ 0xFF` en octet 1. Tant que la souris n'a pas répondu
par radio, le dongle renvoie la requête elle-même : on redemande.

| Commande | Rôle |
|---|---|
| `11 06` | statut : vid, modèle, firmware, lien, **batterie**, charge |
| `12 67` / `12 57` | lecture / écriture du bloc de configuration (63 octets) |
| `12 40` | 6 paliers DPI (+ axe Y), palier courant, nombre de paliers |
| `11 41` | polling (câble, radio), index dans 125…8000 |
| `11 42` | LOD, ripple, angle snapping, motion sync, mode : 0 = inchangé, 1 = oui, 2 = non |
| `11 43` | anti-rebond (ms) |
| `11 0A` | veille (activée, minutes) |

Octet capteur du bloc : bits 0-1 LOD (`00` 0,7 mm, `01` 1 mm, `10` 2 mm),
bit 2 ripple, bit 3 angle snapping, bit 4 motion sync, bits 6-7 mode.
Octet 1 = polling/palier du câble, octet 2 = ceux de la radio (vérifié en
mesurant 500 Hz réels pendant que l'octet 2 disait 500).

Sources : le bundle JavaScript de M HUB (mchose.com.cn), le travail de
[alexfrih/mchose-linux](https://github.com/alexfrih/mchose-linux) et de
[HeyOkay/HaloBattery](https://github.com/HeyOkay/HaloBattery).

## Licence

MIT, voir `LICENSE`.
