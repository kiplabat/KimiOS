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

See what KimiOS found on the machine:

```bash
cat /var/lib/kimios/hardware.json
```

## What agents can do on KimiOS

KimiLM connects to `kimios-mcp`, KimiOS's tool server, by itself (Plugins & connectors → KimiOS). Agents can
then look up the hardware (and what any device without a driver would need), what the machine has shown it can
do (its models' measured speeds and scores), the installed apps and what they open, memory, battery, temperatures, network and
disks, drives, logs and the installed KimiOS versions; and, with your approval where KimiLM asks for it, open
files and apps, change allowed desktop settings (each change reports the old value), mount drives, show
notifications and install or remove Flathub apps.

## Develop

```bash
python3 -m unittest discover -s tests -v   # tests, no root needed
podman build -t kimios .                    # build the image
```

Every push to `main` builds the image and pushes it to `ghcr.io/kiplabat/kimios`.
