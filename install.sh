#!/bin/sh
# Multi Auto Setup: installer for macOS and Linux. No administrator rights, nothing outside your user folder.
#
#   Download, read, run:          curl -fsSLO <url>/install.sh ; less install.sh ; sh install.sh
#   Remove everything:            sh ~/.mas/install.sh --uninstall        (your keys only with --purge)
#
# What it does, in order, asking before each change: (1) uv, the Python installer, if you do not have it; (2) downloads the release archive of ONE pinned tag and checks its
# SHA-256 against the value written below (it stops on a mismatch); (3) builds the program in ~/.mas; (4) puts a `mas` command in ~/.local/bin; (5) asks whether this computer
# is a coordinator or a node. It never changes a firewall, the network, the clock or the router, and it installs our program only: no AI models.
#
# Options:  --tag TAG --sha256 HEX   another release (both are required: the embedded hash is only for the default tag)
#           --role node|coordinator  skip the question          --yes   answer yes to everything shown (for scripts)
#           --home DIR               install under DIR instead of ~/.mas        --base-url URL   another release server (https, or http on this computer)
#           --no-join                do not offer `mas join`    --uninstall [--purge]    remove it (keys only with --purge)

# PIN-BEGIN (scripts/pin_installers.py rewrites these three lines)
DEFAULT_TAG="node-preview-8"
DEFAULT_SHA256="934ed61ee6753c17dbf15bb5dd1b36144db1b6fc307c2ddd15294e1c58911c4e"
DEFAULT_BASE="https://github.com/josuecuguy1307/Multi-Auto-Setup/releases"
# PIN-END
PY_VERSION="3.13"
UV_INSTALL_CMD='curl -LsSf https://astral.sh/uv/install.sh | sh'
BLOCK_BEGIN="# >>> multi-auto-setup >>>"
BLOCK_END="# <<< multi-auto-setup <<<"
SHIM_MARK="# written by the Multi Auto Setup installer"

set -eu

say() { printf '%s\n' "$*"; }
die() { printf '%s\n' "$*" >&2; exit 1; }

ASSUME_YES=0

# One answer from the owner. Reads the terminal even when this script arrives through a pipe; with no terminal at all the answer is "no".
read_answer() { # $1 = prompt ; sets ANSWER, returns 1 when nobody can answer
  ANSWER=""
  if [ -t 0 ]; then
    printf '%s' "$1"; read -r ANSWER || return 1
  elif (: </dev/tty) 2>/dev/null; then
    printf '%s' "$1" >/dev/tty; read -r ANSWER </dev/tty || return 1
  else
    return 1
  fi
}

ask() { # $1 = question ; yes -> 0
  if [ "$ASSUME_YES" = 1 ]; then say "$1 [y/N]: y  (--yes)"; return 0; fi
  if ! read_answer "$1 [y/N]: "; then say "There is no terminal to ask you; that change was not made. Run the script from a terminal, or use --yes if you already reviewed everything."; return 1; fi
  case "$ANSWER" in y|Y|yes|YES|Yes) return 0 ;; *) return 1 ;; esac
}

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
  elif command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | cut -d' ' -f1
  elif command -v openssl >/dev/null 2>&1; then openssl dgst -sha256 "$1" | sed 's/^.*= //'
  else die "I cannot find sha256sum, shasum or openssl to check the download. Install one and run again. Nothing was changed."; fi
}

fetch() { # $1 url $2 out
  case "$1" in
    https://*|http://127.0.0.1*|http://localhost*|http://\[::1\]*) ;;
    *) die "Downloads are only made over HTTPS (or http on this same computer): $1" ;;
  esac
  if command -v curl >/dev/null 2>&1; then curl -fsSL -o "$2" "$1" || return 1
  elif command -v wget >/dev/null 2>&1; then wget -q -O "$2" "$1" || return 1
  else die "curl or wget is needed to download. Install one with your system's package manager and run again. Nothing was changed."; fi
}

guard_home() { # refuse to ever delete something that is not ours
  case "$MAS_HOME_DIR" in ""|/|"$HOME"|"$HOME"/) die "The install folder '$MAS_HOME_DIR' is not safe. Nothing was done." ;; esac
}

