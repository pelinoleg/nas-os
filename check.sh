#!/usr/bin/env bash
# NAS-OS: all static checks in one place.
#
#   ./check.sh          — run everything
#   ./check.sh py js sh — run only the named groups
#
# Groups: py, sh, gen (scripts generated inside heredocs), js, css, i18n, git.
# Node is not installed on the box: JS is checked through the node:20-alpine
# image (see MEMORY: js-validation-no-node). Without docker the js group is
# skipped, not failed.
set -uo pipefail
cd "$(dirname "$0")"

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
FAIL=0; RUN=0
ok(){   printf '  \033[32m✓\033[0m %s\n' "$1"; }
bad(){  printf '  \033[31m✗\033[0m %s\n' "$1"; FAIL=$((FAIL+1)); }
skip(){ printf '  \033[33m—\033[0m %s\n' "$1"; }
head(){ printf '\n\033[1m%s\033[0m\n' "$1"; }
# NB: don't name the array GROUPS — that's a bash builtin variable (group ids),
# assigning to it is silently ignored.
CHECKS=("$@"); [ ${#CHECKS[@]} -eq 0 ] && CHECKS=(py sh gen js css i18n git)
has(){ local g; for g in "${CHECKS[@]}"; do [ "$g" = "$1" ] && return 0; done; return 1; }

# --- extract the shell scripts that live inside heredocs / python strings ---
extract(){
python3 - "$1" "$2" <<'PY'
import re, sys
kind, out = sys.argv[1], sys.argv[2]
if kind == "usb-import":
    src = open("nas-web.py", encoding="utf-8").read()
    m = re.search(r"_USB_SH = r'''(.*?)'''", src, re.S)
elif kind == "netguard":
    src = open("nas-wizard.sh", encoding="utf-8").read()
    m = re.search(r"write_file /usr/local/bin/nas-netguard\.sh <<'GUARD'\n(.*?)\nGUARD\n", src, re.S)
elif kind == "motd":
    src = open("nas-wizard.sh", encoding="utf-8").read()
    m = re.search(r"write_file /etc/update-motd\.d/20-nas-os <<'MOTD'\n(.*?)\nMOTD\n", src, re.S)
elif kind == "dispatcher":
    src = open("nas-wizard.sh", encoding="utf-8").read()
    m = re.search(r"write_file /etc/NetworkManager/dispatcher\.d/50-nas-netguard <<'DISP'\n(.*?)\nDISP\n", src, re.S)
else:
    sys.exit("unknown: " + kind)
if not m:
    sys.exit("block not found: " + kind)
open(out, "w", encoding="utf-8").write(m.group(1) + "\n")
PY
}

# --- pull every <script> / <style> out of the html shell ---
split_html(){
python3 - "$1" "$TMP" <<'PY'
import re, sys, os
html, tmp = sys.argv[1], sys.argv[2]
h = open(html, encoding="utf-8").read()
js = "\n;\n".join(re.findall(r"<script[^>]*>(.*?)</script>", h, re.S))
open(os.path.join(tmp, "inline.js"), "w", encoding="utf-8").write(js)
bad = [i for i, st in enumerate(re.findall(r"<style[^>]*>(.*?)</style>", h, re.S))
       if st.count("{") != st.count("}")]
sys.exit("unbalanced braces in <style> #%s" % bad if bad else 0)
PY
}

node_check(){   # node_check <file> <label>
  if ! command -v docker >/dev/null 2>&1; then skip "$2 (no docker)"; return; fi
  local d; d="$(cd "$(dirname "$1")" && pwd)"
  if docker run --rm -v "$d":/w node:20-alpine node --check "/w/$(basename "$1")" >/dev/null 2>&1
    then ok "$2"; else bad "$2"; fi
}

# ------------------------------------------------------------------ python --
if has py; then
  head "Python"
  RUN=1
  python3 -m py_compile nas-web.py 2>/dev/null && ok "nas-web.py compiles" || bad "nas-web.py"
fi

# ------------------------------------------------------------------- shell --
if has sh; then
  head "Shell"
  RUN=1
  for f in nas-wizard.sh install.sh check.sh; do
    [ -f "$f" ] || continue
    bash -n "$f" 2>/dev/null && ok "$f" || bad "$f"
  done
fi

# ------------------------------------ scripts generated at install/run time --
if has gen; then
  head "Generated scripts (heredoc)"
  RUN=1
  for k in usb-import netguard motd dispatcher; do
    if extract "$k" "$TMP/$k.sh" 2>/dev/null; then
      bash -n "$TMP/$k.sh" 2>/dev/null && ok "$k" || bad "$k (bash -n)"
    else
      bad "$k (block not found — heredoc renamed?)"
    fi
  done
fi

# ------------------------------------------------------------- html js/css --
if has js || has css; then
  head "Web"
  RUN=1
  if split_html web/desktop.html 2>"$TMP/css.err"; then ok "desktop.html: CSS balanced"
  else bad "desktop.html: $(cat "$TMP/css.err")"; fi
  has js && node_check "$TMP/inline.js" "desktop.html: inline JS"
  has js && node_check web/sw.js web/sw.js
fi

# --------------------------------------------------------------------- i18n --
if has i18n; then
  head "Cyrillic (must be gone — the UI is English-only)"
  RUN=1
python3 - <<'PY' && ok "no stray Cyrillic" || bad "stray Cyrillic found (see above)"
import re, subprocess, sys
CY = re.compile(r"[А-Яа-яЁё]")
# Allowed to keep Cyrillic:
#   CLAUDE.md                        — project doc, deliberately Russian
#   web/tui-editor.js, web/tui-hl.js — vendored third-party, kept byte-for-byte
#   nas-web.py                       — _TRANSLIT transliteration keys and the folder-name
#                                      sanitizer regexes (the "А-Яа-яЁё" char-range that
#                                      deliberately allows Russian in user file names)
ALLOW_FILES = {"CLAUDE.md", "web/tui-editor.js", "web/tui-hl.js"}
def line_ok(f, ln):
    t = ln.replace("А-Яа-яЁё", "")            # the Cyrillic char-range literal itself
                                              # (detector regexes here, sanitizer regexes in nas-web.py)
    if f == "nas-web.py":
        t = re.sub(r'"[А-Яа-яЁё]+"\s*:', "", t)   # _TRANSLIT keys: one Cyrillic letter -> Latin
    return not CY.search(t)
bad = []
for f in subprocess.check_output(["git", "ls-files"]).decode().split():
    if f in ALLOW_FILES:
        continue
    try:
        lines = open(f, encoding="utf-8").read().splitlines()
    except (UnicodeDecodeError, IsADirectoryError, FileNotFoundError):
        continue
    for i, ln in enumerate(lines, 1):
        if not CY.search(ln):
            continue
        if line_ok(f, ln):
            continue
        bad.append("%s:%d: %s" % (f, i, ln.strip()[:80]))
for b in bad[:20]:
    print("      " + b, file=sys.stderr)
if len(bad) > 20:
    print("      … %d more" % (len(bad) - 20), file=sys.stderr)
sys.exit(1 if bad else 0)
PY
fi

# ---------------------------------------------------------------------- git --
if has git; then
  head "Git"
  RUN=1
  if [ -z "$(git status --porcelain)" ]; then ok "tree is clean"
  else skip "there are uncommitted changes"; git status --short | sed 's/^/      /'; fi
fi

printf '\n'
if [ "$RUN" = 0 ]; then echo "nothing to run: unknown groups"; exit 2; fi
if [ "$FAIL" = 0 ]; then printf '\033[32mALL CLEAN\033[0m\n'; else printf '\033[31mFAILURES: %d\033[0m\n' "$FAIL"; fi
exit $((FAIL > 0))
