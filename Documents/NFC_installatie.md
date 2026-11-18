# Oradio NFC-demo — installatie en beheer

Status: 18 november 2026. Deze handleiding beschrijft NFC-reader versie 10 en de bridge met maximaal drie automatische herstarts. Zie ook [ontwikkellessen](NFC_ontwikkellessen.md).

## 1. Wat wordt geïnstalleerd?

Dit is een uitbreiding op een werkende Oradio4-installatie, geen volledige installatie van Debian, MPD, audio en de USB-mountservice. Volg daarvoor eerst de [basisdocumentatie](../README.md) en het bijbehorende installatieproces. Controleer dat presets, muziek, knoppen en leds werken voordat je NFC toevoegt.

De ontwikkelopstelling gebruikte:

- Raspberry Pi 3A+, Debian 13 (Trixie), arm64, Python 3.13.5.
- Elechouse PN532 V3 in I²C-modus op `/dev/i2c-1`.
- libnfc 1.8.0; de Python-lezer controleert deze versie expliciet.
- NTAG213-muziekkaarten, één kort NDEF Text-record per kaart.
- PN532-module gevoed met 5 V; SDA/SCL waren volgens de gebruiker gecontroleerd onder 3,3 V. Dit is geen toestemming om willekeurige PN532-modules of GPIO-signalen op 5 V aan te sluiten: controleer de voedings- en signaalspecificaties van de gebruikte module.
- SDA op BCM GPIO2 (headerpin 3), SCL op BCM GPIO3 (headerpin 5), gemeenschappelijke GND. Geen IRQ aangesloten.
- I²C op 100 kHz. De DigiAMP+ kan dezelfde bus gebruiken, maar slechts één NFC-proces mag de PN532 bedienen.

De repository heet Oradio4; de demo op de Pi staat in `/home/pi/Oradio3`. Dat verschil is bewust. Onderstaande opdrachten gebruiken die Pi-locatie en gebruiker `pi`. Pas paden consequent aan als de basisinstallatie elders staat. De huidige USB-standaardpaden gaan uit van `/media/oradio`.

## 2. Leg de gebruikte versie vast

Gebruik een checkout waarin alle NFC-wijzigingen gecommit zijn. Noteer voor iedere nieuwe installatie:

```bash
git rev-parse HEAD
git status --short
```

Bewaar de commit-ID samen met `cat /etc/os-release`, `python3 --version` en `dpkg-query -W libnfc6`. Een branchnaam alleen is geen reproduceerbare versie. Commit geen `.DS_Store`, persoonlijke UID-cache of testlogs.

Benodigde bestanden, als samenhangende versie:

| Bestand | Functie |
|---|---|
| `Main/nfc_service.py` | Kaartinhoud, blijvende cache, herkenning en verwijderingsfilter |
| `Main/pn532_uid_monitor.py` | ctypes/libnfc-backend, UID-selectie, lock en watchdog |
| `Main/nfc_bridge.py` | Apart leesproces, log en begrensd herstel |
| `Main/music_card_control.py` | CARD-bericht naar playlistactie |
| `Main/oradio_control.py` | Play-state, leds en Play-geluid |
| `Main/mpd_control.py` | Playlists én muziekmappen afspelen |
| `Documents/music_cards.json` | Voorbeeld van kaartcode naar muziekbron |

## 3. Bereid de Pi voor

Installeer de runtimebibliotheek indien nodig:

```bash
sudo apt update
sudo apt install libnfc6
python3 --version
dpkg-query -W libnfc6
ls -l /dev/i2c-1
id
```

De NFC-modules gebruiken Python-standaardbibliotheken en ctypes; geen extra Python NFC-pakket. `libnfc-dev` en een C-compiler waren nodig voor eerdere ABI-proeven, maar niet voor deze reader. Controleer dat de geïnstalleerde libnfc-versie 1.8.0 is; een andere versie vraagt nieuwe validatie.

De gebruiker van `oradio.service` moet toegang hebben tot groep `i2c`. Indien nodig:

```bash
sudo usermod -aG i2c pi
```

Log daarna opnieuw in. Controleer ook de servicegebruiker met `systemctl cat oradio.service`.

Zet de PN532 volgens de aanduidingen van de module in I²C-modus terwijl de voeding uit is. Gebruik in `/boot/firmware/config.txt` één actieve I²C-regel:

```ini
dtparam=i2c_arm=on,i2c_arm_baudrate=100000
```

Bewaar overige Oradio/audio-instellingen. Herstart na een wijziging. De lagere snelheden 50 en 25 kHz bleken geen overtuigende oplossing voor de leesproblemen.

## 4. Plaats code en USB-configuratie

Stop eerst de zelfstandige lezer met Ctrl+C. Stop Oradio tijdens het vervangen van code. Als de testservice aanwezig is, stop ook `oradio-powerloop.service`; deze is niet nodig voor de demo.

```bash
sudo systemctl stop oradio.service
```