state_dir() {
  py="$MAS_HOME_DIR/releases/$(cat "$MAS_HOME_DIR/current" 2>/dev/null)/.venv/bin/python"
  if [ -x "$py" ]; then "$py" -c 'import envrec_os; print(envrec_os.default_state_dir())' 2>/dev/null || true; fi
}

profile_file() {
  case "$(basename "${SHELL:-sh}")" in
    zsh) printf '%s\n' "$HOME/.zprofile" ;;
    bash) for f in "$HOME/.bash_profile" "$HOME/.bash_login" "$HOME/.profile"; do [ -f "$f" ] && { printf '%s\n' "$f"; return; }; done; printf '%s\n' "$HOME/.profile" ;;
    *) printf '%s\n' "$HOME/.profile" ;;
  esac
}

# ---- uninstall ---------------------------------------------------------------------------------------------------------------------------------

do_uninstall() {
  guard_home
  [ -e "$MAS_HOME_DIR/current" ] || [ -d "$MAS_HOME_DIR/releases" ] || die "I cannot find an installation in $MAS_HOME_DIR. Nothing was changed."
  STATE="$(state_dir)"
  say "This will be removed from this computer:"
  say "  - the Multi Auto Setup user services, if you installed them (mas service)"
  say "  - the program folder and its environments: $MAS_HOME_DIR"
  [ -f "$BIN_DIR/mas" ] && grep -q "$SHIM_MARK" "$BIN_DIR/mas" 2>/dev/null && say "  - the command: $BIN_DIR/mas"
  P="$(profile_file)"
  [ -f "$P" ] && grep -q "$BLOCK_BEGIN" "$P" 2>/dev/null && say "  - the block the installer added to $P"
  if [ "$PURGE" = 1 ]; then say "  - YOUR KEYS, pairings and logs in ${STATE:-(state folder not found)}  (--purge)"
  else say "Your keys, pairings and logs are KEPT in ${STATE:-(state folder)}; delete that folder by hand or use --purge if you want to forget them."; fi
  say "uv is not touched (you may use it for other things), nor the firewall or the network."
  ask "Continue?" || { say "Nothing was changed."; exit 1; }
  if [ "$PURGE" = 1 ]; then
    say "Deleting the keys is final: this computer would have to pair again and, if it is the coordinator, ALL the nodes too."
    if [ "$ASSUME_YES" = 1 ]; then ANSWER="DELETE"; else read_answer "Type DELETE to confirm: " || ANSWER=""; fi
    [ "$ANSWER" = "DELETE" ] || { say "Not confirmed. Nothing was changed."; exit 1; }
  fi
  MAS_BIN="$MAS_HOME_DIR/releases/$(cat "$MAS_HOME_DIR/current" 2>/dev/null)/.venv/bin/mas"
  if [ -x "$MAS_BIN" ]; then
    for r in node coordinator; do "$MAS_BIN" service uninstall --role "$r" --yes >/dev/null 2>&1 || true; done
  fi
  [ -f "$BIN_DIR/mas" ] && grep -q "$SHIM_MARK" "$BIN_DIR/mas" 2>/dev/null && rm -f "$BIN_DIR/mas"
  if [ -f "$P" ] && grep -q "$BLOCK_BEGIN" "$P" 2>/dev/null; then
    tmp="$P.mas-tmp.$$"; awk -v b="$BLOCK_BEGIN" -v e="$BLOCK_END" '$0==b{skip=1;next} $0==e{skip=0;next} !skip{print}' "$P" >"$tmp" && cat "$tmp" >"$P"; rm -f "$tmp"
    grep -q '[^[:space:]]' "$P" 2>/dev/null || rm -f "$P"  # the file only existed for our block
  fi
  rm -rf "$MAS_HOME_DIR"
  if [ "$PURGE" = 1 ] && [ -n "$STATE" ] && [ -d "$STATE" ]; then rm -rf "$STATE"; fi
  say "Done. Multi Auto Setup was removed from this computer."
}

# ---- install -----------------------------------------------------------------------------------------------------------------------------------

