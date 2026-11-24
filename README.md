# Migrate vDCM XGRESS outputs or inputs to another port

`migrate_outputs.py` uses **only the Python standard library**. Run the same file
with Python 3.9 or newer on Linux or Windows. No `pip install`, Excel installation,
or bundled binary is required for the script. A standalone Linux executable is
also available as described below. It runs on the REST client machine; SSH and root
access are not needed. Copy this folder to the remote Linux machine if you want
to run it there.

## Standalone Linux executable

`dist/migrate-outputs-linux-x86_64` is a single ELF executable built and tested on
the supplied host: **Rocky Linux 9.8, x86-64, glibc 2.34**. It bundles Python
3.9.25, the used standard-library modules, and supporting libraries including
OpenSSL 3.5.8. No system Python, pip, or package downloads are needed to run it.
It was tested with `PATH`, `PYTHONHOME`, and `PYTHONPATH` pointing to nonexistent
locations, including actual REST migration and Excel generation.

This is a Linux x86-64 build, not a universal binary: it requires the Linux
dynamic loader, glibc 2.34 or newer, and the standard OS `libz.so.1` ABI.
It does not run on ARM, Alpine/musl, older glibc distributions, macOS, or Windows.
Use a matching native build for those platforms rather than replacing system
libraries. Certificates still use the system trust store, or your `--ca-file`.
One-file startup extracts the bundled runtime into a private temporary directory;
the temporary filesystem must allow execution. Set `TMPDIR` to an appropriate
private writable location if `/tmp` is mounted `noexec`.

Copy just the executable to the Linux machine and run:

```sh
chmod 700 migrate-outputs-linux-x86_64
./migrate-outputs-linux-x86_64 --host https://localhost:8443 --username guest \
  --source "Port 3" --destination "Port 5" --insecure --dry-run
```

The password is prompted. Review the plan, then replace `--dry-run` with
`--execute`. All original script arguments remain available.
Reports persist in `runs` **beside the executable**, not inside its temporary
extraction directory; use `--report-dir` if that location is not writable.

An older executable may also be present on the build host at
`/root/vdcm-script-migrate-to-another-port/dist/migrate-outputs-linux-x86_64`.
Copy the current project executable to the target host after rebuilding. The
project copy includes:

* `dist/migrate-outputs-linux-x86_64.sha256`: verify using
  `sha256sum -c migrate-outputs-linux-x86_64.sha256` from `dist`.
* `dist/build-info.txt`: build platform, bundled runtime versions, source hash.
* `dist/build-requirements.lock`: resolved packaging-tool versions.

To rebuild, run `sh build-linux.sh` on x86-64 Linux. It uses an isolated
`.build-venv`, installs the pinned **build-only** requirements, runs tests, and
packages the application with PyInstaller. It does not modify system Python.
Python with venv/pip support and `objdump` are needed on the **build machine only**.
Rebuild from an older glibc baseline if targeting older distributions.

## Output folders

The vDCM API represents the output's GUI TS folder as the first entry in `tags`.
The supplied host returns `tags: ["TESTFOLDER"]` on both Port 3 outputs.
Migration copies the complete `tags` array without renaming, clearing, or
substituting it. Folder names containing spaces are preserved. Tags are checked
before source deletion and again in final verification; if the destination
drops or changes the folder, migration fails and does **not** delete the source.
The Excel **Outputs** sheet now includes a **Folder** column.

## Usage

First generate a read-only plan:

```sh
python3 migrate_outputs.py \
  --host https://10.90.245.125:8443 \
  --username guest \
  --source "Port 3" --destination "Port 5" \
  --insecure --dry-run
```

The password is prompted without echo. Alternatively supply `--password` or
set `VDCM_PASSWORD`. Command-line passwords can appear in shell history and
process listings. There are no hardcoded credentials.

After reviewing the plan, replace `--dry-run` with `--execute`:

```sh
python3 migrate_outputs.py \
  --host https://10.90.245.125:8443 \
  --username guest \
  --source "Port 3" --destination "Port 5" \
  --insecure --execute
```

Windows PowerShell (one line):

```powershell
py -3 migrate_outputs.py --host 10.90.245.125 --username guest --source "Port 3" --destination "Port 5" --insecure --dry-run
```

