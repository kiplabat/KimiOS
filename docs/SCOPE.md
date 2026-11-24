# KimiOS — scope v0.2

KimiOS is an LLM-native operating system. It grows out of [KimiLM](https://github.com/kiplabat/KimiLM):
KimiLM is the agent app that runs on Windows, macOS and Linux, and KimiOS is the system it becomes
when it owns the whole machine.

The long-term aim: install KimiOS on any device (a laptop, a robot, a fridge), and it finds out what
hardware it has, learns to drive it, and improves the longer it runs there.

This document sets out what we build first, how the pieces fit, and the rules that keep it safe.

---

## 0. Vision

In the founder's words (20 Nov 2026, lightly edited):

- KimiOS is the de facto engine for people who work in natural language.
- Whatever a user does, the model beneath suggests better ways of working and takes effort away.
- Everything a user touches is served through natural language.
- KimiOS already knows what the installed tools can do, and engages the user so they get the most out of
  them for what they want to achieve.
- It understands the devices connected to the machine.
- It quickly understands any system it is deployed on: as a robot's operating system, it learns what it
  has been given to control, and does so.
- It understands its environment, its constraints and its abilities, and pushes those boundaries just a bit.

### 0.1 What the vision means in the design

| Vision | Built as | Section | Phase |
|---|---|---|---|
| Natural language is the way in | The KimiOS session: KimiLM is the desktop; every other piece is reached through it | §2 Shell | 1 (done) |
| Everything served through natural language | OS MCP server: files, apps, settings, network, devices as tools | §2 | 2 (started) |
| Knows what the installed tools can do | Software capability registry | §2.4 | 2 (apps, commands, services, D-Bus, plugins) |
| Understands connected devices | Hardware capability graph; driver synthesis for what has no driver | §2.1, §2.2 | 0 (done), 3 |
| Suggests ways of working | Suggestions from what the user does, local and opt-in | §2.6 | 2b (started) |
| Learns what it controls (robot, appliance) | Skill learning; KimiOS Edge for microcontrollers | §2.3 | 4 (started, in simulation) |
| Knows its constraints and abilities, pushes them a bit | Self-model and bounded exploration | §2.5 | 2b (started) |

---

## 1. Principles

1. **The core cannot be broken by an agent.** The operating system image is read-only and is replaced
   as a whole (bootc). Every update can be rolled back with one reboot. Agents change the
   machine only through layers above the core.
2. **A runaway model is killed, not the machine.** Every model process runs under kernel memory
   limits (cgroups). The 19 Nov 2026 crash, where a model test and downloads together took Windows down
   and corrupted its registry, must be impossible by construction.
3. **Discover, then act.** KimiOS first describes the hardware it finds (the capability graph), then
   binds existing drivers, and only then writes new ones.
4. **New drivers live in user space.** Agents write and test drivers as ordinary sandboxed programs
   (USB, serial, I2C, GPIO protocols). No agent-written kernel code.
5. **Physical actions have a safety envelope.** Anything that moves, heats, cools or switches power
   has hard limits, a watchdog and a human approval step that agents cannot change. Software mistakes
   can be rolled back; physical ones cannot.
6. **Learning is shared on purpose.** Drivers and skills that work can be published to a registry so
   the next install starts smarter. Sharing is opt-in.
7. **Any hardware.** Nothing assumes an NVIDIA card, a fast CPU or a network. The reference machine
   is slow on purpose (§7).
8. **Watching is the user's choice.** What KimiOS learns from the user's activity stays on the machine,
   is off until the user turns it on, can be seen in full and deleted. A suggestion never acts by itself.
9. **Push boundaries a bit, never far.** KimiOS widens what it can do one small step at a time: each
   step is measured, reversible and inside the limits above. A step is kept only if it is measurably
   better; otherwise it is undone.

---

## 2. Layers

| Layer | What it is | Who can change it |
|---|---|---|
| **Core** | Linux kernel, Fedora bootc image (Silverblue base, GNOME as the fallback desktop), firmware, signed updates | Image builds only (CI) |
| **System services** | KimiLM engine, hardware probe, model runtime, memory limits (`kimilm.slice`), snapshots | Image builds; settings by the user |
| **Shell** | The KimiOS session: KimiLM full screen in a kiosk compositor (`cage`); GNOME as a second session | User |
| **OS MCP server** | Tools that let agents act on the system: files, apps (Flatpak), settings, network, devices, packages (in containers), logs | Agents, within permission tiers |
| **Capability registry** | What the installed software can do (§2.4), next to the hardware graph (§2.1) | Updated by the system when software changes |
| **Self-model** | What this machine can do and where its limits are, measured (§2.5) | Measured by the system; limits set by the user |
| **Suggestions** | Ways of working proposed from what the user does (§2.6) | User opts in; every suggestion needs a yes |
| **Driver and skill space** | User-space drivers and learned control skills, each in its own sandbox with a manifest and tests | Agents propose; promotion needs tests to pass (and approval for physical devices) |
| **Edge** | Small firmware (KimiOS Edge) for microcontrollers that cannot run a model; the node flashes and drives them | Node, with approval |

### 2.1 Capability graph

The hardware probe (`kimi-hwprobe`) reads `/sys` at boot and when devices are plugged in, and writes
`/var/lib/kimios/hardware.json`:

- machine (vendor, model, architecture, CPU, memory);
- every device on PCI, USB, platform and other buses, with its bound driver or **none**;
- device classes: displays and GPUs, audio, network, cameras, input, storage, sensors (IIO, hwmon,
  thermal), power, serial ports, GPIO, I2C, PWM, backlight, Bluetooth;
- capability tags (`gpu`, `camera`, `audio`, `network`, `storage`, `sensor`, `serial`, `gpio`, `i2c`,
  `pwm`, `power`, …);
- **unclaimed devices**: devices with no driver. This list is the queue for driver synthesis.

Agents read the graph through the OS MCP server. It is the single answer to "what is this machine and
what can it do".

### 2.2 Driver synthesis (Phase 3)

For an unclaimed device:

1. Identify it (IDs, descriptors, class, public datasheets, known Linux drivers that are not loaded).
2. If a kernel driver exists in the image, bind it. Most "missing drivers" end here.
3. Otherwise an agent writes a user-space driver in a sandbox (no network, only that device node),
   with a manifest (device match, permissions, risk class) and tests.
4. The driver is promoted only when its tests pass on the real device. For physical-actuation risk
   classes, a person approves it.
5. Promoted drivers are versioned, can be rolled back, and can be shared (§1.6).

### 2.3 Skill learning (Phase 4)

For devices that move or regulate something (motors, compressors, heaters, valves):

1. **System identification:** small, bounded test inputs; measure the response; fit a model.
2. **Practice in simulation** against that model.
3. **Learn a control policy** within the safety envelope (limits, rate limits, watchdog).
4. **Run it for real**, monitored, with automatic fallback to a safe default controller.

Example: on a refrigerator, learn compressor cycling that keeps the temperature band while using less
energy.

### 2.4 Software capability registry (Phase 2)

The hardware graph answers "what is attached"; the registry answers "what can the installed software do".

- Sources: desktop entries and Flatpak manifests (apps, file types, permissions), command-line tools
  (their `--help`, man pages), MCP servers and KimiLM plugins (their tool lists), and system services.
- Each entry: what it does in plain words, the actions it offers, the files it reads and writes, and how
  an agent reaches it (MCP tool, command, D-Bus, or the app's window as a last resort).
- Refreshed when software is installed or removed. Agents ask the registry first, so a request in
  natural language goes to the right tool instead of a web search.

### 2.5 Self-model and bounded exploration (Phase 2b)

KimiOS keeps a measured description of itself: speed per model and setting (KimiLM's tuner already
measures this), memory and thermal headroom, power state, network, disk, and which tasks it has done well
or badly (KimiLM's evaluation and agent records).

Pushing boundaries a bit (principle 9): from time to time, when the machine is idle and on power, it tries
one change at the edge of what it knows, such as a larger context, a different model placement, a newly
found tool or a learned skill on a device, and measures the result. Better results are kept; worse ones are
undone. The user sees what was tried and can turn it off.

### 2.6 Suggestions (Phase 2b)

With the user's consent (principle 8), KimiOS notices repeated work (the same files renamed by hand every
week, the same report rebuilt from the same sources) and offers a better way: a scheduled task, a skill, an
app that already does it. Suggestions are short, can be dismissed for good, and never act without a yes.

---

## 3. Phases

### Phase 0 — Repository and image pipeline *(this delivery)*

- Containerfile on `quay.io/fedora/fedora-silverblue:45`, built by GitHub Actions and pushed to
  `ghcr.io/kiplabat/kimios`.
- KimiOS session (cage + KimiLM), falling back to GNOME when KimiLM is not installed.
- `kimilm.slice` memory limits.
- `kimi-hwprobe` first version, with tests.

**Done when:** CI builds and pushes the image; `sudo bootc switch ghcr.io/kiplabat/kimios:latest` on a
Silverblue machine boots into it; `/var/lib/kimios/hardware.json` describes that machine.

### Phase 1 — KimiLM on Linux *(KimiLM repository)*

- Linux build target for KimiLM (electron-builder `tar.gz` or AppImage; engine and llama.cpp Linux
  runtimes already exist).
- Release asset that the KimiOS image installs (`KIMILM_URL` build argument).
- Engine runs model processes inside the slice, so the memory limits apply to them.

**Done when:** KimiOS boots straight into KimiLM and a local model chat works on the reference laptop.

**Done (20 Nov 2026)** on the reference laptop: KimiOS 0.2.1 with KimiLM 2.22.0. Qwen3.5 4B passed the
evaluation 12 of 12 in 24 min (Windows with KimiLM 2.20.0: 11 of 12 in 37 min); Qwen3.5 9B 12 of 12 in 52 min.
`kimilm.slice` confirmed holding the engine and both model servers (limits 21.7 GB / 26.4 GB).
Found on the way: Fedora 45 ships OpenSSL 4, so KimiOS adds `openssl3-libs` (0.2.1).

### Phase 2 — OS MCP server and installer

- OS MCP server with permission tiers: read (graph, logs, settings) → user actions (files, Flatpak
  apps, settings) → system actions (need approval).
- Snapshot before risky actions; one-step undo.
- Installer ISO (interactive, never "install to the first disk").
- Software capability registry (§2.4), read through the OS MCP server.

**Started (20 Nov 2026), KimiOS 0.3.0:** `kimios-mcp`, an MCP server over stdio with no dependencies, offered to
KimiLM as a system plugin (`/usr/share/kimilm/plugins/kimios`, read by KimiLM 2.23.0). Tools:
look (`machine`, `device_inspect`, `abilities`, `system_status`, `os_status`, `software`, `drives`,
`setting_get`, `logs`), act (`open`,
`launch_app`, `notify`, `setting_set` on an allow-list, returning the old value; `mount_drive`; `app_install`),
remove (`app_remove`). MCP annotations mark read-only and destructive tools; KimiLM's approval mode decides what
asks first. The registry reads desktop entries (system, user, Flatpak) and, on request, matching commands.
A polkit rule lets administrators mount internal drives without a password (KimiLM mounts the drive a model is on).

**0.12.0: the installer ISO.** `.github/workflows/iso.yml` builds an x86_64 installer with image-builder's
`generic-iso` (bootc-image-builder's `anaconda-iso` is legacy upstream, installs to the first disk it finds, and has
no Fedora 45 repositories). The ISO boots into `iso/Containerfile.installer`: Anaconda's graphical installer on
Fedora bootc, with KimiOS embedded as the payload, so installing needs no network. Anaconda is told only what to
install (`interactive-defaults.ks`: `bootc --source-imgref containers-storage:… --target-imgref
ghcr.io/kiplabat/kimios:latest`, so updates come from the registry); it asks for the disk, how to divide it and the
user. The build checks that before making the ISO. Built on request and on pull requests that change it; the ISO
is a workflow artifact. Not yet: an ARM64 ISO, signed images (open decision 2); the installer still shows Fedora's
branding.

**0.12.1:** the first ISO stopped at start on a Dell ("Failed to allocate manager object", SELinux unable to label
`/dev`): the installer's files carry no SELinux labels, and the boot entry lacked the `enforcing=0` that
image-builder's own entries have. Both entries now have it (the installed system stays enforcing), and a second
entry, *Install KimiOS (basic graphics)*, adds `nomodeset` for older graphics. `kimios-registry` in a terminal now
lists apps as well.

**0.12.2:** Anaconda's bootc source always asks for a network, even when the image is on the stick, and the
installer had no Wi-Fi (Fedora's bootc base leaves out the NetworkManager plugin and the firmware), so a laptop
without an Ethernet cable could not finish. The installer now has Wi-Fi for common laptop cards. Without any
network: an Android phone's USB tethering shows up as an Ethernet connection.

**0.12.0: the registry knows more than apps (§2.4).** `kimios_registry.py` indexes, besides desktop entries:
every program on PATH with what it is for (the manual index, `man -k`, and, where that index is not built yet on an
image-based system, the summary of the package that installed it); system and user services with description
and state; D-Bus services on both buses, running or started on demand, with the program behind them; KimiLM
plugins and their MCP servers. Every entry says how an agent reaches it. `software` with a `kind` and `id` goes deeper for
one entry: a command's package, synopsis, description and options (its manual page; `--help` only when there is no
manual, never for programs that could act instead, such as `reboot` or `dd`), a service's command line and
documentation, a D-Bus service's interfaces and methods (with `--auto-start=no`: describing never starts a
service), an app's Flatpak permissions. The index is cached per person with a fingerprint of the folders
software lands in and rebuilt when one changes; `kimios-registry.path` rebuilds it as soon as a Flatpak app, a
program in `~/.local/bin`, a user service or a plugin appears or goes. `kimios-registry search|describe|refresh`
does the same from a terminal. Agents are told to ask `software` before searching the web or installing.

**0.11.0: snapshots before risky actions, one-step undo.** The system is an image (`bootc rollback` undoes an
update); what an agent can damage is the person's own files. `kimi-snapshot`, a small root service, takes a
read-only btrfs snapshot of `/var/home` for the person who asks (their uid, from the socket), instantly, into
`/var/home/.kimios-snapshots` (mode 0711: each person reads their own folder in it, nobody else's); at most one
every two minutes per person, twelve kept, a week old deleted (the newest three stay). Without btrfs the person's
own `kimios-snapshot take PATH…` copies the folders an action names (reflinks where possible, up to 2 GB) into
`~/.local/share/kimios/snapshots`. Undo needs no privilege and is done by the person: `kimios-snapshot changes`
(added, changed, removed since a snapshot under a folder) and `restore` (a file or folder put back; the current
version kept beside it as `NAME.before-undo-TIME`). Agents: the `snapshot` tool (take, list, changes, restore).
KimiLM 2.32.0 takes one before the first change of an unattended or auto-approved reply and before tools that
delete or overwrite, and shows Activity → Undo. `btrfs-progs` is in the image.

**0.11.0 also fixes simulated devices missing after updating from 0.9.0.** 0.10.0 moved `kimi-simbench` and
`kimi-envelope` from `DynamicUser=` to the fixed user `kimi-envelope`; on an updated system the user could be
missing (sysusers runs at boot only when `/etc` needs an update) and the drivers people had promoted sat in
`/var/lib/private/kimios-envelope`. `kimios-envelope-setup.service` (root, before both) makes the user and moves the
store back, keeping every driver version. The envelope serves its socket before it looks for devices, reports what
is wrong (`problems` in the devices op: a store it cannot write, then kept under /run; a port it cannot open; an
empty bench) instead of exiting, and one bad device no longer hides the others.

### Phase 2b — Self-model and suggestions

- Self-model and bounded exploration (§2.5), starting from KimiLM's tuner and evaluation.
- Suggestions from the user's activity, opt-in (§2.6).

**Started (0.3.0):** the `abilities` tool is the first self-model. It reads KimiLM's own measurements (model
speeds, evaluation scores) read-only, adds memory headroom and power, and names the best local model for agent
work (on the reference laptop: Qwen3.5 4B, 12 of 12 in 24 min).

**KimiLM 2.24.0** adds the first bounded exploration and the first suggestions, inside KimiLM so they also work
outside KimiOS:
- *Exploring while idle* (on by default, Settings → "Finding better ways, on its own"): when nothing runs, the
  machine is plugged in and KimiLM has been idle for 30 minutes, it measures the fastest setup of one local model
  not measured in 30 days, at most once a day. Anything the user starts stops it. The result is offered on the
  model's card; nothing is applied on its own (principles 8 and 9).
- *Suggestions* (off until switched on): a request started as a new chat on three or more different days is
  offered as a scheduled task at the usual time. Only KimiLM's own chats are read; nothing leaves the computer.

### Phase 3 — Driver synthesis

- The pipeline in §2.2 for USB, serial, I2C and GPIO devices.
- **Started (0.3.0):** steps 1 and 2 of §2.2 as the `device_inspect` tool: names from hwdata, interfaces,
  kernel modules whose aliases match the device and whether they are loaded, and the next step. On the reference
  laptop's unclaimed list it tells the Synaptics fingerprint reader (user-space, fprintd) from a chipset memory
  controller that needs nothing.
- Test bench: a USB sensor, a serial device and a GPIO board (for example a Raspberry Pi as a second node).
- **Started (0.8.0, with KimiLM 2.29.0): attached devices understood and controlled.** Plug in a phone, a Raspberry
  Pi, a sensor, a display, a keyboard-and-mouse dongle or connect a Bluetooth headset: KimiLM sees it within a few
  seconds, says what it is and what it can do (a notification, and Devices), and agents reach it through `devices`
  and `device_control`. What a device is comes from what it says about itself (USB classes and interfaces, vendor
  ids, EDID, BlueZ, the sound server); for each one KimiLM shows the kernel driver, the kernel modules that match,
  the programs that control it and whether they are installed, and whether the signed-in user may open it.
  Actions: a phone through adb (files, screenshots, apps, commands), a display's brightness (logind for the
  built-in screen, DDC/CI for external ones), sound volume and output, Bluetooth connect and disconnect, a serial
  port read and written, a Pico flashed with a .uf2, sensor readings, a camera picture. KimiOS's part: udev rules
  that give the signed-in user (and nobody else) access to adb phones, serial ports, boards and DDC/CI
  (`70-kimios-devices.rules`, `uaccess`), and the programs in the image (android-tools, libimobiledevice, ddcutil).
  Writing to a serial port or flashing a board asks every time, even with auto-approve, unless the user marks that
  device as safe to control (principle 5).
