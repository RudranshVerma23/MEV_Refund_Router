"""Report figures from the backtest, as standalone SVG (standard library only).

Usage (from the project root, after analyze.py --rows-out):
    python backtest/plots.py --rows backtest/results/rows.csv --out docs/figures

Static figures for docs/REPORT.md. Every figure's numbers also appear in a table in the report.
Colours: the reference data-viz palette, light mode (slot 1 blue, slot 2 orange).
"""

from __future__ import annotations

import argparse
import csv
import math
import os

INK, INK2, MUTED, GRID, SURFACE = "#0b0b0b", "#52514e", "#8a8984", "#e6e5e1", "#fcfcfb"
S1, S2 = "#2a78d6", "#eb6834"
FONT = "font-family='Inter, Segoe UI, Helvetica, Arial, sans-serif'"
BUCKETS = ["<1k", "1k-10k", "10k-50k", "50k-250k", ">=250k"]
BUCKET_LABEL = {"<1k": "< $1k", "1k-10k": "$1k–10k", "10k-50k": "$10k–50k", "50k-250k": "$50k–250k", ">=250k": "≥ $250k"}


class Svg:
    def __init__(self, w: int, h: int, title: str, subtitle: str = ""):
        self.w, self.h, self.parts = w, h, []
        self.text(24, 30, title, size=16, weight=600)
        if subtitle:
            self.text(24, 50, subtitle, size=12, color=INK2)

    def text(self, x, y, s, size=12, color=INK, anchor="start", weight=400, rotate=None):
        tr = f" transform='rotate({rotate} {x} {y})'" if rotate else ""
        s = str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        self.parts.append(f"<text x='{x:.1f}' y='{y:.1f}' {FONT} font-size='{size}' fill='{color}' "
                          f"text-anchor='{anchor}' font-weight='{weight}'{tr}>{s}</text>")

    def line(self, x1, y1, x2, y2, color=GRID, width=1, dash=None):
        d = f" stroke-dasharray='{dash}'" if dash else ""
        self.parts.append(f"<line x1='{x1:.1f}' y1='{y1:.1f}' x2='{x2:.1f}' y2='{y2:.1f}' stroke='{color}' stroke-width='{width}'{d}/>")

    def bar(self, x, y, w, h, color):
        """Vertical bar anchored at the baseline (y + h), 4px rounded data end."""
        if h <= 0:
            return
        r = min(4, w / 2, h)
        b = y + h
        self.parts.append(f"<path d='M{x:.1f},{b:.1f} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} "
                          f"H{x + w - r:.1f} Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f} V{b:.1f} Z' fill='{color}'/>")

    def hbar(self, x, y, w, h, color):
        """Horizontal bar anchored at x, 4px rounded data end on the right."""
        if w <= 0:
            return
        r = min(4, h / 2, w)
        e = x + w
        self.parts.append(f"<path d='M{x:.1f},{y:.1f} H{e - r:.1f} Q{e:.1f},{y:.1f} {e:.1f},{y + r:.1f} "
                          f"V{y + h - r:.1f} Q{e:.1f},{y + h:.1f} {e - r:.1f},{y + h:.1f} H{x:.1f} Z' fill='{color}'/>")

    def polyline(self, pts, color, width=2):
        p = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        self.parts.append(f"<polyline points='{p}' fill='none' stroke='{color}' stroke-width='{width}' stroke-linejoin='round'/>")

    def dot(self, x, y, color, r=4):
        self.parts.append(f"<circle cx='{x:.1f}' cy='{y:.1f}' r='{r}' fill='{color}' stroke='{SURFACE}' stroke-width='2'/>")

    def save(self, path: str) -> None:
        body = "\n".join(self.parts)
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"<svg xmlns='http://www.w3.org/2000/svg' width='{self.w}' height='{self.h}' "
                    f"viewBox='0 0 {self.w} {self.h}' role='img'>\n<rect width='100%' height='100%' fill='{SURFACE}'/>\n{body}\n</svg>\n")


def nice_ticks(hi: float, n: int = 5) -> list[float]:
    step = 10 ** math.floor(math.log10(hi / n)) if hi > 0 else 1
    for m in (1, 2, 2.5, 5, 10):
        if hi / (step * m) <= n:
            step *= m
            break
    return [i * step for i in range(int(hi / step) + 2)]


def load(rows_path: str) -> list[dict]:
    rows = list(csv.DictReader(open(rows_path)))
    for r in rows:
        for k in ("created", "net", "swap_eth", "gas_plain", "gas_fill", "gas_settle", "settle_tx_gas", "gas_price", "weight"):
            r[k] = float(r[k] or 0)
        r["auctioned"] = r["auctioned"] == "True"
    return rows