`--host` accepts an IP, hostname, or HTTPS origin; omitted ports use 8443.
It defaults to `https://localhost:8443`. IPv6 origins can use brackets.
`--source` and `--destination` accept **exact, case-sensitive** `portName` values
(quote names containing spaces) or `interface` values such as `Data2`.
Both are resolved against `/api/v2/AppSettings/Xgress/Configuration`.
Only `outputType == "XGRESS"` outputs on the resolved source are selected.
An ambiguous name, same source/destination, or inactive/input-only destination
fails before changes.

**Current scope:** selection covers `/api/v2/OutputTSs` only. Transparent
transport streams are separate `/api/v2/OutputLTTSs` resources and are not
selected, even when their output type is XGRESS. SRT, RTMP, ZIXI, ASI, DEPI,
RIST, and HTTP outputs are also not migrated by this version. A successful
summary therefore does not mean every output visible on the port was moved.

Certificates are validated by default. Use `--ca-file trusted-ca.pem` for a private
CA. `--insecure` explicitly disables certificate validation for self-signed
appliances; use it only on a trusted network. Environment HTTP proxies and
redirects are disabled to avoid sending appliance credentials elsewhere.

Additional options:

* `--output-id UUID` limits migration to a particular source output. Repeat for
  several IDs. Missing, wrong-type, or wrong-port IDs cause a preflight failure.
* `--timeout 20` sets the timeout of each REST request.
* `--verify-timeout 30` sets the polling deadline for each configuration check.
* `--workers N` bounds simultaneous output migrations and original backups.
  Default: **4**. Accepted range: 1-16. `--workers 1` restores sequential execution.
  There is no limit on the number of selected outputs; hundreds are processed
  through the bounded worker pool rather than starting one thread per output.
* `--report-dir /path/to/runs` selects the parent of the unique per-run folder.
  By default it is `runs` beside the script.

## Input migration (`--migrate inputs`)

Output migration remains the default and is unchanged. Add `--migrate inputs` to
move XGRESS **inputs** (Sources) from one port to another:

```sh
python3 migrate_outputs.py --host 10.90.245.125 --username guest \
  --source "Port 1" --destination "Port 3" --migrate inputs --insecure --dry-run
```

`--input-id UUID` (repeatable) limits the selection; `--input-timeout 60` sets how
long to wait for the new input to receive its stream. `--output-id` cannot be
combined with `--migrate inputs`, and `--input-id` requires it.

vDCM accepts PATCH/PUT of a source `interface` with HTTP 200 but **silently ignores
it**, so each input is recreated. For every input the tool:

1. Scans all readable API collections (about 110, in parallel) for typed
   references to the input or its services, and classifies every consumer.
   An unsupported reference blocks that input (left untouched). Collections the
   user cannot read (HTTP 403/404/410) are listed in the **Unscanned endpoints** sheet.
2. Creates the new source on the destination with the same writable settings
   (including TS backup list and active TS selection), recreates its input
   services (restoring names that stream discovery may overwrite), and copies
   the matching IGMP joins to the destination interface (an identical existing
   join is reused; a conflicting one blocks the input).
3. Waits for the stream (`inputPresent`), verifies the new input, then repoints
   consumers and verifies each one:
   * output service routing (v3 `OutputServiceRoutings` PUT) and output-service
     alternates/backups/merged services;
   * Linear Transcode flows (PUT with `keepInputs`/`keepOutputTSs`) and
     processing alternates;
   * **ABR flows: PATCH `/api/v3/ABRServices/{id}?keepInputs=true` with only
     `inputService`**. On V26_01_01 **any** v3 ABR PUT (even unchanged) returns
     HTTP 500 and deletes the flow, and without `keepOutputTSs` its output TSs.
     PATCH swaps only the input and was verified to keep every flow, output and
     service setting and all IDs;
   * TS-backup references from other inputs (source PATCH of `backupTSs`);
   * loop-through (`OutputTSRoutings` PUT, then LTTS settings re-applied). This
     step cannot be rolled back and therefore runs last.
4. Restores vDCM side effects of re-routing (forced service names, VCT short
   names) and verifies every impacted output against its original snapshot.
5. Re-scans to prove that nothing references the original input, then deletes the
   original IGMP join (only if no other input uses it), input services, and source,
   in that order. Backup TSs are detached first: **DELETE of a source also tries to
   delete its backup sources**.

Inputs run sequentially because consumers are often shared. Any failure before
deletion triggers an automatic rollback (consumers back to the original input,
outputs restored, created joins/services/source deleted). If rollback is impossible
(loop-through already recreated, deletion started, or a consumer disappeared),
the state is `failed_manual_action`/`rollback_failed` with the detail in the log,
and the complete original snapshot is in `<original-input-id>.json`.

