# Habab Home

Habab Home is a beta Home Assistant distribution with an AI setup chat and
Habab integrations. The Docker image targets Linux AMD64 and ARM64. OpenCode
and the Habab interface are included; no separate OpenCode installation is needed
on the host.

**Website: [onout.org](https://onout.org)**

[Source code and release files](https://github.com/marsiandeployer/habab-home-assistant)

## Quick start

Choose a published version on the **Tags** tab. Images are being built and tested
until the first version tag appears.

```sh
docker pull hababer/habab-home:<versioned-tag>
```

Use `habab/distribution/compose.release.yaml` from the matching source release.
Set `HABAB_HOME_IMAGE` to the selected image and tag, then run:

```sh
docker compose --env-file .env -f compose.release.yaml up -d
```

The Compose file stores Home Assistant settings in a persistent `/config` volume.
It binds port 8123 to localhost by default. To open the interface from another
computer at home, set `HABAB_HOME_BIND` to the server's LAN address.
Create your own owner account on first launch.

## AI setup

Choose and configure a provider in your installation's AI settings. Provider keys
and language models are not included in the image. Voice services are configured
separately.

The image contains Home Assistant Core. It does not include Home Assistant OS,
Supervisor or the add-on store. USB devices and additional services require
configuration for your hardware.

## Updates

Back up `/config` and record your current image tag before updating. Select the
new version in `.env`, pull it and recreate the container:

```sh
docker compose --env-file .env -f compose.release.yaml pull
docker compose --env-file .env -f compose.release.yaml up -d --force-recreate
```

To return to the previous application version, restore its tag and repeat these
commands. A tag change does not undo database migrations; keep the backup made
before the update.