# ---------------------------------------------------------------------------------------------
def fig_auction_rate(rows, out):
    rates = []
    for b in BUCKETS:
        g = [r for r in rows if r["bucket"] == b]
        rates.append((b, 100 * sum(r["auctioned"] for r in g) / len(g) if g else 0, len(g)))
    svg = Svg(640, 360, "Share of user swaps worth auctioning, by swap size",
              "Arbitrage created by the swap exceeds the gas of a settle() transaction (2024 sample, n = 1,000)")
    x0, y0, w, h = 70, 80, 540, 220
    for t in (0, 25, 50, 75, 100):
        y = y0 + h - h * t / 100
        svg.line(x0, y, x0 + w, y)
        svg.text(x0 - 8, y + 4, f"{t}%", size=11, color=MUTED, anchor="end")
    bw = w / len(rates)
    for i, (b, v, n) in enumerate(rates):
        bh = h * v / 100
        x = x0 + i * bw + bw * 0.2
        svg.bar(x, y0 + h - bh, bw * 0.6, bh, S1)
        svg.text(x + bw * 0.3, y0 + h - bh - 6, f"{v:.1f}%", size=11, anchor="middle")
        svg.text(x + bw * 0.3, y0 + h + 18, BUCKET_LABEL[b], size=11, color=INK2, anchor="middle")
        svg.text(x + bw * 0.3, y0 + h + 33, f"n = {n}", size=10, color=MUTED, anchor="middle")
    svg.save(os.path.join(out, "fig1_auction_rate_by_size.svg"))


