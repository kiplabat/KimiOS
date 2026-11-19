# KimiOS

An LLM-native operating system. KimiOS is what [KimiLM](https://github.com/kiplabat/KimiLM) becomes
when it owns the whole machine: it boots into KimiLM, keeps models from taking the system down,
describes the hardware it finds, and, in later phases, writes drivers and learns to control devices
it has never seen.

It is built on [Fedora bootc](https://docs.fedoraproject.org/en-US/bootc/): the system is a container
image that is replaced as a whole and can be rolled back with one reboot.

**Status:** Phase 0 (image pipeline, session, memory limits, hardware probe). See
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

## Develop

```bash
python3 -m unittest discover -s tests -v   # tests, no root needed
podman build -t kimios .                    # build the image
```

Every push to `main` builds the image and pushes it to `ghcr.io/kiplabat/kimios`.
