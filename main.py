"""Image Renamer - rename images in a folder using a pasted list of names.

Only dependency: Pillow (imported lazily, only when a preview is shown).
"""
import hashlib
import io
import json
import os
import re
import shutil
import threading
import tkinter as tk
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

try:  # optional: modern Windows 11 look (pip install sv-ttk)
    import sv_ttk
except ImportError:
    sv_ttk = None

CONFIG_FILE = Path.home() / ".image_renamer.json"
CACHE_DIR = Path.home() / ".image_renamer_cache"
EXTENSIONS = (".png", ".jpg", ".jpeg")
PREVIEW_SIZE = (1600, 1600)  # cached source; scaled down to the window on demand
_NON_ALNUM = re.compile(r"[^a-z0-9]")


def sanitize(name: str) -> str:
    return _NON_ALNUM.sub("_", name.strip().lower())


def load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict) -> None:
    try:
        CONFIG_FILE.write_text(json.dumps(cfg))
    except OSError:
        pass


def list_images(directory: str) -> list[str]:
    with os.scandir(directory) as it:
        return sorted(
            (e.name for e in it if e.is_file() and e.name.lower().endswith(EXTENSIONS)),
            key=str.lower,
        )


def plan_renames(directory: str, images: list[str], names: list[str],
                 target_dir: str = "") -> list[tuple[str, str]]:
    """Return (old, new) pairs with unique targets (case-insensitive, like Windows).

    With target_dir (export mode) names are made unique against that folder's contents;
    otherwise against `directory`, ignoring the files that are about to be renamed.
    """
    if target_dir:
        taken = {n.lower() for n in os.listdir(target_dir)}
    else:
        taken = {n.lower() for n in os.listdir(directory)} - {i.lower() for i in images}
    plan = []
    for i, old in enumerate(images):
        stem, ext = os.path.splitext(old)
        base = sanitize(names[i]) if i < len(names) and names[i].strip() else sanitize(stem)
        new, n = base + ext, 0
        while new.lower() in taken:
            n += 1
            new = f"{base}_{n}{ext}"
        taken.add(new.lower())
        plan.append((old, new))
    return plan


