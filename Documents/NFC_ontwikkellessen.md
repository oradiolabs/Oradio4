# Oradio NFC — ontwikkellessen en ontwerpkeuzes

Datum: 18 november 2026. Geldt voor reader versie 10 en de bijbehorende bridge, kaartadapter en MPD/state-aanpassingen. Praktische installatie: [NFC_installatie.md](NFC_installatie.md).

## Doel en afbakening

Een muziekkaart moet een vaste kaartcode naar een muziekbron vertalen en Oradio in Play brengen, inclusief de normale geluidsbevestiging. Betrouwbare muziekkaarten kregen voorrang boven smartphone-interactie. Er is geen iPhone-detectie of target/card-emulation in de uiteindelijke reader.

Dit document onderscheidt waarnemingen op de Pi, eigenschappen van de code en nog onbewezen verklaringen. De proeven waren grotendeels handmatig en niet gecontroleerd op exact gelijke positie, afstand, beweging of duur. Succespercentages uit verschillende reeksen zijn daarom geen vergelijkbare benchmark.

## 1. UID lezen is niet hetzelfde als de volledige kaart lezen

In de praktijk lukte UID-selectie vaak terwijl het daarna uitlezen van NDEF-data mislukte. Dezelfde kaart op een goede vaste positie werkte beter dan bij randen van het bruikbare gebied of beweging. De logs bevatten veel `NFC_ERFTRANS (-20)` tijdens READ, op verschillende pagina's.

Dit ondersteunt het onderscheid tussen detectie en volledige dataoverdracht. Het bewijst niet dat geheugenlezen specifiek meer energie vereist, dat één bepaalde voedingsrail inzakte of dat een bepaalde antenneparameter verkeerd staat. Daarvoor ontbreken elektrische/RF-metingen.

De blijvende cache is de belangrijkste functionele verbetering: eenmaal een volledige bekende code lezen, vervolgens UID → code gebruiken. In een gerapporteerde proef werden twaalf aanbiedingen correct verwerkt: twee inleeracties en tien cachetreffers. Na reboot werden beide kaarten direct uit de cache herkend. Dit valideert die proefopstelling, niet iedere tag of alle posities.

## 2. Een RF-fout en een I²C-fout vragen verschillend gedrag

| Signaal | Betekenis voor deze implementatie |
|---|---|
| READ retourneert `-20` of `-10` | Leesronde afbreken, gedeeltelijke bytes weggooien en opnieuw selecteren/proberen |
| Timeout `-6` of chipfout `-90` tijdens READ | Begrensd herhalen; na drie opeenvolgende van deze fouten stoppen |
| Selectiefouten | Maximaal vijftien opeenvolgende herhaalbare fouten toegestaan; de volgende stopt |
| `NFC_EIO (-1)` | Readerproces stopt; bridge kan begrensd heropenen |
| Ongeldige tekst/NDEF | Apart tellen; na drie opeenvolgende parserafwijzingen `INVALID` |

De foutcodes bepalen het herstelbeleid, maar zijn geen complete diagnose van de fysieke oorzaak. Met name een libnfc-timeout is niet automatisch een onschuldige kaartafwezigheid.

Een echte busfout werd gezien als `read only -1 bytes ... Input/output error`, gevolgd door een mislukte open met `Unexpected PN53x reply`. Na volledig spanningsloos maken werkte de reader opnieuw. De precieze aanleiding is nog niet vastgesteld.

## 3. Langzamer I²C bleek geen oplossing

100, 50 en 25 kHz zijn geprobeerd. Bij 25 kHz was de gerapporteerde reeks slechter. Bij 100 kHz volgde een reeks van elf volledige uitlezingen, waarvan zeven met herhaalpogingen. Daarmee is niet bewezen dat 100 kHz de RF-fout oplost; wel ontbreekt bewijs voor de eerdere hypothese dat steeds verder vertragen helpt.

