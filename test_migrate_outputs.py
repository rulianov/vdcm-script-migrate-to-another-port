import copy
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import urllib.error
import xml.etree.ElementTree as ET
import zipfile

import migrate_outputs as m


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.schemas = m.Schemas({
            "definitions": {
                "Example": {"type": "object", "properties": {
                    "id": {"type": "string", "readOnly": True},
                    "interface": {"type": "string"},
                    "flag": {"type": "boolean"},
                    "generateSyncUUID": {"type": "boolean"},
                    "syncUUID": {"type": "string"},
                    "pmtDescriptors": {"type": "array", "items": {
                        "type": "object", "properties": {
                            "type": {"type": "string"}, "data": {"type": "string"},
                        },
                    }},
                }},
            },
            "paths": {"/api/v2/OutputTSs/{id}": {
                "patch": {"parameters": [{"in": "body", "schema": {"$ref": "#/definitions/Example"}}]},
            }},
        })

    def test_schema_preserves_false_and_filters_readonly_and_passed(self):
        result = self.schemas.payload("/api/v2/OutputTSs/123", "PATCH", {
            "id": "123", "interface": "Data2", "flag": False,
            "pmtDescriptors": [
                {"type": "Passed", "data": "01"}, {"type": "Manual", "data": "02"},
            ],
        })
        self.assertEqual(result, {
            "interface": "Data2", "flag": False,
            "pmtDescriptors": [{"type": "Manual", "data": "02"}],
        })

    def test_unknown_non_null_fields_fail_instead_of_losing_config(self):
        with self.assertRaisesRegex(m.MigrationError, "Unrecognized"):
            self.schemas.payload("/api/v2/OutputTSs/123", "PATCH", {"newSetting": 1})

    def test_auto_uuid_can_regenerate_but_manual_uuid_is_preserved(self):
        path = "/api/v2/OutputTSs/123"
        self.assertEqual(self.schemas.payload(path, "PATCH", {
            "generateSyncUUID": True, "syncUUID": "original",
        }), {"generateSyncUUID": True})
        self.assertEqual(self.schemas.payload(path, "PATCH", {
            "generateSyncUUID": False, "syncUUID": "original",
        }), {"generateSyncUUID": False, "syncUUID": "original"})

    def test_port_spaces_and_ambiguity(self):
        config = {"videoPorts": [{"portName": "Port 3", "interface": "Data2"}]}
        self.assertEqual(m.resolve_port(config, "Port 3")["interface"], "Data2")
        self.assertEqual(m.resolve_port(config, "Data2")["interface"], "Data2")
        config["videoPorts"].append({"portName": "Port 3", "interface": "Data3"})
        with self.assertRaisesRegex(m.MigrationError, "ambiguous"):
            m.resolve_port(config, "Port 3")

    def test_url_and_id_remapping_does_not_modify_input_ids(self):
        value = {"output": "old", "input": "input-id",
                 "url": "https://device:8443/api/v2/OutputServices/old"}
        self.assertEqual(m.remap(value, {"old": "new"}), {
            "output": "new", "input": "input-id",
            "url": "https://device:8443/api/v2/OutputServices/new",
        })

    def test_active_alternate_and_backup_are_copied_separately_from_readonly_flags(self):
        state = m.alternate_activation({"alternateSources": [
            {"activeState": "Inactive", "backupServices": []},
            {"activeState": "Active", "backupServices": [{"activeState": "Active"}],
             "mergedServices": [{"mergedServiceId": "merged", "backupServices": []}]},
        ]})
        self.assertEqual(state["active"], 1)
        self.assertEqual(state["groups"][1], {
            "activeAlternateIndex": 1, "activeBackupIndex": 1,
            "mergedServicesBackup": [{"mergedServiceId": "merged", "activeBackupIndex": 0}],
        })

    def test_ambiguous_activation_fails_instead_of_guessing(self):
        with self.assertRaisesRegex(m.MigrationError, "unambiguously"):
            m.alternate_activation({"alternateSources": [
                {"activeState": "Inactive"}, {"activeState": "Inactive"},
            ]})