**Important:** new inputs and input services get **new IDs**. Observed values
(`sourceIp`, `TSID`, `ONID`) are compared but only reported. The workbook adds
**Inputs**, **Consumers**, and **Unscanned endpoints** sheets.

## Concurrent migrations

Each worker completes the entire migration of **one output** in order:
create its TS and services, verify the configuration/folder/PIDs, enable its
original streaming mode, then delete that particular source. Other workers
process different outputs simultaneously. Failure of one output does not cancel
the remaining outputs or allow its source to be deleted without verification.

All selected original configuration graphs are backed up before the first
mutation, even when backups are concurrent. Workers have independent HTTP
transports, share a synchronized authentication token, and coordinate resource-ID
reservations to avoid adopting another worker's output. Resource creation uses
the authoritative POST response ID and direct resource GET verification, not
ambiguous “new objects” collection differences.

Each worker writes its own atomic journal in `journals/<original-id>.json`.
During execution these journals are the current recovery state; `results.json`
and Excel are aggregate reports refreshed after the backup/migration phases.
This avoids rewriting every output's graph on each individual API request.
On interruption pending work is cancelled, in-flight workers are allowed to
finish safely, and the aggregate journal is saved before the process exits.

`summary.json` records effective concurrency, backup/migration/total elapsed
seconds, successful outputs per second, failure counts, and per-output p95
duration. Excel includes worker names, durations, and folders. Log lines identify
the worker and output ID.

Concurrency temporarily requires capacity for up to N additional output TSs
and their services. Reduce `--workers` if the appliance lacks capacity or shows
REST overload/timeouts. Mutations still are not automatically retried.
Keep exclusive administrative access; parallel workers are not a distributed
transaction against other administrators.

### Measured recommendation: four workers

The default is **4**, selected from actual migrations on 10.90.245.125.
The benchmark created 32 temporary, non-streaming copies of two real output/
service configurations and migrated them in both directions at each worker
count: **256 successful migrations, zero failures** in the final benchmark.
It included normal configuration, folder, routing and PID verification.

| Workers | Successful outputs/minute during migration | Worst directional p95 duration |
| --- | ---: | ---: |
| 1 | 13.9 | 5.1 seconds |
| 2 | 18.5 | 10.3 seconds |
| **4** | **20.8** | **13.7 seconds** |
| 8 | 21.2 | 26.1 seconds |

Four workers delivered about 50% more throughput than sequential execution.
Eight increased throughput by only about 2%, while nearly doubling p95 latency.
The selection rule is the smallest tested count within 5% of the fastest
error-free throughput. These are **host/workload measurements**, not a guarantee
of the same rate or optimal setting on other appliances or more complex outputs.
The worker pool was also tested with 300 queued outputs; live benchmarking used
32, not hundreds.

An initial benchmark exposed HTTP 500 failures when listing every output during
concurrent source-deletion verification. The application now checks the
individual source resource for HTTP 404 instead; it does not treat transport/
server errors as deletion success. The final benchmark verified all temporary
resources were removed and the complete original 24-output inventory remained
unchanged.

Results are in `dist/concurrency-benchmark.json`; detailed final benchmark logs
and reports are in `dist/benchmark-9a4d01e29caf`.
To repeat the opt-in benchmark on an approved test appliance, use
`benchmark_migrations.py --approve-temporary-outputs` with the documented host,
username, binary, source, destination and TLS arguments, and `VDCM_PASSWORD`.
It creates configuration/API load: never run it on production without approval.

## Migration safety and limitations

1. Authenticate with `POST /api/authenticate`; send the returned token directly
   in `Authorization`. Refresh after nine minutes, or once on HTTP 401.
2. Read all outputs and the live `/api/swagger.json` schema. Save the inventory,
   port mapping, original output configurations, services, routing sources,
   alternates/backup/merge settings, component rules, processing, PID assignments,
   SI settings, realignment groups, and other tables **before any mutation**.
3. Prefer `PATCH /api/v2/OutputTSs/{id}` with only `interface`. An in-place
   migration retains the output ID; **do not delete it afterward**.
4. Fall back to create/verify/delete **only** for the application's explicit
   interface-change restriction. Authorization, transport, license, and other
   validation errors are reported instead of triggering reprovisioning.
