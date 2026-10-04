"""記事中の ```chart ブロック（数値データ）をグラフ画像にする。

Cowork には、記事の中でグラフにしたい数値を次の形で書いてもらう（JSON）。

    ```chart
    {"type": "bar", "title": "今週のセクター別騰落率", "unit": "%",
     "labels": ["情報技術", "ヘルスケア"], "values": [1.80, -2.65],
     "source": "S&P Dow Jones Indices（10/2 終値）"}
    ```

- type: "bar"（横棒。項目の比較）/ "line"（折れ線。時系列）
- line で複数の線を引くときは values の代わりに "series": [{"name": "米2年", "values": [...]}, ...]
- source（出典）は必須。画像の下の説明文に「出典: …」として載せる

画像にできなかった（形式の誤り・アップロード失敗など）ときは、記事が壊れないよう
同じ数値を箇条書きの文章に置き換える。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from publisher.thumbnail import CREAM, GRAY, INK, RED, find_font

BLOCK = re.compile(r"^```\s*chart\s*\n(.*?)\n```\s*$", re.S | re.M)
CHART_W, CHART_H = 1240, 760  # 本文では 620 幅で表示される（2 倍の解像度で作る）
SERIES_COLORS = [RED, INK, GRAY, (214, 140, 60)]


class ChartError(ValueError):
    pass


@dataclass
class Chart:
    title: str
    source: str
    unit: str = ""
    type: str = "bar"
    labels: list[str] = field(default_factory=list)
    series: list[tuple[str, list[float]]] = field(default_factory=list)

    @property
    def caption(self) -> str:
        return f"{self.title}（出典: {self.source}）"


def parse_spec(text: str) -> Chart:
    try:
        # メールの折り返しで JSON の途中に改行が入っても読めるよう、改行は空白として扱う
        spec = json.loads(re.sub(r"\s*\n\s*", " ", text))
    except json.JSONDecodeError as e:
        raise ChartError(f"JSON として読めません（{e.msg}）") from e
    if not isinstance(spec, dict):
        raise ChartError("JSON のオブジェクト（{...}）ではありません")
    kind = str(spec.get("type", "bar")).lower()
    if kind not in ("bar", "line"):
        raise ChartError(f"type は bar か line です（{kind}）")
    title, source = str(spec.get("title", "")).strip(), str(spec.get("source", "")).strip()
    if not title:
        raise ChartError("title がありません")
    if not source:
        raise ChartError("source（出典）がありません")
    labels = [str(l) for l in spec.get("labels") or []]
    raw_series = spec.get("series") or [{"name": "", "values": spec.get("values") or []}]
    series = []
    for s in raw_series:
        try:
            values = [float(v) for v in s.get("values") or []]
        except (TypeError, ValueError) as e:
            raise ChartError("values に数値でないものがあります") from e
        if len(values) != len(labels) or not values:
            raise ChartError(f"labels（{len(labels)}個）と values（{len(values)}個）の数が合いません")
        series.append((str(s.get("name", "")), values))
    if len(labels) > 30 or len(series) > 4:
        raise ChartError("項目が多すぎます（30項目・4系列まで）")
    return Chart(title, source, str(spec.get("unit", "")), kind, labels, series)


def _fmt(v: float, unit: str, signed: bool) -> str:
    s = f"{v:+,.2f}" if signed else f"{v:,.2f}"
    s = s.rstrip("0").rstrip(".") if "." in s else s
    return f"{s}{unit}"


def as_text(chart: Chart) -> str:
    """グラフにできなかったときの代わりの文章（Markdown）。"""
    signed = chart.type == "bar" and any(v < 0 for _, vs in chart.series for v in vs)
    lines = [f"**{chart.title}**", ""]
    for name, values in chart.series:
        if name:
            lines.append(f"- {name}: " + " → ".join(_fmt(v, chart.unit, False) for v in values))
        else:
            lines += [f"- {l}: {_fmt(v, chart.unit, signed)}" for l, v in zip(chart.labels, values)]
    return "\n".join(lines + ["", f"出典: {chart.source}"])


def render(chart: Chart, out: Path) -> Path:
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import font_manager
    import matplotlib.pyplot as plt

    font_path, _ = find_font()
    font_manager.fontManager.addfont(font_path)
    family = font_manager.FontProperties(fname=font_path).get_name()
    rgb = lambda c: tuple(x / 255 for x in c)  # noqa: E731

    plt.rcParams.update({
        "font.family": family, "font.size": 15, "axes.edgecolor": rgb(GRAY), "axes.labelcolor": rgb(INK),
        "xtick.color": rgb(INK), "ytick.color": rgb(INK), "axes.unicode_minus": False,
    })
    fig, ax = plt.subplots(figsize=(CHART_W / 100, CHART_H / 100), dpi=100)
    fig.patch.set_facecolor(rgb(CREAM))
    ax.set_facecolor(rgb(CREAM))
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)

    if chart.type == "bar":
        values = chart.series[0][1]
        signed = any(v < 0 for v in values)
        y = list(range(len(values)))[::-1]
        colors = [rgb(RED) if v >= 0 else rgb(INK) for v in values]
        bars = ax.barh(y, values, color=colors, height=0.62)
        ax.set_yticks(y, chart.labels)
        ax.axvline(0, color=rgb(GRAY), linewidth=1)
        span = (max(values + [0]) - min(values + [0])) or 1
        ax.set_xlim(min(values + [0]) - span * 0.18, max(values + [0]) + span * 0.18)
        for bar, v in zip(bars, values):
            ax.text(v + (span * 0.015 if v >= 0 else -span * 0.015), bar.get_y() + bar.get_height() / 2,
                    _fmt(v, chart.unit, signed), va="center", ha="left" if v >= 0 else "right",
                    fontsize=14, color=rgb(INK))
        ax.grid(axis="x", color=rgb(GRAY), alpha=0.25)
    else:
        x = list(range(len(chart.labels)))
        for i, (name, values) in enumerate(chart.series):
            ax.plot(x, values, color=rgb(SERIES_COLORS[i % len(SERIES_COLORS)]), linewidth=3,
                    marker="o" if len(x) <= 15 else None, label=name or None)
        step = max(1, len(x) // 8)
        ax.set_xticks(x[::step], chart.labels[::step])
        ax.grid(axis="y", color=rgb(GRAY), alpha=0.25)
        if chart.unit:
            ax.set_ylabel(chart.unit)
        if any(name for name, _ in chart.series):
            ax.legend(frameon=False)
    ax.set_axisbelow(True)

    fig.suptitle(chart.title, x=0.02, ha="left", fontsize=24, fontweight="bold", color=rgb(INK))
    fig.text(0.02, 0.02, f"出典: {chart.source}", fontsize=13, color=rgb(GRAY))
    fig.tight_layout(rect=(0, 0.05, 1, 0.95))
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, facecolor=fig.get_facecolor())
    plt.close(fig)
    return out


@dataclass
class ChartReport:
    body: str
    notes: list[str] = field(default_factory=list)  # 作業記録（Summary に出す）
    warnings: list[str] = field(default_factory=list)


def embed(body_md: str, out_dir: Path, upload: Callable[[Path], str] | None) -> ChartReport:
    """本文中の ```chart ブロックを画像にして upload し、Markdown の画像（![説明](URL)）に置き換える。

    upload が None（投稿しない確認モード）のときは画像だけ作り、本文は文章版にする。
    """
    report = ChartReport(body_md)
    n = 0

    def replace(m: re.Match) -> str:
        nonlocal n
        n += 1
        try:
            chart = parse_spec(m.group(1))
        except ChartError as e:
            report.warnings.append(f"グラフ{n}: 書式が正しくないため省きました（{e}）")
            return ""
        try:
            path = render(chart, out_dir / f"chart_{n}.png")
        except Exception as e:  # noqa: BLE001  グラフが作れなくても記事は出す
            report.warnings.append(f"グラフ{n}「{chart.title}」: 画像を作れず文章にしました（{e}）")
            return as_text(chart)
        if upload is None:
            report.notes.append(f"グラフ{n}「{chart.title}」を作成（{path}）")
            return as_text(chart)
        try:
            url = upload(path)
        except Exception as e:  # noqa: BLE001
            report.warnings.append(f"グラフ{n}「{chart.title}」: アップロードに失敗したので文章にしました（{e}）")
            return as_text(chart)
        report.notes.append(f"グラフ{n}「{chart.title}」を挿入")
        caption = chart.caption.replace("[", "［").replace("]", "］")
        return f'![{caption}]({url} "620x{round(620 * CHART_H / CHART_W)}")'

    report.body = BLOCK.sub(replace, body_md)
    return report