Er werd ook anders gelogd en handmatig bewogen. Die variabelen zijn niet onafhankelijk gemeten. Behoud daarom de werkende 100 kHz als demonstratorinstelling en wijzig niet tegelijk frequentie, logging, voeding en readerprocedure.

Bij de gebruikte module leverde 5V-voeding een betere handmatige reeks op dan 3,3 V: 19 van 23 softwarematig afgebakende aanbiedingen volledig gelezen. Dit is een waarneming, geen universele voedingsinstructie of bewezen causaliteit. De gebruiker controleerde de I²C-signaalspanning; Pi-GPIO mag niet door een 5V-signaal worden aangestuurd.

## 4. Standaard READ behouden

NTAG213 READ `0x30` leest vier pagina's van vier bytes. De code leest vanaf pagina 4 in stappen van vier pagina's en stopt zodra het NDEF-record compleet is, maximaal binnen de 144 bytes gebruikersgeheugen.

De uiteindelijke variant gebruikt PN532 `InDataExchange`. Er is ook met `InCommunicateThru` getest, zonder duidelijke verbetering in de handmatige reeksen. Extra pauzes na selectie en tussen blokken leverden evenmin een overtuigende oplossing op en zijn verwijderd.

Een aangeleverde externe codevariant vermeldde een mislukte FAST_READ-proef. Die uitspraak is geen onafhankelijke validatie van alle mogelijke FAST_READ-routes. Er staat geen FAST_READ in de huidige implementatie. Minder commando's garandeert bij zwakke koppeling ook niet dat een langer antwoord correct aankomt.

Belangrijk: iedere nieuwe poging begint met selectie en een leeg buffer. Nooit fragmenten van meerdere pogingen samenvoegen; tussendoor kan een andere kaart verschijnen. CRC/pariteit blijven door de bestaande libnfc/PN532-route afgehandeld.

## 5. Cachebeleid en kaartformaat

Codegrammatica, bijvoorbeeld:

```text
oradio:v1:card:theme:jaren_zestig
```

Structurele woorden zijn Engels, de laatste referentie Nederlands. De parser ondersteunt categorieën `moment`, `theme`, `mood`, `artist` en `function`; dat maakt nog geen aparte opdrachtuitvoering voor die categorieën. De huidige demo koppelt bekende codes via `music_cards.json` aan muziek.

Ondersteund: één kort, niet-gechunked NDEF UTF-8 Text-record. URI-records, meerdere records, uitgebreide lengtes en andere tekstformaten worden niet algemeen ondersteund. `INVALID` betekent dus niet noodzakelijk een kapotte tag. `UNKNOWN` betekent een geldige kaartcode zonder mapping.

Alleen een volledig gelezen, syntactisch geldige code die in de configuratie voorkomt wordt ingeleerd. De cache bevat zeven-byte UID's en codes; actuele muziekbron en `name_nl` komen uit de configuratie. Een bekende UID wordt niet opnieuw inhoudelijk geverifieerd. Herschreven kaarten moeten expliciet vergeten worden. UID is geen authenticatiemiddel; de demo veronderstelt beheerde muziekkaarten.

Vanaf versie 10 staat de cache naast `music_cards.json`, standaard op USB. Voordelen: overdraagbaar tussen Pi's en gezamenlijk te back-uppen. Schrijven gebeurt via tijdelijk bestand, flush/fsync en vervanging. Dat verkleint het risico op een gedeeltelijk bestand; het is geen garantie tegen alle fouten van USB-media of stroomuitval.

Een ontbrekende USB-mount mag niet tot een nieuwe cachemap op de SD-kaart leiden. De code controleert bij laden/schrijven de standaardmount en bestaande doelmap; een verandering van opslagidentiteit wordt geweigerd. Dit is geen volledige hot-swapondersteuning: bij USB-wissel Oradio stoppen en opnieuw starten. `CACHE_WRITE_ERROR` betekent dat de koppeling mogelijk alleen in geheugen bestaat. Een al opgeslagen beschadigde cache stopt het opstarten en wordt bewaard voor onderzoek.