- **Programs agents need are installed, not asked for (0.8.0).** The core is read-only, so a command-line program an
  agent needs goes into a toolbox container of KimiLM's own (`kimilm-tools`, `dnf` without a password), reached
  through small launchers on KimiLM's PATH; apps come from Flathub for the user; Python and Node packages go into
  KimiLM's own folder; the browser the browser tools drive is downloaded into the user's cache when it is first
  missing. `kimios-toolbox.service` (user) makes the toolbox in the background after the first login.

### Phase 4 — Skill learning and Edge

- The loop in §2.3, first in simulation, then on one real actuator.
- **Started (0.4.0), in simulation:** `kimios_control.py` behind three tools. `control_identify` fits a
  first-order-plus-dead-time model to recorded samples (gain, time constant, dead time, the output with the input
  off; R² and a trust word); `control_tune` gives SIMC PI gains and on/off advice; `control_simulate` runs on/off or
  PI (also as a duty cycle) against the model inside a safety envelope (input limits, minimum on and off times,
  output limits, disturbances) and reports time in band, error, switches per hour and duty (energy). On a simulated
  refrigerator (K −24 °C, τ 30 min, θ 60 s) identification is within 2–17 % and a ±0.5 °C thermostat holds a
  ±1 °C band 100 % of the time at 6.3 starts per hour, against 4 per hour and 76 % for ±1.0 °C: the wear and
  holding trade-off an agent can now reason about before touching a device. Next: a real sensor and actuator
  through a user-space driver (Phase 3), with the envelope enforced outside the agent.