def _encode(src_path: str, scale: int, quality: int, keep_meta: bool, fp) -> None:
    """Encode src_path into fp (a path or file object) with the given settings.

    scale   (1-100): output dimensions as a percent of the original.
    quality (1-100): JPEG quality; for PNG, palette size (quality% of 256 colours).
    100/100 keeps pixels untouched (JPEG reuses its quantization; PNG is lossless).
    keep_meta=False drops EXIF/ICC/XMP/text (orientation is baked into the pixels first).
    """
    from PIL import Image, ImageOps, PngImagePlugin

    with Image.open(src_path) as src:
        fmt = src.format
        if fmt not in ("JPEG", "PNG"):
            raise ValueError(f"unsupported format {fmt}")
        info = dict(src.info)
        text = dict(getattr(src, "text", {}))
        img, changed = src, False
        if not keep_meta and src.getexif().get(0x0112, 1) != 1:
            img, changed = ImageOps.exif_transpose(src), True
        if scale < 100:
            size = (max(1, img.width * scale // 100), max(1, img.height * scale // 100))
            img, changed = img.resize(size, Image.LANCZOS), True

        if fmt == "JPEG":
            kw = dict(format="JPEG", optimize=True)
            if quality >= 100 and not changed:
                kw.update(quality="keep", subsampling="keep")
            else:
                kw["quality"] = 95 if quality >= 100 else quality
            if info.get("progressive") or info.get("progression"):
                kw["progressive"] = True
            keys = ("exif", "icc_profile", "dpi", "xmp") if keep_meta else ()
        else:
            kw = dict(format="PNG", optimize=True, compress_level=9)
            keys = ("exif", "icc_profile", "dpi") if keep_meta else ()
            if quality < 100:
                alpha = "A" in img.getbands() or "transparency" in info
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGBA" if alpha else "RGB")
                colors = max(2, round(256 * quality / 100))
                img = img.quantize(
                    colors, method=Image.FASTOCTREE if img.mode == "RGBA" else Image.MEDIANCUT
                )
            elif info.get("transparency") is not None:
                kw["transparency"] = info["transparency"]
            if keep_meta:
                pnginfo = PngImagePlugin.PngInfo()
                for k, v in text.items():
                    pnginfo.add_text(k, v)
                kw["pnginfo"] = pnginfo
        for k in keys:
            if info.get(k):
                kw[k] = info[k]
        img.save(fp, **kw)


def estimate_size(src_path: str, scale: int, quality: int, keep_meta: bool) -> int:
    buf = io.BytesIO()
    _encode(src_path, scale, quality, keep_meta, buf)
    return buf.tell()


def process_image(src: str, dst: str, scale: int, quality: int, keep_meta: bool) -> int:
    """Compress src into dst (dst == src means in place). Returns bytes saved.

    The result is only kept if it is smaller than the original; when exporting, the
    original is copied instead so dst always exists. Never leaves a half-written file.
    """
    in_place = os.path.abspath(src) == os.path.abspath(dst)
    tmp = dst + ".compress.tmp"
    try:
        try:
            _encode(src, scale, quality, keep_meta, tmp)
        except ValueError:  # not really a JPEG/PNG: leave it alone
            if not in_place:
                shutil.copy2(src, dst)
            return 0
        old, new = os.path.getsize(src), os.path.getsize(tmp)
        if new < old:
            os.replace(tmp, dst)
            return old - new
        if not in_place:
            shutil.copy2(src, dst)
        return 0
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


# ---------- preview cache (on disk, keyed by path + mtime + size) ----------
def cache_path(path: str) -> Path:
    st = os.stat(path)
    key = f"{path}|{st.st_mtime_ns}|{st.st_size}".encode()
    return CACHE_DIR / (hashlib.sha1(key).hexdigest() + ".webp")


def load_preview(path: str, use_cache: bool):
    """Return a PIL image at most PREVIEW_SIZE, from the disk cache when possible."""
    from PIL import Image, ImageOps

    cp = cache_path(path) if use_cache else None
    if cp and cp.exists():
        try:
            with Image.open(cp) as c:
                c.load()
                return c.copy()
        except Exception:
            pass  # corrupt cache entry: rebuild below
    with Image.open(path) as img:
        img.draft("RGB", PREVIEW_SIZE)  # fast JPEG downscale on decode
        img = ImageOps.exif_transpose(img)
        img.thumbnail(PREVIEW_SIZE)
        out = img.convert("RGBA" if "A" in img.getbands() else "RGB")
    if cp:
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            out.save(cp, "WEBP", quality=85)
        except Exception:
            pass
    return out


def ensure_cached(path: str) -> None:
    if not cache_path(path).exists():
        load_preview(path, True)


PALETTES = {
    "light": dict(bg="#f3f3f3", card="#ffffff", fg="#1f1f1f", muted="#6b6b6b", border="#d0d0d0",
                  accent="#0067c0", accent_hover="#005a9e", btn="#e8e8e8", btn_hover="#dadada",
                  hl="#fff1a8", sel="#cce4f7"),
    "dark": dict(bg="#1f1f1f", card="#2b2b2b", fg="#f0f0f0", muted="#a0a0a0", border="#444444",
                 accent="#3a8fe0", accent_hover="#5aa3ea", btn="#363636", btn_hover="#444444",
                 hl="#6b5d00", sel="#264f78"),
}
# Colours of the plain tk widgets (Text/Listbox/Canvas) so they match sv-ttk's themes.
SV_PALETTES = {
    "light": dict(bg="#fafafa", card="#ffffff", fg="#1c1c1c", muted="#6e6e6e", border="#d9d9d9",
                  accent="#005fb8", hl="#fff1a8", sel="#cce4f7"),
    "dark": dict(bg="#1c1c1c", card="#2b2b2b", fg="#fafafa", muted="#a0a0a0", border="#3d3d3d",
                 accent="#57c8ff", hl="#6b5d00", sel="#264f78"),
}
FONT = ("Segoe UI", 10)
FONT_BOLD = ("Segoe UI Semibold", 10)


def system_theme() -> str:
    try:
        import winreg

        key = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            return "light" if winreg.QueryValueEx(k, "AppsUseLightTheme")[0] else "dark"
    except Exception:
        return "light"


class Slider(ttk.Frame):
    """Label  [-] ----o---- [+]  value%   (1-100, +/- step by 5)."""

    def __init__(self, parent, label: str, value: int, on_change):
        super().__init__(parent)
        self._cb = on_change
        self.var = tk.DoubleVar(value=value)
        ttk.Label(self, text=label, width=8).pack(side="left")
        ttk.Button(self, text="−", width=2, style="Tool.TButton",
                   command=lambda: self.step(-5)).pack(side="left")
        ttk.Scale(self, from_=1, to=100, variable=self.var, length=150,
                  command=self._moved).pack(side="left", padx=8)
        ttk.Button(self, text="+", width=2, style="Tool.TButton",
                   command=lambda: self.step(5)).pack(side="left")
        self.val = ttk.Label(self, width=5, anchor="e", font=FONT_BOLD)
        self.val.pack(side="left", padx=(6, 0))
        self._show()

    def get(self) -> int:
        return round(self.var.get())

    def set(self, v: int) -> None:
        self.var.set(v)
        self._show()

    def step(self, d: int) -> None:
        self.set(min(100, max(1, self.get() + d)))
        self._cb()

    def _moved(self, _v) -> None:
        self._show()
        self._cb()

    def _show(self) -> None:
        self.val.config(text=f"{self.get()}%")


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Image Renamer")
        self.geometry("860x780")
        self.minsize(720, 600)
        self.images: list[str] = []
        self._photo = None
        self._src_img = None
        self._resize_job = None
        self._pool = None
        self._sel_path = None
        self._est_job = None
        self._est_gen = 0
        self._est_result = None
        self._est_head = ""
        self._job_running = False
        self._cfg = load_config()
        self._theme = self._cfg.get("theme") or system_theme()
        self._build()
        self._apply_theme(self._theme)

    # ---------- UI ----------
    def _build(self):
        pad = dict(padx=14)

        head = ttk.Frame(self)
        head.pack(fill="x", pady=(14, 8), **pad)
        head.columnconfigure((0, 1), weight=1, uniform="head")

        # left column: source + output folders
        folders = ttk.LabelFrame(head, text="Folders", padding=(12, 8))
        folders.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        folders.columnconfigure(1, weight=1)
        ttk.Label(folders, text="Source").grid(row=0, column=0, sticky="w", pady=(0, 6))
        self.dir_var = tk.StringVar(value=self._cfg.get("last_directory", ""))
        ttk.Entry(folders, textvariable=self.dir_var).grid(row=0, column=1, sticky="ew", padx=8, pady=(0, 6))
        ttk.Button(folders, text="Browse\u2026", command=self.browse).grid(row=0, column=2, pady=(0, 6))
        ttk.Label(folders, text="Output").grid(row=1, column=0, sticky="w")
        self.out_var = tk.StringVar()
        ttk.Entry(folders, textvariable=self.out_var).grid(row=1, column=1, sticky="ew", padx=8)
        ttk.Button(folders, text="Browse\u2026", command=self.browse_out).grid(row=1, column=2)
        ttk.Label(folders, text="Output empty = process in place.\nRelative paths start from the source folder.",
                  style="Muted.TLabel", justify="left").grid(row=2, column=1, columnspan=2, sticky="w", padx=8, pady=(8, 0))

        # right column: compression settings
        box = ttk.LabelFrame(head, text="Compression", padding=(12, 8))
        box.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        self.scale = Slider(box, "Size", self._cfg.get("scale", 100), self._on_params_changed)
        self.scale.pack(anchor="w")
        self.quality = Slider(box, "Quality", self._cfg.get("quality", 100), self._on_params_changed)
        self.quality.pack(anchor="w", pady=(4, 0))
        flags = ttk.Frame(box)
        flags.pack(fill="x", pady=(8, 0))
        self.keep_meta = tk.BooleanVar(value=self._cfg.get("keep_meta", True))
        ttk.Checkbutton(flags, text="Keep metadata", variable=self.keep_meta,
                        command=self._on_params_changed).pack(side="left")
        self.use_cache = tk.BooleanVar(value=self._cfg.get("cache", False))
        ttk.Checkbutton(flags, text="Cache previews", variable=self.use_cache,
                        command=self._on_cache_toggle).pack(side="left", padx=20)

        # ---- action bar: estimate on the left, buttons on the right
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 8), **pad)
        self.est_lbl = ttk.Label(bar, style="Muted.TLabel")
        self.est_lbl.pack(side="left")
        self.btns = [  # packed right-to-left, so the primary action ends up rightmost
            ttk.Button(bar, text="Rename & Compress", style="Accent.TButton", command=self.rename_compress),
            ttk.Button(bar, text="Compress", command=self.compress),
            ttk.Button(bar, text="Rename", command=self.rename),
            ttk.Button(bar, text="Load", command=self.load),
        ]
        for b in self.btns:
            b.pack(side="right", padx=(8, 0))
        self._set_est("Select an image to see the result size", muted=True)

        # ---- status bar (cache + job progress)
        stat = ttk.Frame(self)
        stat.pack(side="bottom", fill="x", pady=(0, 10), **pad)
        self.status = ttk.Label(stat, text="Ready", style="Muted.TLabel", width=34, anchor="w")
        self.status.pack(side="left")
        self.progress = ttk.Progressbar(stat, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True)
        self.theme_btn = ttk.Button(stat, width=6, command=self._toggle_theme)
        self.theme_btn.pack(side="left", padx=(10, 0))

        # ---- draggable divider between the lists and the preview
        self.paned = tk.PanedWindow(self, orient="vertical", sashwidth=7, sashrelief="flat",
                                    bd=0, sashcursor="sb_v_double_arrow")
        self.paned.pack(fill="both", expand=True, padx=14, pady=(0, 8))

        mid = ttk.Frame(self.paned)
        mid.columnconfigure((0, 1), weight=1, uniform="c")
        mid.rowconfigure(1, weight=1)

        self.h_names = ttk.Label(mid, text="Filenames (0)", font=FONT_BOLD)
        self.h_names.grid(row=0, column=0, pady=(0, 4))
        self.h_images = ttk.Label(mid, text="Images (0)", font=FONT_BOLD)
        self.h_images.grid(row=0, column=1, pady=(0, 4))

        # VS Code style editor: line-number gutter + text in one bordered frame
        self.editor = tk.Frame(mid, bd=0, highlightthickness=1)
        self.editor.grid(row=1, column=0, sticky="nsew", padx=(0, 6))
        self.gutter = tk.Text(self.editor, width=4, font=FONT, bd=0, padx=8, pady=4, wrap="none",
                              takefocus=0, cursor="arrow", highlightthickness=0, state="disabled")
        self.gutter.pack(side="left", fill="y")
        self.gutter.tag_configure("r", justify="right")
        for ev in ("<Button-1>", "<B1-Motion>", "<Double-Button-1>"):
            self.gutter.bind(ev, lambda e: "break")
        self.gutter.bind("<MouseWheel>", lambda e: (
            self.text.yview_scroll(-1 if e.delta > 0 else 1, "units"), "break")[1])
        self.text = tk.Text(self.editor, width=26, height=8, wrap="none", undo=True, font=FONT,
                            bd=0, padx=6, pady=4, highlightthickness=0)
        self.text.pack(side="left", fill="both", expand=True)
        self._gutter_n = 0
        self._hook_text_changes()
        self.listbox = tk.Listbox(mid, width=30, height=8, exportselection=False, font=FONT,
                                  bd=0, activestyle="none")
        self.listbox.grid(row=1, column=1, sticky="nsew")
        self.scroll = ttk.Scrollbar(mid, command=self._yview)
        self.scroll.grid(row=1, column=2, sticky="ns")
        self.text.config(yscrollcommand=lambda a, b: self._on_scroll(self.text, a, b))
        self.listbox.config(yscrollcommand=lambda a, b: self._on_scroll(self.listbox, a, b))
        self.text.bind("<KeyRelease>", lambda e: self._update_gutter())
        self.text.bind("<ButtonRelease-1>", lambda e: self._update_gutter())
        self.listbox.bind("<<ListboxSelect>>", self.show_selected)

        # Canvas doesn't resize to its content, so the preview can track its pane.
        self.preview = tk.Canvas(self.paned, height=240, bd=0)
        self.preview.bind("<Configure>", self._on_preview_resize)

        self.paned.add(mid, minsize=120, stretch="always")
        self.paned.add(self.preview, minsize=120, stretch="always")

    # ---------- theming (built-in ttk 'clam' theme, restyled) ----------
    def _apply_theme(self, mode: str):
        self._theme = mode
        p = self._pal = (SV_PALETTES if sv_ttk else PALETTES)[mode]
        self.configure(bg=p["bg"])
        if sv_ttk:
            sv_ttk.set_theme(mode)
            st = ttk.Style(self)
            st.configure("Muted.TLabel", foreground=p["muted"])
            st.configure("Tool.TButton", padding=(0, 3))
        else:  # built-in 'clam' theme, restyled to look flat
            st = ttk.Style(self)
            st.theme_use("clam")
            st.configure(".", background=p["bg"], foreground=p["fg"], font=FONT, bordercolor=p["border"],
                         lightcolor=p["bg"], darkcolor=p["bg"], troughcolor=p["border"],
                         focuscolor=p["bg"], insertcolor=p["fg"])
            st.configure("Muted.TLabel", foreground=p["muted"])
            st.configure("TLabelframe", bordercolor=p["border"], lightcolor=p["bg"], darkcolor=p["bg"])
            st.configure("TLabelframe.Label", foreground=p["muted"], font=("Segoe UI Semibold", 9))
            for name, bg, hover in (("TButton", p["btn"], p["btn_hover"]),
                                    ("Accent.TButton", p["accent"], p["accent_hover"])):
                st.configure(name, background=bg, bordercolor=bg, lightcolor=bg, darkcolor=bg,
                             foreground="#ffffff" if name.startswith("Accent") else p["fg"],
                             padding=(14, 6), relief="flat", font=FONT)
                st.map(name, background=[("disabled", p["btn"]), ("pressed", hover), ("active", hover)],
                       bordercolor=[("active", hover)], lightcolor=[("active", hover)],
                       darkcolor=[("active", hover)], foreground=[("disabled", p["muted"])])
            st.configure("Tool.TButton", padding=(0, 3))
            st.configure("TEntry", fieldbackground=p["card"], foreground=p["fg"], padding=6,
                         bordercolor=p["border"], lightcolor=p["border"], darkcolor=p["border"])
            st.map("TEntry", bordercolor=[("focus", p["accent"])], lightcolor=[("focus", p["accent"])],
                   darkcolor=[("focus", p["accent"])])
            st.configure("TCheckbutton", indicatorbackground=p["card"], indicatorforeground="#ffffff",
                         indicatormargin=(0, 0, 6, 0))
            st.map("TCheckbutton", indicatorbackground=[("selected", p["accent"])],
                   background=[("active", p["bg"])])
            st.configure("Horizontal.TScale", background=p["accent"], troughcolor=p["border"],
                         bordercolor=p["bg"], lightcolor=p["accent"], darkcolor=p["accent"])
            st.configure("Horizontal.TProgressbar", background=p["accent"], troughcolor=p["border"],
                         bordercolor=p["bg"], lightcolor=p["accent"], darkcolor=p["accent"], thickness=8)
            st.configure("Vertical.TScrollbar", background=p["btn"], troughcolor=p["bg"],
                         bordercolor=p["bg"], arrowcolor=p["fg"], lightcolor=p["btn"], darkcolor=p["btn"])
            st.map("Vertical.TScrollbar", background=[("active", p["btn_hover"])])

        # plain tk widgets don't follow ttk styles
        self.editor.config(bg=p["card"], highlightbackground=p["border"], highlightthickness=1)
        self.listbox.config(bg=p["card"], fg=p["fg"], selectbackground=p["sel"], selectforeground=p["fg"],
                            highlightthickness=1, highlightbackground=p["border"],
                            highlightcolor=p["accent"], relief="flat")
        self.text.config(bg=p["card"], fg=p["fg"], selectbackground=p["sel"], selectforeground=p["fg"],
                         insertbackground=p["fg"])
        self.gutter.config(bg=p["card"], fg=p["muted"], selectbackground=p["card"])
        self.gutter.tag_configure("cur", foreground=p["fg"])
        self.text.tag_configure("hl", background=p["hl"])
        self.preview.config(bg=p["card"], highlightthickness=1, highlightbackground=p["border"])
        self.paned.config(bg=p["border"])
        self.theme_btn.config(text="Light" if mode == "dark" else "Dark")
        self._draw_preview()

    def _toggle_theme(self):
        self._apply_theme("light" if self._theme == "dark" else "dark")
        self._cfg["theme"] = self._theme
        save_config(self._cfg)

    def _set_est(self, text: str, muted: bool = False):
        self.est_lbl.config(text=text, style="Muted.TLabel" if muted else "TLabel")

    # ---------- synced scrolling ----------
    def _hook_text_changes(self):
        """Refresh counts + line numbers after any edit (typing, paste, cut, undo...)
        using Tk's <<Modified>> event; the flag is reset so the next edit fires again."""
        def on_modified(_e):
            if self.text.edit_modified():
                self.text.edit_modified(False)
                self._update_counts()

        self.text.bind("<<Modified>>", on_modified)

    def _update_gutter(self):
        n = int(self.text.index("end-1c").split(".")[0])
        g = self.gutter
        if n != self._gutter_n:
            self._gutter_n = n
            g.config(state="normal", width=max(3, len(str(n))) + 1)
            g.delete("1.0", "end")
            g.insert("1.0", "\n".join(str(i) for i in range(1, n + 1)), "r")
            g.config(state="disabled")
        g.yview_moveto(self.text.yview()[0])
        g.tag_remove("cur", "1.0", "end")  # brighten the active line's number
        line = self.text.index("insert").split(".")[0]
        g.tag_add("cur", f"{line}.0", f"{line}.end")

    def _yview(self, *args):
        self.text.yview(*args)
        self.listbox.yview(*args)
        self.gutter.yview(*args)

    def _on_scroll(self, source, first, last):
        self.scroll.set(first, last)
        for other in (self.listbox, self.text):
            if other is not source and other.yview()[0] != float(first):
                other.yview_moveto(first)
        self.gutter.yview_moveto(first)

    # ---------- actions ----------
    def _names(self) -> list[str]:
        raw = self.text.get("1.0", "end-1c")
        return raw.split("\n") if raw else []

    def _update_counts(self):
        n = sum(1 for line in self._names() if line.strip())
        self.h_names.config(text=f"Filenames ({n})")
        self._update_gutter()

    def _valid_dir(self) -> bool:
        if os.path.isdir(self.dir_var.get()):
            return True
        messagebox.showerror("Error", "Please select a valid directory.")
        return False

    def browse(self):
        d = filedialog.askdirectory(initialdir=self.dir_var.get() or None)
        if d:
            self.dir_var.set(d)
            self._cfg["last_directory"] = d
            save_config(self._cfg)
            self.load()

    def load(self):
        if not self._valid_dir():
            return
        self.images = list_images(self.dir_var.get())
        self.h_images.config(text=f"Images ({len(self.images)})")
        self._update_counts()
        self.listbox.delete(0, "end")
        self.listbox.insert("end", *self.images)
        self.text.tag_remove("hl", "1.0", "end")
        self._src_img = None
        self._sel_path = None
        self._set_est("Select an image to see the result size", muted=True)
        self._draw_preview()
        if self.use_cache.get() and not self._job_running:
            self._start_cache()

    # ---------- preview cache ----------
    def _on_cache_toggle(self):
        self._cfg["cache"] = self.use_cache.get()
        save_config(self._cfg)
        if self._job_running:
            return
        if self.use_cache.get() and self.images:
            self._start_cache()
        else:
            self._stop_cache()

    def _reset_progress(self):
        self.progress.config(value=0)
        self.status.config(text="Ready")

    def _stop_cache(self):
        if self._pool:
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None
            self._reset_progress()

    def _start_cache(self):
        self._stop_cache()
        d = self.dir_var.get()
        paths = [os.path.join(d, n) for n in self.images]
        if not paths:
            return
        self._cache_total, self._cache_done = len(paths), 0
        lock = threading.Lock()

        def work(p):
            try:
                ensure_cached(p)
            except Exception:
                pass  # unreadable image: preview will report it on demand
            with lock:
                self._cache_done += 1

        self._pool = pool = ThreadPoolExecutor(max_workers=4)
        for p in paths:
            pool.submit(work, p)
        self.progress.config(maximum=len(paths), value=0)
        self._poll_cache(pool)

    def _poll_cache(self, pool):
        if pool is not self._pool:  # superseded or cancelled
            return
        done = self._cache_done
        self.progress.config(value=done)
        self.status.config(text=f"Caching previews  {done}/{self._cache_total}")
        if done >= self._cache_total:
            self._stop_cache()
        else:
            self.after(100, self._poll_cache, pool)

    # ---------- dynamic preview ----------
    def _on_preview_resize(self, _event=None):
        if self._resize_job:
            self.after_cancel(self._resize_job)
        self._resize_job = self.after(40, self._draw_preview)  # debounce

    def _draw_preview(self, message: str = ""):
        self._resize_job = None
        c = self.preview
        c.delete("all")
        self._photo = None
        w, h = c.winfo_width(), c.winfo_height()
        if self._src_img is not None and w > 1 and h > 1:
            from PIL import Image, ImageTk

            img = self._src_img.copy()
            img.thumbnail((w - 4, h - 4), Image.LANCZOS)
            self._photo = ImageTk.PhotoImage(img)
            c.create_image(w // 2, h // 2, image=self._photo)
        else:
            c.create_text(w // 2, h // 2, fill=self._pal["muted"], font=FONT,
                          text=message or "Select an image to preview it")

    def show_selected(self, _event=None):
        sel = self.listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        self.text.tag_remove("hl", "1.0", "end")
        if idx < int(self.text.index("end-1c").split(".")[0]):
            self.text.tag_add("hl", f"{idx + 1}.0", f"{idx + 1}.end")
            self.text.see(f"{idx + 1}.0")
        try:
            self._sel_path = os.path.join(self.dir_var.get(), self.images[idx])
            self._on_params_changed()
            self._src_img = load_preview(self._sel_path, self.use_cache.get())
            self._draw_preview()
        except Exception as e:  # unreadable/corrupt image or missing Pillow
            self._src_img = None
            self._draw_preview(f"Preview failed: {e}")

    # ---------- result-size estimate ----------
    def _on_params_changed(self):
        """Show the new dimensions at once; estimate the file size shortly after."""
        if not self._sel_path:
            return
        sc = self.scale.get()
        try:
            from PIL import Image

            with Image.open(self._sel_path) as im:
                w, h = im.size
            orig = os.path.getsize(self._sel_path)
        except Exception:
            return
        self._est_head = (f"{w}×{h} → {max(1, w * sc // 100)}×{max(1, h * sc // 100)} px"
                          f"    {human(orig)} → ")
        self._set_est(self._est_head + "…")
        if self._est_job:
            self.after_cancel(self._est_job)
        self._est_job = self.after(300, self._run_estimate)

    def _run_estimate(self):
        self._est_job = None
        self._est_gen += 1
        gen, path = self._est_gen, self._sel_path
        args = (self.scale.get(), self.quality.get(), self.keep_meta.get())

        def work():
            try:
                self._est_result = (gen, estimate_size(path, *args))
            except Exception:
                self._est_result = (gen, None)

        threading.Thread(target=work, daemon=True).start()
        self.after(100, self._poll_estimate, gen)

    def _poll_estimate(self, gen):
        if gen != self._est_gen:
            return  # superseded by a newer adjustment
        r = self._est_result
        if r and r[0] == gen:
            self._set_est(self._est_head + ("~" + human(r[1]) if r[1] is not None else "n/a"))
        else:
            self.after(100, self._poll_estimate, gen)

    # ---------- output folder ----------
    def browse_out(self):
        d = filedialog.askdirectory(initialdir=self.out_var.get() or self.dir_var.get() or None)
        if d:
            self.out_var.set(os.path.normpath(d))

    def _out_dir(self):
        """Empty string for in place, a ready absolute folder for export, None if unusable.

        A relative path (like .\\OUT) is taken relative to the SOURCE folder.
        """
        out = self.out_var.get().strip()
        if not out:
            return ""
        src = os.path.abspath(self.dir_var.get())
        out = os.path.abspath(os.path.join(src, os.path.expanduser(out)))
        if os.path.normcase(out) == os.path.normcase(src):
            return ""
        try:
            os.makedirs(out, exist_ok=True)
        except OSError as e:
            messagebox.showerror("Error", f"Output folder unusable: {e}")
            return None
        self.out_var.set(out)  # show where it really is
        return out

    # ---------- background jobs with progress ----------
    def _set_busy(self, busy: bool):
        self._job_running = busy
        for b in self.btns:
            b.config(state="disabled" if busy else "normal")
        self.config(cursor="watch" if busy else "")

    def _run_job(self, label: str, tasks: list, on_done):
        """tasks: [(name, callable returning bytes saved)]; runs off the UI thread."""
        self._stop_cache()
        self._set_busy(True)
        job = dict(done=0, total=len(tasks), saved=0, failed=[])

        def work():
            for name, fn in tasks:
                try:
                    job["saved"] += fn() or 0
                except Exception as e:
                    job["failed"].append(f"{name}: {e}")
                job["done"] += 1

        t = threading.Thread(target=work, daemon=True)
        t.start()
        self.progress.config(maximum=max(1, len(tasks)), value=0)
        self._poll_job(t, label, job, on_done)

    def _poll_job(self, t, label, job, on_done):
        self.progress.config(value=job["done"])
        self.status.config(text=f"{label}  {job['done']}/{job['total']}")
        if t.is_alive():
            self.after(80, self._poll_job, t, label, job, on_done)
            return
        self._set_busy(False)
        self._reset_progress()
        on_done(job)

    def _finish(self, title: str, msg: str, job: dict, out: str):
        if job["failed"]:
            msg += f"\n\n{len(job['failed'])} failed:\n" + "\n".join(job["failed"][:10])
        if out:
            if messagebox.askyesno(title, msg + f"\n\nOutput folder:\n{out}\n\nOpen it now?"):
                os.startfile(out)
        else:
            messagebox.showinfo(title, msg)

    # ---------- compress / rename ----------
    def _mode_text(self, scale, quality):
        return "lossless" if scale == quality == 100 else f"size {scale}%, quality {quality}%"

    def _confirm(self, title, n, what, out, keep, scale, quality) -> bool:
        where = (f"export to:\n{out}\n(same-named files there are overwritten)" if out
                 else "OVERWRITE the originals in place")
        return messagebox.askyesno(
            title,
            f"{what} {n} images ({self._mode_text(scale, quality)}) and {where}?\n\n"
            f"Metadata will be {'kept' if keep else 'REMOVED'}. "
            "Files that don't get smaller are left as they are.",
        )

    def _compress_done(self, title, out):
        def done(job):
            if not out and self.use_cache.get():
                self._start_cache()  # files changed on disk; cache entries are keyed by mtime
            self._on_params_changed()
            self._finish(title, f"Processed {job['done'] - len(job['failed'])} images, "
                         f"saved {human(job['saved'])}.", job, out)
        return done

    def compress(self):
        if not self._valid_dir():
            return
        if not self.images:
            messagebox.showinfo("Nothing to do", "Load a folder with images first.")
            return
        out = self._out_dir()
        if out is None:
            return
        scale, quality, keep = self.scale.get(), self.quality.get(), self.keep_meta.get()
        if not self._confirm("Compress", len(self.images), "Compress", out, keep, scale, quality):
            return
        self._cfg.update(scale=scale, quality=quality, keep_meta=keep)
        save_config(self._cfg)
        d = self.dir_var.get()
        tasks = []
        for name in self.images:
            src = os.path.join(d, name)
            dst = os.path.join(out, name) if out else src
            tasks.append((name, lambda s=src, t=dst: process_image(s, t, scale, quality, keep)))
        self._run_job("Compressing", tasks, self._compress_done("Compress", out))

    def _rename_in_place(self, d, pairs):
        # Two phases so swaps/chains between files in the batch can't collide.
        tmp = []
        for old, new in pairs:
            t = f".{uuid.uuid4().hex}.tmp"
            os.rename(os.path.join(d, old), os.path.join(d, t))
            tmp.append((t, new))
        for t, new in tmp:
            os.rename(os.path.join(d, t), os.path.join(d, new))

    def rename(self):
        if not self._valid_dir():
            return
        out = self._out_dir()
        if out is None:
            return
        d = self.dir_var.get()
        plan = plan_renames(d, self.images, self._names(), out)
        if not out:
            plan = [(o, n) for o, n in plan if o != n]
        if not plan:
            messagebox.showinfo("Nothing to do", "No files need renaming.")
            return
        if out:  # export: originals stay untouched
            tasks = [(new, lambda o=old, n=new: shutil.copy2(os.path.join(d, o), os.path.join(out, n)) and 0)
                     for old, new in plan]
            self._run_job("Exporting", tasks, lambda job: self._finish(
                "Rename", f"Exported {job['done'] - len(job['failed'])} renamed images.", job, out))
            return
        try:
            self._rename_in_place(d, plan)
        except OSError as e:
            messagebox.showerror("Error", f"Rename failed: {e}")
        else:
            messagebox.showinfo("Success", f"Renamed {len(plan)} images.")
        self.load()

    def rename_compress(self):
        """Rename using the names list, then compress the renamed files - in one pass."""
        if not self._valid_dir():
            return
        if not self.images:
            messagebox.showinfo("Nothing to do", "Load a folder with images first.")
            return
        out = self._out_dir()
        if out is None:
            return
        scale, quality, keep = self.scale.get(), self.quality.get(), self.keep_meta.get()
        d = self.dir_var.get()
        plan = plan_renames(d, self.images, self._names(), out)
        if not self._confirm("Rename & Compress", len(plan), "Rename and compress", out, keep, scale, quality):
            return
        self._cfg.update(scale=scale, quality=quality, keep_meta=keep)
        save_config(self._cfg)
        if out:  # originals untouched; compressed copy is written under the new name
            tasks = [(new, lambda s=os.path.join(d, old), t=os.path.join(out, new):
                      process_image(s, t, scale, quality, keep)) for old, new in plan]
        else:
            try:
                self._rename_in_place(d, [(o, n) for o, n in plan if o != n])
            except OSError as e:
                messagebox.showerror("Error", f"Rename failed: {e}")
                self.load()
                return
            self.load()
            tasks = [(new, lambda p=os.path.join(d, new): process_image(p, p, scale, quality, keep))
                     for _, new in plan]
        self._run_job("Renaming & compressing", tasks, self._compress_done("Rename & Compress", out))


if __name__ == "__main__":
    App().mainloop()
