"""Read-only, allowlisted training aggregates; no training or model dependencies."""
from copy import deepcopy
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
import time

REFRESH_SECONDS = 30
STATES = {"running", "completed", "failed", "failed_artifact_audit"}
PHASES = {"loading", "training", "validation", "finalizing"}
EVENT_PHASES = {"final_run_preflight", "validation_start", "validation_progress",
                "validation_complete", "training", "checkpoint_complete", "training_complete"}
TRAIN_FIELDS = ("step", "loss", "gradient_norm", "learning_rate", "step_seconds",
                "tokens", "supervised_tokens", "examples", "elapsed_seconds")
RESOURCE_FIELDS = ("gpu_used_mib", "gpu_total_mib", "gpu_utilization_percent",
                   "ram_available_gib", "ram_total_gib", "disk_free_gib", "disk_total_gib",
                   "cpu_percent", "process_rss_gib")
AGGREGATES = {"configs/final-run.json", "data/manifest.json", "runs/final-preparation.json",
              "runs/monitor/latest.json", "runs/full/checkpoints.json"}


def numeric(value, *, integer=False):
    if type(value) not in (int, float) or value < 0 or value > 1e15 or not math.isfinite(value):
        return None
    if integer and (type(value) is not int or value > 10_000_000):
        return None
    return value


def boolean(value):
    return value if type(value) is bool else None


def object_value(value):
    return value if isinstance(value, dict) else {}


def timestamp(value):
    if numeric(value) is not None:
        return numeric(value)
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return numeric(parsed.timestamp()) if parsed.tzinfo else None
    except (ValueError, OverflowError, OSError):
        return None