- **Started (0.9.0, with KimiLM 2.30.0): the whole loop on simulated devices.** `kimi-simbench` runs devices that
  behave like boards on serial ports: a two-compartment fridge (compressor with its own short-cycle lockout, an
  air damper, two temperatures, a door switch), a room with an electric heater, an air sensor. Their physics run
  60 times faster than the clock, so a two-hour experiment takes two minutes. Each has a datasheet: sensors with
  units, valid ranges and allowed set points; outputs with hard limits (minimum on and off times, starts per hour,
  range, rate, safe value); trips; its own controller.
  `kimi-envelope` is the guard (principle 5), a system service under its own user: only it can open the devices'
  ports, and agents reach them through its socket. **Drivers are manifests, not code** (Phase 3, steps 3–5): the
  commands that read the sensors (a pattern with one named group per sensor) and the template that sets each
  output. An agent writes one from the datasheet and the device's HELP; the test reads every sensor and writes
  each output's current value (it moves nothing); a driver that passes is promoted as a new version, older ones
  can be rolled back. **Controllers are specs**: loops of sensor → output with a set point inside the datasheet's
  range, on/off with a band or PI (a duty cycle for on/off outputs), and `also` for outputs a loop needs (the top
  of a fridge only cools while the compressor runs). Every tick the guard applies the datasheet whatever is
  asked; a controller that fails or loses its sensors is replaced by the device's own (fallback), and with no
  usable readings every output goes to its safe value. Experiments (steps of outputs over time) are recorded for
  `control_identify`.
  The agents' path, as five tools in `kimios-mcp` 0.9.0: `bench` (datasheet, state, questions to the device),
  `driver` (test, promote, versions, rollback), `bench_experiment`, `controller` (check, start, stop),
  `bench_event` (door open, warm food, a dead sensor). Tools that move something carry `physicalHint`: KimiLM asks
  every time for a real device. On the simulated fridge, from a step experiment to a controller: top 4.0 °C and
  bottom −18.0 °C held (±0.4 and ±0.7 °C), three compressor starts an hour, never a lockout; an open door and warm
  food are absorbed; a dead freezer sensor sends it to fallback and then to safe.
  The guarantee is in the limits: no driver or controller can take a device past its datasheet. Approving a first
  run is the app's step (KimiLM), not a security boundary: anything the signed-in user can run, an agent of theirs
  can. Real devices join the same way, with a datasheet that the envelope reads from the system.
