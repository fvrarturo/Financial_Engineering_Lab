"""Chart theme shared by every figure: light and dark versions, same palette as the SIG write-up."""
from pathlib import Path

import matplotlib
import matplotlib.pyplot as plt
import numpy as np

FIG = Path(__file__).resolve().parents[2] / "figures"

THEMES = {
    "light": dict(bg="#ffffff", ink="#1f2328", muted="#59636e", grid="#e6eaef", axis="#c4ccd4", gray="#b4bcc5",
                  rec="#eceff2", inv="#fde4d6",
                  s=["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"],
                  seq=["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]),
    "dark": dict(bg="#0d1117", ink="#e6edf3", muted="#9198a1", grid="#21262d", axis="#3d444d", gray="#4d5560",
                 rec="#1a1f26", inv="#3a2216",
                 s=["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"],
                 seq=["#184f95", "#1c5cab", "#256abf", "#2a78d6", "#5598e7", "#86b6ef", "#cde2fb"]),
}


def _register_fonts():
    """matplotlib only reads the first face of macOS's HelveticaNeue.ttc (regular). Extract bold/medium once into a
    local cache so titles can be bold. Falls back silently to Arial/DejaVu elsewhere."""
    try:
        from fontTools.ttLib import TTCollection
        from matplotlib import font_manager as fm
        src = Path("/System/Library/Fonts/HelveticaNeue.ttc")
        cache = Path.home() / ".cache" / "fineng_fonts"
        if not src.exists():
            return
        cache.mkdir(parents=True, exist_ok=True)
        for i, name in [(0, "HelveticaNeue-Regular"), (1, "HelveticaNeue-Bold"), (10, "HelveticaNeue-Medium")]:
            f = cache / f"{name}.ttf"
            if not f.exists():
                TTCollection(str(src)).fonts[i].save(str(f))
            fm.fontManager.addfont(str(f))
    except Exception:
        pass


_register_fonts()


def style(T):
    plt.rcParams.update({
        "font.family": ["Helvetica Neue", "Arial", "DejaVu Sans"], "font.size": 10.5, "axes.unicode_minus": False,
        "figure.facecolor": T["bg"], "axes.facecolor": T["bg"], "savefig.facecolor": T["bg"],
        "text.color": T["ink"], "axes.labelcolor": T["muted"], "xtick.color": T["muted"], "ytick.color": T["muted"],
        "axes.edgecolor": T["axis"], "axes.grid": True, "grid.color": T["grid"], "grid.linewidth": 0.8,
        "axes.spines.top": False, "axes.spines.right": False, "axes.titlesize": 12.5, "axes.titleweight": "bold",
        "axes.titlelocation": "left", "axes.titlepad": 24, "legend.frameon": False, "legend.fontsize": 9.5,
        "xtick.major.size": 0, "ytick.major.size": 0, "xtick.minor.size": 0, "lines.solid_capstyle": "round",
    })


def save(fig, name, theme):
    FIG.mkdir(exist_ok=True)
    fig.savefig(FIG / f"{name}-{theme}.png", dpi=150, bbox_inches="tight", pad_inches=0.25)
    plt.close(fig)


def subtitle(ax, text, T, y=1.012):
    ax.text(0, y, text, transform=ax.transAxes, color=T["muted"], fontsize=9.5, va="bottom")


def panel_title(ax, text, T):
    """Smaller bold title for small-multiple panels (no subtitle gap)."""
    ax.set_title(text, fontsize=10.5, pad=8, color=T["ink"])


def shade(ax, periods, color, z=0):
    for a, b in periods:
        ax.axvspan(a, b, color=color, lw=0, zorder=z)


def maturity_axis(ax, ticks=(0.25, 1, 2, 5, 10, 30), labels=("3m", "1y", "2y", "5y", "10y", "30y")):
    ax.set_xscale("log")
    ax.set_xticks(ticks)
    ax.set_xticklabels(labels)
    ax.xaxis.set_minor_locator(matplotlib.ticker.NullLocator())


def note(ax, xy, text, T, dx=0, dy=20, ha="center", fs=9, data_xy=True):
    ax.annotate(text, xy, xytext=(dx, dy), textcoords="offset points", ha=ha, fontsize=fs, color=T["muted"],
                arrowprops=dict(arrowstyle="-", color=T["axis"], lw=0.8),
                xycoords="data" if data_xy else "axes fraction")


def ramp(T, n):
    """n colours along the sequential ramp, early -> late (late is the most prominent in both themes)."""
    seq = T["seq"]
    idx = np.linspace(1, len(seq) - 1, n)
    from matplotlib.colors import LinearSegmentedColormap
    cmap = LinearSegmentedColormap.from_list("r", seq)
    return [cmap(i / (len(seq) - 1)) for i in idx]