def fig_mev_vs_size(rows, out):
    pts = [r for r in rows if r["created"] > 0 and r["swap_eth"] > 0]
    gas_cost = sorted(r["settle_tx_gas"] * r["gas_price"] / 1e18 for r in rows)[len(rows) // 2]
    svg = Svg(640, 400, "Arbitrage created vs swap size (log–log)",
              "Each dot is a sampled swap that created any arbitrage; dashed line = median settle() cost")
    x0, y0, w, h = 80, 80, 500, 250
    xs = [math.log10(r["swap_eth"]) for r in pts]
    ys = [math.log10(r["created"] / 1e18) for r in pts]
    xlo, xhi = math.floor(min(xs)), math.ceil(max(xs))
    ylo, yhi = math.floor(min(ys + [math.log10(gas_cost)])), math.ceil(max(ys))
    X = lambda v: x0 + w * (v - xlo) / (xhi - xlo)  # noqa: E731
    Y = lambda v: y0 + h - h * (v - ylo) / (yhi - ylo)  # noqa: E731
    for e in range(xlo, xhi + 1):
        svg.line(X(e), y0, X(e), y0 + h)
        svg.text(X(e), y0 + h + 18, f"{10 ** e:g}", size=11, color=MUTED, anchor="middle")
    for e in range(ylo, yhi + 1):
        svg.line(x0, Y(e), x0 + w, Y(e))
        svg.text(x0 - 8, Y(e) + 4, f"{10 ** e:g}", size=11, color=MUTED, anchor="end")
    svg.text(x0 + w / 2, y0 + h + 38, "swap size (ETH)", size=12, color=INK2, anchor="middle")
    svg.text(24, y0 + h / 2, "arbitrage created (ETH)", size=12, color=INK2, anchor="middle", rotate=-90)
    gy = Y(math.log10(gas_cost))
    svg.line(x0, gy, x0 + w, gy, color=INK2, width=1.5, dash="5 4")
    svg.text(x0 + w, gy - 6, f"settle() gas ≈ {gas_cost:.4f} ETH", size=11, color=INK2, anchor="end")
    for r, xv, yv in zip(pts, xs, ys):
        svg.dot(X(xv), Y(yv), S1 if r["auctioned"] else S2)
    ly = 64
    svg.dot(x0 + w - 230, ly, S1)
    svg.text(x0 + w - 220, ly + 4, "auctioned", size=11, color=INK2)
    svg.dot(x0 + w - 140, ly, S2)
    svg.text(x0 + w - 130, ly + 4, "below gas cost", size=11, color=INK2)
    svg.save(os.path.join(out, "fig2_mev_vs_size.svg"))


def fig_concentration(rows, out):
    refunds = sorted((r["net"] * 0.81 * r["weight"] for r in rows if r["auctioned"]), reverse=True)
    total = sum(refunds)
    cum, acc = [0.0], 0.0
    for v in refunds:
        acc += v
        cum.append(100 * acc / total)
    n = len(refunds)
    svg = Svg(640, 360, "Refunds are concentrated in a few very large swaps",
              f"Cumulative share of total refunds vs number of top swaps ({n} auctioned swaps, ranked)")
    x0, y0, w, h = 70, 80, 530, 220
    X = lambda i: x0 + w * i / n  # noqa: E731
    Y = lambda v: y0 + h - h * v / 100  # noqa: E731
    for t in (0, 25, 50, 75, 100):
        svg.line(x0, Y(t), x0 + w, Y(t))
        svg.text(x0 - 8, Y(t) + 4, f"{t}%", size=11, color=MUTED, anchor="end")
    for t in nice_ticks(n, 6):
        if t <= n:
            svg.text(X(t), y0 + h + 18, f"{int(t)}", size=11, color=MUTED, anchor="middle")
    svg.text(x0 + w / 2, y0 + h + 38, "top N swaps", size=12, color=INK2, anchor="middle")
    svg.polyline([(X(i), Y(v)) for i, v in enumerate(cum)], S1)
    for k in (1, 3):
        svg.dot(X(k), Y(cum[k]), S1)
        svg.text(X(k) + 8, Y(cum[k]) + 14, f"top {k}: {cum[k]:.0f}%", size=11, color=INK)
    svg.save(os.path.join(out, "fig3_refund_concentration.svg"))


def fig_gas(rows, out):
    med = lambda xs: sorted(xs)[len(xs) // 2]  # noqa: E731
    plain = med([r["gas_plain"] for r in rows])
    fill = med([r["gas_fill"] for r in rows])
    settle = med([r["gas_settle"] for r in rows if r["auctioned"]])
    items = [("plain pair swap", plain, ""), ("router fill() (no backrun)", fill, f"+{fill - plain:,.0f} overhead"),
             ("router settle() (swap + backrun + refund)", settle, f"+{settle - fill:,.0f} backrun & payment")]
    svg = Svg(640, 280, "Execution gas per path (median, mainnet fork)",
              "Excludes the 21,000 intrinsic gas and calldata of a real transaction")
    x0, y0, w = 290, 80, 300
    hi = max(v for _, v, _ in items)
    for i, (label, v, note) in enumerate(items):
        y = y0 + i * 56
        svg.text(x0 - 10, y + 15, label, size=12, color=INK2, anchor="end")
        svg.hbar(x0, y, w * v / hi, 22, S1)
        svg.text(x0 + w * v / hi + 6, y + 15, f"{v:,.0f}", size=11)
        if note:
            svg.text(x0, y + 38, note, size=10, color=MUTED)
    svg.save(os.path.join(out, "fig4_gas_breakdown.svg"))


def fig_sensitivity(rows, out):
    fracs = [0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 0.99]
    series = {}
    for r_share, color in ((0.5, S2), (0.9, S1)):
        series[r_share] = (color, [sum(r["net"] * f * r_share * r["weight"] for r in rows if r["auctioned"]) / 1e18 for f in fracs])
    hi = max(max(v) for _, v in series.values())
    svg = Svg(640, 360, "Total refunds to users in 2024 vs searcher bid aggressiveness",
              "Weighted to all user swaps; bid = fraction of net arbitrage; lines = share of the bid refunded")
    x0, y0, w, h = 70, 80, 450, 220
    ticks = nice_ticks(hi)
    top = ticks[-1] if ticks[-1] >= hi else hi
    X = lambda f: x0 + w * (f - 0.5) / 0.5  # noqa: E731
    Y = lambda v: y0 + h - h * v / top  # noqa: E731
    for t in ticks:
        if t <= top:
            svg.line(x0, Y(t), x0 + w, Y(t))
            svg.text(x0 - 8, Y(t) + 4, f"{t:g}", size=11, color=MUTED, anchor="end")
    for f in (0.5, 0.6, 0.7, 0.8, 0.9, 1.0):
        svg.text(X(f), y0 + h + 18, f"{f:.0%}", size=11, color=MUTED, anchor="middle")
    svg.text(x0 + w / 2, y0 + h + 38, "bid as share of net arbitrage", size=12, color=INK2, anchor="middle")
    svg.text(24, y0 + h / 2, "ETH refunded (2024)", size=12, color=INK2, anchor="middle", rotate=-90)
    for r_share, (color, vals) in series.items():
        svg.polyline([(X(f), Y(v)) for f, v in zip(fracs, vals)], color)
        svg.text(X(fracs[-1]) + 8, Y(vals[-1]) + 4, f"refund {r_share:.0%}: {vals[-1]:.1f} ETH", size=11, color=INK)
    svg.save(os.path.join(out, "fig5_bid_sensitivity.svg"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", default="backtest/results/rows.csv")
    ap.add_argument("--out", default="docs/figures")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    rows = load(args.rows)
    for fn in (fig_auction_rate, fig_mev_vs_size, fig_concentration, fig_gas, fig_sensitivity):
        fn(rows, args.out)
    print(f"wrote 5 figures to {args.out}")


if __name__ == "__main__":
    main()