class ClientTests(unittest.TestCase):
    def client(self):
        client = m.Client("https://device:8443", "user", "secret")
        client.token = "token"
        client.token_at = m.time.monotonic()
        return client

    def test_error_body_is_complete_and_redacts_secrets(self):
        client = self.client()
        body = json.dumps({"errors": [{"error": "Detailed rejection secret token " + "x" * 1000}]})
        client.opener.open = Mock(side_effect=urllib.error.HTTPError(
            "https://device:8443/api/test", 409, "Conflict", {}, io.BytesIO(body.encode()),
        ))
        with self.assertRaises(m.APIError) as context:
            client.request("PATCH", "/api/test", {"interface": "Data3"})
        self.assertIn("x" * 1000, context.exception.detail)
        self.assertNotIn("secret", context.exception.detail)
        self.assertNotIn("token", context.exception.detail)

    def test_mutation_timeout_is_never_retried(self):
        client = self.client()
        client.opener.open = Mock(side_effect=TimeoutError("lost response"))
        with self.assertRaises(m.APIError):
            client.request("POST", "/api/test", {})
        self.assertEqual(client.opener.open.call_count, 1)

    def test_read_retry_and_auth_refresh(self):
        client = self.client()
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"ok": true}'
        client.opener.open = Mock(side_effect=[TimeoutError("once"), response])
        with patch.object(m.time, "sleep"):
            self.assertEqual(client.request("GET", "/api/test"), {"ok": True})
        self.assertEqual(client.opener.open.call_count, 2)

    def test_401_refreshes_once(self):
        client = self.client()
        client._request = Mock(side_effect=[
            m.APIError("PATCH", "/api/test", 401, "expired"), {"ok": True},
        ])
        client.authenticate = Mock()
        self.assertEqual(client.request("PATCH", "/api/test", {}), {"ok": True})
        client.authenticate.assert_called_once()

    def test_inline_failure_and_interface_specific_fallback(self):
        with self.assertRaises(m.APIError):
            m.check_inline_errors({"results": [{"status": 409, "error": "Conflict"}]}, "DELETE", "/api/test")
        error = m.APIError("PATCH", "/api/test", 409, json.dumps({
            "errors": [{"field_name": "interface", "error": "Restricted to change the 'interface' value"}],
        }))
        self.assertTrue(error.interface_restricted())
        self.assertFalse(m.APIError("PATCH", "/api/test", 409, '{"errors": [{"field_name": "rate"}]}')
                         .interface_restricted())


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.migrator = m.Migrator(Mock(), Mock(), Path(self.temporary.name), verify_timeout=0.001)
        self.snapshot = {"output": {"id": "source", "streaming": "On", "interface": "Data2"}}
        self.entry = {"output": self.snapshot["output"], "state": "started"}
        self.migrator.entries = [self.entry]
        self.migrator.assert_unchanged = Mock()

    def test_failed_create_or_verification_never_deletes_source(self):
        self.migrator.interface_patch_rejected = True
        self.migrator.clone = Mock(side_effect=m.MigrationError("verification failed"))
        self.migrator.delete_verified_source = Mock()
        with self.assertRaisesRegex(m.MigrationError, "verification failed"):
            self.migrator.migrate(self.snapshot, "Data3", self.entry)
        self.migrator.delete_verified_source.assert_not_called()

    def test_only_verified_clone_can_be_deleted(self):
        self.migrator.interface_patch_rejected = True
        self.migrator.clone = Mock(return_value="new")
        self.migrator.delete_verified_source = Mock()
        self.migrator.migrate(self.snapshot, "Data3", self.entry)
        self.migrator.delete_verified_source.assert_called_once_with(self.snapshot, "new", self.entry)
        self.assertEqual(self.entry["state"], "migrated")

    def test_delete_rejection_stops_original_only_after_verified_clone(self):
        self.migrator.write = Mock(side_effect=[
            m.APIError("DELETE", "/api/test", 409, "in use"), None,
        ])
        self.migrator.get = Mock(side_effect=[
            self.snapshot["output"], {"streaming": "Off"},
        ])
        with self.assertRaisesRegex(m.MigrationError, "source streaming is Off"):
            self.migrator.delete_verified_source(self.snapshot, "new", self.entry)
        self.assertEqual(self.entry["state"], "source_retained_off")
        self.assertEqual(self.migrator.write.call_args.args,
                         ("PATCH", "/api/v2/OutputTSs/source", {"streaming": "Off"}))

    def test_lost_delete_response_is_reconciled_without_stopping_deleted_source(self):
        self.migrator.write = Mock(side_effect=m.APIError("DELETE", "/api/test", None, "timeout"))
        self.migrator.get = Mock(side_effect=m.APIError("GET", "/api/test", 404, "Not Found"))
        self.migrator.delete_verified_source(self.snapshot, "new", self.entry)
        self.assertEqual(self.entry["state"], "source_deleted")
        self.assertEqual(self.migrator.write.call_count, 1)

    def test_source_read_error_is_not_mistaken_for_deletion_or_used_to_stop_replacement(self):
        self.entry["new_id"] = "new"
        self.migrator.write = Mock()
        self.migrator.get = Mock(side_effect=m.APIError("GET", "/api/test", 500, "Server error"))
        with self.assertRaises(m.APIError):
            self.migrator.delete_verified_source(self.snapshot, "new", self.entry)
        self.assertEqual(self.entry["state"], "source_delete_started")
        self.migrator.cleanup_failed_replacement(self.entry)
        self.assertEqual(self.migrator.write.call_count, 1)
        self.assertEqual(self.migrator.write.call_args.args, ("DELETE", "/api/v2/OutputTSs/source"))

    def test_in_place_output_is_never_deleted(self):
        with self.assertRaisesRegex(m.MigrationError, "Refusing to delete"):
            self.migrator.delete_verified_source(self.snapshot, "source", self.entry)

    def test_configured_secret_blocks_recreation(self):
        with self.assertRaisesRegex(m.MigrationError, "not retrievable"):
            self.migrator.ensure_no_hidden_secrets({"tsScrambling": {"sessionWordConfigured": True}})

    def test_unknown_patch_error_does_not_fall_back(self):
        self.migrator.write = Mock(side_effect=m.APIError("PATCH", "/api/test", 403, "Not authorized"))
        self.migrator.clone = Mock()
        with self.assertRaises(m.APIError):
            self.migrator.migrate(self.snapshot, "Data3", self.entry)
        self.migrator.clone.assert_not_called()

    def test_pid_drift_fails_before_delete(self):
        with self.assertRaisesRegex(m.MigrationError, "PID assignment changed"):
            self.migrator.verify_pid_assignment(
                {"pmtPid": {"pid": 480, "pidProperty": 1}},
                {"pmtPid": {"pid": 481, "pidProperty": 0}},
            )

    def test_es_order_change_is_tolerated_only_when_allowed_and_set_is_identical(self):
        original = {"pmt": 100, "es": [{"pid": 101, "streamType": 27}, {"pid": 201, "streamType": 15}]}
        reordered = {"pmt": 100, "es": [{"pid": 201, "streamType": 15}, {"pid": 101, "streamType": 27}]}
        self.assertFalse(self.migrator.verify_pid_assignment(original, copy.deepcopy(original)))
        with self.assertRaisesRegex(m.MigrationError, "PID assignment changed"):
            self.migrator.verify_pid_assignment(original, reordered)
        self.assertTrue(self.migrator.verify_pid_assignment(original, reordered, allow_es_order=True))
        changed = copy.deepcopy(reordered)
        changed["es"][1]["pid"] = 102
        with self.assertRaisesRegex(m.MigrationError, "PID assignment changed"):
            self.migrator.verify_pid_assignment(original, changed, allow_es_order=True)

    def test_matching_defaults_are_verified_without_rejected_unnecessary_put(self):
        config = {"autoPassTables": []}
        self.migrator.get = Mock(return_value=config)
        self.migrator.schemas.project = Mock(return_value=config)
        self.migrator.wait = Mock()
        self.migrator.write = Mock()
        self.migrator.verify_parts("/api/output", {
            "SiComponents/AutoPassTables": {"config": config, "schema": {}, "method": "PUT"},
        }, {}, apply=True)
        self.migrator.write.assert_not_called()
        self.migrator.wait.assert_called_once()

    def test_activation_restores_backup_and_finishes_on_original_active_alternate(self):
        original = {"alternateSources": [
            {"activeState": "Inactive", "backupServices": []},
            {"activeState": "Active", "backupServices": [{"activeState": "Active"}]},
        ]}
        actual = {"alternateSources": [
            {"activeState": "Active", "backupServices": []},
            {"activeState": "Inactive", "backupServices": [{"activeState": "Inactive"}]},
        ]}
        self.migrator.get = Mock(side_effect=[actual, original])
        self.migrator.write = Mock()
        self.migrator.schemas.payload = Mock(side_effect=lambda path, method, value: value)
        with patch.object(m.time, "sleep"):
            self.migrator.verify_activation("/api/service", original, apply=True)
        bodies = [call.args[2] for call in self.migrator.write.call_args_list]
        self.assertEqual([body["activeAlternateIndex"] for body in bodies], [0, 1])
        self.assertEqual(bodies[-1]["activeBackupIndex"], 1)

    def test_dry_run_excel_is_valid_xml_not_csv_or_formulas(self):
        path = Path(self.temporary.name) / "report.xlsx"
        m.write_excel(path, {"Outputs": [["Name", "Port"], ["=unsafe()", "Port 3 & 5"]]})
        with zipfile.ZipFile(path) as archive:
            self.assertIsNone(archive.testzip())
            for filename in archive.namelist():
                ET.fromstring(archive.read(filename))
            sheet = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
            namespace = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
            self.assertEqual(sheet.findall(".//x:f", namespace), [])
            self.assertIn("Port 3 & 5", [v.text for v in sheet.findall(".//x:t", namespace)])


