"""Wholly invented fixtures; counts and metrics do not describe a real run."""
import json
from pathlib import Path

from metrics import MetricsStore


def write_json(root, relative, value):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def fixture_source(tmp_path):
    root = tmp_path / "synthetic-source"
    write_json(root, "configs/final-run.json", {
        "model": {"id": "google/gemma-4-E2B-it", "lora_rank": 8, "quantization": "nf4-double-fp32"},
        "dataset": {"train_examples": 12345, "validation_examples": 321},
        "training": {"optimizer_updates": 80, "max_length": 64, "micro_batch_size": 2,
                     "accumulation": 4, "epochs": 3, "learning_rate": .0001,
                     "warmup_steps": 5, "save_every": 10, "memory_gib": 6,
                     "seed": 7, "scheduler": "cosine", "checkpoint_retention": "best_and_last_two"},
        "validation": {"eval_every": 10}})
    write_json(root, "data/manifest.json", {"splits": {"test": {"examples": 111}},
                                            "private_field": "PRIVATE_MESSAGE_SENTINEL"})
    write_json(root, "runs/final-preparation.json", {"wallclock_estimate": {"point_hours": 3.5}})
    write_json(root, "runs/monitor/latest.json", {
        "state": "running", "phase": "validation", "training_step": 30, "total_steps": 80,
        "elapsed_seconds": 120, "estimated_remaining_hours": .25, "heartbeat_at": 999,
        "started_at": "1970-01-01T00:00:01+00:00", "resumed": False,
        "last_training_loss": 4.0, "last_gradient_norm": .75, "learning_rate": .00008,
        "recent_seconds_per_optimizer_update": 32, "validation_step": 30,
        "validation_examples_completed": 40, "validation_examples_total": 321,
        "validation_loss_so_far": 3.4, "validation_elapsed_seconds": 300,
        "validation_baseline": False, "validation_passes_completed": 3, "total_validation_passes": 12,
        "resources": {"gpu_used_mib": 4096, "gpu_total_mib": 8192, "gpu_utilization_percent": 99,
                      "host_available_ram_gib": 8, "host_cpu_percent": 35, "disk_free_gib": 45,
                      "process_rss_gib": 7, "pipeline_processes": [{"command": "PRIVATE_COMMAND_SENTINEL"}]},
        "errors": ["PRIVATE_ERROR_SENTINEL"], "path": "/private/forbidden"})
    baseline = {"step": 0, "baseline": True, "validation_loss": 9.0,
                "validation_examples": 321, "validation_supervised_tokens": 4567,
                "evaluation_seconds": 1800}
    validation = {"step": 20, "baseline": False, "validation_loss": 2.75,
                  "validation_examples": 321, "validation_supervised_tokens": 4567,
                  "evaluation_seconds": 1810}
    write_json(root, "runs/full/checkpoints.json", {
        "latest_checkpoint": "checkpoint-20", "best_checkpoint": "/private/forbidden",
        "checkpoint_steps": [10, 20], "evaluations": [baseline, validation],
        "private_path": "/private/checkpoint"})
    events = root / "runs/monitor/events-20990101T000000Z.jsonl"
    rows = [
        {"phase": "final_run_preflight", "command": "PRIVATE_COMMAND_SENTINEL"},
        {"phase": "training", "step": 1, "loss": 4.25, "gradient_norm": .8,
         "learning_rate": .0001, "step_seconds": 31, "tokens": 200,
         "supervised_tokens": 100, "examples": 8, "elapsed_seconds": 32,
         "peak_allocated_gib": 3, "text": "PRIVATE_MESSAGE_SENTINEL"},
        {"phase": "validation_complete", **baseline},
        {"phase": "checkpoint_complete", "step": 10},
        {"phase": "checkpoint_complete", "step": 20},
    ]
    events.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return root, events


def test_snapshot_is_allowlisted_numeric_and_never_opens_private_files(tmp_path, monkeypatch):
    root, _ = fixture_source(tmp_path)
    original = Path.open

    def guard(path, *args, **kwargs):
        relative = path.relative_to(root).as_posix() if path.is_relative_to(root) else ""
        assert relative in {"configs/final-run.json", "data/manifest.json", "runs/final-preparation.json",
                            "runs/monitor/latest.json", "runs/full/checkpoints.json",
                            "runs/monitor/events-20990101T000000Z.jsonl"}
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guard)
    result = MetricsStore(root, clock=lambda: 1000).snapshot()
    encoded = json.dumps(result, allow_nan=False)
    assert "PRIVATE" not in encoded and "/private" not in encoded
    assert result["run"]["step"] == 30 and result["run"]["eta_seconds"] == 900
    assert result["current"]["validation_count"] == 40
    assert result["history"]["training"][0]["loss"] == 4.25
    assert result["history"]["validation"][0]["supervised_tokens"] == 4567
    assert result["checkpoints"]["best_step"] == 20
    assert result["checkpoints"]["completed_count"] == 2
    assert result["config"]["effective_batch_size"] == 8
    assert result["meta"]["error_count"] == 1


