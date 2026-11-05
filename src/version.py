"""One place for the desktop build version and where its updates live.

Bump ``APP_VERSION``, push the matching ``vX.Y.Z`` tag, and the GitHub Actions
workflow builds the EXE and publishes it as a release. Every installed copy
picks that release up through :mod:`updater` on its next launch.
"""
APP_VERSION = "1.1.7"
GITHUB_REPO = "siddharthdeshmukh25/Stealth-Co-Pilot"
