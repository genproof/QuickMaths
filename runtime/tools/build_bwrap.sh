#!/usr/bin/env bash
# build_bwrap.sh PREFIX : bubblewrap 0.12.0 for the lake-comparator flow, built the way the organizers build it
# and without root. Prints the path of PREFIX/bwrap.
#
# The build itself is the organizers' scripts/install_bwrap.sh (lean-eval-submissions, fetched at a pinned
# commit and SHA-256 checked; it downloads the pinned bubblewrap release and checks that too). What this
# script adds is the build environment a shared host often lacks without sudo:
#   - meson and ninja from a Python venv in the build directory (PREFIX/../.bwrap-build);
#   - libcap headers: when pkg-config cannot find libcap, the distribution's libcap-dev package is downloaded
#     (`apt-get download`, nothing is installed) and unpacked in the build directory, its libcap.pc is
#     pointed there and a dangling libcap.so link is pointed at the host's runtime libcap.so.2.
# Still needed on the host: a C compiler, pkg-config, curl, xz, and python3 with venv.
#
# On hosts where unprivileged user namespaces are AppArmor-restricted
# (kernel.apparmor_restrict_unprivileged_userns=1, the Ubuntu 24.04 default) the organizers' script loads an
# AppArmor profile for PREFIX/bwrap with sudo; that one step needs root (Ubuntu 22.04 hosts do not need it).
set -euo pipefail
INSTALLER_COMMIT=4704027e5e221bbe8b0f8356dec1ce5c1965fd6e
INSTALLER_SHA256=c290df6cebdf1cb26edb69ed4164d9551062bfeb65243b93d1d1d3b77eac0a35
PREFIX=${1:?usage: build_bwrap.sh <install-dir>}
case $PREFIX in /*) ;; *) echo "build_bwrap: the install directory must be an absolute path" >&2; exit 2 ;; esac
mkdir -p "$PREFIX" "$(dirname "$PREFIX")/.bwrap-build"
B=$(cd "$(dirname "$PREFIX")/.bwrap-build" && pwd -P)
PY=${PYTHON:-python3}

if [ -x "$PREFIX/bwrap" ] && [ "$("$PREFIX/bwrap" --version 2>/dev/null)" = "bubblewrap 0.12.0" ]; then
  echo "$PREFIX/bwrap"; exit 0
fi
command -v pkg-config >/dev/null || { echo "build_bwrap: pkg-config is required" >&2; exit 1; }
[ -x "$B/venv/bin/meson" ] && [ -x "$B/venv/bin/ninja" ] || {
  "$PY" -m venv "$B/venv" && "$B/venv/bin/pip" install -q meson ninja; }
export PATH="$B/venv/bin:$PATH"

if ! pkg-config --exists libcap || [ ! -f "$(pkg-config --variable=includedir libcap)/sys/capability.h" ]; then
  command -v apt-get >/dev/null && command -v dpkg-deb >/dev/null || {
    echo "build_bwrap: libcap headers are missing; install libcap-dev (or its equivalent)" >&2; exit 1; }
  rm -rf "$B/deb" "$B/capdev"
  mkdir -p "$B/deb" "$B/capdev"
  (cd "$B/deb" && apt-get download libcap-dev >/dev/null)
  dpkg-deb -x "$B"/deb/libcap-dev_*.deb "$B/capdev"
  pc=$(find "$B/capdev" -name libcap.pc | head -1)
  [ -n "$pc" ] || { echo "build_bwrap: libcap-dev has no libcap.pc" >&2; exit 1; }
  libdir=$(dirname "$(dirname "$pc")")
  sed -i "s|^prefix=.*|prefix=$B/capdev/usr|; s|^libdir=.*|libdir=$libdir|; s|^includedir=.*|includedir=$B/capdev/usr/include|" "$pc"
  if [ ! -e "$libdir/libcap.so" ]; then  # the package's link points at the runtime library's usual path
    real=$(ldconfig -p | awk '/libcap\.so\.2 /{print $NF; exit}')
    [ -n "$real" ] || { echo "build_bwrap: the host has no libcap.so.2 runtime library" >&2; exit 1; }
    ln -sfn "$real" "$libdir/libcap.so"
  fi
  export PKG_CONFIG_PATH="$(dirname "$pc")${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
  export LIBRARY_PATH="$libdir${LIBRARY_PATH:+:$LIBRARY_PATH}"
fi

installer=$B/install_bwrap-${INSTALLER_COMMIT:0:12}.sh
curl --fail --location --silent --show-error --proto '=https' --tlsv1.2 --output "$installer" \
  "https://raw.githubusercontent.com/leanprover/lean-eval-submissions/$INSTALLER_COMMIT/scripts/install_bwrap.sh"
echo "$INSTALLER_SHA256  $installer" | sha256sum --check --quiet
bash "$installer" "$PREFIX" >/dev/null
[ "$("$PREFIX/bwrap" --version)" = "bubblewrap 0.12.0" ] || { echo "build_bwrap: unexpected version" >&2; exit 1; }
echo "$PREFIX/bwrap"