find_uv() {
  if command -v uv >/dev/null 2>&1; then command -v uv; elif [ -x "$HOME/.local/bin/uv" ]; then printf '%s\n' "$HOME/.local/bin/uv"; fi
}

do_install() {
  guard_home
  if [ "$TAG" != "$DEFAULT_TAG" ] && [ -z "$SHA" ]; then
    die "You asked for version '$TAG'. The SHA-256 this installer carries is only the one for '$DEFAULT_TAG'; for another version add --sha256 HEX (the one in its published manifest). The check is never skipped. Nothing was changed."
  fi
  case "$TAG" in *[!A-Za-z0-9._-]*|"") die "Invalid version name: $TAG" ;; esac
  case "$SHA" in *[!0-9a-f]*) die "--sha256 must be 64 lowercase hexadecimal characters." ;; esac
  [ "${#SHA}" = 64 ] || die "--sha256 must be 64 hexadecimal characters."
  ARCHIVE="mas-$TAG.tar.gz"
  URL="$BASE/download/$TAG/$ARCHIVE"
  REL="$MAS_HOME_DIR/releases/$TAG"
  say "Multi Auto Setup: installation for your user (no administrator)."
  say ""
  say "What I am going to do, asking before each change:"
  say "  1. use uv (the Python installer); if you do not have it, I show you the official command and ask"
  say "  2. download $URL"
  say "     and check that its SHA-256 is $SHA (if it differs, I stop)"
  say "  3. create $REL with its own Python $PY_VERSION environment"
  say "  4. put the 'mas' command in $BIN_DIR"
  say "  5. ask whether this computer will be a coordinator or a node"
  say "I do not touch the firewall, the network, the clock or the router. I do not install AI models."
  say ""

  UV="$(find_uv || true)"
  if [ -z "$UV" ]; then
    say "I cannot find uv. The official command to install it for your user is:"
    say "    $UV_INSTALL_CMD"
    say "I will run it with UV_NO_MODIFY_PATH=1, so uv does NOT edit your shell files (we decide about PATH below). Undo: delete ~/.local/bin/uv and ~/.local/bin/uvx."
    ask "Run that command now?" || die "I cannot continue without uv. Nothing was changed."
    command -v curl >/dev/null 2>&1 || die "curl is needed to install uv. Nothing was changed."
    curl -LsSf https://astral.sh/uv/install.sh | UV_NO_MODIFY_PATH=1 sh
    UV="$(find_uv || true)"
    [ -n "$UV" ] || die "uv did not end up installed. Stopping."
  else
    say "uv found: $UV"
  fi

  TMP="$(mktemp -d "${TMPDIR:-/tmp}/mas-install.XXXXXX")"
  trap 'rm -rf "$TMP"' EXIT INT TERM
  HEALTHY=0
  if [ -x "$REL/.venv/bin/python" ] && [ -f "$REL/RELEASE" ] && grep -q "$SHA" "$REL/RELEASE" && "$REL/.venv/bin/python" -c 'import envrec_mas.cli' 2>/dev/null; then HEALTHY=1; fi
  if [ "$HEALTHY" = 1 ]; then
    say "Version $TAG is already installed and responds: I repeat without downloading (repairing the command and the pointer)."
  else
    if [ -e "$REL" ]; then
      say "There is an incomplete installation of $TAG in $REL (it is ours); I replace it."
      ask "Delete $REL and rebuild it?" || die "Nothing was changed."
      rm -rf "$REL"
    fi
    ask "Download and build version $TAG?" || die "Nothing was changed."
    say "Downloading..."
    fetch "$URL" "$TMP/$ARCHIVE" || die "Could not download $URL. Nothing was changed."
    GOT="$(sha256_of "$TMP/$ARCHIVE")"
    if [ "$GOT" != "$SHA" ]; then
      die "The SHA-256 does NOT match.
  expected: $SHA
  obtained: $GOT