- **0.10.0: real devices join through a person.** `sudo kimios-device register FILE` checks the device's
  datasheet (the limits the guard will hold it to: sensor ranges and allowed set points, output timing, safe
  values, trips, its own controller), writes `/etc/kimios/devices/<id>.json`, and writes a udev rule that gives the
  device's port to the envelope alone (`GROUP=kimi-envelope`, `TAG-="uaccess"`: the signed-in session, and so any
  agent, loses direct access) at `/dev/kimios/<id>`. Agents cannot register a device or change its limits (sudo).
  From there it is the same path as a simulated device: a driver manifest tested and promoted, experiments,
  controllers inside the guard, fallback and safe values; raw commands are refused, and KimiLM asks a person
  before every experiment or controller start on it. A registered device that is unplugged is listed as not
  connected; a registration with a bad datasheet is listed with its problems and not run. The envelope and the
  bench now run as a fixed system user `kimi-envelope` (sysusers.d), so udev can give it a real port.
- KimiOS Edge firmware for one microcontroller family.
- Opt-in registry for drivers and skills.
- **Started (0.14.0): the registry, shared on purpose (principle 6).** Drivers and controllers that work go to
  `kiplabat/kimios-registry` as `drivers/<model>/driver-<id>.json` and `controllers/<model>/controller-<id>.json`.
  Each entry has:
  - the model;
  - its shape (the datasheet's sensor and output names);
  - the datasheet it was written for, without anything that names the machine (device id, port, serial number);
  - its evidence (a passed test; for a controller, measured results such as time in band and starts per hour).

  `<id>` is a hash of the driver or controller, so the same one shared twice is the same file. Agents use new
  actions on the existing tools (no new tool):
  - `driver` and `controller`: `find` lists the entries for a device's model and shape;
  - `get` checks an entry against the index's sha256, then tests a driver on the device (it moves nothing;
    promoting stays a separate step, only after it passes) or checks a controller against the machine's own
    datasheet (the guard holds it to that datasheet whatever the entry says);
  - `share` only works for what passes here now. It writes the entry under `~/.local/share/kimios/share` and gives
    a link to the registry's "new file" page with the entry filled in. The person reads it and presses *Propose
    changes*, which opens a pull request. Nothing is sent on its own, and no GitHub sign-in is kept on the machine
    (`kimios_share.py`).

  The registry starts with the simulated fridge's and room's drivers and the fridge's two-loop controller. That
  controller was measured over 6 h of device time on the simulator: top 3.98 °C mean (max 0.7 °C off 4), bottom
  −18.02 °C (max 1.2 off), 2.8 compressor starts an hour, 53 % duty.

