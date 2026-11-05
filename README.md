# Stealth Co-Pilot — desktop app

Windows desktop companion for Stealth Co-Pilot: setup wizard, local audio capture,
live captions, pairing QR and interview history.

This repository is the **release home** of the app. GitHub Actions builds
`StealthInterviewCoPilot.exe` and attaches it to every `vX.Y.Z` release, and the
installed app updates itself from `releases/latest`.

## Download

Latest build (always the newest release):

```
https://github.com/siddharthdeshmukh25/Stealth-Co-Pilot/releases/latest/download/StealthInterviewCoPilot.exe
```

## How a release works

1. Double-click `release.bat`. It bumps the patch version in `src/version.py` and
   `assets/version_info.txt`, syncs this folder into the public repo, pushes
   `main` and pushes the matching `vX.Y.Z` tag. Add `--keep-version` to publish
   the version that is already set (for example after editing it to `1.2.0`).
2. The `Build Windows EXE` workflow builds the EXE and publishes the release.
3. Anyone running an older build sees **Update vX.Y.Z** in the top bar on the next
   launch; one click downloads the new EXE and restarts the app.

`src/updater.py` does the checking, downloading and the on-exit EXE swap, so no
manual reinstall is needed.

## Run from source

```bat
python -m pip install -r requirements.txt
python src\main.py
```

`COPILOT_WEB_URL` and `COPILOT_BACKEND_URL` point the app at the hosted web app
and backend; both have production defaults baked in.
