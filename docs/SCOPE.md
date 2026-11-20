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
| Knows what the installed tools can do | Software capability registry | §2.4 | 2 (started) |
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
Still to do: snapshots before risky actions, the installer ISO, richer registry entries (D-Bus, `--help`).

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
- KimiOS Edge firmware for one microcontroller family.
- Opt-in registry for drivers and skills.

### Phase 5 — Other architectures and form factors

- ARM64 images (Raspberry Pi and similar boards); RISC-V when Fedora bootc supports it.
- Headless profile for appliances and robots (no desktop; voice and network control).

---

## 4. Installing on the reference laptop (Phase 0)

The custom ISO comes in Phase 2. Until then, install KimiOS in two steps:

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
  usr/libexec/kimios/         kimios-session, kimi-hwprobe, kimios-mcp (the tool server), kimios_control.py
  usr/share/kimilm/plugins/   the KimiOS plugin KimiLM loads (points at kimios-mcp)
  usr/share/polkit-1/rules.d/ administrators mount internal drives without a password
  usr/share/wayland-sessions/ kimios.desktop
  usr/lib/systemd/            kimilm.slice (user), kimi-hwprobe.service (system)
  usr/lib/udev/rules.d/       re-probe on hot-plug
tests/                        unit tests (no root needed)
.github/workflows/build.yml   tests, image build, push to GHCR
docs/SCOPE.md                 this document
```

Build locally: `podman build -t kimios .`
Tests: `python3 -m unittest discover -s tests -v`

---

## 6. Open decisions

1. **Licence** (KimiOS repository): Apache-2.0 is suggested (matches bootc tooling; patent grant).
2. **Image signing:** cosign keys in GitHub secrets, so `bootc` can verify updates (Phase 1).
3. **Registry for drivers and skills:** where it lives and how submissions are reviewed (Phase 4).
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
