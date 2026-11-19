# KimiOS — scope v0.1

KimiOS is an LLM-native operating system. It grows out of [KimiLM](https://github.com/kiplabat/KimiLM):
KimiLM is the agent app that runs on Windows, macOS and Linux, and KimiOS is the system it becomes
when it owns the whole machine.

The long-term aim: install KimiOS on any device (a laptop, a robot, a fridge), and it finds out what
hardware it has, learns to drive it, and improves the longer it runs there.

This document sets out what we build first, how the pieces fit, and the rules that keep it safe.

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

---

## 2. Layers

| Layer | What it is | Who can change it |
|---|---|---|
| **Core** | Linux kernel, Fedora bootc image (Silverblue base, GNOME as the fallback desktop), firmware, signed updates | Image builds only (CI) |
| **System services** | KimiLM engine, hardware probe, model runtime, memory limits (`kimilm.slice`), snapshots | Image builds; settings by the user |
| **Shell** | The KimiOS session: KimiLM full screen in a kiosk compositor (`cage`); GNOME as a second session | User |
| **OS MCP server** | Tools that let agents act on the system: files, apps (Flatpak), settings, network, devices, packages (in containers), logs | Agents, within permission tiers |
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

### Phase 2 — OS MCP server and installer

- OS MCP server with permission tiers: read (graph, logs, settings) → user actions (files, Flatpak
  apps, settings) → system actions (need approval).
- Snapshot before risky actions; one-step undo.
- Installer ISO (interactive, never "install to the first disk").

### Phase 3 — Driver synthesis

- The pipeline in §2.2 for USB, serial, I2C and GPIO devices.
- Test bench: a USB sensor, a serial device and a GPIO board (for example a Raspberry Pi as a second node).

### Phase 4 — Skill learning and Edge

- The loop in §2.3, first in simulation, then on one real actuator.
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
  usr/libexec/kimios/         kimios-session, kimi-hwprobe
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
