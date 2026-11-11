---
name: Bug report
about: Something in Oradio4 (main or an experiment branch) does not work as it should
title: ''
type: Bug
assignees: ''

---

**Version**
Branch and commit installed on the Oradio. On the Oradio, run:
`jq -r .gitinfo /var/log/oradio_sw_version.log`
e.g. `Branch 'main' @ 084f719`

**What happened**
A clear and concise description of what went wrong.

**Steps to reproduce**
What you did, in order: buttons, volume knob, web interface, USB stick, power on/off, ...

1.
2.
3.

**Expected behaviour**
What you expected to happen instead.

**Reproducible?**
- [ ] Every time
- [ ] Sometimes
- [ ] Happened once

**Logs**
Attach the relevant files from `~/Oradio4/logging/` (e.g. `oradio.log`, `traceback.log`, `install.log`).

**Additional context**
Hardware, network, screenshots of the web interface, anything else that helps.

> Found the bug on an Oradio3 in use? Report it in [Oradio3](https://github.com/oradiolabs/Oradio3/issues/new/choose) instead.
