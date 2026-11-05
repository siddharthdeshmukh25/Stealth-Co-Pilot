# Stealth Co-Pilot — desktop app

Windows desktop companion for Stealth Co-Pilot: setup wizard, local audio capture,
live captions, pairing QR and interview history.

This repository is the **release home** of the app. GitHub Actions builds
`StealthInterviewCoPilot.exe` and attaches it to every `vX.Y.Z` release, and the
installed app updates itself from `releases/latest`.

## Download

Latest build (always the newest release):

```
https://github.com/siddharthdeshmukh25/Stealth-Co-Pilot-desktop/releases/latest/download/StealthInterviewCoPilot.exe
```

## How a release works

1. Bump `APP_VERSION` in `src/version.py` (and `FileVersion` in `assets/version_info.txt`).
2. Push the change and a matching tag:
   ```bat
   git tag v1.1.0
   git push origin main --tags
   ```
3. The `Build Windows EXE` workflow builds the EXE and publishes the release.
4. Anyone running an older build sees **Update vX.Y.Z** in the top bar on the next
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
