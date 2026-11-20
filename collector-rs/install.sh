#!/bin/sh
# Installs the devdiary CLI from the GitHub release that carries this script:
#
#   curl -fsSL https://devdiary.me/cli/install.sh | sh
#   curl -fsSL https://devdiary.me/cli/install.sh | sh -s -- --version X.Y.Z
#
# The release job writes its tag into the RELEASE_TAG line.
#
# ~ and $HOME in quotes are text shown to the person, not paths for this shell.
# shellcheck disable=SC2016,SC2088

set -eu

RELEASE_TAG=unreleased
DOWNLOADS=https://github.com/techwright-lab/devdiary-cli/releases/download

say() { printf '%s\n' "$*"; }

fail() {
  printf 'devdiary install: %s\n' "$*" >&2
  exit 1
}

target_for() {
  case "$1 $2" in
    "Darwin arm64") echo aarch64-apple-darwin ;;
    "Linux x86_64") echo x86_64-unknown-linux-musl ;;
    *) return 1 ;;
  esac
}

download() {
  curl --proto '=https' --tlsv1.2 -fsSL -o "$2" "$1" || fail "could not download $1"
}

sha256() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{ print $1 }'
  else
    shasum -a 256 "$1" | awk '{ print $1 }'
  fi
}

startup_file() {
  case ${SHELL:-} in
    */zsh) echo "$HOME/.zshrc" ;;
    */bash)
      # Terminal windows on macOS start bash as a login shell, which skips ~/.bashrc.
      if [ "$system" = Darwin ]; then echo "$HOME/.bash_profile"; else echo "$HOME/.bashrc"; fi
      ;;
    */fish) echo "${XDG_CONFIG_HOME:-$HOME/.config}/fish/config.fish" ;;
    *) echo "$HOME/.profile" ;;
  esac
}

path_line() {
  case ${SHELL:-} in
    */fish) echo 'fish_add_path $HOME/.local/bin' ;;
    *) echo 'export PATH="$HOME/.local/bin:$PATH"' ;;
  esac
}

shown() {
  case $1 in
    "$HOME"/*) printf '~/%s\n' "${1#"$HOME"/}" ;;
    *) printf '%s\n' "$1" ;;
  esac
}

offer_path() {
  file=$(startup_file)
  line=$(path_line)
  name=$(shown "$file")
  if [ -f "$file" ] && grep -Fqx -- "$line" "$file"; then
    say "$name already puts ~/.local/bin on PATH. Open a new terminal to run devdiary by name."
    return
  fi
  printf '~/.local/bin is not on your PATH. Add this line to %s?\n\n  %s\n\n[y/N] ' "$name" "$line" >/dev/tty
  answer=
  read -r answer </dev/tty || true
  case $answer in
    y | Y | yes | Yes | YES)
      mkdir -p "$(dirname "$file")"
      if [ -s "$file" ] && [ -n "$(tail -c 1 "$file")" ]; then echo >>"$file"; fi
      printf '%s\n' "$line" >>"$file"
      say "Added it to $name. Open a new terminal to run devdiary by name."
      ;;
    *) say "Left $name unchanged. Until ~/.local/bin is on PATH, run ~/.local/bin/devdiary." ;;
  esac
}

main() {
  tag=$RELEASE_TAG
  while [ $# -gt 0 ]; do
    case $1 in
      --version)
        [ $# -ge 2 ] || fail "--version needs a release number, such as --version 0.4.0."
        tag=v${2#v}
        shift 2
        ;;
      *) fail "unknown option: $1" ;;
    esac
  done
  [ "$tag" != unreleased ] || fail "this install.sh does not come from a release. Pass --version X.Y.Z."
  printf '%s\n' "$tag" | grep -Eqx 'v[0-9]+\.[0-9]+\.[0-9]+' ||
    fail "${tag#v} is not a release number such as 0.4.0."
  version=${tag#v}

  system=$(uname -s)
  machine=$(uname -m)
  target=$(target_for "$system" "$machine") ||
    fail "devdiary does not run on $system $machine. It runs on macOS on Apple silicon and on Linux on x86_64."

  [ -n "${HOME:-}" ] || fail "HOME is not set."
  command -v curl >/dev/null 2>&1 || fail "curl is not installed."
  command -v sha256sum >/dev/null 2>&1 || command -v shasum >/dev/null 2>&1 ||
    fail "neither sha256sum nor shasum is installed."

  tmp=$(mktemp -d "${TMPDIR:-/tmp}/devdiary-install.XXXXXX")
  staged=
  trap 'rm -rf "$tmp"; [ -z "$staged" ] || rm -f "$staged"' EXIT
  trap 'exit 1' HUP INT TERM

  archive=devdiary-$version-$target.tar.gz
  say "Downloading devdiary $version for $target."
  download "$DOWNLOADS/$tag/$archive" "$tmp/$archive"
  download "$DOWNLOADS/$tag/SHA256SUMS" "$tmp/SHA256SUMS"
  expected=$(awk -v name="$archive" '$2 == name { print $1 }' "$tmp/SHA256SUMS")
  actual=$(sha256 "$tmp/$archive")
  if [ -z "$expected" ] || [ "$expected" != "$actual" ]; then
    fail "$archive does not match SHA256SUMS in release $tag for $target. Nothing was installed."
  fi

  mkdir "$tmp/unpacked"
  tar -xzf "$tmp/$archive" -C "$tmp/unpacked" devdiary
  if [ ! -f "$tmp/unpacked/devdiary" ] || [ -L "$tmp/unpacked/devdiary" ]; then
    fail "$archive in release $tag holds no devdiary binary. Nothing was installed."
  fi

  # A new file renamed over the old one: macOS caches code signatures per file.
  bin_dir=$HOME/.local/bin
  mkdir -p "$bin_dir"
  staged=$(mktemp "$bin_dir/.devdiary.XXXXXX")
  cp "$tmp/unpacked/devdiary" "$staged"
  chmod 755 "$staged"
  mv -f "$staged" "$bin_dir/devdiary"
  staged=
  rm -rf "$tmp"
  say "Installed devdiary $version at ~/.local/bin/devdiary."

  case ":$PATH:" in
    *":$bin_dir:"*) on_path=yes ;;
    *) on_path=no ;;
  esac

  if ! (: </dev/tty) 2>/dev/null; then
    say "Next, in a terminal, run: devdiary setup"
    [ "$on_path" = yes ] || say "~/.local/bin is not on your PATH, so until it is, run ~/.local/bin/devdiary setup."
    exit 0
  fi

  [ "$on_path" = yes ] || offer_path
  # By its installed path, so an older devdiary earlier on PATH cannot take the run.
  status=0
  "$bin_dir/devdiary" setup </dev/tty || status=$?
  exit "$status"
}

main "$@"
