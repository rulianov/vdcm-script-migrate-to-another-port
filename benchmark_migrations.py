#!/usr/bin/env python3
"""Opt-in migration benchmark using temporary non-streaming output/service copies."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

import migrate_outputs as m


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", type=m.host_url, required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--insecure", action="store_true")
    parser.add_argument("--approve-temporary-outputs", action="store_true", required=True)
    args = parser.parse_args()
    password = os.environ.get("VDCM_PASSWORD")
    if password is None:
        raise m.MigrationError("Set VDCM_PASSWORD for the benchmark")
    binary = args.binary.resolve()
    directory = binary.parent / ("benchmark-" + uuid.uuid4().hex[:12])
    directory.mkdir(mode=0o700)
    client = m.Client(args.host, args.username, password, insecure=args.insecure)
    schemas = m.Schemas(client.request("GET", "/api/swagger.json"))
    migrator = m.Migrator(client, schemas, directory)
    config = client.request("GET", "/api/v2/AppSettings/Xgress/Configuration")
    source = m.resolve_port(config, args.source)["interface"]
    destination = m.resolve_port(config, args.destination)["interface"]
    original = m.rows(client.request("GET", m.TS), "OutputTSs")
    templates = [v for v in original if v.get("outputType") == "XGRESS"
                 and v.get("interface") == source and v.get("streaming") == "Off"]
    if source == destination or len(templates) < 2:
        raise m.MigrationError("Benchmark requires two non-streaming templates and distinct ports")
    snapshots = [migrator.snapshot(v["id"]) for v in templates[:2]]
    for snapshot in snapshots:
        migrator.ensure_no_hidden_secrets(snapshot)
    folder = "BENCHMARK-" + uuid.uuid4().hex[:12]
    state = {"folder": folder, "original_inventory": original, "owned": {}, "runs": [],
             "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest()}

    def checkpoint() -> None:
        m.atomic_json(directory / "benchmark.json", state)

    checkpoint()
    current_ids = []
    try:
        used_targets = {(v.get("targetIp"), v.get("targetUdp")) for v in original}
        for index in range(32):
            snapshot = snapshots[index % len(snapshots)]
            payload = schemas.payload(m.TS, "POST", snapshot["output"])
            payload.update(interface=source, streaming="Off", tags=[folder],
                           name=f"{folder}-{index}", targetIp=f"239.255.245.{index + 1}")
            if (payload["targetIp"], payload["targetUdp"]) in used_targets:
                raise m.MigrationError("Benchmark multicast target already exists")
            entry = {"output": {"id": f"benchmark-{index}"}, "state": "provisioning"}
            migrator.entries = [entry]
            created = migrator.create(m.TS, "OutputTSs", payload, entry, "new_id")
            new_id = created["id"]
            current_ids.append(new_id)
            state["owned"][new_id] = payload["targetIp"]
            checkpoint()
            migrator.wait(m.resource_path(m.TS, new_id), payload, schemas.operation(m.TS, "POST"))
            for service in snapshot["services"]:
                service_payload = schemas.payload(m.SERVICES, "POST", service["output"])
                service_payload["outputTS"] = {"id": new_id}
                routing = service["routing"]
                if not routing.get("inputService", {}).get("id"):
                    raise m.MigrationError("Benchmark templates must use existing input service IDs")
                route = {"inputService": {"id": routing["inputService"]["id"]},
                         "outputService": service_payload}
                created_service = migrator.create(
                    m.ROUTES, "OutputServiceRoutings", route, entry, "last_new_service_id",
                )
                service_path = m.resource_path(m.SERVICES, created_service["outputService"]["id"])
                mapping = {snapshot["output"]["id"]: new_id}
                migrator.verify_parts(service_path, service["parts"], mapping, apply=True)
            migrator.verify_parts(m.resource_path(m.TS, new_id), snapshot["parts"],
                                  {snapshot["output"]["id"]: new_id}, apply=True)
        expected = [migrator.signature(migrator.snapshot(value)) for value in current_ids]
        checkpoint()
        for workers in (1, 2, 4, 8):
            for from_port, to_port in ((args.source, args.destination), (args.destination, args.source)):
                reports = directory / f"workers-{workers}"
                before = set(reports.iterdir()) if reports.exists() else set()
                command = [str(binary), "--host", args.host, "--username", args.username,
                           "--source", from_port, "--destination", to_port, "--execute",
                           "--workers", str(workers), "--report-dir", str(reports)]
                if args.insecure:
                    command.append("--insecure")
                for value in current_ids:
                    command.extend(["--output-id", value])
                start = time.monotonic()
                with (directory / f"workers-{workers}-{from_port}.log").open("w") as log:
                    result = subprocess.run(command, env=os.environ.copy(), stdout=log,
                                            stderr=subprocess.STDOUT, check=False)
                created_runs = set(reports.iterdir()) - before
                if len(created_runs) != 1:
                    raise m.MigrationError("Cannot identify benchmark report directory")
                run = created_runs.pop()
                entries = json.loads((run / "results.json").read_text())
                for entry in entries:
                    if entry.get("new_id"):
                        state["owned"][entry["new_id"]] = entry["output"]["targetIp"]
                checkpoint()
                summary = json.loads((run / "summary.json").read_text())
                summary.update(elapsed_seconds=time.monotonic() - start, run=str(run),
                               from_port=from_port, to_port=to_port)
                state["runs"].append(summary)
                checkpoint()
                print(json.dumps(summary), flush=True)
                if result.returncode or summary["migrated"] != 32 or summary["unsuccessful"]:
                    raise m.MigrationError("Benchmark migration failed; inspect its full logs")
                mapping = {e["output"]["id"]: e["new_id"] for e in entries}
                current_ids = [mapping[value] for value in current_ids]
            for index, value in enumerate(current_ids):
                current = migrator.signature(migrator.snapshot(value))
                # Reprovisioning may change resource IDs, but no configuration.
                def normalize(data):
                    if isinstance(data, dict):
                        return {k: normalize(v) for k, v in data.items() if k not in m.IDENTITY}
                    if isinstance(data, list):
                        return [normalize(v) for v in data]
                    return data
                changes = m.difference(normalize(expected[index]), normalize(current))
                if changes:
                    raise m.MigrationError("Benchmark round-trip configuration drift: " + "; ".join(changes))
        measurements = []
        for workers in (1, 2, 4, 8):
            runs = [r for r in state["runs"] if r["workers_effective"] == workers]
            migration_seconds = sum(r["migration_seconds"] for r in runs)
            measurements.append({"workers": workers, "outputs": sum(r["migrated"] for r in runs),
                                 "migration_seconds": migration_seconds,
                                 "outputs_per_second": 64 / migration_seconds,
                                 "total_seconds": sum(r["elapsed_seconds"] for r in runs),
                                 "p95_seconds": max(r["duration_p95_seconds"] for r in runs)})
        fastest = max(v["outputs_per_second"] for v in measurements)
        recommended = min(v["workers"] for v in measurements if v["outputs_per_second"] >= fastest * 0.95)
        state.update(measurements=measurements, recommended_workers=recommended,
                     selection_rule="Smallest tested worker count within 5% of fastest error-free throughput")
        checkpoint()
    finally:
        # Include only benchmark-tagged resources created during an uncertain POST.
        candidates = m.rows(client.request("GET", m.TS), "OutputTSs")
        for output in candidates:
            if output.get("tags") == [folder] and output.get("streaming") == "Off":
                if output["id"] in {v["id"] for v in original}:
                    raise m.MigrationError("Refusing to clean up an original resource")
                client.request("DELETE", m.resource_path(m.TS, output["id"]))
        restored = m.rows(client.request("GET", m.TS), "OutputTSs")
        changes = m.difference({v["id"]: v for v in original}, {v["id"]: v for v in restored})
        state["original_inventory_equal_after_cleanup"] = not changes
        checkpoint()
        if changes:
            raise m.MigrationError("Inventory differs after benchmark cleanup: " + "; ".join(changes))
    print("BENCHMARK REPORT", directory / "benchmark.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