Maak vóór vervanging een backup van de bestaande Main-bestanden. Kopieer de zes bestanden uit de tabel vanuit de gekozen checkout naar `/home/pi/Oradio3/Main`. Gebruik geen mix van oude en nieuwe modules. Bij een nieuwe installatie hoort de rest van Oradio uit dezelfde gekozen codeversie te komen.

Controleer dat de USB echt gemount is:

```bash
mountpoint /media/oradio
ls -l /media/oradio/Systeem
```

Plaats `music_cards.json` in `/media/oradio/Systeem`, naast `presets.json`. Overschrijf geen bestaande kaartconfiguratie zonder backup. Het veld `playlist` mag een opgeslagen MPD-playlist óf een muziekmap bevatten. Gebruik exacte namen; `Pop60s` en `Pop70s` waren in de demo mappen, geen opgeslagen playlists. Andere voorbeeldnamen moeten op de betreffende USB worden gecontroleerd.

```bash
python3 -m json.tool /media/oradio/Systeem/music_cards.json
mpc lsplaylists
mpc ls
```

`mpc` is een optioneel diagnosehulpmiddel (`sudo apt install mpc` als het ontbreekt). Een map op USB moet ook door MPD geïndexeerd zijn voordat die afspeelbaar is.

Voorbeeld van een koppeling binnen `cards`:

```json
"oradio:v1:card:theme:jaren_zestig": {
  "name_nl": "Jaren 60",
  "playlist": "Pop60s"
}
```

## 5. Cache en inleren

Standaard staat de cache naast `music_cards.json`:

```text
/media/oradio/Systeem/nfc_uid_cache.json
```

Bij een verse installatie mag dit bestand ontbreken. De bestaande map moet wel schrijfbaar zijn voor `pi`. De eerste volledig gelezen, bekende kaartcode wordt automatisch opgeslagen. Niet handmatig een leeg bestand aanmaken: een leeg bestand is geen geldige JSON-cache.

Bij migratie van versie 9, met Oradio en de lezer gestopt:

```bash
mountpoint -q /media/oradio && cp -n \
  /home/pi/.local/share/oradio/nfc_uid_cache.json \
  /media/oradio/Systeem/nfc_uid_cache.json
python3 -m json.tool /media/oradio/Systeem/nfc_uid_cache.json
sync
```

Controleer fouten voordat je verdergaat. `cp -n` laat een bestaande USB-cache intact; controleer in dat geval welke koppelingen al aanwezig zijn. De oude lokale cache blijft als backup staan, maar wordt standaard niet meer gebruikt.

De cache bewaart alleen UID → code, niet de playlist. Playlistwijzigingen in `music_cards.json` vereisen dus geen nieuwe kaart, maar wel een herstart van de lezer. Bij herschrijven van de code op de kaart moet je de oude UID-koppeling vergeten; zie beheer hieronder.

## 6. Zelfstandige test

Oradio moet gestopt zijn zodat er geen tweede PN532-lezer draait. Activeer de venv van de basisinstallatie en start:

```bash
source /home/pi/.venv/bin/activate
python3 -u /home/pi/Oradio3/Main/nfc_service.py
```

Verwacht `READY` met `reader_version: 10`, het USB-cachepad en het aantal ingeleerde UID's. Een nieuwe kaart geeft `CACHE_SAVED` en `CARD` met `source: tag`. Na verwijderen en opnieuw aanbieden volgt `source: cache` en `bytes_read: 0`.

Schrijf op NTAG213 één NDEF **Tekst-record**, bijvoorbeeld exact `oradio:v1:card:theme:jaren_zestig`. Geen URL, aanhalingstekens, extra spaties of regels. De demo ondersteunt één kort UTF-8 Text-record; niet alle geldige NDEF-formaten worden ondersteund.

Stop met Ctrl+C. `SUMMARY` en `STOPPED` horen te verschijnen. Na opnieuw starten moet dezelfde kaart direct uit de cache komen. Een kaart minstens 1,2 seconde onafgebroken niet vinden geeft `REMOVED`; dit is een softwarewaarneming, geen fysiek verwijderingsbewijs.

## 7. De precieze inhoud van 95-nfc-demo.conf

Bestand op de Pi:

```text
/etc/systemd/system/oradio.service.d/95-nfc-demo.conf
```

Volledige inhoud:

```ini
[Service]
Environment=ORADIO_NFC_ENABLED=1
```

Dit is een aanvulling op de bestaande `oradio.service`, geen losse NFC-service. Het verandert geen `ExecStart`, gebruiker, werkmap, cachepad of herstartbeleid van Oradio. De bridge start met dezelfde Python-interpreter als Oradio en wacht op USB en muziekbibliotheek. Zonder de waarde `1` start Oradio geen NFC-bridge.

Een identiek sjabloon staat in [install_resources/95-nfc-demo.conf](../install_resources/95-nfc-demo.conf). Vanuit de gekozen repositorycheckout op de Pi:

```bash
sudo install -D -m 0644 install_resources/95-nfc-demo.conf \
  /etc/systemd/system/oradio.service.d/95-nfc-demo.conf
sudo systemctl daemon-reload
sudo systemctl start oradio.service
systemctl status oradio.service --no-pager
systemctl show oradio.service -p Environment
```

