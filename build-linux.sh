#!/bin/sh
set -eu

cd "$(dirname "$0")"
if [ "$(uname -s)" != Linux ] || [ "$(uname -m)" != x86_64 ]; then
    echo "Build on x86_64 Linux, not macOS/Windows or ARM." >&2
    exit 1
fi
umask 077
python="${BUILD_PYTHON:-python3}"
if [ ! -x .build-venv/bin/python ]; then
    "$python" -m venv .build-venv
fi
.build-venv/bin/python -m pip install --disable-pip-version-check -r requirements-build.txt
.build-venv/bin/python -m unittest -v
.build-venv/bin/python -m PyInstaller \
    --noconfirm --clean --onefile --name migrate-outputs-linux-x86_64 \
    --distpath dist --workpath build/pyinstaller --specpath build \
    migrate_outputs.py
dist/migrate-outputs-linux-x86_64 --help
.build-venv/bin/python -m pip freeze > dist/build-requirements.lock
{
    uname -srm
    getconf GNU_LIBC_VERSION
    .build-venv/bin/python --version
    .build-venv/bin/python -c 'import ssl; print(ssl.OPENSSL_VERSION)'
    sha256sum migrate_outputs.py
} > dist/build-info.txt
cd dist
sha256sum migrate-outputs-linux-x86_64 > migrate-outputs-linux-x86_64.sha256
