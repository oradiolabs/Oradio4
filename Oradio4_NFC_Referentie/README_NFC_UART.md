# Oradio4 – NFC UART referentie

Datum: 22 november 2026

## Status

Werkende integratie van Elechouse PN532 met Oradio3
als technische basis voor Oradio4.

Getest op Raspberry Pi 3A+, Debian 12.

## Hardware

- NFC-controller: Elechouse PN532 V3
- Interface: UART (HSU), 115200 baud
- Raspberry Pi GPIO14: UART TX
- Raspberry Pi GPIO15: UART RX
- Play-LED verplaatst naar GPIO8
- I2S-audio ongewijzigd
- Bluetooth-controller ingeschakeld

## Software

- libnfc 1.8.0
- PN532 via pn532_uart:/dev/serial0
- NFC-service met retrylogica
- UID-cache op USB-stick
- NFC-bridge geïntegreerd in Oradio
- Automatische start via oradio.service

## Testresultaten

- Vier NFC-kaarten succesvol gelezen via UART.
- NDEF-uitlezing zonder cache getest.
- RF-leesfouten bij bewegende kaarten;
  succesvolle herkenning vaak na een retry.
- Volledige Oradio inclusief NFC start na reboot.
- Vier kaarten na reboot correct herkend.
- MPD-muziekweergave actief.

## Bestanden

Main/    Operationele Oradio- en NFC-software
Tests/   I2C-, UART- en raw-NDEF-tests
Config/  Kaartconfiguratie en systeeminstellingen

## Opmerkingen

De operationele NFC-service gebruikt UART.
De UID-cache wordt niet meegeleverd.

De volgende tests zijn nog gewenst:
- Koude start
- Langdurige werking
- Gelijktijdige Bluetooth-audio en NFC
- Vergelijking met Grove PN532 en externe antenne
