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

from xml.etree import ElementTree

from PIL import Image, ImageDraw


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


BODY_PADDING = 2
ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def visible_len(text: str) -> int:
    return len(ANSI_RE.sub("", text))


def wrap_body(text: str, cols: int) -> list[str]:
    pad = " " * BODY_PADDING
    usable = cols - BODY_PADDING
    out: list[str] = []
    for line in text.splitlines():
        if visible_len(line) <= usable:
            out.append(pad + line)
        else:
            out.extend(
                pad + w
                for w in (textwrap.wrap(line, usable, break_long_words=True) or [""])
            )
    return out


def draw_review(
    header: str,
    body: str,
    footer: str,
    image: Image.Image | None = None,
) -> None:
    rows, cols = term_size()
    clear_screen()

    sys.stdout.write(f"\033[7m{fit(header, cols)}\033[0m\n")

    body_lines = wrap_body(body, cols)
    for line in body_lines[: rows - 3]:
        sys.stdout.write(line + "\n")

    if image is not None:
        sys.stdout.flush()
        display_image(image)

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

IMG_SRC_RE = re.compile(r'<img[^>]+src="([^"]+)"', re.IGNORECASE)


def extract_images(html: str) -> list[str]:
    return IMG_SRC_RE.findall(html)


def _find_icat() -> str | None:
    import shutil

    return shutil.which("kitten") or shutil.which("icat")


def parse_color(color: str) -> tuple[int, int, int, int]:
    color = color.strip()
    if color.startswith("#"):
        h = color[1:]
        if len(h) == 3:
            h = h[0] * 2 + h[1] * 2 + h[2] * 2
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 255)
    return (0, 0, 0, 255)


SVG_NS = "{http://www.w3.org/2000/svg}"


def render_svg_rects(path: Path) -> Image.Image | None:
    """Rasterize an Image Occlusion SVG (contains only rect elements)."""
    try:
        tree = ElementTree.parse(path)
    except ElementTree.ParseError:
        return None
    root = tree.getroot()
    w = int(float(root.get("width", "0")))
    h = int(float(root.get("height", "0")))
    if w == 0 or h == 0:
        return None
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    for rect in root.iter(f"{SVG_NS}rect"):
        x = float(rect.get("x", "0"))
        y = float(rect.get("y", "0"))
        rw = float(rect.get("width", "0"))
        rh = float(rect.get("height", "0"))
        fill = rect.get("fill", "#000000")
        stroke = rect.get("stroke")
        box = (x, y, x + rw, y + rh)
        draw.rectangle(box, fill=parse_color(fill))
        if stroke:
            draw.rectangle(box, outline=parse_color(stroke), width=2)
    return img


def load_image(filename: str) -> Image.Image | None:
    path = MEDIA_DIR / filename
    if not path.exists():
        return None
    if path.suffix.lower() == ".svg":
        return render_svg_rects(path)
    try:
        return Image.open(path).convert("RGBA")
    except Exception:
        return None


def composite_with_mask(base_img: Image.Image, mask_img: Image.Image) -> Image.Image:
    if mask_img.size != base_img.size:
        mask_img = mask_img.resize(base_img.size, Image.Resampling.LANCZOS)
    result = base_img.copy()
    result.alpha_composite(mask_img)
    return result


def display_image(img: Image.Image) -> None:
    """Display image inline using kitten icat."""
    icat = _find_icat()
    if icat is None:
        return
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        img.save(f, format="PNG")
        tmp_path = f.name
    try:
        cmd = [icat, "icat"] if icat.endswith("kitten") else [icat]
        cmd += ["--align", "left", "--stdin", "no", tmp_path]
        subprocess.run(cmd, timeout=5)
    except Exception:
        pass
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def render_images_for_card(fields: dict[str, str], side: str) -> Image.Image | None:
    """Build composite image for image-occlusion or plain image cards."""
    base_src = None
    mask_src = None

    img_field = fields.get("Image", "")
    if img_field:
        srcs = extract_images(img_field)
        if srcs:
            base_src = srcs[0]

    if base_src:
        mask_field_name = "Question Mask" if side == "front" else "Answer Mask"
        mask_html = fields.get(mask_field_name, "")
        mask_srcs = extract_images(mask_html)
        if mask_srcs:
            mask_src = mask_srcs[0]

    if base_src is None:
        all_srcs: list[str] = []
        for v in fields.values():
            all_srcs.extend(extract_images(v))
        if all_srcs:
            base_src = all_srcs[0]

    if base_src is None:
        return None

    base_img = load_image(base_src)
    if base_img is None:
        return None

    if mask_src:
        mask_img = load_image(mask_src)
        if mask_img:
            return composite_with_mask(base_img, mask_img)

    return base_img


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


BOLD = "\033[1m"
UNDERLINE = "\033[4m"
RESET = "\033[0m"
DIM = "\033[2m"


def resolve_cloze(text: str, ordinal: int, reveal: bool) -> str:
    """Resolve cloze deletions. When reveal=False, replace the target cloze with [...].
    When reveal=True, show the answer highlighted. Non-target clozes are always revealed."""
    target = ordinal + 1

    def replacer(m: re.Match[str]) -> str:
        cloze_num = int(m.group(1))
        answer = m.group(2)
        hint = m.group(3)
        if cloze_num == target:
            if reveal:
                return f"{BOLD}{UNDERLINE}[{answer}]{RESET}"
            return f"{BOLD}[{hint or '...'}]{RESET}"
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


