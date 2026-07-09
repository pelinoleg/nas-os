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
# NB: не называть массив GROUPS — это встроенная переменная bash (id групп),
# присваивание в неё молча игнорируется.
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
  if ! command -v docker >/dev/null 2>&1; then skip "$2 (нет docker)"; return; fi
  local d; d="$(cd "$(dirname "$1")" && pwd)"
  if docker run --rm -v "$d":/w node:20-alpine node --check "/w/$(basename "$1")" >/dev/null 2>&1
    then ok "$2"; else bad "$2"; fi
}

# ------------------------------------------------------------------ python --
if has py; then
  head "Python"
  RUN=1
  python3 -m py_compile nas-web.py 2>/dev/null && ok "nas-web.py компилируется" || bad "nas-web.py"
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
  head "Генерируемые скрипты (heredoc)"
  RUN=1
  for k in usb-import netguard motd dispatcher; do
    if extract "$k" "$TMP/$k.sh" 2>/dev/null; then
      bash -n "$TMP/$k.sh" 2>/dev/null && ok "$k" || bad "$k (bash -n)"
    else
      bad "$k (блок не найден — переименовали heredoc?)"
    fi
  done
fi

# ------------------------------------------------------------- html js/css --
if has js || has css; then
  head "Веб"
  RUN=1
  if split_html web/desktop.html 2>"$TMP/css.err"; then ok "desktop.html: CSS сбалансирован"
  else bad "desktop.html: $(cat "$TMP/css.err")"; fi
  has js && node_check "$TMP/inline.js" "desktop.html: инлайн-JS"
  has js && for f in web/i18n.js web/sw.js; do node_check "$f" "$f"; done
fi

# --------------------------------------------------------------------- i18n --
if has i18n; then
  head "Переводы"
  RUN=1
python3 - <<'PY' && ok "все строки UI переведены" || bad "есть непереведённые строки (см. выше)"
import re, sys
src = open("web/i18n.js", encoding="utf-8").read()
# пары ищем где угодно: в i18n.js часть словаря записана по несколько пар в строку,
# и построчный разбор молча терял их (дни недели считались непереведёнными)
pairs = re.findall(r'"((?:[^"\\]|\\.)*)"\s*:\s*"((?:[^"\\]|\\.)*)"', src)
m = {a.replace('\\"', '"'): b for a, b in pairs}
keys = sorted([k for k in m if k], key=len, reverse=True)
rx = re.compile(r"(?<![А-Яа-яЁё])(?:" + "|".join(re.escape(k) for k in keys) + r")(?![А-Яа-яЁё])")
tr = lambda s: rx.sub(lambda mo: m.get(mo.group(0), mo.group(0)), s)

h = open("web/desktop.html", encoding="utf-8").read()
js = "\n".join(re.findall(r"<script[^>]*>(.*?)</script>", h, re.S))
js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
js = re.sub(r"(?m)(?<!:)//(?!\s*\w+\.\w).*$", "", js)
lits = re.findall(r"`((?:[^`\\]|\\.)*)`|\"((?:[^\"\\\n]|\\.)*)\"|'((?:[^'\\\n]|\\.)*)'", js, re.S)
CY = re.compile(r"[А-Яа-яЁё]+")
bad = []
for t in lits:
    lit = next((x for x in t if x), "")
    if not CY.search(lit):
        continue
    # теги НЕ вырезаем: nasTr переводит сырую строку innerHTML, и ключи вроде
    # "<имя>.local" содержат угловые скобки. Убираем только ${...}-вставки.
    s = re.sub(r"\$\{[^{}]*\}", "", lit)
    left = CY.findall(tr(s))
    if left:
        bad.append((sorted(set(left))[:4], lit[:70].replace("\n", " ")))
for words, ex in bad[:12]:
    print("      осталось %s  ⟵ %s" % (", ".join(words), ex), file=sys.stderr)
if len(bad) > 12:
    print("      … ещё %d" % (len(bad) - 12), file=sys.stderr)
sys.exit(1 if bad else 0)
PY
fi

# ---------------------------------------------------------------------- git --
if has git; then
  head "Git"
  RUN=1
  if [ -z "$(git status --porcelain)" ]; then ok "дерево чистое"
  else skip "есть незакоммиченные правки"; git status --short | sed 's/^/      /'; fi
fi

printf '\n'
if [ "$RUN" = 0 ]; then echo "нечего запускать: неизвестные группы"; exit 2; fi
if [ "$FAIL" = 0 ]; then printf '\033[32mВСЁ ЧИСТО\033[0m\n'; else printf '\033[31mПРОВАЛОВ: %d\033[0m\n' "$FAIL"; fi
exit $((FAIL > 0))