### Phase 5 — Other architectures and form factors

- ARM64 images (Raspberry Pi and similar boards); RISC-V when Fedora bootc supports it.
- **Started (0.5.0, with KimiLM 2.25.0):** the image is built for x86_64 and ARM64, each on a runner of its own
  architecture, and published as one multi-architecture image under the same tags (`latest`, `45`,
  `45-<commit>`). KimiLM publishes `KimiLM-linux-arm64.tar.gz` next to the x64 build, with llama.cpp's ARM64 CPU
  and Vulkan builds; each image installs the KimiLM build of its own architecture. Not yet on ARM64: the image and
  music engines (no upstream builds), and booting on a Raspberry Pi (needs UEFI firmware on the board).
- Headless profile for appliances and robots (no desktop; voice and network control).

---

## 4. Installing on the reference laptop (Phase 0)

**With the KimiOS installer (0.12.0):** GitHub → KimiOS → Actions → *Build KimiOS installer ISO* → *Run
workflow*; when it finishes, download `kimios-installer-x86_64` (a zip holding the ISO and its SHA-256), write the
ISO to a USB stick (Fedora Media Writer, or `dd`), and boot from it. The installer asks everything: choose the disk
and *Installation Destination* → free space or the old Windows partition, **leave the data partition (Labatt)
alone**; Fedora's automatic partitioning uses btrfs (KimiOS's snapshots need `/var/home` on btrfs: check after the
first boot with `stat -f -c %T /var/home`); create your user. After the first boot `bootc upgrade` takes new images from GitHub.