5. Create a replacement TS with streaming `Off`. Recreate services using
   `/api/v3/OutputServiceRoutings`, referencing the existing input/processed/
   descrambled service IDs rather than rebuilding inputs. Copy writable settings
   according to the live schema. Skip writes when settings already match;
   certain default empty settings are returned by GET but rejected by PUT.
   Active alternate/backup selections are copied separately through `/Activate`
   when necessary, since their GET flags are read-only.
6. Verify output/service settings, routing, and actual PID assignments. Recheck
   that the source did not change and that the replacement remains intact.
   Restore the replacement's original streaming setting, then delete **only the
   specific original output ID**. Verify its absence. Never use collection DELETE.
7. If deletion fails, reconcile the host state first. If the original still
   exists unchanged, try `PATCH {"streaming": "Off"}` on it. The API enum is `Off`,
   not `Stopped`. Report this as **unsuccessful / cleanup required**, not a fully
   completed migration. The verified replacement remains available.
8. Continue with other selected outputs after individual failures. Known failed
   replacements are set to `Off` and left for inspection; their IDs are recorded.
   Never stop or delete the original after failed creation or verification.

**Important:**

* On the tested host both PATCH and full-configuration PUT rejected an interface
  change with HTTP 409: `Restricted to change the 'interface' value`.
  PUT is therefore not an additional runtime fallback on this appliance.
* Reprovisioning changes output/service resource IDs. External systems referencing
  those IDs must be updated separately. Unknown fields and unsupported required
  schemas fail the affected output rather than silently discarding configuration.
* For multi-service output TSs (MPTS), services are recreated in an order inferred
  from their recorded PID assignments and input PIDs, so a service using the
  original PID is created before one remapped because of a collision. Exact PID
  verification remains mandatory. If the order cannot reproduce the original
  assignments, the original output is retained and the failed replacement is Off.
* Automatically generated `syncUUID` values change with the destination port.
  As approved, `generateSyncUUID=true` is retained and the generated UUID is
  allowed to change. Manually configured UUIDs are copied and checked.
* Output names must be unique. A named replacement uses a temporary name while
  the original exists. After source deletion its original name is restored. A
  rename failure is explicitly reported; inspect the replacement, not the deleted
  source. If source deletion fails, its name is not cleared to force a rename.
* A configured secret that GET exposes only as a `...Configured=true` flag cannot
  be faithfully copied. Reprovisioning of that output is refused, leaving the
  original intact. Plaintext exported configuration and application error bodies
  can themselves be sensitive: protect the reports. API passwords/tokens are not
  included in backups and are redacted from HTTP errors.
* This is configuration verification, **not a guarantee of packet delivery**.
  Network routing, bandwidth, destination VLAN availability, and receivers must
  be checked separately. Cutover can cause brief interruption or overlap.
* Use exclusive administrative access while migrating. A local lock prevents
  simultaneous executions using the same report directory and host, and source/
  replacement rechecks catch configuration drift. These are not distributed locks
  or atomic transactions against other administrators.
* GET requests have bounded retries. POST/PATCH/PUT/DELETE are **not retried after
  a timeout**, because the application may already have applied them. After an
  uncertain POST, inspect the host and journal before restarting.
* The script requires the documented v2 configuration/subresource APIs, v3 service
  routing API, and live Swagger endpoint. Missing endpoints or inaccessible
  configuration are explicit failures, not guessed defaults.

## Transcoded outputs (ABR and Linear Transcode flows)

XGRESS outputs fed by an ABR (`/api/v3/ABRServices`) or Linear Transcode
(`/api/v2/LinearTranscodeServices`) flow are fully supported. Flow membership is
read at startup and shown in the report's **Transcoding flow / Flow type / Flow
ID** columns (in the dry run as well).

* **Method:** the replacement service is created through
  `/api/v3/OutputServiceRoutings` with the same `processedService.sourceId`.
  vDCM automatically adds it to the flow with the same video profile
  (ABR) or processing service (LTS), merged components and track rules. Deleting
  the original TS removes the old flow output. **Flows are never PUT**: on this
  appliance a v3 ABR PUT returned HTTP 500 and deleted the whole flow.
* **Verification:** flow configuration (everything except `outputs`) and all
  other flow outputs must be unchanged. The replacement must have the original's
  profile/merge/track settings before the source is deleted, and the source must
  be gone from the flow afterwards.
* **Serialisation:** outputs of the same flow migrate one after another (per-flow
  lock); outputs of different flows migrate in parallel. Expect about 60 s per
  flow output.