class JournalConcurrencyTests(unittest.TestCase):
    def test_parallel_checkpoints_to_one_journal_do_not_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            migrator = m.Migrator(Mock(), Mock(), Path(directory))
            migrator.entries = [{"state": "planned", "n": i} for i in range(20)]
            errors = []

            def writer(index):
                try:
                    for _ in range(40):
                        with migrator._journal_lock:
                            migrator.entries[index]["n"] += 1
                        migrator.checkpoint()
                except Exception as exc:  # noqa: BLE001 - surface any failure to the test thread
                    errors.append(exc)

            threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(errors, [])
            self.assertEqual(json.loads((Path(directory) / "results.json").read_text())[0]["n"], 40)
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])


class FlowTests(unittest.TestCase):
    FLOW = "/api/v3/ABRServices/flow"

    @staticmethod
    def flow(outputs, profile="Profile#1"):
        return {"id": "flow", "name": "ABR 1", "processing": {"profile": profile}, "outputs": [
            {"outputId": oid, "videoProfileId": vp, "mergedComponents": [{"mergedServiceId": "flow"}],
             "trackRules": [], "outputService": {"id": sid, "outputTS": {"interface": "Data1"}}}
            for oid, sid, vp in outputs]}

    def migrator(self, directory, responses):
        migrator = m.Migrator(Mock(), Mock(), Path(directory), verify_timeout=0)
        migrator.get = Mock(side_effect=lambda path: copy.deepcopy(responses[0]))
        entry = {"flows": [{"flow_type": "ABR", "base": m.ABR, "flow_id": "flow", "flow_name": "ABR 1",
                            "service_id": "s1", "output_id": "Custom@1"}]}
        migrator.entries = [entry]
        return migrator, entry

    def test_replacement_must_join_flow_with_identical_settings_and_records_new_output_id(self):
        state = [self.flow([("Custom@1", "s1", "VP#1"), ("Output#2", "s2", "VP#2")])]
        with tempfile.TemporaryDirectory() as directory:
            migrator, entry = self.migrator(directory, state)
            migrator.capture_flows(entry)
            entry["service_id_map"] = {"s1": "n1"}
            with self.assertRaisesRegex(m.MigrationError, "/outputs/n1"):
                migrator.verify_flows(entry, final=False)
            state[0] = self.flow([("Custom@1", "s1", "VP#1"), ("Output#2", "s2", "VP#2"),
                                  ("Output#3", "n1", "VP#1")])
            migrator.verify_flows(entry, final=False)
            with self.assertRaisesRegex(m.MigrationError, "/outputs/s1"):
                migrator.verify_flows(entry, final=True)
            state[0] = self.flow([("Output#2", "s2", "VP#2"), ("Output#3", "n1", "VP#1")])
            migrator.verify_flows(entry, final=True)
            self.assertEqual(entry["flow_output_ids"]["s1"]["old_output_id"], "Custom@1")
            self.assertEqual(entry["flow_output_ids"]["s1"]["new_output_id"], "Output#3")
            self.assertNotIn("flow_warning", entry)
            state[0] = self.flow([("Output#2", "s2", "VP#2"), ("Output#2", "n1", "VP#1")])
            with self.assertLogs(level="WARNING"):
                migrator.verify_flows(entry, final=True)
            self.assertIn("already used by s2", entry["flow_warning"])

    def test_wrong_profile_or_changed_flow_settings_fail_verification(self):
        state = [self.flow([("Output#1", "s1", "VP#1")])]
        with tempfile.TemporaryDirectory() as directory:
            migrator, entry = self.migrator(directory, state)
            migrator.capture_flows(entry)
            entry["service_id_map"] = {"s1": "n1"}
            state[0] = self.flow([("Output#2", "n1", "VP#2")])
            with self.assertRaisesRegex(m.MigrationError, "videoProfileId"):
                migrator.verify_flows(entry, final=True)
            state[0] = self.flow([("Output#2", "n1", "VP#1")], profile="Profile#2")
            with self.assertRaisesRegex(m.MigrationError, "/flow/processing"):
                migrator.verify_flows(entry, final=True)

    def test_flow_index_maps_services_and_tolerates_unlicensed_flow_types(self):
        client = Mock()

        def request(method, path, body=None):
            if path == m.ABR:
                return {"ABRServices": [self.flow([("Output#1", "s1", "VP#1")])], "count": 1}
            raise m.APIError(method, path, 403, "Forbidden")
        client.request.side_effect = request
        index = m.flow_index(client)
        self.assertEqual(list(index), ["s1"])
        self.assertEqual(index["s1"][0]["flow_id"], "flow")
        self.assertEqual(index["s1"][0]["output_id"], "Output#1")

    def test_lss_acquisition_point_tolerated_only_for_disabled_flow_outputs(self):
        def service(mode):
            return {"output": {"id": "s1"}, "parts": {"Processing": {"config": {
                "lss": {"mode": mode, "acquisitionPointId": "Program 1", "zoneId": ""}}}}}
        with tempfile.TemporaryDirectory() as directory:
            migrator = m.Migrator(Mock(), Mock(), Path(directory))
            migrator.get = Mock(return_value={"lss": {"acquisitionPointId": "hbo (MR)"}})
            flow_entry = {"flows": [{"service_id": "s1"}]}
            parts = migrator.flow_service_parts(service("Disabled"), "/svc/n1", flow_entry)
            self.assertEqual(parts["Processing"]["config"]["lss"],
                             {"mode": "Disabled", "acquisitionPointId": "hbo (MR)", "zoneId": ""})
            self.assertEqual(flow_entry["acquisition_point_changes"]["s1"],
                             {"old": "Program 1", "new": "hbo (MR)"})
            for original, entry in ((service("Enabled"), {"flows": [{"service_id": "s1"}]}),
                                    (service("Disabled"), {"flows": []})):
                self.assertIs(migrator.flow_service_parts(original, "/svc/n1", entry), original["parts"])
                self.assertNotIn("acquisition_point_changes", entry)

    def test_unexpected_exception_is_journaled_without_aborting_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            migrator = m.Migrator(Mock(), Mock(), Path(directory))
            migrator.migrate = Mock(side_effect=AttributeError("'NoneType' object has no attribute 'get'"))
            entry = {"output": {"id": "old"}, "state": "planned", "snapshot": {}}
            migrator.entries = [entry]
            with self.assertLogs(level="ERROR"):
                migrator.execute_entry(entry, "Data3")
            self.assertEqual(entry["state"], "failed")
            self.assertIn("AttributeError", entry["error"])


class InputSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.migrator = m.InputMigrator(Mock(), Mock(), Path(self.temporary.name), verify_timeout=0.001)
        self.migrator.write = Mock()

    def test_abr_flow_input_is_patched_never_put(self):
        # vDCM V26 answers any v3 ABR PUT with HTTP 500 and deletes the flow.
        states = iter([{"inputService": {"id": "old"}}])
        self.migrator.get = Mock(side_effect=lambda path: next(states, {"inputService": {"id": "new"}}))
        self.migrator.flow_config = Mock(return_value={})
        self.migrator.apply_alternates = Mock()
        consumer = {"key": ("flow", m.ABR, "f1"), "alternates": {},
                    "flow": {"inputService": {"id": "old"}, "outputs": []}}
        self.migrator.apply_consumer(consumer, {"old": "new"}, {})
        (method, path, body), = [c.args for c in self.migrator.write.call_args_list]
        self.assertEqual((method, body), ("PATCH", {"inputService": {"id": "new"}}))
        self.assertIn("keepInputs=true", path)

    def test_backups_are_detached_before_source_delete(self):
        # DELETE of a source cascades to its backup sources, which may be live inputs.
        source = {"inputType": "XGRESS", "xgressSettings": {"tsBackup": {"activeBackupTS": 1,
                                                                         "backupTSs": [{"id": "b"}]}}}
        detached = {"inputType": "XGRESS", "xgressSettings": {"tsBackup": {"backupTSs": []}}}
        self.migrator.get = Mock(side_effect=[source, detached])
        order = []
        self.migrator.write.side_effect = lambda *a: order.append(a[0])
        self.migrator.delete_resource = Mock(side_effect=lambda p: order.append("DELETE"))
        self.migrator.delete_source("s1")
        self.assertEqual(order, ["PATCH", "DELETE"])
        self.assertEqual(self.migrator.write.call_args.args[2]["xgressSettings"]["tsBackup"],
                         {"activeBackupTS": 0, "backupTSs": []})

    def test_remapped_merged_alternate_is_not_an_activation_change(self):
        def alternates(merged):
            return {"alternateSources": [{"activeState": "Active", "backupServices": [],
                                          "mergedServices": [{"mergedServiceId": merged, "backupServices": []}]}]}
        self.migrator.schemas.payload = lambda path, method, value: value
        self.migrator.schemas.project = lambda value, schema, location=None: value
        self.migrator.wait = Mock()
        self.migrator.get = Mock(return_value=alternates("new"))
        self.migrator.apply_alternates("/alt", alternates("old"), {"old": "new"})
        self.migrator.write.assert_not_called()

    def test_new_input_on_backup_ts_is_not_ready(self):
        statuses = iter([{"inputPresent": True, "xgressStatus": {"tsInBackup": True}},
                         {"inputPresent": True, "xgressStatus": {"tsInBackup": False}}])
        self.migrator.get = Mock(side_effect=lambda path: next(statuses))
        with patch("time.sleep"):
            self.migrator.wait_input_present("new")
        self.assertEqual(self.migrator.get.call_count, 2)

    def test_floating_backup_waits_for_own_multicast_sender(self):
        # In Floating mode tsInBackup stays false while the backup TS is used.
        present = {"inputPresent": True, "xgressStatus": {"tsInBackup": False}}
        responses = iter([present, {"xgressSettings": {"sourceIp": "0.0.0.0"}},
                          present, {"xgressSettings": {"sourceIp": "192.0.2.1"}}])
        self.migrator.get = Mock(side_effect=lambda path: next(responses))
        with patch("time.sleep"):
            self.migrator.wait_input_present("new", require_main_stream=True)
        self.assertEqual(self.migrator.get.call_count, 4)

    def rollback_entry(self, references):
        entry = {"input": {"id": "orig"}, "state": "input_present", "new_source_id": "new",
                 "applied": [("output_service", "a"), ("output_service", "b")], "created_joins": ["j"],
                 "snapshot": {"outputs": {}, "consumers": [{"key": ("output_service", "a")},
                                                           {"key": ("output_service", "b")}]}}
        restored = []

        def apply(consumer, mapping, entry):
            if consumer["key"][1] == "b":
                raise m.MigrationError("HTTP 409 stale")
            restored.append(consumer["key"][1])

        self.migrator.apply_consumer = Mock(side_effect=apply)
        self.migrator.restore_outputs = Mock()
        self.migrator.get = Mock(return_value={"InputServices": [{"id": "new-svc"}]})
        self.migrator.remaining_references = Mock(return_value=references)
        self.migrator.delete_resource = Mock()
        self.migrator.delete_source = Mock()
        self.migrator.rollback_input(entry)
        return entry, restored

    def test_rollback_continues_after_consumer_failure_and_cleans_unused_new_input(self):
        entry, restored = self.rollback_entry([])
        self.assertEqual(restored, ["a"])
        self.assertEqual(entry["state"], "rollback_failed")
        self.assertIn("stale", entry["rollback_error"])
        self.migrator.delete_source.assert_called_once_with("new")

    def test_rollback_keeps_new_input_that_is_still_referenced(self):
        entry, _ = self.rollback_entry(["('output_service', 'b')"])
        self.migrator.delete_source.assert_not_called()
        self.migrator.delete_resource.assert_not_called()
        self.assertIn("still referenced", entry["rollback_error"])