## 6. REMOVED is een waarneming met vertraging

Drie lege polls waren gevoelig voor korte ontvangstonderbrekingen. Nu moet minstens 1,2 seconde aaneengesloten via geslaagde selectiepogingen geen target worden gevonden. Een selectiefout telt niet als bewijs van verwijdering en onderbreekt deze afwezigheidsperiode.

De pollstart heeft een streefinterval van 0,2 seconde; een langzame libnfc-call kan dit overschrijden. Het is geen harde realtimegarantie. De daadwerkelijke `REMOVED` kan later komen, bijvoorbeeld na 1,4 seconde.

Dezelfde UID blijft per aanbieding eenmaal afgehandeld. Een andere UID kan direct een nieuwe aanbieding starten, zonder eerst `REMOVED` van de vorige kaart. Kort weghalen en terugleggen van dezelfde kaart wordt bewust genegeerd. Een langere blinde vlek kan nog steeds een onterechte nieuwe aanbieding veroorzaken.

## 7. De reader is een apart proces

`pn532_uid_monitor.py` gebruikt ctypes en een watchdog die bij een vastgelopen call het proces kan beëindigen. Dit hoort daarom niet rechtstreeks als hardwareloop in het hoofdproces van Oradio.

De bridge start `nfc_service.py` als childproces met dezelfde interpreter. Alleen `CARD` gaat via `CommandMessage("NFC", "CARD", event)` naar Oradio. `REMOVED`, `INVALID` en diagnostiek stoppen de muziek niet. Een advisory lock voorkomt gelijktijdige lezers uit deze implementatie, maar beschermt niet tegen andere software die dezelfde PN532 zonder die lock aanspreekt.

Bij normale readerstop wordt RF uitgeschakeld. De applicatie roept bewust geen `nfc_close`/`nfc_exit` aan; het OS ruimt procesresources op. In eerdere debugproeven was PowerDown zichtbaar in het sluitpad. Dat motiveerde de standalone lifecycle-keuze, maar bewijst niet dat PowerDown iedere latere storing veroorzaakt. Ook libnfc kan tijdens een mislukte open intern cleanup uitvoeren.

## 8. Herstel is begrensd en zichtbaar

De bridge probeert een onverwacht geëindigde lezer maximaal drie keer opnieuw te starten, na 2, 5 en 10 seconden. De processen draaien achtereenvolgens, niet gelijktijdig. Iedere poging en fout komt als waarschuwing in `oradio.log`. De NFC-bridge-log bevat tijdstempels.

`READY` na een herstart betekent dat openen en initialisatie gelukt zijn. Een volgende kaartdetectie geeft een aparte herstelbevestiging. De budgetteller wordt niet gereset door READY; zo blijft een herhaald crashende module begrensd. De budgetteller begint opnieuw bij een nieuwe Oradio/bridge-start.

De laatst doorgestuurde UID blijft in de bridge onthouden om onbedoeld opnieuw starten van muziek door een heropenende reader te vermijden. Een REMOVED of een andere UID maakt een volgende kaartactie mogelijk. Omdat tijdens uitval geen betrouwbare afwezigheidsinformatie bestaat, kan opnieuw verwijderen en aanbieden nodig zijn na herstel.

Deze herstelsoftware is met gesimuleerde childprocessen getest. Zij bewijst geen elektrisch herstel van een vastgelopen PN532. Er is geen bestuurbare reset- of voedingsschakelaar toegevoegd. De oorspronkelijke I²C-oorzaak blijft open. Automatisch de hele Pi rebooten is niet geïmplementeerd en is voor de demo niet de gekozen oplossing.

## 9. Muziekmappen waren geen opgeslagen playlists

De eerste kaartactie zocht alleen in MPD `listplaylists`. Daardoor werden `Pop60s` en `Pop70s` afgewezen: het zijn mappen. De aangepaste actie volgt het onderscheid van Oradio:

