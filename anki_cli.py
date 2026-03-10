from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import termios
import textwrap
import tty
import zipfile
from datetime import date, timedelta
from html import unescape
from pathlib import Path
from random import shuffle


def read_key() -> str:
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        return sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def term_size() -> tuple[int, int]:
    cols, rows = os.get_terminal_size()
    return rows, cols


def clear_screen() -> None:
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()


def fit(text: str, width: int) -> str:
    return text[:width].ljust(width)


def wrap_body(text: str, cols: int) -> list[str]:
    out: list[str] = []
    for line in text.splitlines():
        if len(line) <= cols:
            out.append(line)
        else:
            out.extend(textwrap.wrap(line, cols, break_long_words=True) or [""])
    return out


def draw_review(
    header: str,
    body: str,
    footer: str,
) -> None:
    rows, cols = term_size()
    clear_screen()

    sys.stdout.write(f"\033[7m{fit(header, cols)}\033[0m\n")

    body_lines = wrap_body(body, cols)
    available = rows - 3
    for line in body_lines[:available]:
        sys.stdout.write(line + "\n")

    sys.stdout.write(f"\033[{rows};0H")
    sys.stdout.write(f"\033[7m{fit(footer, cols)}\033[0m")
    sys.stdout.flush()


STATE_FILE = Path("anki_state.json")
MEDIA_DIR = Path("media")

# SM-2 defaults
INITIAL_EASE = 2.5
MIN_EASE = 1.3
GRADUATING_INTERVAL = 1
EASY_INTERVAL = 4


def strip_html(text: str) -> str:
    for tag in ("style", "script", "svg"):
        text = re.sub(
            rf"<{tag}[^>]*>.*?</{tag}>",
            "",
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = unescape(text)
    return text.strip()


def resolve_cloze(text: str, ordinal: int, reveal: bool) -> str:
    """Resolve cloze deletions. When reveal=False, replace the target cloze with [...].
    When reveal=True, show the answer. Non-target clozes are always revealed."""
    target = ordinal + 1

    def replacer(m: re.Match[str]) -> str:
        cloze_num = int(m.group(1))
        answer = m.group(2)
        hint = m.group(3)
        if cloze_num == target:
            if reveal:
                return f"[{answer}]"
            return f"[{hint or '...'}]"
        return answer

    return re.sub(r"\{\{c(\d+)::([^}]*?)(?:::([^}]*?))?\}\}", replacer, text)


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"decks": {}, "cards": {}, "files": {}}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def import_apkg(apkg_path: Path, state: dict) -> int:
    file_prefix = apkg_path.name + ":"

    with zipfile.ZipFile(apkg_path) as z:
        names = z.namelist()
        db_name = (
            "collection.anki21" if "collection.anki21" in names else "collection.anki2"
        )
        db_bytes = z.read(db_name)

        if "media" in names:
            media_map: dict[str, str] = json.loads(z.read("media"))
            MEDIA_DIR.mkdir(exist_ok=True)
            extracted = 0
            for num, filename in media_map.items():
                dest = MEDIA_DIR / filename
                if not dest.exists() and num in names:
                    dest.write_bytes(z.read(num))
                    extracted += 1
            if extracted:
                print(f"  Extracted {extracted} media files.")

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        tmp.write(db_bytes)
        tmp_path = tmp.name

    deck_ids: list[str] = []
    try:
        conn = sqlite3.connect(tmp_path)
        cur = conn.cursor()

        cur.execute("SELECT models, decks FROM col")
        row = cur.fetchone()
        models = json.loads(row[0])
        decks = json.loads(row[1])

        deck_names = {int(did): d["name"] for did, d in decks.items()}
        model_fields: dict[int, list[str]] = {}
        model_tmpls: dict[int, list[dict]] = {}
        for mid_str, m in models.items():
            mid = int(mid_str)
            model_fields[mid] = [f["name"] for f in m["flds"]]
            model_tmpls[mid] = m["tmpls"]

        cur.execute("SELECT id, mid, flds FROM notes")
        notes: dict[int, tuple[int, list[str]]] = {}
        for nid, mid, flds in cur.fetchall():
            notes[nid] = (mid, flds.split("\x1f"))

        cur.execute("SELECT id, nid, did, ord FROM cards")
        imported = 0
        for cid, nid, did, ord_ in cur.fetchall():
            card_key = file_prefix + str(cid)
            did_str = file_prefix + str(did)

            if did_str not in deck_ids:
                deck_ids.append(did_str)
            deck_name = deck_names.get(did, "Default")
            state["decks"][did_str] = deck_name

            if card_key in state["cards"]:
                continue

            mid, fields = notes[nid]
            field_names = model_fields[mid]
            field_map = dict(zip(field_names, fields))
            tmpls = model_tmpls[mid]

            tmpl = tmpls[ord_] if ord_ < len(tmpls) else tmpls[0]
            is_cloze = "{{cloze:" in tmpl.get("qfmt", "")

            state["cards"][card_key] = {
                "deck_id": did_str,
                "fields": field_map,
                "template_front": tmpl["qfmt"],
                "template_back": tmpl["afmt"],
                "is_cloze": is_cloze,
                "cloze_ord": ord_,
                "due": str(date.today()),
                "interval": 0,
                "ease": INITIAL_EASE,
                "reps": 0,
            }
            imported += 1

        conn.close()
    finally:
        Path(tmp_path).unlink()

    state["files"][apkg_path.name] = deck_ids
    return imported