def field_has_content(html: str) -> bool:
    if strip_html(html).strip():
        return True
    return bool(IMG_SRC_RE.search(html))


COND_POS_RE = re.compile(r"\{\{#(.+?)\}\}(.*?)\{\{/\1\}\}", re.DOTALL)
COND_NEG_RE = re.compile(r"\{\{\^(.+?)\}\}(.*?)\{\{/\1\}\}", re.DOTALL)


def render_template(template: str, fields: dict[str, str]) -> str:
    """Process Anki Mustache-like templates: conditionals and field substitution."""
    text = template
    for _ in range(10):
        new = COND_POS_RE.sub(
            lambda m: (
                m.group(2) if field_has_content(fields.get(m.group(1), "")) else ""
            ),
            text,
        )
        new = COND_NEG_RE.sub(
            lambda m: (
                "" if field_has_content(fields.get(m.group(1), "")) else m.group(2)
            ),
            new,
        )
        if new == text:
            break
        text = new

    def replacer(m: re.Match[str]) -> str:
        name = m.group(1)
        if name.startswith("cloze:"):
            field_name = name[len("cloze:") :]
            return fields.get(field_name, "")
        if name.startswith("type:"):
            return ""
        return fields.get(name, "")

    return re.sub(r"\{\{([^#/^}]+?)\}\}", replacer, text)


def render_card(card: dict, side: str) -> tuple[str, list[str], Image.Image | None]:
    tmpl = card["template_front"] if side == "front" else card["template_back"]
    text = render_template(tmpl, card["fields"])

    if card["is_cloze"]:
        text = resolve_cloze(text, card["cloze_ord"], reveal=(side == "back"))

    text = re.sub(r"\{\{FrontSide\}\}", "", text)

    img = render_images_for_card(card["fields"], side)

    text = strip_html(text)
    sounds = re.findall(r"\[sound:([^\]]+)\]", text)
    text = re.sub(r"\[sound:[^\]]+\]", "", text)
    lines = [line.strip() for line in text.splitlines()]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip(), sounds, img


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


def format_interval(days: int) -> str:
    if days == 0:
        return "<1d"
    if days < 30:
        return f"{days}d"
    if days < 365:
        months = days / 30.4
        return f"{months:.1f}mo"
    years = days / 365.25
    return f"{years:.1f}y"


def preview_intervals(card: dict) -> str:
    """Show what each grade would give as the next interval."""
    labels = []
    for quality, name in enumerate(["Again", "Hard", "Good", "Easy"]):
        c = dict(card)
        sm2_update(c, quality)
        interval = c["interval"]
        labels.append(f"({quality + 1}) {name}: {format_interval(interval)}")
    return "  ".join(labels)


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


def pick_uniform(due_by_deck: dict[str, list[str]], total: int) -> list[str]:
    """Pick up to `total` cards, spreading evenly across decks."""
    for keys in due_by_deck.values():
        shuffle(keys)
    remaining = {d: list(keys) for d, keys in due_by_deck.items() if keys}
    picked: list[str] = []
    while remaining and len(picked) < total:
        per_deck = max(1, (total - len(picked)) // len(remaining))
        exhausted: list[str] = []
        for d, keys in remaining.items():
            take = min(per_deck, len(keys), total - len(picked))
            picked.extend(keys[:take])
            del keys[:take]
            if not keys:
                exhausted.append(d)
        for d in exhausted:
            del remaining[d]
    shuffle(picked)
    return picked


def review(state: dict, deck_id: str | None = None) -> None:
    today = str(date.today())

    due_keys = [
        k
        for k, c in state["cards"].items()
        if c["due"] <= today and (deck_id is None or c["deck_id"] == deck_id)
    ]

    if not due_keys:
        draw_review(" Review", "\n  No cards due!", " [any key] Back")
        read_key()
        return

    session_size = pick_session_size(len(due_keys))
    if session_size is None:
        return

    if deck_id is not None:
        shuffle(due_keys)
        due_keys = due_keys[:session_size]
    else:
        due_by_deck: dict[str, list[str]] = {}
        for k in due_keys:
            d = state["cards"][k]["deck_id"]
            due_by_deck.setdefault(d, []).append(k)
        due_keys = pick_uniform(due_by_deck, session_size)

    total = len(due_keys)

    for i, key in enumerate(due_keys, 1):
        card = state["cards"][key]
        deck_name = state["decks"].get(card["deck_id"], "?")
        header = f" {deck_name}  —  Card {i}/{total}"

        front, front_sounds, front_img = render_card(card, "front")
        front_body = f"\n{DIM}Question{RESET}\n\n{front}"
        footer = (
            " [Space] Reveal  [r] Replay  [q] Quit"
            if front_sounds
            else " [Space] Reveal  [q] Quit"
        )
        draw_review(header, front_body, footer, image=front_img)
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

        _, cols = term_size()
        separator = f"\n{'─' * (cols - BODY_PADDING * 2)}\n"
        back, back_sounds, back_img = render_card(card, "back")
        back_body = f"\n{DIM}Question{RESET}\n\n{front}{separator}\n{DIM}Answer{RESET}\n\n{back}"
        intervals = preview_intervals(card)
        draw_review(header, back_body, f" {intervals}  [q] Quit", image=back_img)
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