* **Accepted, logged changes** (approved; vDCM offers no API to preserve them):
  * `outputId` is reassigned by vDCM (`Output#N`); old -> new is in the log
    (`FLOW ...`) and report. vDCM may assign an `outputId` already used in the
    same flow; this is logged as a warning and shown in the Flow note column.
    Flow PATCH of `outputs` returns HTTP 500/409, so it cannot be renamed.
  * LSS `acquisitionPointId` when `lss.mode` is `Disabled`: vDCM assigns the
    profile service name and ignores writes. With LSS enabled all LSS fields are
    copied and verified.
  * PMT ES **order** (same PIDs/types/set): merged and track-rule components are
    appended last by vDCM. Any other PID difference still fails.
* **Failure:** if verification fails, the original is untouched; the replacement
  (already in the flow) is left with streaming `Off` and recorded in the Flow
  note. Delete that replacement TS to remove it from the flow.
* Validated in production on 4 ABR flows (11 outputs, Port 1 -> Port 5 -> Port 1)
  and on a disposable Linear Transcode flow. Take a settings backup
  (`GET /api/v2/AppSettings/SettingsBackup`) before migrating transcoded outputs.

## Reports and recovery

Every run creates a unique UTC-stamped folder containing:

* `migration.xlsx`: real `.xlsx`, generated without third-party packages.
  **Outputs** lists selected outputs, source/destination, state, new IDs, and
  errors; **Services** lists original service identities and routing; **Original
  configuration** contains complete JSON split into Excel-sized chunks;
  **Errors** contains complete error details, also chunked.
* `migration.log`: timestamped actions, full application rejection bodies,
  successes, unsuccessful migrations, and a final count summary.
* `backup.json`: original inventory, port mapping, host origin, and live schema.
* `<original-output-id>.json`: original complete selected configuration graph.
* `results.json`: durable per-output journal, snapshots, original/new IDs,
  service ID mapping, create responses, verification state, and errors.
* `journals/<original-id>.json`: independent current worker journal; consult these
  first if the aggregate report is stale because execution is still in progress.
* `summary.json`: measured concurrency, elapsed time, throughput, and failure counts.

Backups and logs use restrictive POSIX permissions where supported; Windows ACLs
should be managed by the operator. Do not remove a stale `.migration-*.lock`
until its recorded PID has been checked and the host state reconciled.

Exit codes: `0` all selected outputs migrated/planned (including an empty
selection); `1` one or more individual failures; `2` preflight/global failure;
`130` interrupted. A dry-run performs authentication and GET requests only.

To reverse **only this run's successful migrations**, obtain each `new_id` whose
state is `migrated` from `results.json`, then select those IDs explicitly:

```sh
python3 migrate_outputs.py --host 10.90.245.125 --username guest \
  --source "Port 5" --destination "Port 3" \
  --output-id NEW_ID_1 --output-id NEW_ID_2 --insecure --execute
```

Do not migrate *all* of Port 5 back unless you also intend to move outputs that
were already on Port 5 before the forward run. Reverse migration restores the
configuration and source port, **not the old resource IDs**.

Tests (no additional packages):

```sh
python3 -m unittest -v
```

### Validation on the supplied host

* 45 focused tests pass on macOS and native Linux, including bounded concurrency
  for 300 outputs, simultaneous full migrations with independent journals,
  synchronized authentication, duplicate-ID rejection, and folder-preserving
  named-output migration, rejection of missing destination folders, non-default setting
  copying, in-place migration, read-only dry-run, and continuing after failures.
* Two outputs migrated Port 3 -> Port 5 -> Port 3 successfully.
* The returned configuration graphs match their originals. The complete raw
  24-output inventory also matches the inventory captured before testing.
* The pre-existing Port 5 output was not migrated.
* Reports for the successful forward and reverse runs are in
  `runs/20261112T221958Z-a7ce36eb` and `runs/20261112T235837Z-208c3873`.
  The latter includes `roundtrip-verification.json`.
* The latest read-only plan is in `runs/20261113T000437Z-d6ba6a6b`.
* The standalone Linux binary also migrated both `TESTFOLDER` outputs
  Port 3 -> Port 5 -> Port 3, verifying the folder on the destination.
  Forward/reverse binary reports are in `dist/runs/20261113T051542Z-fa32d548`
  and `dist/runs/20261113T051604Z-2bf04881`, with final comparison in
  `dist/folder-roundtrip-verification.json`.
* Native Windows execution has not been tested.

### Input migration validation (Port 1 -> Port 3)