def remove_file_decks(filename: str, state: dict) -> int:
    deck_ids = state["files"].get(filename, [])
    removed = 0
    for did in deck_ids:
        count = sum(1 for c in state["cards"].values() if c["deck_id"] == did)
        state["cards"] = {
            k: v for k, v in state["cards"].items() if v["deck_id"] != did
        }
        state["decks"].pop(did, None)
        removed += count
    state["files"].pop(filename, None)
    return removed


def sync_decks() -> dict:
    state = load_state()
    state.setdefault("files", {})

    apkg_files = {p.name for p in Path(".").glob("*.apkg")}
    known_files = set(state["files"].keys())

    for filename in sorted(apkg_files - known_files):
        print(f"Importing {filename}...")
        imported = import_apkg(Path(filename), state)
        print(f"  {imported} new cards.")

    for filename in sorted(known_files - apkg_files):
        removed = remove_file_decks(filename, state)
        print(f"Removed {filename} ({removed} cards).")

    save_state(state)
    return state


def render_template(template: str, fields: dict[str, str]) -> str:
    """Process Anki Mustache-like templates: conditionals and field substitution."""
    text = template
    # {{#Field}}...{{/Field}} — show block if field non-empty
    text = re.sub(
        r"\{\{#(.+?)\}\}(.*?)\{\{/\1\}\}",
        lambda m: m.group(2) if strip_html(fields.get(m.group(1), "")).strip() else "",
        text,
        flags=re.DOTALL,
    )
    # {{^Field}}...{{/Field}} — show block if field empty
    text = re.sub(
        r"\{\{\^(.+?)\}\}(.*?)\{\{/\1\}\}",
        lambda m: "" if strip_html(fields.get(m.group(1), "")).strip() else m.group(2),
        text,
        flags=re.DOTALL,
    )

    def replacer(m: re.Match[str]) -> str:
        name = m.group(1)
        if name.startswith("cloze:"):
            field_name = name[len("cloze:") :]
            return fields.get(field_name, "")
        if name.startswith("type:"):
            return ""
        return fields.get(name, "")

    return re.sub(r"\{\{([^#/^}]+?)\}\}", replacer, text)


def render_card(card: dict, side: str) -> tuple[str, list[str]]:
    tmpl = card["template_front"] if side == "front" else card["template_back"]
    text = render_template(tmpl, card["fields"])

    if card["is_cloze"]:
        text = resolve_cloze(text, card["cloze_ord"], reveal=(side == "back"))

    text = re.sub(r"\{\{FrontSide\}\}", "", text)
    text = strip_html(text)
    sounds = re.findall(r"\[sound:([^\]]+)\]", text)
    text = re.sub(r"\[sound:[^\]]+\]", "", text)
    lines = [line.strip() for line in text.splitlines()]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip(), sounds


