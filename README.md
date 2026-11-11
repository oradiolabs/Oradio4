# Stichting Oradio - Oradio4 sources

> [!WARNING]
> **Status: RESEARCH.** This repository is in its study and experiment phase.
> The code is not ready for use, and anything in it can change or disappear.
> For the Oradio in use today, see [Oradio3](https://github.com/oradiolabs/Oradio3).

Oradio4 is the successor of [Oradio3](https://github.com/oradiolabs/Oradio3).
It started as a copy of Oradio3 `main` at commit `084f719`, without its history.
Oradio3 continues to exist and is maintained separately.

### Working in the research phase

- Each experiment lives in its own branch, named `exp/<topic>`.
- Pull requests to `main` are only checked on one thing: `main` still installs
  and the Oradio4 starts.
- Describe in each pull request what the experiment wanted to find out and what
  it showed, also when the result is negative.

## Installation

This repo contains an install script that loads and configures the software
required to prepare an SD card for an Oradio4.

> [!IMPORTANT]
> Install Oradio4 on a **freshly flashed SD card** only. Oradio4 uses the same
> service names and paths as Oradio3; the install script stops when it finds an
> Oradio3 installation on the card.

Create an SD card using the Trixie 64bit Lite image:

- Set hostname (e.g. oradio)
- Enable SSH with password authentication
- Configure SSH username and password (e.g. pi and oradio)
- Optionally you can provide wifi SSID and password for your network with internet access, and Wifi country: NL

Load the SD card in your Oradio4 (Raspberry Pi 3A+).

Connect the Oradio4 to your network with internet access using a USB hub with Ethernet dongle.<br>
<ins>Note</ins>: This is optional if you provided wifi SSID and password when creating the SD card.

Start the Oradio4 with the SD card inserted.

When the Oradio4 has configured itself after first boot SSH into the Oradio4 with your login and password.<br>
<ins>Note</ins>: If you provided a hostname when creating the SD card you can ssh to &lt;hostname&gt;.local

At the prompt, to install the _main branch_, execute command:

    bash <(curl -fsSL https://oradiolabs.nl/Oradio4/install) main

Or, at the prompt, to install _your branch_ (e.g. an experiment), execute command:

    bash <(curl -fsSL https://oradiolabs.nl/Oradio4/install) <branch name>

Or, at the prompt, to install the _latest release_, execute command:

    bash <(curl -fsSL https://oradiolabs.nl/Oradio4/install)

<ins>Note</ins>: during the research phase there are no releases yet. Without an
argument the script then installs the main branch.

The script will install and configure required packages and services.<br>
<ins>Note:</ins> The script may prompt you for the Oradio password.

Wait for the installation to finish.<br>
<ins>Note</ins>: this can take up to half an hour.

The Oradio4 reboots during the installation, so your SSH session is closed
partway through. This is expected: the installation continues by itself after
the reboot, without you logging in again. To follow it, SSH back in and run:

    journalctl -fu oradio-install-continue.service

or:

    tail -f ~/Oradio4/logging/install.log

When everything is installed the Oradio4 reboots once more to start.<br>
<ins>Note</ins>: the reboot halfway is skipped when the boot configuration is
already up to date, for example when you install again on the same SD card.

## Finish

The Oradio4 is ready for use when you hear the startup tune (harp).