- Opgeslagen playlist: controleer inhoud en behoud de volgorde.
- Map: controleer aanwezigheid en MPD-songtelling, voeg de map inclusief submappen toe en shuffle.
- Ontbrekende/lege bron of ongereed MPD: bestaande wachtrij ongemoeid laten.

Een playlist heeft voorrang bij een identieke mapnaam. De methode heet nog `play_playlist` en het JSON-veld blijft `playlist` voor compatibiliteit. Het veld kan dus beide soorten bron aanduiden. MPD-commando's vormen geen rollbacktransactie: bij een transportfout midden in het vervangen van de wachtrij is behoud van de oude muziek niet gegarandeerd.

De kaartactie zet `prev_state` en `StatePlay`, schakelt presetleds uit en geeft de Play-aanduiding. Zij wordt geserialiseerd met de bestaande state-workers; oudere workers mogen de kaartactie niet achteraf overschrijven. Iedere geslaagde nieuwe kaartactie geeft het bestaande `SOUND_PLAY`-geluid, ook bij Play → Play. In de ontwikkelopstelling heet dat hoorbare geluid AAN.wav; de koppeling gebruikt de bestaande geluidsnaam, geen nieuw hardgecodeerd audiopad.

## 10. Wat is bevestigd en wat staat open?

Bevestigd door gebruikersproeven: twee kaarten inleren, cacheherkenning na reboot, schakelen tussen Pop60s/Pop70s, muziekstart en Play-leds in de logs, en Play-geluidsbevestiging na de aanpassingen.

De latere USB-cachewijziging en herstelbridge vragen nog expliciete acceptatie op de uiteindelijke installatie. Een READY-regel met USB-cachepad, herkenning na reboot, inleren van een derde kaart en duurtest moeten bij de oplevering worden vastgelegd.

Softwaretests dekken parsing, retries, cacheherladen, schrijffouten, storage-controles, verwijderingsfilter, procesherstel, playlist/mapkeuze, geluid en state-races. Zij meten geen RF-bereik, voedingskwaliteit, I²C-signalen of langdurige stabiliteit. NFC-logrotatie en een definitieve I²C-oorzaakanalyse blijven werkpunten voor productie.

## Bronnen en reproduceerbaarheid

Primaire documenten die tijdens de ontwikkeling zijn geraadpleegd:

- [NXP NTAG213/215/216-datasheet](https://www.nxp.com/docs/en/data-sheet/NTAG213_215_216.pdf): kaartgeheugen, READ en FAST_READ.
- [NXP PN532 User Manual](https://www.nxp.com/docs/en/user-guide/141520.pdf): PN532-hostcommando's, statuscodes en communicatiemodi.
- [NXP PN532 application note](https://www.nxp.com/docs/en/nxp/application-notes/AN133910.pdf): hostinterfaces en ready-handshake.
- [libnfc 1.8.0 pn53x.c](https://github.com/nfc-tools/libnfc/blob/libnfc-1.8.0/libnfc/chips/pn53x.c) en [pn532_i2c.c](https://github.com/nfc-tools/libnfc/blob/libnfc-1.8.0/libnfc/drivers/pn532_i2c.c): daadwerkelijke commandoroute en drivergedrag.
- [Adafruit PN532-driver](https://github.com/adafruit/Adafruit-PN532/blob/master/Adafruit_PN532.cpp): vergelijking met gebruikelijke NTAG READ-opdrachten; deze link volgt een veranderlijke branch en is geen gepinde dependency.

De handleiding beschrijft de implementatie, niet alle mogelijkheden van deze bronnen. Bewaar bij een nieuwe installatie de Git-commit, de configuratie, de hardwaregegevens en logs met tijdstempels. Verander bij onderzoek één variabele tegelijk en noteer fysieke kaartaanbiedingen apart van softwarematige `presentations`.