The download was discarded and nothing was installed."
    fi
    say "SHA-256 correct."
    tar -tzf "$TMP/$ARCHIVE" >"$TMP/list" 2>/dev/null || die "The downloaded archive cannot be read. Nothing was installed."
    if grep -E '(^/|(^|/)\.\.(/|$))' "$TMP/list" >/dev/null; then die "The archive contains paths that are not allowed. Nothing was installed."; fi
    [ "$(cut -d/ -f1 "$TMP/list" | sort -u | wc -l | tr -d ' ')" = 1 ] || die "The archive must have a single root folder. Nothing was installed."
    mkdir -p "$TMP/x" "$MAS_HOME_DIR/releases"
    tar -xzf "$TMP/$ARCHIVE" -C "$TMP/x"
    if find "$TMP/x" -type l | grep . >/dev/null; then die "The archive contains symbolic links. Nothing was installed."; fi
    TOP="$(ls "$TMP/x")"
    mv "$TMP/x/$TOP" "$REL"
    say "Building the environment (uv will download Python $PY_VERSION if needed)..."
    if ! "$UV" venv --python "$PY_VERSION" "$REL/.venv" || ! "$UV" pip install --python "$REL/.venv/bin/python" -e "$REL"; then
      rm -rf "$REL"; die "Could not build the environment. The partial work was deleted; nothing is left half-done."
    fi
    printf '{"tag": "%s", "sha256": "%s", "installed_at": %s}\n' "$TAG" "$SHA" "$(date +%s)" >"$REL/RELEASE"
    "$REL/.venv/bin/python" -c 'import envrec_mas.cli' || { rm -rf "$REL"; die "The built version does not start. It was deleted; nothing is left half-done."; }
  fi

  OLD="$(cat "$MAS_HOME_DIR/current" 2>/dev/null || true)"
  printf '%s\n' "$TAG" >"$MAS_HOME_DIR/current.tmp" && mv "$MAS_HOME_DIR/current.tmp" "$MAS_HOME_DIR/current"
  if [ -n "$OLD" ] && [ "$OLD" != "$TAG" ]; then printf '%s\n' "$OLD" >"$MAS_HOME_DIR/previous"; fi
  printf '%s\n' "$UV" >"$MAS_HOME_DIR/uv"
  # keep a copy of this installer next to the program, so the uninstaller exists even when this script arrived through a pipe or the release predates it
  if [ -f "$REL/install.sh" ]; then cp "$REL/install.sh" "$MAS_HOME_DIR/install.sh"
  elif [ -f "$0" ] && grep -q "Multi Auto Setup: installer" "$0" 2>/dev/null; then cp "$0" "$MAS_HOME_DIR/install.sh"; fi

  # the `mas` command: a small script that runs the release named in `current`
  mkdir -p "$BIN_DIR"
  if [ -e "$BIN_DIR/mas" ] && ! grep -q "$SHIM_MARK" "$BIN_DIR/mas" 2>/dev/null; then
    say "A program called 'mas' already exists in $BIN_DIR and it is not ours; I do not touch it. Use $REL/.venv/bin/mas."
    SHIM="$REL/.venv/bin/mas"
  else
    cat >"$BIN_DIR/mas" <<EOF