def test_event_stream_incremental_partial_lines_and_cache_copy(tmp_path):
    root, events = fixture_source(tmp_path)
    now = [1000]
    store = MetricsStore(root, clock=lambda: now[0])
    first = store.snapshot()
    first["history"]["training"][0]["loss"] = 999
    assert store.snapshot()["history"]["training"][0]["loss"] == 4.25
    row = json.dumps({"phase": "training", "step": 2, "loss": 2.0, "gradient_norm": 1.,
                      "learning_rate": .0001, "step_seconds": 30, "tokens": 200,
                      "supervised_tokens": 100, "examples": 8, "elapsed_seconds": 62})
    with events.open("a") as stream:
        stream.write(row[:len(row)//2])
    assert len(store.snapshot(force=True)["history"]["training"]) == 1
    with events.open("a") as stream:
        stream.write(row[len(row)//2:] + "\n")
    now[0] += 31
    assert [row["step"] for row in store.snapshot()["history"]["training"]] == [1, 2]


def test_malformed_values_and_nonfinite_metrics_do_not_escape(tmp_path):
    root, events = fixture_source(tmp_path)
    with events.open("a") as stream:
        stream.write(json.dumps({"phase": {"private": "PRIVATE_SENTINEL"}}) + "\n")
        stream.write(json.dumps({"phase": "training", "step": 2, "loss": float("nan")}) + "\n")
        stream.write(json.dumps({"phase": "training", "step": 3, "loss": 10**20}) + "\n")
        stream.write("not-json PRIVATE_SENTINEL\n")
    value = json.loads((root / "runs/monitor/latest.json").read_text())
    value.update(state=["PRIVATE_SENTINEL"], phase={"private": "PRIVATE_SENTINEL"},
                 last_training_loss=float("inf"), last_gradient_norm=True)
    write_json(root, "runs/monitor/latest.json", value)
    result = MetricsStore(root, clock=lambda: 1000).snapshot()
    assert result["run"]["state"] == "unavailable" and result["run"]["phase"] == "unknown"
    assert len(result["history"]["training"]) == 1
    assert result["meta"]["history_partial"] is True
    assert "PRIVATE" not in json.dumps(result, allow_nan=False)


def test_symlink_event_source_is_not_followed(tmp_path):
    root, events = fixture_source(tmp_path)
    events.unlink()
    private = tmp_path / "private"
    private.write_text('{"phase":"training","step":99,"loss":123}\n')
    events.symlink_to(private)
    result = MetricsStore(root, clock=lambda: 1000).snapshot()
    assert result["history"]["training"] == []
    assert result["meta"]["history_partial"] is True


def test_resource_history_is_persisted_and_sanitized_on_reload(tmp_path):
    root, _ = fixture_source(tmp_path)
    state = tmp_path / "dashboard-state"
    first = MetricsStore(root, state, clock=lambda: 1000).snapshot()
    assert first["meta"]["resource_history_persisted"] is True
    saved = json.loads((state / "resource-history.json").read_text())
    saved["points"][0]["private_text"] = "PRIVATE_SENTINEL"
    write_json(state, "resource-history.json", saved)
    result = MetricsStore(root, state, clock=lambda: 1031).snapshot()
    assert len(result["history"]["resources"]) == 1
    assert result["meta"]["resource_history_since"] == 1000
    assert "PRIVATE" not in json.dumps(result)


def test_missing_source_is_a_safe_unavailable_snapshot(tmp_path):
    result = MetricsStore(tmp_path / "missing", clock=lambda: 1000).snapshot()
    assert result["run"]["state"] == "unavailable"
    assert result["run"]["stale"] is True
    assert result["meta"]["source_available"] is False
    assert result["history"]["training"] == []


def test_completed_run_has_zero_eta_and_is_not_reported_stale(tmp_path):
    root, _ = fixture_source(tmp_path)
    write_json(root, "runs/monitor/latest.json", {
        "state": "completed", "phase": "finalizing", "training_step": 80,
        "total_steps": 80, "heartbeat_at": 1, "completion_audit": {"passed": True}})
    result = MetricsStore(root, clock=lambda: 1000).snapshot()
    assert result["run"]["progress_percent"] == 100
    assert result["run"]["eta_seconds"] == 0
    assert result["run"]["stale"] is False
    assert result["run"]["completion_audit_passed"] is True


def training_event(step, **changes):
    return {"phase": "training", "step": step, "loss": 2., "gradient_norm": 1.,
            "learning_rate": .0001, "step_seconds": 30., "tokens": 200,
            "supervised_tokens": 100, "examples": 8, "elapsed_seconds": step * 30., **changes}


def test_completed_update_boundary_excludes_start_markers_and_newer_events(tmp_path):
    root, events = fixture_source(tmp_path)
    monitor = json.loads((root / "runs/monitor/latest.json").read_text())
    monitor["training_step"] = 2
    write_json(root, "runs/monitor/latest.json", monitor)
    with events.open("a") as stream:
        for row in ({"phase": "training", "step": 0, "loss": 0},
                    {"phase": "training", "step": 99, "loss": 0},
                    training_event(2), training_event(3, examples=11)):
            stream.write(json.dumps(row) + "\n")
    store = MetricsStore(root, clock=lambda: 1000)
    result = store.snapshot()
    assert [row["step"] for row in result["history"]["training"]] == [1, 2]
    assert result["current"]["examples"] == 8
    assert result["meta"]["training_records_count"] == 2
    assert result["meta"]["training_updates_missing_count"] == 0
    monitor["training_step"] = 3
    write_json(root, "runs/monitor/latest.json", monitor)
    assert [row["step"] for row in store.snapshot(force=True)["history"]["training"]] == [1, 2, 3]


def test_missing_update_history_is_explicit_and_never_filled_with_fake_points(tmp_path):
    root, events = fixture_source(tmp_path)
    monitor = json.loads((root / "runs/monitor/latest.json").read_text())
    monitor["training_step"] = 4
    write_json(root, "runs/monitor/latest.json", monitor)
    with events.open("a") as stream:
        stream.write(json.dumps(training_event(3)) + "\n")
    result = MetricsStore(root, clock=lambda: 1000).snapshot()
    assert [row["step"] for row in result["history"]["training"]] == [1, 3]
    assert result["meta"]["training_updates_missing_count"] == 2
    assert result["meta"]["history_partial"] is True


def test_new_validation_pass_suppresses_previous_pass_loss_and_elapsed(tmp_path):
    root, _ = fixture_source(tmp_path)
    monitor = json.loads((root / "runs/monitor/latest.json").read_text())
    monitor.update(validation_examples_completed=0, validation_loss_so_far=999,
                   validation_elapsed_seconds=1800)
    write_json(root, "runs/monitor/latest.json", monitor)
    result = MetricsStore(root, clock=lambda: 1000).snapshot()
    assert result["current"]["validation_count"] == 0
    assert result["current"]["validation_loss"] is None
    assert result["current"]["validation_elapsed_seconds"] is None
    assert result["checkpoints"]["last_loss"] == 2.75


def test_validation_completion_uses_checkpoint_index_numbers_and_full_duration(tmp_path):
    root, events = fixture_source(tmp_path)
    with events.open("a") as stream:
        stream.write(json.dumps({"phase": "validation_complete", "step": 20, "baseline": False,
                                 "validation_loss": 999, "validation_examples": 4,
                                 "evaluation_seconds": 1}) + "\n")
    monitor = json.loads((root / "runs/monitor/latest.json").read_text())
    monitor.update(phase="training", last_validation_loss=2.75,
                   validation_examples_completed=321, validation_elapsed_seconds=1700)
    write_json(root, "runs/monitor/latest.json", monitor)
    result = MetricsStore(root, clock=lambda: 1000).snapshot()
    completed = result["history"]["validation"][-1]
    assert completed["loss"] == 2.75 and completed["examples"] == 321
    assert completed["seconds"] == 1810
    assert result["current"]["validation_elapsed_seconds"] == 1810


def test_resource_units_and_timestamp_mean_monitor_observation_not_acquisition(tmp_path):
    root, _ = fixture_source(tmp_path)
    result = MetricsStore(root, clock=lambda: 1000).snapshot()
    row = result["history"]["resources"][-1]
    assert row["gpu_used_mib"] == 4096 and row["gpu_total_mib"] == 8192
    assert row["ram_available_gib"] == 8 and row["disk_free_gib"] == 45
    assert row["timestamp"] == 999
    assert result["meta"]["resource_timestamp_semantics"] == "monitor_heartbeat"
    assert result["meta"]["resource_source_interval_seconds"] == 60