Deze aanvulling wordt momenteel handmatig geïnstalleerd; de algemene installer is hiervoor niet aangepast. Start niet tegelijkertijd de zelfstandige reader.

## 8. Acceptatietest met muziek

| Proef | Verwacht |
|---|---|
| Preset actief, kaart Jaren 60 aanbieden | Pop60s, StatePlay, presetleds uit, Play-led aan, normaal Play-geluid |
| Jaren 70 aanbieden terwijl Play actief is | Pop70s en opnieuw het Play-geluid |
| Kaart laten liggen of kort verschuiven | Geen herhaalde start bij een korte detectieonderbreking |
| Kaart verwijderen | Muziek speelt door |
| Stop drukken terwijl kaart blijft liggen | Blijft gestopt; verwijder en bied opnieuw aan om opnieuw te starten |
| Terug naar een preset | Preset en bijbehorende leds werken |
| Reboot | UID's geladen vanaf USB; directe cacheherkenning |
| Ongeldige/onbekende kaart | Geen wijziging van de muziek |

Test daarna 30–60 minuten met spelende muziek en kaartwissels. Controleer één normale serviceherstart. De eerdere I²C-storing is nog niet definitief verklaard; een geslaagde korte proef is geen duurtest.

## 9. Logs en herstel

```bash
tail -n 30 -F /home/pi/.local/state/oradio/nfc_bridge.log
tail -f /home/pi/Oradio3/logging/oradio.log
```

De bridge-log bevat tijdstempels en meerdere sessies. Kijk na de laatste `NFC reader started`/`READY`; oude fouten blijven zichtbaar. Het werkelijke Oradio-logpad kan bij een andere basisinstallatie afwijken. De NFC-log heeft nog geen ingebouwde rotatie.

Bij onverwacht eindigen volgen maximaal drie herstartpogingen na 2, 5 en 10 seconden. Dit budget geldt per bridge/Oradio-start en wordt niet door `READY` gereset. `oradio.log` meldt de fout, iedere poging, opnieuw openen en latere kaartdetectie. Als het budget op is, blijft NFC offline terwijl Oradio doorgaat. Bridge-interne fouten, bijvoorbeeld een fout bij doorgeven van berichten, stoppen de bridge zonder deze herstartlus.

Dit is procesherstel, geen elektrische reset. Bij blijvend `NFC_EIO`, `Unexpected PN53x reply` of mislukte open kan volledig spanningsloos maken nodig zijn: eerst `sudo systemctl stop oradio.service`, dan `sudo poweroff`, wachten op afsluiten, vervolgens zowel Pi als eventuele aparte PN532-voeding losnemen. Het programma reboot de Pi niet automatisch.

De bridge onderdrukt een opnieuw gemelde, laatst verwerkte UID na readerherstel om ongewenste muziekherstart te voorkomen. Als een kaart na herstel niet opnieuw start: verwijderen tot `REMOVED` en nogmaals aanbieden.

## 10. Beheer, backup en terugzetten

Herschreven kaart opnieuw inleren, met de reader gestopt:

```bash
sudo systemctl stop oradio.service
/home/pi/.venv/bin/python3 /home/pi/Oradio3/Main/nfc_service.py \
  --forget-uid 04:7A:85:22:F0:1C:91
sudo systemctl start oradio.service
```

Het opgegeven UID is een voorbeeld uit de demo; gebruik het UID van de betreffende kaart. Voor een andere configuratie zijn `--cards` en `--cache` beschikbaar. De standaardcache volgt `--cards`; een expliciet `--cache` heeft voorrang. Bij een eigen lokale cachemap moet die map vooraf bestaan.

Wissel een USB-stick met Oradio gestopt en start daarna opnieuw. Configuratie en cache worden niet automatisch opnieuw ingelezen bij hot-swap. Een cacheschrijffout geeft `CACHE_WRITE_ERROR`; de koppeling kan die sessie in geheugen werken, maar is niet gegarandeerd bewaard. Beschadigde cachebestanden worden niet stilzwijgend overschreven.

Backup: gekozen Git-commit, `music_cards.json`, `nfc_uid_cache.json`, het systemd-drop-inbestand en de werkende firmware/I²C-instelling. Bewaar de UID-cache bij de USB-backup, niet als standaard gebruikersdata in Git.

Alleen NFC uitschakelen:

```bash
sudo systemctl stop oradio.service
sudo rm /etc/systemd/system/oradio.service.d/95-nfc-demo.conf
sudo systemctl daemon-reload
sudo systemctl start oradio.service
```

Bij een code-regressie herstel je bovendien de vooraf geback-upte code als samenhangende set. Zet geen losse willekeurige oudere module terug.

## 11. Softwaretests

Vanuit de checkout, zonder NFC-hardware:

```bash
cd module_test
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  test_nfc_service test_nfc_bridge test_music_cards state_machine_race_test -v
```

Deze tests gebruiken gesimuleerde hardware en tijdelijke bestanden/processen. Ze vervangen de acceptatietest op de Pi niet.