#!/bin/sh
$SHIM_MARK
MAS_HOME="\${MAS_HOME:-$MAS_HOME_DIR}"
T="\$(cat "\$MAS_HOME/current" 2>/dev/null)" || { echo "mas: no installation in \$MAS_HOME" >&2; exit 1; }
exec "\$MAS_HOME/releases/\$T/.venv/bin/mas" "\$@"
EOF
    chmod 755 "$BIN_DIR/mas"
    SHIM="$BIN_DIR/mas"
    say "Command installed: $SHIM"
  fi

  case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *)
      P="$(profile_file)"
      say ""
      say "$BIN_DIR is not on your PATH, so 'mas' would not be found when you open another terminal."
      say "I can add this block to the end of $P:"
      say "    $BLOCK_BEGIN"
      say "    export PATH=\"$BIN_DIR:\$PATH\""
      say "    $BLOCK_END"
      say "Undo: delete those three lines, or run the uninstaller (it removes them by itself)."
      if ask "Add that block?"; then
        { printf '\n%s\n' "$BLOCK_BEGIN"; printf 'export PATH="%s:$PATH"\n' "$BIN_DIR"; printf '%s\n' "$BLOCK_END"; } >>"$P"
        say "Done. Open a new terminal (or run: export PATH=\"$BIN_DIR:\$PATH\")."
      else
        say "I did not add it. Meanwhile use the full path: $SHIM"
      fi
      ;;
  esac

  say ""
  if [ -z "$ROLE" ]; then
    say "Will this machine be a coordinator or a node?"
    say "  coordinator = the computer you run the network from (only one)."
    say "  node        = a computer that joins that coordinator."
    if read_answer "Type c (coordinator) or n (node): "; then
      case "$ANSWER" in c|C|coordinator|Coordinator) ROLE=coordinator ;; n|N|node|Node) ROLE=node ;; esac
    fi
  fi
  [ -n "$ROLE" ] && printf '%s\n' "$ROLE" >"$MAS_HOME_DIR/role"
  case "$ROLE" in
    coordinator)
      say "This computer will be the COORDINATOR."
      say "A coordinator listens on ONE port of your network (TCP 7443 by default and, optionally, UDP 7444 so nodes can find it)."
      say "If your system's firewall asks, you decide: this installer does not change it. To start it:"
      say "    mas coordinator start --bind THIS_COMPUTERS_ADDRESS --web"
      say "To start it by itself when you sign in:  mas service install --role coordinator --bind ADDRESS"
      ;;
    node)
      say "This computer will be a NODE: it listens on no port, it only calls the coordinator."
      if [ "$NO_JOIN" = 1 ]; then say "When you want to join it:  mas join"
      elif ask "Look for a coordinator on your network and join this computer now (mas join)?"; then
        "$SHIM" join || say "It did not get paired. You can repeat it whenever you want with:  mas join"
      else say "When you want to join it:  mas join"; fi
      say "To start it by itself when you sign in (after pairing):  mas service install"
      ;;
    *) say "You did not choose a role. When you know: 'mas join' (node) or 'mas coordinator start' (coordinator)." ;;
  esac
  say ""
  say "Done. Installed version: $TAG. To update later: mas update.  To remove everything: sh $MAS_HOME_DIR/install.sh --uninstall"
}

main() {
  TAG="$DEFAULT_TAG"; SHA=""; ROLE=""; NO_JOIN=0; UNINSTALL=0; PURGE=0
  BASE="${MAS_RELEASE_BASE:-$DEFAULT_BASE}"
  MAS_HOME_DIR="${MAS_HOME:-$HOME/.mas}"
  while [ $# -gt 0 ]; do
    case "$1" in
      --tag) [ $# -ge 2 ] || die "--tag needs a value"; TAG="$2"; shift 2 ;;
      --sha256) [ $# -ge 2 ] || die "--sha256 needs a value"; SHA="$2"; shift 2 ;;
      --role) [ $# -ge 2 ] || die "--role needs node or coordinator"; ROLE="$2"; shift 2 ;;
      --home) [ $# -ge 2 ] || die "--home needs a folder"; MAS_HOME_DIR="$2"; shift 2 ;;
      --base-url) [ $# -ge 2 ] || die "--base-url needs a URL"; BASE="$2"; shift 2 ;;
      --yes|-y) ASSUME_YES=1; shift ;;
      --no-join) NO_JOIN=1; shift ;;
      --uninstall) UNINSTALL=1; shift ;;
      --purge) PURGE=1; shift ;;
      -h|--help) sed -n '2,16p' "$0" 2>/dev/null | sed 's/^# \{0,1\}//'; exit 0 ;;
      *) die "Unknown option: $1 (use --help)" ;;
    esac
  done
  case "$ROLE" in ""|node|coordinator) ;; *) die "--role must be node or coordinator" ;; esac
  [ -z "$SHA" ] && [ "$TAG" = "$DEFAULT_TAG" ] && SHA="$DEFAULT_SHA256"
  BASE="${BASE%/}"
  case "$MAS_HOME_DIR" in /*) ;; *) MAS_HOME_DIR="$(pwd)/$MAS_HOME_DIR" ;; esac
  BIN_DIR="${MAS_BIN_DIR:-$HOME/.local/bin}"
  if [ "$UNINSTALL" = 1 ]; then do_uninstall; else do_install; fi
}

main "$@"
exit $?