All 7 XGRESS inputs on Port 1 (Data1) were migrated to Port 3 (Data2); no XGRESS
input remains on Data1. Covered consumers: output routing, merged alternates, LTS
flow, LTS processing backup, ABR flow (PATCH), TS backup, and loop-through
(LTTS settings identical afterwards). Reports are in `runs/inputs/`.

Issues found during live testing, each fixed and covered by a test where possible:

* Old-source DELETE returned 409 "Delete not possible" while services existed:
  services are now deleted first.
* vDCM materialises an empty `snapLiveWatermark` as empty strings: empty values
  are normalised before comparison.
* **Incident:** v3 ABR PUT returned HTTP 500 and deleted ABR flow "AP-Bio"; a
  later no-op PUT experiment without `keepOutputTSs` also deleted its 7 output
  TSs. The flow and TSs were recreated from the snapshot (TS IDs were reused,
  flow and output-service IDs changed) and every setting was restored, except
  `Processing.lss.acquisitionPointId` on the 7 ABR output services (`Program 1`
  before, now `AP Bio (<profile>) (MR)`). vDCM derives this from the upstream
  service name at creation and ignores writes while LSS is Disabled; enabling LSS
  requires non-empty `zoneId`/`poisBaseUrl`, which then cannot be cleared again.
  It has no effect while LSS is Disabled. ABR flows are now only PATCHed. The
  settings archive shows that the stored value is empty in both cases: vDCM
  derives it from the ABR sub-output-service record, whose hidden
  `m_ServiceIdentification` is empty for the old flow (giving `Program <SID>`) and
  set to the profile UUID for a re-created flow (giving the tag name). The re-created
  output TSs also lost an inactive FEC address that the API does not expose.
  Details: `runs/inputs/abr-incident-recovery/`.
* **Residue repaired by a settings restore:** a fresh node settings archive
  (`GET /api/v2/AppSettings/SettingsBackup`) was edited in only two places, then
  applied with `PUT /api/v2/AppSettings/SettingsBackup?restoreType=ApplySettings`
  (multipart field `Attachment`). The node rebooted and the API came back in about
  1 minute.
  * `board2/SubOutServices.xml`: the 3 AP-Bio `m_ServiceIdentification` values
    were cleared.
  * `board1/TsOutSettings.xml`: the 6 ABR TSs got back their ColFEC/RowFEC
    addresses (192.168.1.5/.6/.7, ports 49412/49414).

  Result:
  * `acquisitionPointId` is `Program 1` again on all 7 AP-Bio output services.
  * The stored settings match the applied archive except for values the node
    generates at boot (sub-service identification UUIDs and the ABR2TS start
    timer).
  * All 24 outputs, all 4 ABR flows and all 40 input services are back. The
    ABR2TS input returns after its configured start delay of about 16 minutes.

  Side effects of the reboot:
  * The internal input-service UUIDs changed, and every alternate and ABR profile
    reference followed them.
  * On 4 other services that had derived `acquisitionPointId` from their input
    service name (UEFA Champions League, neox, The Good Place, Soccer), the value
    is now `Program <SID>`, the same as all other services.
  * Neither has any effect while LSS is Disabled.

  Rollback archive, applied archive and diff, and before/after REST dumps:
  `runs/inputs/abr-incident-recovery/settings-restore/`.
* Merged-alternate verification falsely flagged the intended service-ID change
  (rolled back safely); the check now compares against the remapped original.
* A new input with a backup TS starts on its backup until its stream arrives; the
  original active TS selection is now restored.
* DELETE of a source cascades to its backup sources (refused only because the
  backup was in use); backups are now detached before any source deletion.
* Parallel input planning raced on the shared `results.json` temporary file
  (dry-run of 7 inputs aborted); journal writes are now serialised and use unique
  temporary files.

The inputs were then migrated back from Port 3 to Port 1 with the Linux binary
(all 7 on Data1, each receiving its own multicast on the main TS). That run found:

* Inputs share consumers (merged alternates, TS backups), so a snapshot taken during
  planning goes stale once an earlier input in the same run is migrated. A stale
  alternates PUT was rejected (HTTP 409) and its rollback failed the same way.
  Every input after the first is now re-planned (fresh reference scan and snapshot)
  just before it is executed.
* Rollback now restores every consumer even if one fails. The new input is deleted
  only when a fresh scan proves nothing references it.
* A new input with a Floating TS backup reports `inputPresent` (and
  `tsInBackup: false`) while it is using the backup; output service names then
  followed the backup's service. The tool now also waits until the input's own
  multicast sender (`sourceIp`) is observed before it repoints consumers.