class FixtureClient:
    """Small API fixture exercising the real snapshot, clone, and deletion code."""

    def __init__(self):
        self.calls = []
        self.outputs = {
            "old": {"id": "old", "interface": "Data2", "outputType": "XGRESS",
                    "name": "Original name", "streaming": "On", "tags": ["Folder with spaces"]},
        }
        self.settings = {"old": {suffix: {"setting": 7} for suffix in m.TS_PARTS}}
        properties = {
            "id": {"type": "string", "readOnly": True}, "interface": {"type": "string"},
            "outputType": {"type": "string"}, "name": {"type": "string"},
            "streaming": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
        }
        output_schema = {"type": "object", "properties": properties}
        part_schema = {"type": "object", "properties": {"setting": {"type": "integer"}}}

        def operation(schema):
            return {"parameters": [{"in": "body", "schema": schema}]}

        paths = {m.TS: {"post": operation(output_schema)}}
        for suffix in m.TS_PARTS:
            method = "patch" if suffix.endswith("TablePlayout") else "put"
            paths[m.TS + "/{id}/" + suffix] = {method: operation(part_schema)}
        for suffix in m.COLLECTIONS:
            paths[m.TS + "/{id}/" + suffix] = {"post": operation(part_schema)}
        self.swagger = {"definitions": {"Output": output_schema}, "paths": paths}
        self.config = {"videoPorts": [
            {"portName": "Port 3", "interface": "Data2", "portType": "InputOutput", "active": True},
            {"portName": "Port 5", "interface": "Data3", "portType": "InputOutput", "active": True},
        ]}

    def request(self, method, path, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        path = path.split("?")[0]
        if path == "/api/swagger.json":
            return self.swagger
        if path == "/api/v2/AppSettings/Xgress/Configuration":
            return copy.deepcopy(self.config)
        if path == m.ABR:
            raise m.APIError(method, path, 403, "Forbidden")
        if path == m.LTS:
            return {"LinearTranscodeServices": [], "count": 0}
        if path == m.TS:
            if method == "GET":
                return {"OutputTSs": copy.deepcopy(list(self.outputs.values()))}
            if method == "POST":
                self.outputs["new"] = {"id": "new", **copy.deepcopy(body)}
                self.settings["new"] = {suffix: {"setting": 1} for suffix in m.TS_PARTS}
                return copy.deepcopy(self.outputs["new"])
        relative = path[len(m.TS) + 1:]
        output_id, _, suffix = relative.partition("/")
        if suffix == "OutputServices":
            return {"OutputServices": []}
        if suffix in m.COLLECTIONS:
            return {suffix.split("/")[-1]: []}
        if suffix in m.TS_PARTS:
            if method in ("PUT", "PATCH"):
                self.settings[output_id][suffix] = copy.deepcopy(body)
            return copy.deepcopy(self.settings[output_id][suffix])
        if method == "GET":
            if output_id not in self.outputs:
                raise m.APIError(method, path, 404, "Not Found")
            return copy.deepcopy(self.outputs[output_id])
        if method == "PATCH":
            if "interface" in body:
                raise m.APIError(method, path, 409, json.dumps({
                    "errors": [{"field_name": "interface", "error": "Restricted to change the 'interface' value"}],
                }))
            self.outputs[output_id].update(body)
            return None
        if method == "DELETE":
            del self.outputs[output_id]
            return None
        raise AssertionError((method, path))


class WorkflowTests(unittest.TestCase):
    def tearDown(self):
        self.close_logging()

    @staticmethod
    def close_logging():
        for handler in list(m.logging.getLogger().handlers):
            handler.close()
            m.logging.getLogger().removeHandler(handler)

    def test_named_output_restores_name_and_nondefault_parts_before_source_delete(self):
        client = FixtureClient()
        with tempfile.TemporaryDirectory() as directory:
            migrator = m.Migrator(client, m.Schemas(client.swagger), Path(directory), verify_timeout=0.01)
            snapshot = migrator.snapshot("old")
            entry = {"output": snapshot["output"], "state": "planned"}
            migrator.entries.append(entry)
            migrator.migrate(snapshot, "Data3", entry)
        self.assertEqual(entry["state"], "migrated")
        self.assertNotIn("old", client.outputs)
        self.assertEqual(client.outputs["new"]["name"], "Original name")
        self.assertEqual(client.outputs["new"]["streaming"], "On")
        self.assertEqual(client.outputs["new"]["interface"], "Data3")
        self.assertEqual(client.outputs["new"]["tags"], ["Folder with spaces"])
        create = next(c for c in client.calls if c[0] == "POST" and c[1].startswith(m.TS))
        self.assertEqual(create[2]["tags"], ["Folder with spaces"])
        self.assertEqual(client.settings["new"], client.settings["old"])
        delete_index = next(i for i, c in enumerate(client.calls) if c[0] == "DELETE")
        rename_index = next(i for i, c in enumerate(client.calls)
                            if c[0] == "PATCH" and c[2] == {"name": "Original name"})
        self.assertGreater(rename_index, delete_index)
        self.assertTrue(all(i < delete_index for i, c in enumerate(client.calls)
                            if c[0] in ("PUT",) or c[1].endswith("/TablePlayout") and c[0] == "PATCH"))

    def test_main_dry_run_never_mutates_and_writes_complete_workbook(self):
        client = FixtureClient()
        with tempfile.TemporaryDirectory() as directory, patch.object(m, "Client", return_value=client):
            code = m.main([
                "--username", "user", "--password", "password",
                "--source", "Port 3", "--destination", "Port 5",
                "--dry-run", "--report-dir", directory,
            ])
            self.assertEqual(code, 0)
            self.assertTrue(all(method == "GET" for method, _, _ in client.calls))
            reports = list(Path(directory).glob("*/migration.xlsx"))
            self.assertEqual(len(reports), 1)
            results = json.loads(reports[0].with_name("results.json").read_text())
            self.assertEqual(results[0]["state"], "planned")
            with zipfile.ZipFile(reports[0]) as archive:
                sheet = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
                ns = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
                values = [[c.find("x:is/x:t", ns).text or "" for c in r.findall("x:c", ns)]
                          for r in sheet.findall("x:sheetData/x:row", ns)]
                folder = values[0].index("Folder")
                self.assertEqual(values[1][folder], "Folder with spaces")
            self.close_logging()

    def test_missing_folder_on_replacement_never_deletes_original(self):
        client = FixtureClient()
        original_request = client.request

        def request(method, path, body=None):
            response = original_request(method, path, body)
            if method == "POST" and path.startswith(m.TS):
                client.outputs["new"]["tags"] = []
            return response

        client.request = request
        with tempfile.TemporaryDirectory() as directory:
            migrator = m.Migrator(client, m.Schemas(client.swagger), Path(directory), verify_timeout=0.001)
            snapshot = migrator.snapshot("old")
            entry = {"output": snapshot["output"], "state": "planned"}
            migrator.entries.append(entry)
            with self.assertRaisesRegex(m.MigrationError, "/tags"):
                migrator.migrate(snapshot, "Data3", entry)
        self.assertIn("old", client.outputs)
        self.assertFalse(any(c[0] == "DELETE" for c in client.calls))

    def test_frozen_report_directory_is_beside_executable_not_extraction_directory(self):
        with patch.object(m.sys, "frozen", True, create=True), \
                patch.object(m.sys, "executable", "/opt/vdcm-migrate/migrate-outputs"):
            self.assertEqual(m.default_report_dir(), Path("/opt/vdcm-migrate/runs"))

    def test_in_place_migration_retains_id_and_never_deletes(self):
        client = FixtureClient()
        original_request = client.request

        def request(method, path, body=None):
            if method == "PATCH" and body == {"interface": "Data3"}:
                client.calls.append((method, path, body))
                client.outputs["old"]["interface"] = "Data3"
                return None
            return original_request(method, path, body)

        client.request = request
        with tempfile.TemporaryDirectory() as directory:
            migrator = m.Migrator(client, m.Schemas(client.swagger), Path(directory), verify_timeout=0.01)
            snapshot = migrator.snapshot("old")
            entry = {"output": snapshot["output"], "state": "planned"}
            migrator.entries.append(entry)
            migrator.migrate(snapshot, "Data3", entry)
        self.assertEqual(entry["new_id"], "old")
        self.assertEqual(entry["method"], "PATCH")
        self.assertEqual(client.outputs["old"]["tags"], ["Folder with spaces"])
        self.assertFalse(any(c[0] in ("POST", "DELETE") for c in client.calls))

    def test_main_continues_after_one_output_failure(self):
        client = FixtureClient()
        client.outputs["second"] = {**client.outputs["old"], "id": "second", "name": "Second"}
        client.settings["second"] = copy.deepcopy(client.settings["old"])
        visited = []

        def migrate(instance, snapshot, destination, entry):
            visited.append(snapshot["output"]["id"])
            if snapshot["output"]["id"] == "old":
                raise m.MigrationError("Application rejected first output")
            entry.update(state="migrated", new_id="new-second")

        with tempfile.TemporaryDirectory() as directory:
            with patch.object(m, "Client", return_value=client), patch.object(m.Migrator, "migrate", migrate):
                code = m.main([
                    "--username", "user", "--password", "password",
                    "--source", "Port 3", "--destination", "Port 5",
                    "--execute", "--report-dir", directory,
                ])
            self.assertEqual(code, 1)
            results_path = next(Path(directory).glob("*/results.json"))
            entries = json.loads(results_path.read_text())
            self.assertEqual([e["state"] for e in entries], ["failed", "migrated"])
            self.assertFalse(list(Path(directory).glob("*.lock")))
            self.close_logging()
        self.assertCountEqual(visited, ["old", "second"])


class ConcurrentFixtureClient(FixtureClient):
    def __init__(self, count):
        super().__init__()
        template = self.outputs["old"]
        self.outputs = {f"old-{i}": {**template, "id": f"old-{i}", "name": f"Name {i}"}
                        for i in range(count)}
        self.settings = {key: {suffix: {"setting": 7} for suffix in m.TS_PARTS}
                         for key in self.outputs}
        self.lock = threading.Lock()
        self.serial = 0

    def request(self, method, path, body=None):
        time.sleep(0.001)
        with self.lock:
            if method == "POST" and path.split("?")[0] == m.TS:
                self.serial += 1
                new_id = f"new-{self.serial}"
                self.calls.append((method, path, copy.deepcopy(body)))
                self.outputs[new_id] = {"id": new_id, **copy.deepcopy(body)}
                self.settings[new_id] = {suffix: {"setting": 1} for suffix in m.TS_PARTS}
                return copy.deepcopy(self.outputs[new_id])
            return super().request(method, path, body)


class ConcurrencyTests(unittest.TestCase):
    def tearDown(self):
        WorkflowTests.close_logging()

    def test_parallel_full_migrations_have_distinct_ids_and_complete_atomic_journals(self):
        client = ConcurrentFixtureClient(12)
        with tempfile.TemporaryDirectory() as directory, patch.object(m, "Client", return_value=client):
            code = m.main([
                "--username", "user", "--password", "password", "--source", "Port 3",
                "--destination", "Port 5", "--workers", "4", "--execute", "--report-dir", directory,
            ])
            self.assertEqual(code, 0)
            run = next(Path(directory).glob("*/results.json")).parent
            entries = json.loads((run / "results.json").read_text())
            self.assertEqual(len(entries), 12)
            self.assertEqual(len({e["new_id"] for e in entries}), 12)
            self.assertTrue(all(e["state"] == "migrated" for e in entries))
            journals = list((run / "journals").glob("*.json"))
            self.assertEqual(len(journals), 12)
            by_id = {e["output"]["id"]: e for e in entries}
            for path in journals:
                data = json.loads(path.read_text())
                self.assertEqual(data, [by_id[data[0]["output"]["id"]]])
            self.assertFalse(list(run.rglob("*.tmp")))
            summary = json.loads((run / "summary.json").read_text())
            self.assertEqual(summary["workers_effective"], 4)
            self.assertGreater(summary["outputs_per_second"], 0)
            self.assertGreater(len({e["worker"] for e in entries}), 1)
            WorkflowTests.close_logging()
        self.assertEqual(len(client.outputs), 12)
        self.assertTrue(all(v["tags"] == ["Folder with spaces"] and v["interface"] == "Data3"
                            for v in client.outputs.values()))

    def test_hundreds_of_outputs_never_exceed_worker_limit(self):
        lock = threading.Lock()
        active = maximum = finished = 0

        def operation(entry):
            nonlocal active, maximum, finished
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.002)
            with lock:
                active -= 1
                finished += 1

        m.run_batch([{"id": i} for i in range(300)], 4, operation)
        self.assertEqual(finished, 300)
        self.assertEqual(active, 0)
        self.assertEqual(maximum, 4)

    def test_batch_error_cancels_pending_work_and_waits_for_active_workers(self):
        lock = threading.Lock()
        active = finished = 0

        def operation(entry):
            nonlocal active, finished
            if entry["id"] == 0:
                raise m.MigrationError("Stop submission")
            with lock:
                active += 1
            time.sleep(0.02)
            with lock:
                active -= 1
                finished += 1

        with self.assertRaisesRegex(m.MigrationError, "Stop submission"):
            m.run_batch([{"id": i} for i in range(100)], 4, operation)
        self.assertEqual(active, 0)
        self.assertLess(finished, 99)

    def test_duplicate_source_ids_fail_before_workers_or_mutations(self):
        client = FixtureClient()
        original = client.request

        def request(method, path, body=None):
            if path == m.TS:
                output = copy.deepcopy(client.outputs["old"])
                return {"OutputTSs": [output, copy.deepcopy(output)]}
            return original(method, path, body)

        client.request = request
        with tempfile.TemporaryDirectory() as directory, patch.object(m, "Client", return_value=client):
            code = m.main([
                "--username", "user", "--password", "password", "--source", "Port 3",
                "--destination", "Port 5", "--workers", "4", "--execute", "--report-dir", directory,
            ])
            self.assertEqual(code, 2)
            self.assertTrue(all(c[0] == "GET" for c in client.calls))
            WorkflowTests.close_logging()

    def test_shared_authentication_is_single_and_transport_is_per_thread(self):
        client = m.Client("https://device:8443", "user", "secret")
        barrier = threading.Barrier(4)
        openers = []
        auth_calls = []

        def request(method, path, body=None):
            if path == "/api/authenticate":
                auth_calls.append(path)
                time.sleep(0.01)
                return {"token": "shared-token"}
            return {"ok": True}

        def operation(entry):
            barrier.wait(timeout=5)
            openers.append(client.opener)
            self.assertEqual(client.request("GET", "/api/test"), {"ok": True})

        client._request = request
        m.run_batch([{} for _ in range(4)], 4, operation)
        self.assertEqual(len(auth_calls), 1)
        self.assertEqual(len({id(v) for v in openers}), 4)

    def test_simultaneous_401_responses_refresh_only_once(self):
        client = m.Client("https://device:8443", "user", "secret")
        client.token, client.token_at = "old-token", time.monotonic()
        barrier = threading.Barrier(4)
        local = threading.local()
        auth_calls = []

        def request(method, path, body=None):
            if path == "/api/authenticate":
                auth_calls.append(path)
                return {"token": "new-token"}
            if not getattr(local, "retried", False):
                local.retried = True
                barrier.wait(timeout=5)
                raise m.APIError(method, path, 401, "Expired")
            return {"ok": True}

        client._request = request

        def operation(entry):
            self.assertEqual(client.request("GET", "/api/test"), {"ok": True})

        m.run_batch([{} for _ in range(4)], 4, operation)
        self.assertEqual(auth_calls, ["/api/authenticate"])

    def test_duplicate_created_id_is_not_adopted_by_second_worker(self):
        client = ConcurrentFixtureClient(0)
        original = client.request

        def request(method, path, body=None):
            if method == "POST":
                return {"id": "duplicate"}
            if path == m.TS:
                return {"OutputTSs": []}
            if path == m.TS + "/duplicate":
                return {"id": "duplicate"}
            return original(method, path, body)

        client.request = request
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "journals").mkdir()
            coordinator = m.Migrator(client, m.Schemas(client.swagger), root)
            entries = [{"output": {"id": str(i)}, "state": "planned"} for i in range(2)]
            successes, errors = [], []

            def operation(entry):
                worker = coordinator.worker(entry)
                try:
                    successes.append(worker.create(m.TS, "OutputTSs", {}, entry, "new_id"))
                except m.MigrationError as exc:
                    errors.append(str(exc))

            m.run_batch(entries, 2, operation)
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(errors), 1)
        self.assertIn("existing resource", errors[0])


if __name__ == "__main__":
    unittest.main()
