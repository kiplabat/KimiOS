# KimiOS

An LLM-native operating system. KimiOS is what [KimiLM](https://github.com/kiplabat/KimiLM) becomes
when it owns the whole machine: it boots into KimiLM, keeps models from taking the system down,
describes the hardware it finds, and, in later phases, writes drivers and learns to control devices
it has never seen.

It is built on [Fedora bootc](https://docs.fedoraproject.org/en-US/bootc/): the system is a container
image that is replaced as a whole and can be rolled back with one reboot.

**Status:** Phase 1 done; Phase 2 started (the tool server, a first software registry). See
[docs/SCOPE.md](docs/SCOPE.md) for the full plan.

## Try it

On a machine running Fedora Silverblue 45:

```bash
sudo bootc switch ghcr.io/kiplabat/kimios:latest
sudo systemctl reboot
```

Choose the **KimiOS** session on the login screen. To go back: `sudo bootc rollback`.

KimiLM's **Performance** window shows the computer's use live (processor, graphics, memory, network, disks,
temperatures, power). On KimiOS the `kimi-telemetry` service adds what only the system can read and writes it to
`/run/kimios/telemetry.json` once a second: how busy an Intel GPU really is (its i915 counters, not just its clock),
processor and graphics power from RAPL, fans, slowing down for heat, and KimiLM's memory slice. To look yourself:

```bash
/usr/libexec/kimios/kimi-telemetry --once        # as root for power and GPU busy time
systemctl status kimi-telemetry
```

See what KimiOS found on the machine:

```bash
cat /var/lib/kimios/hardware.json
```

## What agents can do on KimiOS

KimiLM connects to `kimios-mcp`, KimiOS's tool server, by itself (Plugins & connectors → KimiOS). Agents can
then look up the hardware (and what any device without a driver would need), what the machine has shown it can
do (its models' measured speeds and scores), how busy it is right now (processor, graphics, watts, heat), the installed apps and what they open, memory, battery, temperatures, network and
disks, drives, logs and the installed KimiOS versions; and, with your approval where KimiLM asks for it, open
files and apps, change allowed desktop settings (each change reports the old value), mount drives, show
notifications and install or remove Flathub apps. For devices that heat, cool or move, agents can learn how a
device responds from recorded data, tune a controller and try it in simulation first (`control_*` tools).

Devices you plug in (0.8.0, with KimiLM 2.29.0): phones (adb), Raspberry Pi and other boards, sensors, displays,
keyboard-and-mouse dongles, sound and Bluetooth devices are announced when they arrive and shown in KimiLM's
Devices with their drivers and what they can do; agents use them through `devices` and `device_control`. KimiOS
gives the signed-in user access to them (`70-kimios-devices.rules`) and ships adb, libimobiledevice and ddcutil.
Programs an agent needs and that are not installed go into KimiLM's own toolbox (`kimilm-tools`), with no password;
the toolbox is made in the background after the first login (`kimios-toolbox.service`).

Making devices do what you ask (0.9.0): `kimi-simbench` runs simulated devices (a two-compartment fridge, a heated
room, an air sensor) on serial ports, and `kimi-envelope` is the guard between them and agents. An agent reads a
device's datasheet, writes its driver as a manifest and tests it, records step experiments, fits a model, checks
a controller (for example "top at 4 °C, bottom at −18 °C") and starts it; the guard applies the datasheet's limits
on every tick whatever is asked, and falls back to the device's own controller, or to safe values, when
something fails. Tools: `bench`, `driver`, `bench_experiment`, `controller`, `bench_event`. Watch it in KimiLM's
Devices. Remove the simulations with `sudo systemctl disable --now kimi-simbench`.

Real devices (0.10.0): `kimios-device template` prints a datasheet to fill in; `sudo kimios-device register FILE`
registers the device (its port then belongs to the envelope alone, at `/dev/kimios/<id>`); `kimios-device list`;
`sudo kimios-device unregister ID`. Agents then work with it exactly as with a simulated one, and KimiLM asks you
before anything that moves it.

What is installed and what it can do (0.12.0): `kimios-registry search convert pdf` lists the apps, commands,
services, D-Bus services and plugins that match, each with how to use it; `kimios-registry describe commands tar`
shows a program's options from its manual. Agents ask the same through the `software` tool.

Snapshots and undo (0.11.0): before an unattended task changes files, or before software is removed, KimiLM takes a
snapshot of your home folder (instant on btrfs, through `kimi-snapshot`; elsewhere the folders the action names are
copied). `kimios-snapshot changes latest ~/Documents` lists what was added, changed and removed since;
`kimios-snapshot restore ID ~/Documents/report.md` puts it back (the current version is kept beside it). Agents use
the `snapshot` tool. If the simulated devices are missing after updating from 0.9.0, run
`sudo systemctl restart kimios-envelope-setup kimi-simbench kimi-envelope` (0.11.0 does this at boot).

## Develop

```bash
python3 -m unittest discover -s tests -v   # tests, no root needed
podman build -t kimios .                    # build the image
```

Every push to `main` builds the image and pushes it to `ghcr.io/kiplabat/kimios`.