**Or in two steps, from Fedora Silverblue:**

1. Install **Fedora Silverblue 45** from the official ISO. Its installer is interactive: choose the
   free space or the old Windows partition, and **leave the data partition (Labatt) alone**. Linux reads
   NTFS, so the repositories and models on it stay usable.
2. In a terminal: `sudo bootc switch ghcr.io/kiplabat/kimios:latest`, then reboot. To go back:
   `sudo bootc rollback`.

Keeping Windows alongside (dual boot) is recommended until KimiOS can do daily work.

---

## 5. Build and repository layout

```
Containerfile                 image definition
build_files/build.sh          package installs and set-up inside the build
system_files/                 files copied into / of the image
  usr/libexec/kimios/         kimios-session, kimi-hwprobe, kimios-mcp (the tool server), kimios_control.py,
                              kimi-simbench + kimios_bench.py (simulated devices), kimi-envelope +
                              kimios_envelope.py (drivers, controllers, the guard)
  usr/share/kimilm/plugins/   the KimiOS plugin KimiLM loads (points at kimios-mcp)
  usr/share/polkit-1/rules.d/ administrators mount internal drives without a password
  usr/share/wayland-sessions/ kimios.desktop
  usr/lib/systemd/            kimilm.slice and kimios-toolbox.service (user), kimi-hwprobe.service (system)
  usr/lib/udev/rules.d/       re-probe on hot-plug; access to plugged-in devices for the signed-in user
tests/                        unit tests (no root needed)
.github/workflows/build.yml   tests, image build, push to GHCR
docs/SCOPE.md                 this document
```

