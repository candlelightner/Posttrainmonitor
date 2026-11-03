"""A small, authenticated NiceGUI view over aggregate training metrics."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from nicegui import app, run as nicegui_run, ui

from metrics import MetricsStore
from security import AuthSettings, install_auth

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"
TEAL = "#31595c"
COPPER = "#ae7644"
INK = "#292524"
MUTED = "#78716c"
LINE = "#e7e5e4"


def number(value: Any) -> float | None:
    return float(value) if type(value) in (int, float) and math.isfinite(value) else None


def fmt(value: Any, places: int = 0) -> str:
    value = number(value)
    return "—" if value is None else f"{value:,.{places}f}"


def duration(value: Any) -> str:
    value = number(value)
    if value is None:
        return "—"
    minutes = max(0, int(value // 60))
    days, hours, minutes = minutes // 1440, minutes // 60 % 24, minutes % 60
    if days:
        return f"{days}d {hours}h {minutes:02d}m"
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


def local_time(value: Any, tz: Any, *, date: bool = False) -> str:
    value = number(value)
    if value is None:
        return "—"
    try:
        return datetime.fromtimestamp(value, tz).strftime("%d %b, %H:%M" if date else "%H:%M:%S")
    except (ValueError, OverflowError, OSError):
        return "—"


def ema(rows: list[dict], span: int = 20) -> list[tuple[dict, float]]:
    result: list[tuple[dict, float]] = []
    previous = None
    alpha = 2 / (span + 1)
    for row in rows:
        value = number(row.get("loss"))
        if value is None:
            continue
        previous = value if previous is None else alpha * value + (1 - alpha) * previous
        result.append((row, previous))
    return result


def line_series(name: str, data: list, color: str, **extra: Any) -> dict:
    return {
        "name": name, "type": "line", "data": data, "showSymbol": False,
        "connectNulls": False, "lineStyle": {"width": 2, "color": color},
        "itemStyle": {"color": color}, "emphasis": {"focus": "series"},
        "animation": False, **extra,
    }


def chart_options(*, x_type: str = "value", x_name: str = "Optimizer update", y_name: str = "", second_axis: str | None = None) -> dict:
    axes = [{
        "type": "value", "name": y_name, "nameTextStyle": {"color": MUTED, "fontSize": 10, "align": "left", "padding": [0, 0, 8, 0]},
        "axisLabel": {"color": MUTED, "fontSize": 11}, "splitLine": {"lineStyle": {"color": "#f0eeeb"}},
        "axisLine": {"show": False}, "axisTick": {"show": False}, "scale": True,
    }]
    if second_axis:
        axes.append({
            "type": "value", "name": second_axis, "nameTextStyle": {"color": MUTED, "fontSize": 10, "align": "right"},
            "position": "right", "axisLabel": {"color": MUTED, "fontSize": 11},
            "splitLine": {"show": False}, "axisLine": {"show": False}, "axisTick": {"show": False},
        })
    return {
        "animation": False, "backgroundColor": "transparent",
        "textStyle": {"fontFamily": "system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif", "color": INK},
        "color": [TEAL, COPPER, "#9aaba6", "#cbbcae"],
        "grid": {"left": 12, "right": 20, "top": 47, "bottom": 52, "containLabel": True},
        "legend": {"top": 0, "left": 0, "icon": "roundRect", "itemWidth": 14, "itemHeight": 3, "textStyle": {"color": MUTED, "fontSize": 11}},
        "tooltip": {
            "trigger": "axis", "confine": True, "backgroundColor": "#ffffff", "borderColor": LINE,
            "textStyle": {"color": INK, "fontSize": 12}, "extraCssText": "box-shadow:0 6px 24px #29252412;border-radius:8px;",
            ":valueFormatter": "v => typeof v === 'number' ? v.toLocaleString(undefined, {maximumSignificantDigits: 6}) : '—'",
            "axisPointer": {"type": "line", "lineStyle": {"color": "#a8a29e", "type": "dashed"}},
        },
        "xAxis": {
            "type": x_type, "name": x_name, "nameLocation": "middle", "nameGap": 31,
            "nameTextStyle": {"color": MUTED, "fontSize": 11}, "axisLine": {"lineStyle": {"color": LINE}},
            "axisLabel": {"color": MUTED, "fontSize": 11, "hideOverlap": True}, "axisTick": {"show": False},
            "splitLine": {"show": False}, "minInterval": 1 if x_type == "value" else None,
        },
        "yAxis": axes,
        "dataZoom": [
            {"id": "inside", "type": "inside", "filterMode": "none"},
            {"id": "slider", "type": "slider", "height": 13, "bottom": 3, "borderColor": LINE,
             "backgroundColor": "#fafaf9", "fillerColor": "#31595c12", "handleStyle": {"color": TEAL},
             "dataBackground": {"lineStyle": {"color": "#d6d3d1"}, "areaStyle": {"color": "#e7e5e4"}}, "showDetail": False},
        ],
        "series": [],
    }


def configure(store: MetricsStore, auth_settings: AuthSettings) -> None:
    """Register the protected dashboard and local assets once, before ui.run."""
    install_auth(app, login_html_path=STATIC / "login.html", settings=auth_settings)
    app.add_static_files("/static", STATIC)
    collector_task: asyncio.Task | None = None

    async def collect_metrics() -> None:
        while True:
            try:
                await nicegui_run.io_bound(store.snapshot, force=True)
            except Exception:
                pass  # Preserve the last successful aggregate snapshot; retry on the next tick.
            await asyncio.sleep(30)

    async def start_collection() -> None:
        nonlocal collector_task
        collector_task = asyncio.create_task(collect_metrics(), name="posttrain-metric-collection")

    async def stop_collection() -> None:
        if collector_task is not None:
            collector_task.cancel()
            with suppress(asyncio.CancelledError):
                await collector_task

    app.on_startup(start_collection)
    app.on_shutdown(stop_collection)

    @ui.page("/")
    async def dashboard() -> None:
        ui.colors(primary=TEAL, secondary=COPPER, positive=TEAL, warning=COPPER)
        ui.add_head_html('<meta name="color-scheme" content="light"><link rel="stylesheet" href="/static/dashboard.css">')
        ui.dark_mode().disable()
        state: dict[str, Any] = {"snapshot": {}, "range": "all", "axis": "step", "raw": True, "baseline": False, "log_gradient": False, "timezone": timezone.utc, "timezone_name": "UTC", "refreshing": False}
        labels: dict[str, Any] = {}
        charts: dict[str, Any] = {}
        notes: dict[str, Any] = {}

        def label(key: str, text: str = "—", css: str = "") -> Any:
            labels[key] = ui.label(text).classes(css)
            return labels[key]

        def section_title(kicker: str, title: str, subtitle: str) -> None:
            with ui.column().classes("section-heading gap-1"):
                ui.label(kicker).classes("eyebrow")
                ui.label(title).classes("section-title")
                ui.label(subtitle).classes("section-description")

        def chart_card(key: str, title: str, subtitle: str, *, wide: bool = False, height: int = 240, control: str | None = None) -> None:
            with ui.card().classes("panel chart-panel " + ("span-2" if wide else "")):
                with ui.row().classes("card-heading items-start justify-between w-full no-wrap"):
                    with ui.column().classes("gap-1"):
                        ui.label(title).classes("card-title")
                        ui.label(subtitle).classes("card-subtitle")
                    if control == "baseline":
                        ui.switch("Baseline", value=False, on_change=lambda e: set_control("baseline", e.value)).props("dense").classes("small-switch")
                    elif control == "gradient":
                        ui.switch("Log scale", value=False, on_change=lambda e: set_control("log_gradient", e.value)).props("dense").classes("small-switch")
                charts[key] = ui.echart(chart_options()).classes("w-full").style(f"height:{height}px")
                notes[key] = ui.label("").classes("chart-note")

        def set_control(key: str, value: Any) -> None:
            state[key] = value
            update_charts()

        with ui.element("div").classes("dashboard-shell"):
            with ui.element("aside").classes("sidebar"):
                with ui.link(target="#overview").classes("brand"):
                    ui.html('<span class="brand-mark" aria-hidden="true"><i></i><i></i><i></i></span>')
                    ui.label("posttrain").classes("brand-name")
                ui.label("TRAINING MONITOR").classes("sidebar-kicker")
                with ui.element("nav").classes("sidebar-nav"):
                    for target, text, glyph in [("overview", "Overview", "01"), ("learning", "Learning", "02"), ("performance", "Performance", "03"), ("run", "Run details", "04")]:
                        with ui.link(target=f"#{target}").classes("nav-item"):
                            ui.label(glyph).classes("nav-number")
                            ui.label(text)
                with ui.element("div").classes("sidebar-bottom"):
                    ui.html('<span class="tiny-dot"></span><span>Automatic updates</span>')
                    ui.label("Every 30 seconds").classes("sidebar-small")
                    ui.html('<form method="post" action="/logout"><button type="submit" class="logout-button">Sign out <span aria-hidden="true">↗</span></button></form>')

            with ui.element("main").classes("main-content"):
                with ui.element("header").classes("topbar"):
                    ui.label("MODEL TRAINING / FULL RUN").classes("eyebrow")
                    with ui.row().classes("items-center gap-3"):
                        label("synced", "Connecting…", "sync-label")
                        ui.button("Refresh", on_click=lambda: refresh(force=True)).props("flat no-caps dense").classes("refresh-button")
                        ui.html('<form method="post" action="/logout" class="header-logout"><button type="submit">Sign out</button></form>')

                with ui.element("section").props('id="overview"').classes("overview-section anchor-section"):
                    with ui.row().classes("justify-between items-start w-full heading-row"):
                        with ui.column().classes("gap-2"):
                            ui.label("Training overview").classes("page-title")
                            label("model", "Gemma 4", "page-subtitle")
                        with ui.row().classes("status-group items-center gap-2"):
                            label("status", "Connecting", "status-pill")
                            label("freshness", "Waiting for source", "freshness-label")
                    label("notice", "", "notice-banner")
                    labels["notice"].set_visibility(False)
                    with ui.element("div").classes("stats-grid"):
                        for key, title, descriptor in [("updates", "Optimizer updates", "Of the planned full run"), ("loss", "Training loss", "Latest optimizer update"), ("validation", "Validation loss", "Latest complete validation"), ("remaining", "Estimated remaining", "Based on recent run speed")]:
                            with ui.card().classes("panel stat-card"):
                                ui.label(title).classes("stat-label")
                                label(key, "—", "stat-value")
                                label(f"{key}_detail", descriptor, "stat-detail")
                    with ui.card().classes("panel progress-panel"):
                        with ui.row().classes("w-full justify-between items-center"):
                            with ui.row().classes("items-center gap-3"):
                                ui.label("EPOCH PROGRESS").classes("eyebrow")
                                label("epoch", "1 of 1", "progress-caption")
                            label("percent", "—", "progress-percent")
                        labels["progress_bar"] = ui.linear_progress(value=0, show_value=False).props('rounded size="7px"').classes("epoch-progress")
                        with ui.row().classes("w-full justify-between progress-details"):
                            label("examples", "Training examples unavailable")
                            label("elapsed", "Elapsed —")
                        with ui.element("div").classes("validation-strip") as validation_strip:
                            with ui.row().classes("w-full justify-between items-center gap-2"):
                                label("evaluation_title", "Full validation")
                                label("evaluation_count", "—")
                            labels["evaluation_bar"] = ui.linear_progress(value=0, show_value=False, color="secondary").props('rounded size="4px"')
                            label("evaluation_note", "", "evaluation-note")
                        labels["validation_strip"] = validation_strip

                with ui.element("section").props('id="learning"').classes("anchor-section"):
                    with ui.row().classes("section-title-row items-end justify-between w-full"):
                        section_title("LEARNING", "The shape of this run", "Full validation points are highlighted in copper. Hover for exact values; drag or scroll to zoom.")
                        with ui.row().classes("chart-controls items-center gap-3"):
                            ui.select({"all": "Full run", "250": "Last 250 updates", "1000": "Last 1,000 updates"}, value="all", on_change=lambda e: set_control("range", e.value)).props("outlined dense options-dense").classes("range-select")
                            ui.toggle({"step": "Step", "time": "Time"}, value="step", on_change=lambda e: set_control("axis", e.value)).props("no-caps unelevated dense").classes("axis-toggle")
                    with ui.element("div").classes("charts-grid"):
                        with ui.card().classes("panel chart-panel span-2 loss-panel"):
                            with ui.row().classes("card-heading items-start justify-between w-full"):
                                with ui.column().classes("gap-1"):
                                    ui.label("Training loss").classes("card-title")
                                    ui.label("Completion-token mean · lower is better").classes("card-subtitle")
                                ui.switch("Raw updates", value=True, on_change=lambda e: set_control("raw", e.value)).props("dense").classes("small-switch")
                            charts["loss"] = ui.echart(chart_options(y_name="Loss")).classes("w-full").style("height:320px")
                            notes["loss"] = ui.label("").classes("chart-note")
                        chart_card("validation", "Full validation", "Each point covers the complete validation split.", control="baseline")
                        chart_card("gradient", "Gradient norm", "True pre-clipping values · optimizer clip threshold 1.0", control="gradient")
                        chart_card("lr", "Learning rate", "Warmup followed by cosine decay.")
                        chart_card("speed", "Update speed & throughput", "Input tokens processed per second, including context.")

                with ui.element("section").props('id="performance"').classes("anchor-section"):
                    section_title("PERFORMANCE", "A view of the machine", "Resource history begins when this dashboard starts collecting. Earlier values are not reconstructed.")
                    with ui.element("div").classes("resource-summary"):
                        for key, title in [("gpu", "GPU memory"), ("ram", "Available RAM"), ("disk", "Free disk"), ("cpu", "Host CPU")]:
                            with ui.element("div").classes("resource-summary-item"):
                                ui.label(title).classes("stat-label")
                                label(key, "—", "resource-value")
                                label(f"{key}_detail", "—", "stat-detail")
                    with ui.element("div").classes("charts-grid"):
                        chart_card("gpu", "GPU memory & utilization", "Device memory in GiB · utilization on the right axis.")
                        chart_card("host", "Host CPU & process memory", "Host CPU on the left · training process RSS on the right.")
                        chart_card("capacity", "Available RAM & free disk", "Both axes use GiB; each keeps its own readable scale.", wide=True, height=220)
                    label("resource_since", "Resource history not available yet.", "section-footnote")

                with ui.element("section").props('id="run"').classes("anchor-section run-section"):
                    section_title("RUN DETAILS", "Checkpoints & configuration", "A compact record of the run, its saved milestones, and the data counts.")
                    with ui.card().classes("panel checkpoint-panel"):
                        with ui.row().classes("w-full items-center justify-between card-heading"):
                            ui.label("Checkpoints").classes("card-title")
                            label("checkpoint_count", "—", "card-subtitle")
                        columns = [
                            {"name": "step", "label": "Update", "field": "step", "align": "left", "sortable": True},
                            {"name": "loss", "label": "Validation loss", "field": "loss_display", "align": "left"},
                            {"name": "roles", "label": "Selection", "field": "roles", "align": "left"},
                            {"name": "retention", "label": "Retention", "field": "retention", "align": "right"},
                        ]
                        labels["checkpoint_table"] = ui.table(columns=columns, rows=[], row_key="step", pagination={"rowsPerPage": 12, "sortBy": "step", "descending": True}).props("flat dense hide-bottom").classes("checkpoint-table w-full")
                        labels["checkpoint_table"].add_slot("body-cell-roles", '<q-td :props="props"><span v-if="props.row.best" class="table-tag best-tag">Best</span><span v-if="props.row.latest" class="table-tag latest-tag">Latest</span><span v-if="!props.row.best && !props.row.latest" class="muted">—</span></q-td>')
                        label("checkpoint_note", "The first checkpoint appears after its full validation completes.", "chart-note")
                    with ui.card().classes("panel config-panel"):
                        with ui.row().classes("w-full justify-between items-start card-heading"):
                            ui.label("Run configuration").classes("card-title")
                            label("run_started", "Started —", "card-subtitle")
                        with ui.element("div").classes("config-grid"):
                            for key, title in [("config_model", "Model"), ("config_epoch", "Training plan"), ("config_batch", "Effective batch"), ("config_length", "Context limit"), ("config_lora", "LoRA rank"), ("config_lr", "Peak learning rate"), ("config_schedule", "Schedule"), ("config_quantization", "Quantization"), ("config_validation", "Validation cadence"), ("config_retention", "Checkpoint retention"), ("config_seed", "Random seed"), ("config_cap", "Allocator cap")]:
                                with ui.element("div").classes("config-item"):
                                    ui.label(title).classes("config-label")
                                    label(key, "—", "config-value")
                        with ui.element("div").classes("dataset-counts"):
                            for key, title in [("train_count", "TRAINING EXAMPLES"), ("validation_count", "VALIDATION EXAMPLES"), ("test_count", "HELD-OUT TEST EXAMPLES")]:
                                with ui.element("div"):
                                    label(key, "—", "dataset-number")
                                    ui.label(title).classes("eyebrow")
                    with ui.element("footer").classes("page-footer"):
                        ui.label("posttrain").classes("footer-brand")
                        ui.link("Legal & Privacy", "/legal").classes("text-stone-500 text-xs")
                        label("footer", "Aggregate metrics only · updates every 30 seconds")

        def update_charts() -> None:
            snapshot = state["snapshot"]
            history = snapshot.get("history", {})
            run = snapshot.get("run", {})
            meta = snapshot.get("meta", {})
            training = sorted(history.get("training", []), key=lambda row: number(row.get("step")) or 0)
            validation = sorted(history.get("validation", []), key=lambda row: number(row.get("step")) or 0)
            resources = history.get("resources", [])
            last_step = number(run.get("step")) or 0
            minimum = 0 if state["range"] == "all" else max(0, last_step - int(state["range"]) + 1)
            selected = [row for row in training if (number(row.get("step")) or 0) >= minimum]
            smooth = [(row, value) for row, value in ema(training) if (number(row.get("step")) or 0) >= minimum]
            time_axis = state["axis"] == "time"
            elapsed_by_step = {row.get("step"): number(row.get("elapsed_seconds")) for row in training}

            def x(row: dict) -> float | None:
                if not time_axis:
                    return number(row.get("step"))
                elapsed = number(row.get("elapsed_seconds"))
                if "baseline" in row:
                    before = 0 if row.get("baseline") else elapsed_by_step.get(row.get("step"))
                    seconds = number(row.get("seconds"))
                    elapsed = before + seconds if before is not None and seconds is not None else None
                return elapsed / 3600 if elapsed is not None else None

            def points(rows: list[dict], field: str, transform: Any = None) -> list:
                result = []
                previous_step = None
                for row in rows:
                    xv, yv = x(row), number(row.get(field))
                    step_value = number(row.get("step"))
                    if xv is not None:
                        if "baseline" not in row and previous_step is not None and step_value is not None and step_value > previous_step + 1:
                            result.append([xv, None])
                        result.append([xv, transform(yv) if transform and yv is not None else yv])
                    previous_step = step_value
                return result

            def base(y_name: str = "", second: str | None = None) -> dict:
                options = chart_options(x_name="Elapsed training time · h" if time_axis else "Optimizer update", y_name=y_name, second_axis=second)
                if time_axis:
                    options["xAxis"]["minInterval"] = None
                return options

            def apply(key: str, options: dict) -> None:
                charts[key].options.update(options)
                charts[key].update()

            full_points = [row for row in validation if not row.get("baseline") and (number(row.get("step")) or 0) >= minimum]
            loss_options = base("Loss")
            loss_series = []
            if state["raw"]:
                loss_series.append(line_series("Raw update", points(selected, "loss"), "#aebfba", lineStyle={"width": 1, "color": "#aebfba", "opacity": .65}, z=1))
            smooth_points = []
            previous_smooth_step = None
            for row, value in smooth:
                xv, step_value = x(row), number(row.get("step"))
                if xv is not None:
                    if previous_smooth_step is not None and step_value is not None and step_value > previous_smooth_step + 1:
                        smooth_points.append([xv, None])
                    smooth_points.append([xv, value])
                previous_smooth_step = step_value
            loss_series.append(line_series("Smoothed · EMA 20", smooth_points, TEAL, z=3))
            loss_series.append(line_series("Full validation", points(full_points, "loss"), COPPER, showSymbol=True, symbolSize=10, lineStyle={"width": 1.5, "color": COPPER, "type": "dashed"}, z=5))
            loss_options["series"] = loss_series
            apply("loss", loss_options)
            notes["loss"].set_text("EMA uses a 20-update span. Toggle raw updates to inspect the original measurements." if training else "Training history is not available yet. No earlier updates have been invented.")
            if training and time_axis:
                notes["loss"].set_text("Elapsed training time includes validation, excludes model loading. Full validation markers occur at pass completion.")
            if training and meta.get("history_partial"):
                notes["loss"].set_text(f"Available training history starts at update {fmt(training[0].get('step'))}. EMA uses a 20-update span.")
            if training and (number(meta.get("training_updates_missing_count")) or 0) > 0:
                notes["loss"].set_text(f"{fmt(meta.get('training_updates_missing_count'))} completed updates are unavailable; gaps are not filled. EMA uses a 20-update span.")

            chosen_validation = [row for row in validation if (state["baseline"] or not row.get("baseline")) and (number(row.get("step")) or 0) >= minimum]
            validation_options = base("Loss")
            validation_options["series"] = [line_series("Full validation", points(chosen_validation, "loss"), COPPER, showSymbol=True, symbolSize=10, lineStyle={"width": 2, "color": COPPER}, z=4)]
            apply("validation", validation_options)
            notes["validation"].set_text("Baseline is update 0. Every displayed point is a complete validation pass." if validation else "The first complete validation result has not been recorded yet.")

            gradient_options = base("Pre-clip norm")
            gradient_options["yAxis"][0]["type"] = "log" if state["log_gradient"] else "value"
            gradient_options["yAxis"][0]["min"] = .1 if state["log_gradient"] else 0
            gradient_options["series"] = [line_series("Pre-clip norm", points(selected, "gradient_norm"), TEAL, markLine={"symbol": "none", "silent": True, "lineStyle": {"color": COPPER, "type": "dashed", "width": 1}, "label": {"formatter": "clip 1.0", "position": "insideEndTop", "color": COPPER}, "data": [{"yAxis": 1}]})]
            apply("gradient", gradient_options)
            notes["gradient"].set_text("Spikes are shown at their true value. The dashed line is the clipping threshold.")

            lr_options = base("Learning rate")
            lr_options["yAxis"][0]["min"] = 0
            lr_options["yAxis"][0]["axisLabel"][":formatter"] = "v => v === 0 ? '0' : v.toExponential(1)"
            lr_options["series"] = [line_series("Learning rate", points(selected, "learning_rate"), TEAL, areaStyle={"color": "#31595c0a"})]
            apply("lr", lr_options)
            notes["lr"].set_text("The rate used for each optimizer update, rather than the following update.")

            speed_options = base("Seconds / update", "Input tokens / s")
            throughput = []
            for row in selected:
                seconds, tokens = number(row.get("step_seconds")), number(row.get("tokens"))
                if x(row) is not None and seconds and tokens is not None:
                    throughput.append([x(row), tokens / seconds])
            speed_options["series"] = [line_series("Update time · s", points(selected, "step_seconds"), TEAL), line_series("Input throughput · tokens/s", throughput, COPPER, yAxisIndex=1)]
            apply("speed", speed_options)
            notes["speed"].set_text("Update timing excludes the full validation passes and checkpoint saves.")

            def resource_points(field: str, divisor: float = 1) -> list:
                return [[float(row["timestamp"]) * 1000, float(row[field]) / divisor] for row in resources if number(row.get("timestamp")) is not None and number(row.get(field)) is not None]

            def resource_axes(options: dict) -> None:
                times = [float(row["timestamp"]) * 1000 for row in resources if number(row.get("timestamp")) is not None]
                if times:
                    padding = max(15000, (max(times) - min(times)) * .02)
                    options["xAxis"].update({"min": min(times) - padding, "max": max(times) + padding})
                options["xAxis"]["axisLabel"][":formatter"] = "v => new Date(v).toLocaleTimeString(undefined, {hour:'2-digit',minute:'2-digit',second:'2-digit'})"

            gpu_options = chart_options(x_type="time", x_name="Local time", y_name="GiB", second_axis="Utilization · %")
            resource_axes(gpu_options)
            gpu_options["yAxis"][1].update({"min": 0, "max": 100})
            gpu_options["series"] = [line_series("GPU memory · GiB", resource_points("gpu_used_mib", 1024), TEAL, showSymbol=True, symbolSize=4, areaStyle={"color": "#31595c0a"}), line_series("GPU utilization · %", resource_points("gpu_utilization_percent"), COPPER, showSymbol=True, symbolSize=4, yAxisIndex=1)]
            apply("gpu", gpu_options)
            host_options = chart_options(x_type="time", x_name="Local time", y_name="CPU · %", second_axis="Process RSS · GiB")
            resource_axes(host_options)
            host_options["yAxis"][0].update({"min": 0, "max": 100})
            host_options["series"] = [line_series("Host CPU · %", resource_points("cpu_percent"), TEAL, showSymbol=True, symbolSize=4), line_series("Process RSS · GiB", resource_points("process_rss_gib"), COPPER, showSymbol=True, symbolSize=4, yAxisIndex=1)]
            apply("host", host_options)
            capacity_options = chart_options(x_type="time", x_name="Local time", y_name="Available RAM · GiB", second_axis="Free disk · GiB")
            resource_axes(capacity_options)
            capacity_options["series"] = [line_series("Available RAM · GiB", resource_points("ram_available_gib"), TEAL, showSymbol=True, symbolSize=4), line_series("Free disk · GiB", resource_points("disk_free_gib"), COPPER, showSymbol=True, symbolSize=4, yAxisIndex=1)]
            apply("capacity", capacity_options)
            for key in ("gpu", "host", "capacity"):
                notes[key].set_text("Dashboard observations every 30s; source resource readings refresh about every 60s." if resources else "Resource history is not available yet. The chart will populate as observations arrive.")

        def render(snapshot: dict) -> None:
            state["snapshot"] = snapshot
            run, current, config = snapshot.get("run", {}), snapshot.get("current", {}), snapshot.get("config", {})
            checkpoints, meta = snapshot.get("checkpoints", {}), snapshot.get("meta", {})
            tz = state["timezone"]
            step, total = number(run.get("step")), number(run.get("total_steps"))
            phase, run_state = run.get("phase"), run.get("state")
            is_validation = phase == "validation" and run_state == "running"
            stale = bool(run.get("stale")) or not meta.get("source_available", False)
            status = "Validating" if is_validation else {"training": "Training", "loading": "Loading", "finalizing": "Finalizing"}.get(phase, "Running")
            if run_state != "running":
                status = {"completed": "Completed", "failed": "Run stopped", "failed_artifact_audit": "Review needed"}.get(run_state, "Waiting for run")
            labels["status"].set_text(status)
            labels["status"].classes(replace="status-pill " + ("status-warning" if stale or run_state in ("failed", "failed_artifact_audit") else "status-validation" if is_validation else "status-running"))
            labels["freshness"].set_text("Source delayed" if stale else "Source current")
            labels["synced"].set_text(f"Synced {local_time(meta.get('updated_at'), tz)}")
            labels["model"].set_text(f"{config.get('model') or 'Model unavailable'} · one full training run")
            labels["updates"].set_text(fmt(step))
            labels["updates_detail"].set_text(f"of {fmt(total)} planned updates")
            labels["loss"].set_text(fmt(current.get("loss"), 4))
            labels["loss_detail"].set_text(f"Update {fmt(step)} · completion-token mean")
            last_loss, baseline = number(checkpoints.get("last_loss")), number(checkpoints.get("baseline_loss"))
            labels["validation"].set_text(fmt(last_loss, 4))
            reduction = (1 - last_loss / baseline) * 100 if last_loss is not None and baseline else None
            labels["validation_detail"].set_text(f"{abs(reduction):.1f}% {'below' if reduction >= 0 else 'above'} baseline" if reduction is not None else "First full result pending")
            eta = number(run.get("eta_seconds"))
            labels["remaining"].set_text(duration(eta))
            labels["remaining_detail"].set_text(f"Estimated finish {local_time((number(meta.get('updated_at')) or 0) + eta, tz, date=True)}" if eta is not None else "Waiting for enough timing data")
            progress = min(1, max(0, step / total)) if step is not None and total else 0
            labels["percent"].set_text(f"{progress * 100:.1f}%")
            labels["progress_bar"].set_value(progress)
            labels["epoch"].set_text(f"1 of {fmt(config.get('epochs') or 1)}")
            train_count, accumulation = number(config.get("training_examples")), number(config.get("accumulation"))
            examples = min(train_count, step * accumulation) if train_count is not None and step is not None and accumulation is not None else None
            labels["examples"].set_text(f"{fmt(examples)} of {fmt(train_count)} training examples processed")
            labels["elapsed"].set_text(f"Elapsed {duration(run.get('elapsed_seconds'))}")
            labels["validation_strip"].set_visibility(is_validation)
            count, vtotal = number(current.get("validation_count")), number(current.get("validation_total"))
            labels["evaluation_title"].set_text(f"Full validation · update {fmt(current.get('validation_step'))}")
            labels["evaluation_count"].set_text(f"{fmt(count)} / {fmt(vtotal)} examples")
            labels["evaluation_bar"].set_value(min(1, count / vtotal) if count is not None and vtotal else 0)
            labels["evaluation_note"].set_text(f"{fmt(run.get('validation_passes_completed'))} of {fmt(run.get('total_validation_passes'))} complete passes · optimizer progress pauses during evaluation")

            notice = ""
            if not meta.get("source_available", False):
                notice = "Waiting for the training source. Available history stays visible; this page will retry automatically."
            elif stale:
                notice = f"Progress is delayed. The last source heartbeat was {duration(run.get('heartbeat_age_seconds'))} ago; the displayed values are the last available measurements."
            elif run_state in ("failed", "failed_artifact_audit"):
                notice = "The run needs review. The dashboard preserves its last recorded metrics and checkpoints."
            labels["notice"].set_text(notice)
            labels["notice"].set_visibility(bool(notice))

            labels["gpu"].set_text(f"{fmt((number(current.get('gpu_used_mib')) or 0) / 1024, 1)} GiB" if number(current.get("gpu_used_mib")) is not None else "—")
            labels["gpu_detail"].set_text(f"of {fmt((number(current.get('gpu_total_mib')) or 0) / 1024, 1)} GiB · {fmt(current.get('gpu_utilization_percent'))}% utilization")
            labels["ram"].set_text(f"{fmt(current.get('ram_available_gib'), 1)} GiB")
            labels["ram_detail"].set_text(f"{fmt(current.get('process_rss_gib'), 1)} GiB training process RSS")
            labels["disk"].set_text(f"{fmt(current.get('disk_free_gib'), 1)} GiB")
            labels["disk_detail"].set_text(f"of {fmt(current.get('disk_total_gib'), 1)} GiB total")
            labels["cpu"].set_text(f"{fmt(current.get('cpu_percent'))}%")
            labels["cpu_detail"].set_text("Host utilization")
            since = meta.get("resource_history_since")
            labels["resource_since"].set_text(f"Resource history since {local_time(since, tz, date=True)} · {state['timezone_name']} · updates every 30 seconds" if since else "Resource collection has not started yet.")

            retained = set(checkpoints.get("retained_steps", []))
            validations = {row.get("step"): row for row in snapshot.get("history", {}).get("validation", []) if not row.get("baseline")}
            steps = sorted(retained | set(validations), reverse=True)
            rows = [{"step": s, "loss_display": fmt(validations.get(s, {}).get("loss"), 4), "best": s == checkpoints.get("best_step"), "latest": s == checkpoints.get("latest_step"), "retention": "Retained" if s in retained else "Pruned"} for s in steps]
            labels["checkpoint_table"].rows = rows
            labels["checkpoint_table"].update()
            labels["checkpoint_count"].set_text(f"{fmt(checkpoints.get('completed_count'))} saved · {len(retained)} retained")
            labels["checkpoint_note"].set_text("Best validation checkpoint and the latest two are retained. Loss values refer to complete validation passes." if rows else "The first checkpoint appears after its full validation completes.")
            labels["run_started"].set_text(f"Started {local_time(run.get('started_at'), tz, date=True)} · {state['timezone_name']}")
            config_values = {
                "config_model": config.get("model") or "—", "config_epoch": f"{fmt(config.get('epochs'))} epoch · no repeat",
                "config_batch": f"{fmt(config.get('effective_batch_size'))} ({fmt(config.get('micro_batch_size'))} × {fmt(config.get('accumulation'))})",
                "config_length": f"{fmt(config.get('max_length'))} tokens", "config_lora": fmt(config.get("lora_rank")),
                "config_lr": f"{number(config.get('learning_rate')):.1e}" if number(config.get("learning_rate")) is not None else "—",
                "config_schedule": f"{fmt(config.get('warmup_steps'))} warmup · cosine", "config_quantization": "NF4 · double · FP32" if config.get("quantization") else "—",
                "config_validation": f"Every {fmt(config.get('eval_every'))} updates + final", "config_retention": "Best + latest two" if config.get("checkpoint_retention") else "—",
                "config_seed": fmt(config.get("seed")), "config_cap": f"{fmt(config.get('memory_gib'), 0)} GiB",
                "train_count": fmt(config.get("training_examples")), "validation_count": fmt(config.get("validation_examples")), "test_count": fmt(config.get("test_examples")),
            }
            for key, value in config_values.items():
                labels[key].set_text(value)
            labels["footer"].set_text(f"Aggregate metrics only · updates every 30 seconds · times in {state['timezone_name']}")
            update_charts()

        async def refresh(*, force: bool = False) -> None:
            if state["refreshing"]:
                return
            state["refreshing"] = True
            try:
                if not force:
                    try:
                        if await ui.run_javascript("document.hidden", timeout=2):
                            return
                    except (TimeoutError, RuntimeError):
                        return
                try:
                    zone = await ui.run_javascript("Intl.DateTimeFormat().resolvedOptions().timeZone", timeout=2)
                    if isinstance(zone, str):
                        state["timezone"], state["timezone_name"] = ZoneInfo(zone), zone
                except (TimeoutError, RuntimeError, ValueError, KeyError):
                    pass
                snapshot = await asyncio.to_thread(store.snapshot, force=force)
                if not force and snapshot.get("meta", {}).get("updated_at") == state["snapshot"].get("meta", {}).get("updated_at"):
                    snapshot = await asyncio.to_thread(store.snapshot, force=True)
                render(snapshot)
            except Exception:
                labels["notice"].set_text("The dashboard could not refresh just now. Last available values stay visible; it will retry in 30 seconds.")
                labels["notice"].set_visibility(True)
                labels["freshness"].set_text("Refresh delayed")
            finally:
                state["refreshing"] = False

        try:
            render(await asyncio.to_thread(store.snapshot))
        except Exception:
            render({})
        ui.timer(.2, lambda: refresh(force=True), once=True)
        ui.timer(30.0, refresh)


def main() -> None:
    store = MetricsStore(
        source_root=Path(os.environ.get("POSTTRAIN_SOURCE_ROOT", "/home/dirigent/group-member")),
        storage_dir=Path(os.environ.get("POSTTRAIN_DATA_DIR", str(HERE / "runtime"))),
    )
    configure(store, AuthSettings.from_env())
    ui.run(
        host=os.environ.get("POSTTRAIN_HOST", "127.0.0.1"),
        port=int(os.environ.get("POSTTRAIN_PORT", "8766")),
        title="Posttrain · training monitor", favicon=str(STATIC / "favicon.svg"),
        reload=False, show=False, dark=False, access_log=False,
    )


if __name__ == "__main__":
    main()