class MetricsStore:
    """Thread-safe 30-second snapshots containing only fixed-schema values.

    Supervisor event files were reviewed against monitor_training.py's producer
    allowlist. Every decoded event is independently reduced here: unknown keys,
    paths, commands, error strings, message text, and nonfinite numbers disappear.
    Resource history starts when this dashboard first observes resources.
    """

    def __init__(self, source_root=Path("/home/dirigent/group-member"), storage_dir=None, *, clock=time.time):
        self.root = Path(source_root)
        self.storage = Path(storage_dir) if storage_dir is not None else None
        self.clock = clock
        self._lock = threading.RLock()
        self._cached = None
        self._cached_at = float("-inf")
        self._files = {}
        self._training = {}
        self._validation = {}
        self._checkpoints = set()
        self._latest_training = {}
        self._resources = []
        self._resource_since = None
        self._errors = 0
        self._history_partial = False
        self._resource_persisted = False
        self._load_resource_history()

    def _path(self, relative):
        current = self.root
        if Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise OSError("disallowed_source")
        for part in Path(relative).parts:
            current = current / part
            if current.is_symlink():
                raise OSError("source_symlink")
        return current

    def _aggregate(self, relative):
        if relative not in AGGREGATES:
            raise ValueError("aggregate_not_allowlisted")
        try:
            path = self._path(relative)
            if not path.exists():
                return {}
            if path.stat().st_size > 8 * 1024 * 1024:
                raise ValueError("aggregate_too_large")
            return object_value(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, UnicodeError):
            self._errors += 1
            return {}

    def _event(self, value):
        if not isinstance(value, dict):
            return
        phase = value.get("phase")
        if not isinstance(phase, str) or phase not in EVENT_PHASES:
            if phase == "error" or value.get("status") == "failed":
                self._errors += 1
            return
        step = numeric(value.get("step"), integer=True)
        if phase == "training" and step is not None and step > 0:
            row = {key: numeric(value.get(key), integer=key in {"step", "tokens", "supervised_tokens", "examples"})
                   for key in TRAIN_FIELDS}
            # The trainer emits this record only after optimizer.step(). Start
            # markers or incomplete diagnostics must never become chart points.
            required_positive = ("gradient_norm", "step_seconds", "tokens", "supervised_tokens", "examples")
            if (row["loss"] is None or row["learning_rate"] is None or row["elapsed_seconds"] is None
                    or any(row[key] is None or row[key] <= 0 for key in required_positive)):
                self._errors += 1
                self._history_partial = True
                return
            self._training[step] = row
            self._latest_training = {**row, **{key: numeric(value.get(key)) for key in
                                     ("peak_allocated_gib", "peak_reserved_gib")}}
        elif phase == "validation_complete" and step is not None:
            loss = numeric(value.get("validation_loss"))
            baseline = boolean(value.get("baseline"))
            examples = numeric(value.get("validation_examples"), integer=True)
            if loss is None or baseline is not (step == 0) or examples is None or examples == 0:
                self._errors += 1
                return
            self._validation[(step, baseline)] = {
                "step": step, "baseline": baseline, "loss": loss,
                "examples": examples,
                "supervised_tokens": numeric(value.get("supervised_tokens", value.get("validation_supervised_tokens")), integer=True),
                "seconds": numeric(value.get("evaluation_seconds")),
            }
        elif phase == "checkpoint_complete" and step is not None and step > 0:
            self._checkpoints.add(step)

    def _scan_events(self):
        available = False
        try:
            directory = self._path("runs/monitor")
            paths = sorted(path for path in directory.glob("events-*.jsonl")
                           if re.fullmatch(r"events-[0-9]{8}T[0-9]{6}Z\.jsonl", path.name))
            for path in paths:
                relative = path.relative_to(self.root).as_posix()
                path = self._path(relative)
                stat = path.stat()
                available = True
                prior = self._files.get(relative)
                if prior and prior[0] == stat.st_ino and stat.st_size >= prior[1]:
                    offset, pending = prior[1], prior[2]
                else:
                    offset, pending = 0, b""
                with path.open("rb") as stream:
                    stream.seek(offset)
                    block = stream.read(4 * 1024 * 1024)
                    offset = stream.tell()
                lines = (pending + block).split(b"\n")
                pending = lines.pop()
                if len(pending) > 65536:
                    pending = b""
                    self._history_partial = True
                for line in lines:
                    if len(line) > 65536:
                        self._history_partial = True
                        continue
                    try:
                        self._event(json.loads(line))
                    except (ValueError, UnicodeError):
                        self._history_partial = True
                self._files[relative] = (stat.st_ino, offset, pending)
                if offset < stat.st_size:
                    self._history_partial = True
        except OSError:
            self._errors += 1
            self._history_partial = True
        return available

    @staticmethod
    def _resource_point(value):
        if not isinstance(value, dict) or numeric(value.get("timestamp")) is None:
            return None
        return {"timestamp": numeric(value["timestamp"]),
                **{key: numeric(value.get(key)) for key in RESOURCE_FIELDS}}

    def _load_resource_history(self):
        if self.storage is None:
            return
        try:
            path = self.storage / "resource-history.json"
            if path.is_symlink() or not path.exists():
                return
            if path.stat().st_size > 8 * 1024 * 1024:
                raise ValueError("resource_history_too_large")
            value = object_value(json.loads(path.read_text(encoding="utf-8")))
            rows = value.get("points", [])
            if not isinstance(rows, list):
                raise ValueError("invalid_resource_history")
            self._resources = [point for row in rows[-10000:]
                               if (point := self._resource_point(row)) is not None]
            self._resource_since = numeric(value.get("since"))
            self._resource_persisted = True
        except (OSError, ValueError, UnicodeError):
            self._errors += 1

    def _save_resource_history(self):
        if self.storage is None:
            return
        temporary = None
        try:
            if self.storage.is_symlink():
                raise OSError("state_symlink")
            self.storage.mkdir(mode=0o700, parents=True, exist_ok=True)
            target = self.storage / "resource-history.json"
            if target.is_symlink():
                raise OSError("state_symlink")
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.storage,
                                             prefix=".resources-", delete=False) as stream:
                temporary = stream.name
                json.dump({"since": self._resource_since, "points": self._resources}, stream, allow_nan=False)
                stream.write("\n")
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
            self._resource_persisted = True
        except OSError:
            self._errors += 1
        finally:
            if temporary and Path(temporary).exists():
                Path(temporary).unlink()

    def _resource_values(self, monitor):
        source = object_value(monitor.get("resources"))
        result = {key: numeric(source.get(key)) for key in
                  ("gpu_used_mib", "gpu_total_mib", "gpu_utilization_percent", "process_rss_gib")}
        result.update(ram_available_gib=numeric(source.get("host_available_ram_gib")),
                      disk_free_gib=numeric(source.get("disk_free_gib")),
                      cpu_percent=numeric(source.get("host_cpu_percent")), ram_total_gib=None,
                      disk_total_gib=None)
        try:
            result["ram_total_gib"] = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024**3
            result["disk_total_gib"] = shutil.disk_usage(self.root).total / 1024**3
        except (OSError, ValueError):
            pass
        for key in ("gpu_utilization_percent", "cpu_percent"):
            if result[key] is not None and result[key] > 100:
                result[key] = None
        return result

    def snapshot(self, *, force=False):
        with self._lock:
            now = self.clock()
            if not force and self._cached is not None and now - self._cached_at < REFRESH_SECONDS:
                return deepcopy(self._cached)
            monitor = self._aggregate("runs/monitor/latest.json")
            config = self._aggregate("configs/final-run.json")
            manifest = self._aggregate("data/manifest.json")
            prepared = self._aggregate("runs/final-preparation.json")
            inventory = self._aggregate("runs/full/checkpoints.json")
            events_available = self._scan_events()
            training, validation, model = (object_value(config.get(key)) for key in ("training", "validation", "model"))
            dataset = object_value(config.get("dataset"))
            splits = object_value(manifest.get("splits"))
            total = numeric(monitor.get("total_steps"), integer=True) or numeric(training.get("optimizer_updates"), integer=True)
            step = numeric(monitor.get("training_step"), integer=True)
            # The event file can advance between reading latest.json and reading
            # events. Keep the displayed history at the same completed update as
            # the supervisor snapshot; the next refresh releases newer points.
            visible_steps = sorted(index for index in self._training
                                   if (step is None or index <= step) and (total is None or index <= total))
            current_training = self._training[visible_steps[-1]] if visible_steps else {}
            missing_updates = max(0, step - len(visible_steps)) if step is not None else None
            state = monitor.get("state") if isinstance(monitor.get("state"), str) and monitor["state"] in STATES else "unavailable"
            phase = monitor.get("phase") if isinstance(monitor.get("phase"), str) and monitor["phase"] in PHASES else "unknown"
            started = timestamp(monitor.get("started_at"))
            heartbeat = timestamp(monitor.get("heartbeat_at"))
            heartbeat_age = max(0, now - heartbeat) if heartbeat is not None else None
            elapsed = numeric(monitor.get("elapsed_seconds"))
            if elapsed is None and started is not None:
                elapsed = max(0, now - started)
            hours = numeric(monitor.get("estimated_remaining_hours"))
            eta = hours * 3600 if hours is not None else None
            if state == "completed":
                eta = 0
            resource = self._resource_values(monitor)
            if any(resource[key] is not None for key in ("gpu_used_mib", "ram_available_gib", "cpu_percent")):
                # The producer exposes no resource acquisition timestamp. This
                # is its heartbeat observation of values refreshed about 60s.
                sample_time = heartbeat if heartbeat is not None else now
                if not self._resources or sample_time > self._resources[-1]["timestamp"]:
                    self._resource_since = self._resource_since if self._resource_since is not None else now
                    self._resources.append({"timestamp": sample_time, **resource})
                    self._resources = self._resources[-10000:]
                    self._save_resource_history()
            current = {"loss": numeric(monitor.get("last_training_loss")),
                       "gradient_norm": numeric(monitor.get("last_gradient_norm")),
                       "learning_rate": numeric(monitor.get("learning_rate")),
                       "step_seconds": numeric(monitor.get("recent_seconds_per_optimizer_update")),
                       "validation_count": numeric(monitor.get("validation_examples_completed"), integer=True),
                       "validation_total": numeric(monitor.get("validation_examples_total"), integer=True),
                       "validation_step": numeric(monitor.get("validation_step"), integer=True),
                       "validation_loss": numeric(monitor.get("validation_loss_so_far" if phase == "validation" else "last_validation_loss")),
                       "validation_baseline": boolean(monitor.get("validation_baseline")),
                       "validation_elapsed_seconds": numeric(monitor.get("validation_elapsed_seconds")),
                       "peak_reserved_gib": numeric(monitor.get("peak_reserved_gib")),
                       "peak_allocated_gib": self._latest_training.get("peak_allocated_gib"),
                       **resource}
            for key in ("loss", "gradient_norm", "learning_rate", "step_seconds", "tokens", "supervised_tokens", "examples"):
                if current.get(key) is None:
                    current[key] = current_training.get(key)
            if phase == "validation" and (current["validation_count"] is None or current["validation_count"] == 0):
                # validation_start resets only count/step in the producer;
                # previous-pass loss and elapsed fields remain until progress.
                current["validation_loss"] = None
                current["validation_elapsed_seconds"] = None
            evaluation_rows = inventory.get("evaluations", [])
            if isinstance(evaluation_rows, list):
                for row in evaluation_rows:
                    if isinstance(row, dict):
                        self._event({**row, "phase": "validation_complete"})
            validation_history = sorted(self._validation.values(), key=lambda row: (row["step"], not row["baseline"]))
            if phase != "validation" and validation_history:
                current["validation_elapsed_seconds"] = validation_history[-1]["seconds"]
            nonbaseline = [row for row in validation_history if not row["baseline"]]
            best = min(nonbaseline, key=lambda row: row["loss"]) if nonbaseline else None
            latest = None
            if isinstance(inventory.get("latest_checkpoint"), str):
                match = re.fullmatch(r"checkpoint-([1-9][0-9]*)", inventory["latest_checkpoint"])
                latest = numeric(int(match[1]), integer=True) if match else None
            retained = inventory.get("checkpoint_steps", [])
            retained = sorted({value for value in retained if numeric(value, integer=True) is not None}) if isinstance(retained, list) else []
            interval = numeric(training.get("save_every"), integer=True)
            completed_count = len(self._checkpoints)
            if latest is not None and interval:
                completed_count = max(completed_count, latest // interval + int(latest % interval != 0))
            estimate = object_value(prepared.get("wallclock_estimate"))
            point_hours = numeric(estimate.get("point_hours"))
            config_view = {"training_examples": numeric(dataset.get("train_examples"), integer=True),
                           "validation_examples": numeric(dataset.get("validation_examples"), integer=True),
                           "test_examples": numeric(object_value(splits.get("test")).get("examples"), integer=True),
                           **{key: numeric(training.get(key), integer=key not in {"learning_rate", "memory_gib", "epochs"})
                              for key in ("max_length", "micro_batch_size", "accumulation", "epochs", "learning_rate", "warmup_steps", "save_every", "memory_gib", "seed")},
                           "lora_rank": numeric(model.get("lora_rank"), integer=True),
                           "eval_every": numeric(validation.get("eval_every"), integer=True),
                           "quantization": "nf4-double-fp32" if model.get("quantization") == "nf4-double-fp32" else "unknown",
                           "scheduler": "cosine" if training.get("scheduler") == "cosine" else "unknown",
                           "model": "gemma-4-e2b" if model.get("id") == "google/gemma-4-E2B-it" else "unknown",
                           "checkpoint_retention": "best_and_last_two" if training.get("checkpoint_retention") == "best_and_last_two" else "unknown",
                           "estimated_total_seconds": point_hours * 3600 if point_hours is not None else None}
            batch, accumulation = config_view["micro_batch_size"], config_view["accumulation"]
            config_view["effective_batch_size"] = batch * accumulation if batch is not None and accumulation is not None else None
            result = {"schema_version": 1,
                      "run": {"step": step, "total_steps": total,
                              "progress_percent": min(100, step / total * 100) if step is not None and total else None,
                              "state": state, "phase": phase, "elapsed_seconds": elapsed, "eta_seconds": eta,
                              "started_at": started, "heartbeat_at": heartbeat, "heartbeat_age_seconds": heartbeat_age,
                              "stale": state == "unavailable" or (state == "running" and (heartbeat_age is None or heartbeat_age > 120)),
                              "validation_passes_completed": numeric(monitor.get("validation_passes_completed"), integer=True),
                              "total_validation_passes": numeric(monitor.get("total_validation_passes"), integer=True),
                              "resumed": boolean(monitor.get("resumed")),
                              "completion_audit_passed": boolean(object_value(monitor.get("completion_audit")).get("passed"))},
                      "current": current,
                      "history": {"training": [self._training[key] for key in visible_steps],
                                  "validation": validation_history, "resources": self._resources},
                      "config": config_view,
                      "checkpoints": {"best_step": best["step"] if best else None, "latest_step": latest,
                                      "retained_steps": retained, "best_loss": best["loss"] if best else None,
                                      "baseline_loss": next((row["loss"] for row in validation_history if row["baseline"]), None),
                                      "last_loss": validation_history[-1]["loss"] if validation_history else None,
                                      "completed_count": completed_count},
                      "meta": {"updated_at": now, "refresh_seconds": REFRESH_SECONDS,
                               "source_available": bool(monitor), "event_history_available": events_available,
                               "resource_history_since": self._resource_since,
                               "resource_history_persisted": self._resource_persisted,
                               "resource_timestamp_semantics": "monitor_heartbeat",
                               "resource_source_interval_seconds": 60,
                               "training_records_count": len(visible_steps),
                               "training_updates_missing_count": missing_updates,
                               "history_partial": self._history_partial or not events_available or bool(missing_updates),
                               "error_count": self._errors + (len(monitor["errors"]) if isinstance(monitor.get("errors"), list) else 0)}}
            self._cached, self._cached_at = result, now
            return deepcopy(result)