def play_sounds(sounds: list[str]) -> None:
    for filename in sounds:
        path = MEDIA_DIR / filename
        if path.exists():
            subprocess.Popen(
                ["afplay", str(path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )


def sm2_update(card: dict, quality: int) -> None:
    """Update card scheduling using SM-2. quality: 0=again, 1=hard, 2=good, 3=easy."""
    today = str(date.today())
    card["reps"] += 1

    if quality < 1:
        card["interval"] = 0
        card["due"] = today
        card["ease"] = max(MIN_EASE, card["ease"] - 0.2)
    elif card["interval"] == 0:
        if quality == 3:
            card["interval"] = EASY_INTERVAL
        else:
            card["interval"] = GRADUATING_INTERVAL
        card["due"] = str(date.today() + timedelta(days=card["interval"]))
    else:
        ease_mod = {1: -0.15, 2: 0.0, 3: 0.15}[quality]
        card["ease"] = max(MIN_EASE, card["ease"] + ease_mod)
        card["interval"] = max(1, round(card["interval"] * card["ease"]))
        card["due"] = str(date.today() + timedelta(days=card["interval"]))


DEFAULT_SESSION_SIZE = 25


def pick_session_size(due_count: int) -> int | None:
    """TUI prompt to pick how many cards to study. Returns None if user cancels."""
    buf = ""
    while True:
        body = (
            f"\n  {due_count} cards due\n\n"
            f"  How many to review? [{DEFAULT_SESSION_SIZE}]: {buf}_"
        )
        draw_review(" Session size", body, " [Enter] Confirm  [q] Cancel")
        k = read_key()
        if k in ("\x03", "\x04", "q"):
            return None
        if k in ("\r", "\n"):
            if buf == "":
                return min(DEFAULT_SESSION_SIZE, due_count)
            if buf.isdigit() and int(buf) > 0:
                return min(int(buf), due_count)
        elif k == "\x7f" and buf:
            buf = buf[:-1]
        elif k.isdigit():
            buf += k


def review(state: dict, deck_id: str | None = None) -> None:
    today = str(date.today())

    due_keys = [
        k
        for k, c in state["cards"].items()
        if c["due"] <= today and (deck_id is None or c["deck_id"] == deck_id)
    ]
    shuffle(due_keys)

    if not due_keys:
        draw_review(" Review", "\n  No cards due!", " [any key] Back")
        read_key()
        return

    session_size = pick_session_size(len(due_keys))
    if session_size is None:
        return
    due_keys = due_keys[:session_size]

    total = len(due_keys)

    for i, key in enumerate(due_keys, 1):
        card = state["cards"][key]
        deck_name = state["decks"].get(card["deck_id"], "?")
        header = f" {deck_name}  —  Card {i}/{total}"

        front, front_sounds = render_card(card, "front")
        footer = (
            " [Space] Reveal  [r] Replay  [q] Quit"
            if front_sounds
            else " [Space] Reveal  [q] Quit"
        )
        draw_review(header, f"\n{front}", footer)
        play_sounds(front_sounds)

        while True:
            k = read_key()
            if k in ("\x03", "\x04", "q"):
                save_state(state)
                return
            if k == "r" and front_sounds:
                play_sounds(front_sounds)
            if k == " ":
                break

        back, back_sounds = render_card(card, "back")
        draw_review(
            header,
            f"\n{back}",
            " (1) Again  (2) Hard  (3) Good  (4) Easy  [q] Quit",
        )
        play_sounds(back_sounds)

        while True:
            k = read_key()
            if k in ("\x03", "\x04", "q"):
                save_state(state)
                return
            if k in ("1", "2", "3", "4"):
                sm2_update(card, int(k) - 1)
                break

    save_state(state)
    draw_review(
        " Review", f"\n  Session complete! Reviewed {total} cards.", " [any key] Back"
    )
    read_key()


def deck_stats(state: dict) -> dict[str, tuple[int, int]]:
    today = str(date.today())
    counts: dict[str, tuple[int, int]] = {}
    for c in state["cards"].values():
        did = c["deck_id"]
        total, due = counts.get(did, (0, 0))
        total += 1
        if c["due"] <= today:
            due += 1
        counts[did] = (total, due)
    return counts


def draw_menu(state: dict, cursor: int) -> None:
    _, cols = term_size()
    counts = deck_stats(state)
    sorted_decks = sorted(state["decks"].items(), key=lambda x: x[1])

    total_due = sum(d for _, d in counts.values())
    num_w = 7
    name_w = max(10, cols - 2 - 3 - num_w - 1 - num_w - 2)
    lines: list[str] = [""]

    marker = ">" if cursor == 0 else " "
    label = f"{marker} Review all due cards"
    lines.append(f"  {label:<{name_w + 3}} {'':>{num_w}} {total_due:>{num_w}}")
    lines.append("")
    lines.append(f"  {'  Deck':<{name_w + 3}} {'Total':>{num_w}} {'Due':>{num_w}}")
    lines.append(f"  {'-' * (name_w + 3 + num_w + 1 + num_w)}")

    for idx, (did, name) in enumerate(sorted_decks):
        total, due = counts.get(did, (0, 0))
        marker = ">" if idx + 1 == cursor else " "
        truncated = name[: name_w - 1]
        lines.append(
            f"  {marker} {truncated:<{name_w}} {total:>{num_w}} {due:>{num_w}}"
        )

    body = "\n".join(lines)
    draw_review(" Anki", body, " [Up/Down] Navigate  [Enter/Space] Review  [q] Quit")


def main_menu() -> None:
    state = sync_decks()

    if not state["decks"]:
        print("No .apkg files found in current directory.")
        return

    sorted_decks = sorted(state["decks"].items(), key=lambda x: x[1])
    max_idx = len(sorted_decks)
    cursor = 0

    while True:
        draw_menu(state, cursor)
        k = read_key()

        if k in ("\x03", "\x04", "q"):
            clear_screen()
            return
        elif k == "\x1b":
            seq = read_key()
            if seq == "[":
                arrow = read_key()
                if arrow == "A":
                    cursor = max(0, cursor - 1)
                elif arrow == "B":
                    cursor = min(max_idx, cursor + 1)
        elif k in (" ", "\r", "\n"):
            if cursor == 0:
                review(state)
            else:
                did = sorted_decks[cursor - 1][0]
                review(state, deck_id=did)
            state = sync_decks()
            sorted_decks = sorted(state["decks"].items(), key=lambda x: x[1])
            max_idx = len(sorted_decks)


def main() -> None:
    main_menu()


if __name__ == "__main__":
    main()