Build locally: `podman build -t kimios .`
Tests: `python3 -m unittest discover -s tests -v`

---

## 6. Open decisions

1. **Licence** (KimiOS repository): Apache-2.0 is suggested (matches bootc tooling; patent grant).
2. **Image signing:** decided and built (0.13.0): cosign, a key pair. The private key is the repository secret
   `SIGNING_SECRET` (its password `COSIGN_PASSWORD`); the publish job signs every published image (the image for
   both architectures and each one's own) and verifies it. The public key is `system_files/etc/pki/containers/kimios.pub`;
   with it in the image, `/etc/containers/policy.json` requires KimiOS images to be signed by it
   (`build_files/sign_policy.py`) and `registries.d/kimios.yaml` tells bootc and podman where signatures are. A system
   checks them on every update after `sudo bootc switch --enforce-container-sigpolicy ghcr.io/kiplabat/kimios:latest`
   once; the `os_status` tool says whether it does. Until the key is in the repository nothing is enforced.
   **0.13.1:** bootc refuses `--enforce-container-sigpolicy` while policy.json's top-level default is
   `insecureAcceptAnything` (Fedora's). The default is now `reject`, and each transport (docker, docker-daemon,
   containers-storage, the archives, dir, oci, sif, tarball) keeps accepting other images through its own `""`
   scope, so podman, toolbox and local builds behave as before; only KimiOS's own scope requires the signature.
3. **Registry for drivers and skills:** decided and built (0.14.0): the public repository
   `kiplabat/kimios-registry` (Apache-2.0), reviewed by pull request. Its check runs KimiOS's own datasheet,
   manifest and spec checks on every entry (and a simulated device's driver against KimiOS's simulator), and
   rebuilds `index.json` on main.
4. **Name of the edge firmware** and the first microcontroller family (ESP32 suggested).

---

## 7. Reference hardware

The operator's laptop (also the KimiLM reference):

| Item | Value |
|---|---|
| CPU | Intel Core i7-10810U, 6 cores / 12 threads |
| Memory | 31.8 GB |
| Graphics | Intel UHD (integrated; up to 15.9 GB shared). Open-source drivers in the kernel and Mesa; Vulkan via Mesa ANV |
| Disk | SATA SSD; data partition "Labatt" (NTFS) holds repositories and models |
| Known | The CPU is faster than the integrated GPU for llama.cpp on this machine |
