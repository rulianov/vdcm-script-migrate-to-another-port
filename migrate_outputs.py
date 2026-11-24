#!/usr/bin/env python3
"""Safely migrate vDCM XGRESS outputs or inputs between ports using only the Python standard library."""

from __future__ import annotations

import argparse
import contextlib
import copy
from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime as dt
import getpass
import hashlib
import http.client
import json
import logging
import math
import os
from pathlib import Path
import re
import ssl
import sys
import threading
import time
from typing import Any, Callable
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
import zipfile


TS = "/api/v2/OutputTSs"
SERVICES = "/api/v2/OutputServices"
ROUTES = "/api/v3/OutputServiceRoutings"
TS_PARTS = (
    "Pids", "SiComponents/AutoPassTables", "SiComponents/CatSettings",
    "SiComponents/PatSettings", "SiComponents/SiTables",
    "SiComponents/TablePlayout", "SiComponents/TotSettings",
)
SERVICE_PARTS = ("Alternates", "Components", "Processing", "SiComponents")
COLLECTIONS = ("RealignmentMux", "SiComponents/OtherTables")
IDENTITY = {"id", "url", "sw_ref", "legacyUUID"}


class MigrationError(Exception):
    """An explicit safety or configuration failure."""


def failure(exc: Exception) -> Exception:
    if isinstance(exc, (MigrationError, KeyError, TypeError, ValueError)):
        return exc
    logging.exception("Unexpected error")
    return MigrationError(f"Unexpected {type(exc).__name__}: {exc}")


class APIError(MigrationError):
    def __init__(self, method: str, path: str, status: int | None, detail: str):
        self.method, self.path, self.status, self.detail = method, path, status, detail
        super().__init__(f"{method} {path}: HTTP {status or 'unavailable'}: {detail}")

    def interface_restricted(self) -> bool:
        if self.status != 409:
            return False
        try:
            body = json.loads(self.detail)
        except json.JSONDecodeError:
            return False
        return any(
            e.get("field_name") == "interface"
            and "Restricted to change" in e.get("error", "")
            for e in body.get("errors", [])
        )


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def host_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value if "://" in value else "https://" + value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.path not in ("", "/") or parsed.query
            or parsed.fragment):
        raise argparse.ArgumentTypeError("Host must be an HTTPS origin or IP/hostname")
    try:
        port = parsed.port or 8443
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    hostname = parsed.hostname
    if ":" in hostname:
        hostname = "[" + hostname + "]"
    return f"https://{hostname}:{port}"


class Client:
    def __init__(self, base: str, username: str, password: str, *,
                 insecure: bool = False, ca_file: str | None = None,
                 timeout: float = 20, read_retries: int = 2):
        self.base, self.username, self.password = base, username, password
        self.timeout, self.read_retries = timeout, read_retries
        self.token, self.token_at = "", 0.0
        self._auth_lock = threading.RLock()
        self._transport = threading.local()
        context = ssl.create_default_context(cafile=ca_file)
        if insecure:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        self._context = context

    @property
    def opener(self) -> urllib.request.OpenerDirector:
        if not hasattr(self._transport, "opener"):
            self._transport.opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}), NoRedirect(),
                urllib.request.HTTPSHandler(context=self._context),
            )
        return self._transport.opener

    @opener.setter
    def opener(self, value: urllib.request.OpenerDirector) -> None:
        self._transport.opener = value

    def redact(self, text: str) -> str:
        for secret in (self.token, self.password):
            if secret:
                text = text.replace(secret, "[REDACTED]")
        return text

    def _request(self, method: str, path: str, body: Any = None) -> Any:
        if not path.startswith("/") or path.startswith("//"):
            raise MigrationError(f"Refusing non-local API path: {path}")
        headers = {"Accept": "application/json"}
        if path != "/api/authenticate":
            headers["Authorization"] = self.token
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body, allow_nan=False).encode("utf-8")
        request = urllib.request.Request(
            self.base + path, data=data, headers=headers, method=method,
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace") or str(exc.reason)
            raise APIError(method, path, exc.code, self.redact(detail)) from exc
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
            raise APIError(method, path, None, self.redact(str(exc))) from exc
        if not raw.strip():
            return None
        try:
            result = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise APIError(method, path, None, "Non-JSON response: " + self.redact(raw)) from exc
        if method != "GET":
            check_inline_errors(result, method, path)
        return result

    def authenticate(self) -> None:
        with self._auth_lock:
            self._authenticate()

    def _authenticate(self) -> None:
        response = self._request(
            "POST", "/api/authenticate",
            {"login": self.username, "password": self.password},
        )
        if not isinstance(response, dict) or not isinstance(response.get("token"), str):
            raise MigrationError("Authentication response has no string token")
        self.token = response["token"]
        if not self.token:
            raise MigrationError("Authentication returned an empty token")
        self.token_at = time.monotonic()

    def request(self, method: str, path: str, body: Any = None) -> Any:
        with self._auth_lock:
            if not self.token or time.monotonic() - self.token_at >= 540:
                self.authenticate()
        refreshed = False
        attempts = 0
        while True:
            attempted_token = self.token
            try:
                return self._request(method, path, body)
            except APIError as exc:
                if exc.status == 401 and not refreshed:
                    with self._auth_lock:
                        if self.token == attempted_token:
                            self.authenticate()
                    refreshed = True
                    continue
                if (method == "GET" and attempts < self.read_retries
                        and exc.status in (None, 500, 502, 503, 504)):
                    attempts += 1
                    logging.warning("Read retry %d: %s", attempts, exc)
                    time.sleep(attempts)
                    continue
                raise


def check_inline_errors(value: Any, method: str, path: str) -> None:
    if isinstance(value, dict):
        status = value.get("status", value.get("status_code"))
        failed = isinstance(status, int) and status >= 400
        if failed or isinstance(value.get("errors"), list) and value["errors"]:
            raise APIError(method, path, status if failed else 409, json.dumps(value))
        for child in value.values():
            check_inline_errors(child, method, path)
    elif isinstance(value, list):
        for child in value:
            check_inline_errors(child, method, path)


def rows(response: Any, key: str) -> list[dict[str, Any]]:
    if not isinstance(response, dict) or not isinstance(response.get(key), list):
        raise MigrationError(f"Expected JSON object containing list {key!r}")
    result = response[key]
    if not all(isinstance(v, dict) for v in result):
        raise MigrationError(f"Invalid objects in {key}")
    if isinstance(response.get("count"), int) and response["count"] != len(result):
        raise MigrationError(f"{key}: incomplete collection; pagination is not supported")
    return result


def resource_path(base: str, resource_id: str) -> str:
    if not isinstance(resource_id, str) or not resource_id:
        raise MigrationError("Missing resource ID")
    return base + "/" + urllib.parse.quote(resource_id, safe="")


def resolve_port(config: Any, name: str) -> dict[str, Any]:
    ports = rows(config, "videoPorts")
    matches = [p for p in ports if name in (p.get("portName"), p.get("interface"))]
    if len(matches) != 1:
        available = ", ".join(f"{p.get('portName')!r} ({p.get('interface')})" for p in ports)
        raise MigrationError(f"Port/interface {name!r} is missing or ambiguous. Available: {available}")
    port = matches[0]
    if not isinstance(port.get("interface"), str):
        raise MigrationError(f"No interface for port {name!r}")
    return port


class Schemas:
    def __init__(self, swagger: Any):
        if not isinstance(swagger, dict) or not swagger.get("definitions") or not swagger.get("paths"):
            raise MigrationError("/api/swagger.json did not return a Swagger schema")
        self.swagger = swagger

    def resolve(self, schema: dict[str, Any]) -> dict[str, Any]:
        if "$ref" in schema:
            prefix = "#/definitions/"
            if not schema["$ref"].startswith(prefix):
                raise MigrationError(f"Unsupported schema reference: {schema['$ref']}")
            target = self.swagger["definitions"].get(schema["$ref"][len(prefix):])
            if not isinstance(target, dict):
                raise MigrationError(f"Missing schema reference: {schema['$ref']}")
            schema = {**target, **{k: v for k, v in schema.items() if k != "$ref"}}
        return schema

    def operation(self, path: str, method: str) -> dict[str, Any]:
        path = path.split("?")[0]
        for template, operations in self.swagger["paths"].items():
            pattern = re.sub(r"\\\{[^}]+\\\}", "[^/]+", re.escape(template))
            if re.fullmatch(pattern, path) and method.lower() in operations:
                for param in operations[method.lower()].get("parameters", []):
                    if param.get("in") == "body":
                        return param["schema"]
        raise MigrationError(f"No documented {method} request schema for {path}")

    def project(self, value: Any, schema: dict[str, Any], *,
                writing: bool = False, location: str = "") -> Any:
        schema = self.resolve(schema)
        if isinstance(value, dict):
            properties = schema.get("properties")
            if properties is None:
                raise MigrationError(f"No object schema at {location}")
            unknown = set(value) - set(properties) - IDENTITY
            if any(value[k] is not None for k in unknown):
                raise MigrationError(f"Unrecognized configuration at {location}: {sorted(unknown)}")
            result = {}
            for key, child in value.items():
                definition = properties.get(key)
                if definition is None or self.resolve(definition).get("readOnly") or child is None:
                    continue
                result[key] = self.project(
                    child, definition, writing=writing, location=location + "/" + key,
                )
            if value.get("generateSyncUUID") is True:
                result.pop("syncUUID", None)
            return result
        if isinstance(value, list):
            if "items" not in schema:
                raise MigrationError(f"No array item schema at {location}")
            # Passed descriptors are generated from the input, not writable configuration.
            values = [
                v for v in value
                if not (location.endswith("/pmtDescriptors") and isinstance(v, dict)
                        and v.get("type") == "Passed")
            ]
            return [
                self.project(v, schema["items"], writing=writing, location=location + "[]")
                for v in values
            ]
        return value

    def payload(self, path: str, method: str, value: Any) -> Any:
        return self.project(value, self.operation(path, method), writing=True, location=path)


def difference(expected: Any, actual: Any, location: str = "") -> list[str]:
    if isinstance(expected, dict) and isinstance(actual, dict):
        result = []
        for key in sorted(set(expected) | set(actual)):
            if key not in expected or key not in actual:
                result.append(f"{location}/{key}: {expected.get(key)!r} != {actual.get(key)!r}")
            else:
                result.extend(difference(expected[key], actual[key], location + "/" + key))
        return result
    if expected != actual:
        return [f"{location}: {expected!r} != {actual!r}"]
    return []


def remap(value: Any, mapping: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {k: remap(v, mapping) for k, v in value.items()}
    if isinstance(value, list):
        return [remap(v, mapping) for v in value]
    if isinstance(value, str):
        if value in mapping:
            return mapping[value]
        parsed = urllib.parse.urlsplit(value)
        if parsed.path.startswith("/api/"):
            parts = parsed.path.split("/")
            parts = [mapping.get(part, part) for part in parts]
            return urllib.parse.urlunsplit(parsed._replace(path="/".join(parts)))
    return value


def alternate_activation(value: Any) -> dict[str, Any]:
    sources = rows(value, "alternateSources")
    if not sources:
        return {"active": None, "groups": []}
    active = [i for i, source in enumerate(sources) if source.get("activeState") == "Active"]
    if len(active) != 1:
        raise MigrationError("Cannot unambiguously identify the active alternate source")

    def backup_index(backups: list[dict[str, Any]]) -> int:
        selected = [i + 1 for i, backup in enumerate(backups) if backup.get("activeState") == "Active"]
        if len(selected) > 1:
            raise MigrationError("Multiple active backups; cannot preserve backup selection")
        return selected[0] if selected else 0

    groups = []
    for index, source in enumerate(sources):
        merged = [{
            "mergedServiceId": service["mergedServiceId"],
            "activeBackupIndex": backup_index(service.get("backupServices", [])),
        } for service in source.get("mergedServices", [])]
        groups.append({
            "activeAlternateIndex": index,
            "activeBackupIndex": backup_index(source.get("backupServices", [])),
            "mergedServicesBackup": merged,
        })
    return {"active": active[0], "groups": groups}


def atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=True, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def write_excel(path: Path, sheets: dict[str, list[list[Any]]]) -> None:
    """Write an actual Office Open XML workbook, without Excel packages."""
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    package_rel = "http://schemas.openxmlformats.org/package/2006/relationships"
    content_ns = "http://schemas.openxmlformats.org/package/2006/content-types"
    ET.register_namespace("", ns)

    def xml(element: ET.Element) -> bytes:
        return ET.tostring(element, encoding="utf-8", xml_declaration=True)

    def column(number: int) -> str:
        text = ""
        while number:
            number, remainder = divmod(number - 1, 26)
            text = chr(65 + remainder) + text
        return text

    types = ET.Element("Types", xmlns=content_ns)
    ET.SubElement(types, "Default", Extension="rels",
                  ContentType="application/vnd.openxmlformats-package.relationships+xml")
    ET.SubElement(types, "Default", Extension="xml", ContentType="application/xml")
    for part, kind in [
        ("/xl/workbook.xml", "sheet.main"),
        *[(f"/xl/worksheets/sheet{i}.xml", "worksheet") for i in range(1, len(sheets) + 1)],
    ]:
        ET.SubElement(types, "Override", PartName=part,
                      ContentType=f"application/vnd.openxmlformats-officedocument.spreadsheetml.{kind}+xml")
    root_rels = ET.Element("Relationships", xmlns=package_rel)
    ET.SubElement(root_rels, "Relationship", Id="rId1", Type=rel + "/officeDocument",
                  Target="xl/workbook.xml")
    workbook = ET.Element("workbook", xmlns=ns)
    sheet_elements = ET.SubElement(workbook, "sheets")
    workbook_rels = ET.Element("Relationships", xmlns=package_rel)
    temporary = path.with_suffix(".xlsx.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as output, zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for index, (name, values) in enumerate(sheets.items(), 1):
            if len(values) > 1048576:
                raise MigrationError(f"Excel sheet {name} exceeds row limit")
            ET.SubElement(sheet_elements, "sheet", name=name, sheetId=str(index),
                          attrib={f"{{{rel}}}id": f"rId{index}"})
            ET.SubElement(workbook_rels, "Relationship", Id=f"rId{index}",
                          Type=rel + "/worksheet", Target=f"worksheets/sheet{index}.xml")
            sheet = ET.Element("worksheet", xmlns=ns)
            views = ET.SubElement(sheet, "sheetViews")
            view = ET.SubElement(views, "sheetView", workbookViewId="0")
            ET.SubElement(view, "pane", ySplit="1", topLeftCell="A2",
                          activePane="bottomLeft", state="frozen")
            data = ET.SubElement(sheet, "sheetData")
            for row_number, row in enumerate(values, 1):
                element = ET.SubElement(data, "row", r=str(row_number))
                for col_number, value in enumerate(row, 1):
                    cell = ET.SubElement(element, "c", r=f"{column(col_number)}{row_number}",
                                         t="inlineStr")
                    text = "" if value is None else str(value)
                    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
                    if len(text) > 32767:
                        raise MigrationError(f"Excel cell too large in {name}, row {row_number}")
                    ET.SubElement(ET.SubElement(cell, "is"), "t",
                                  attrib={"{http://www.w3.org/XML/1998/namespace}space": "preserve"}).text = text
            if values and values[0]:
                ET.SubElement(sheet, "autoFilter",
                              ref=f"A1:{column(len(values[0]))}{len(values)}")
            archive.writestr(f"xl/worksheets/sheet{index}.xml", xml(sheet))
        archive.writestr("[Content_Types].xml", xml(types))
        archive.writestr("_rels/.rels", xml(root_rels))
        archive.writestr("xl/workbook.xml", xml(workbook))
        archive.writestr("xl/_rels/workbook.xml.rels", xml(workbook_rels))
    os.replace(temporary, path)


class Migrator:
    def __init__(self, client: Client, schemas: Schemas, run_dir: Path, *,
                 verify_timeout: float = 30, coordinator: Migrator | None = None,
                 journal_path: Path | None = None):
        self.client, self.schemas, self.run_dir = client, schemas, run_dir
        self.verify_timeout = verify_timeout
        self.entries: list[dict[str, Any]] = []
        self.journal_path = journal_path or run_dir / "results.json"
        self._journal_lock = threading.RLock()
        self._patch_rejected = coordinator._patch_rejected if coordinator else threading.Event()
        self._creation_lock = coordinator._creation_lock if coordinator else threading.Lock()
        self._known_ids: dict[str, set[str]] = coordinator._known_ids if coordinator else {}
        # Output service ID -> transcoding flow memberships (shared by all workers).
        self.flow_members: dict[str, list[dict[str, Any]]] = (
            coordinator.flow_members if coordinator else {})
        self._flow_locks: dict[str, threading.Lock] = coordinator._flow_locks if coordinator else {}
        self._flow_guard = coordinator._flow_guard if coordinator else threading.Lock()

    @property
    def interface_patch_rejected(self) -> bool:
        return self._patch_rejected.is_set()

    @interface_patch_rejected.setter
    def interface_patch_rejected(self, value: bool) -> None:
        if value:
            self._patch_rejected.set()
        else:
            self._patch_rejected.clear()

    def worker(self, entry: dict[str, Any]) -> Migrator:
        worker = Migrator(
            self.client, self.schemas, self.run_dir, verify_timeout=self.verify_timeout,
            coordinator=self, journal_path=self.run_dir / "journals" /
            (urllib.parse.quote(entry["output"]["id"], safe="") + ".json"),
        )
        worker.entries = [entry]
        return worker

    def backup_entry(self, entry: dict[str, Any]) -> None:
        try:
            snapshot = self.snapshot(entry["output"]["id"])
            self.signature(snapshot)
            entry["snapshot"] = snapshot
            entry["flows"] = [member for service in snapshot["services"]
                              for member in self.flow_members.get(service["output"]["id"], [])]
            atomic_json(self.run_dir / (urllib.parse.quote(entry["output"]["id"], safe="") + ".json"), snapshot)
        except Exception as exc:  # journal every failure; never abort the batch
            exc = failure(exc)
            entry.update(state="failed", error=str(exc))
            logging.error("Snapshot failed for %s: %s", entry["output"]["id"], exc)
        self.checkpoint()

    def execute_entry(self, entry: dict[str, Any], destination: str) -> None:
        if entry["state"] == "failed":
            return
        start = time.monotonic()
        entry["worker"] = threading.current_thread().name
        logging.info("START %s worker=%s", entry["output"]["id"], entry["worker"])
        try:
            # Outputs of one transcoding flow migrate sequentially so flow verification is exact.
            with contextlib.ExitStack() as stack:
                for flow_id in sorted({m["flow_id"] for m in entry.get("flows", [])}):
                    with self._flow_guard:
                        lock = self._flow_locks.setdefault(flow_id, threading.Lock())
                    stack.enter_context(lock)
                self.migrate(entry["snapshot"], destination, entry)
            logging.info("SUCCESS %s -> %s", entry["output"]["id"], entry["new_id"])
            for change in entry.get("flow_output_ids", {}).values():
                logging.info("FLOW %s (%s): outputId %r -> %r", change["flow_name"], change["flow_id"],
                             change["old_output_id"], change["new_output_id"])
            for service_id, change in entry.get("acquisition_point_changes", {}).items():
                logging.info("LSS %s (Disabled): acquisitionPointId %r -> %r", service_id,
                             change["old"], change["new"])
            for service_id, change in entry.get("es_order_changes", {}).items():
                logging.info("PMT %s: ES order %s -> %s (identical ES set)", service_id,
                             change["old"], change["new"])
        except Exception as exc:  # journal every failure; never abort the batch
            exc = failure(exc)
            entry["state_before_failure"] = entry["state"]
            entry["error"] = str(exc)
            logging.error("FAILED %s: %s", entry["output"]["id"], exc)
            self.cleanup_failed_replacement(entry)
            if entry["state"] not in ("source_retained_off", "source_deleted"):
                entry["state"] = "failed"
            if entry.get("flows") and entry.get("service_id_map") and entry["state"] == "failed":
                entry["flow_note"] = (
                    "Replacement service(s) " + ", ".join(entry["service_id_map"].values())
                    + " joined transcoding flow(s) " + ", ".join(sorted({
                        f"{m['flow_name']} ({m['flow_id']})" for m in entry["flows"]}))
                    + "; replacement output " + str(entry.get("new_id"))
                    + " is kept with streaming Off for inspection. Delete it to remove the extra flow output.")
                logging.error("%s", entry["flow_note"])
        finally:
            entry["duration_seconds"] = time.monotonic() - start
            self.checkpoint()

    def get(self, path: str) -> Any:
        return self.client.request("GET", path)

    def wait(self, path: str, expected: Any, schema: dict[str, Any]) -> Any:
        deadline = time.monotonic() + self.verify_timeout
        while True:
            actual = self.get(path)
            projected = self.schemas.project(actual, schema, location=path)
            errors = difference(expected, projected)
            if not errors:
                return actual
            if time.monotonic() >= deadline:
                raise MigrationError(f"Verification failed for {path}: " + "; ".join(errors))
            time.sleep(0.5)

    def part(self, path: str, method: str) -> dict[str, Any]:
        raw = self.get(path)
        schema = self.schemas.operation(path, method)
        return {"raw": raw, "method": method, "schema": schema,
                "config": self.schemas.project(raw, schema, location=path)}

    def snapshot(self, output_id: str) -> dict[str, Any]:
        path = resource_path(TS, output_id)
        raw = self.get(path)
        snapshot = {"output": raw, "parts": {}, "collections": {}, "services": []}
        for suffix in TS_PARTS:
            method = "PATCH" if suffix.endswith("TablePlayout") else "PUT"
            snapshot["parts"][suffix] = self.part(path + "/" + suffix, method)
        for suffix in COLLECTIONS:
            collection_path = path + "/" + suffix
            key = suffix.split("/")[-1]
            collection = rows(self.get(collection_path), key)
            schema = self.schemas.operation(collection_path, "POST")
            snapshot["collections"][suffix] = [
                {"raw": self.get(resource_path(collection_path, item["id"])), "schema": schema}
                for item in collection
            ]
        for service in rows(self.get(path + "/OutputServices"), "OutputServices"):
            service_path = resource_path(SERVICES, service["id"])
            detail = self.get(service_path)
            routing = self.get(resource_path(ROUTES, service["id"]))
            snapshot["services"].append({
                "output": detail, "routing": routing,
                "parts": {suffix: self.part(service_path + "/" + suffix, "PUT")
                          for suffix in SERVICE_PARTS},
                "pids": self.get(service_path + "/Pids"),
            })
        return snapshot

    def signature(self, snapshot: dict[str, Any]) -> Any:
        return {
            "output": self.schemas.payload(TS, "POST", snapshot["output"]),
            "parts": {k: v["config"] for k, v in snapshot["parts"].items()},
            "collections": {k: [self.schemas.project(v["raw"], v["schema"]) for v in values]
                            for k, values in snapshot["collections"].items()},
            "services": [{
                "output": self.schemas.payload(SERVICES, "POST", s["output"]),
                "routing": s["routing"],
                "parts": {k: v["config"] for k, v in s["parts"].items()},
                "activation": alternate_activation(s["parts"]["Alternates"]["raw"]),
            } for s in snapshot["services"]],
        }

    def checkpoint(self) -> None:
        with self._journal_lock:
            atomic_json(self.journal_path, self.entries)

    def write(self, method: str, path: str, body: Any = None) -> Any:
        logging.info("%s %s", method, path)
        self.checkpoint()
        return self.client.request(method, path, body)

    def create(self, collection: str, key: str, payload: Any,
               entry: dict[str, Any], id_key: str) -> dict[str, Any]:
        with self._creation_lock:
            if collection not in self._known_ids:
                self._known_ids[collection] = {v["id"] for v in rows(self.get(collection), key)}
        # Never retry POST: a lost response may already have created the resource.
        response = self.write("POST", collection + "?results=true", payload)
        entry["last_create_response"] = response
        if not isinstance(response, dict) or not isinstance(response.get("id"), str):
            raise MigrationError("POST response has no authoritative resource ID; manual inspection required")
        created_id = response["id"]
        with self._creation_lock:
            if created_id in self._known_ids[collection]:
                raise MigrationError("POST returned an existing resource; refusing to adopt or delete it")
            self._known_ids[collection].add(created_id)
        entry[id_key] = created_id
        self.checkpoint()
        deadline = time.monotonic() + self.verify_timeout
        while True:
            try:
                return self.get(resource_path(collection, created_id))
            except APIError as exc:
                if exc.status != 404:
                    raise
            if time.monotonic() >= deadline:
                raise MigrationError(f"POST accepted but new {key} cannot be identified; manual inspection required")
            time.sleep(0.5)

    def ensure_no_hidden_secrets(self, value: Any, path: str = "") -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key.endswith("Configured") and child is True:
                    raise MigrationError(f"{path}/{key}: configured secret is not retrievable; cannot safely recreate")
                self.ensure_no_hidden_secrets(child, path + "/" + key)
        elif isinstance(value, list):
            for child in value:
                self.ensure_no_hidden_secrets(child, path + "[]")

    def assert_unchanged(self, snapshot: dict[str, Any]) -> None:
        current = self.snapshot(snapshot["output"]["id"])
        changes = difference(self.signature(snapshot), self.signature(current))
        if changes:
            raise MigrationError("Source configuration changed during migration: " + "; ".join(changes))

    def verify_parts(self, base: str, parts: dict[str, Any], mapping: dict[str, str],
                     *, apply: bool) -> None:
        for suffix, part in parts.items():
            path = base + "/" + suffix
            expected = remap(part["config"], mapping)
            if apply:
                current = self.schemas.project(self.get(path), part["schema"], location=path)
                if difference(expected, current):
                    self.write(part["method"], path, expected)
            self.wait(path, expected, part["schema"])

    def verify_activation(self, service_path: str, original: Any, *,
                          apply: bool) -> None:
        expected = alternate_activation(original)
        path = service_path + "/Alternates"
        actual = alternate_activation(self.get(path))
        if apply and difference(expected, actual):
            groups = expected["groups"]
            active = expected["active"]
            if active is None:
                raise MigrationError(f"Cannot restore an empty alternate selection for {service_path}")
            order = [i for i in range(len(groups)) if i != active] + [active]
            for index in order:
                activation_path = service_path + "/Activate"
                payload = self.schemas.payload(activation_path, "PUT", groups[index])
                self.write("PUT", activation_path, payload)
        deadline = time.monotonic() + self.verify_timeout
        while difference(expected, actual):
            if time.monotonic() >= deadline:
                raise MigrationError(f"Active alternate/backup verification failed for {service_path}")
            time.sleep(0.5)
            actual = alternate_activation(self.get(path))

    def service_creation_order(self, services: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if len(services) < 2:
            return services

        assigned: list[set[int]] = []
        displaced: list[set[int]] = []
        for service in services:
            pids = service["pids"]
            used = {pids[k] for k in ("pmt", "pcr") if isinstance(pids.get(k), int)}
            used.update(es["pid"] for es in pids.get("es", [])
                        if isinstance(es.get("pid"), int))
            needed = {es["sourcePid"] for es in pids.get("es", [])
                      if isinstance(es.get("sourcePid"), int)
                      and es["sourcePid"] != es.get("pid")}
            input_id = (service["routing"].get("inputService") or {}).get("id")
            if input_id:
                input_pids = self.get(resource_path("/api/v2/InputServices", input_id) + "/Pids")
                for key in ("pmt", "pcr"):
                    value = input_pids.get(key)
                    if isinstance(value, int) and value != pids.get(key):
                        needed.add(value)
            assigned.append(used)
            displaced.append(needed)

        dependencies = [{owner for owner in range(len(services))
                         if owner != index and assigned[owner] & displaced[index]}
                        for index in range(len(services))]
        remaining = list(range(len(services)))
        order: list[int] = []
        while remaining:
            ready = next((index for index in remaining if not dependencies[index]), None)
            if ready is None:
                logging.warning("MPTS PID allocation dependencies are cyclic; keeping original service "
                                "order and requiring exact PID verification")
                return services
            order.append(ready)
            remaining.remove(ready)
            for index in remaining:
                dependencies[index].discard(ready)
        if order != list(range(len(services))):
            logging.info("MPTS service creation order adjusted to preserve PID assignments: %s",
                         ", ".join(services[index]["output"]["id"] for index in order))
        return [services[index] for index in order]

    def clone(self, snapshot: dict[str, Any], destination: str, entry: dict[str, Any]) -> str:
        self.ensure_no_hidden_secrets(snapshot)
        services_in_order = self.service_creation_order(snapshot["services"])
        original = snapshot["output"]
        payload = self.schemas.payload(TS, "POST", original)
        payload["interface"] = destination
        original_name = payload.get("name", "")
        if original_name:
            payload["name"] = "migration-" + uuid.uuid4().hex[:20]
        payload["streaming"] = "Off"
        entry["temporary_name"] = payload.get("name", "")
        created = self.create(TS, "OutputTSs", payload, entry, "new_id")
        new_id = created["id"]
        mapping = {original["id"]: new_id}
        new_path = resource_path(TS, new_id)
        schema = self.schemas.operation(TS, "POST")
        self.wait(new_path, payload, schema)
        service_parts: dict[str, dict[str, Any]] = {}
        for service in services_in_order:
            source_service = service["output"]
            service_config = self.schemas.payload(SERVICES, "POST", source_service)
            service_config["outputTS"] = {"id": new_id}
            route_payload: dict[str, Any] = {"outputService": service_config}
            routing = service["routing"]
            if (routing.get("inputService") or {}).get("id"):
                route_payload["inputService"] = {"id": routing["inputService"]["id"]}
            elif (routing.get("descrambledService") or {}).get("descrambledServiceId"):
                route_payload["descrambledService"] = {
                    "descrambledServiceId": routing["descrambledService"]["descrambledServiceId"],
                }
            elif (routing.get("processedService") or {}).get("sourceId"):
                route_payload["processedService"] = {"sourceId": routing["processedService"]["sourceId"]}
            created_service = self.create(
                ROUTES, "OutputServiceRoutings", route_payload, entry, "last_new_service_id",
            )
            new_service_id = created_service["outputService"]["id"]
            mapping[source_service["id"]] = new_service_id
            entry.setdefault("service_id_map", {})[source_service["id"]] = new_service_id
            self.checkpoint()
            service_path = resource_path(SERVICES, new_service_id)
            service_parts[source_service["id"]] = self.flow_service_parts(
                service, service_path, entry)
            self.verify_parts(service_path, service_parts[source_service["id"]], mapping, apply=True)
            self.verify_activation(service_path, service["parts"]["Alternates"]["raw"], apply=True)
        self.verify_parts(new_path, snapshot["parts"], mapping, apply=True)
        for suffix, items in snapshot["collections"].items():
            collection_path = new_path + "/" + suffix
            for item in items:
                config = remap(self.schemas.project(item["raw"], item["schema"]), mapping)
                self.write("POST", collection_path + "?results=true", config)
            actual = rows(self.get(collection_path), suffix.split("/")[-1])
            expected = [remap(self.schemas.project(item["raw"], item["schema"]), mapping)
                        for item in items]
            projected = [self.schemas.project(v, items[0]["schema"]) for v in actual] if items else []
            if len(actual) != len(items) or difference(expected, projected):
                raise MigrationError(f"Collection verification failed for {collection_path}")
        # Service-level settings and actual PID assignment must survive reprovisioning.
        for service in snapshot["services"]:
            source_service = service["output"]
            service_path = resource_path(SERVICES, mapping[source_service["id"]])
            expected = self.schemas.payload(SERVICES, "POST", source_service)
            expected["outputTS"] = self.schemas.project(self.get(new_path), schema)
            expected["outputTS"] = {
                k: expected["outputTS"][k] for k in source_service["outputTS"]
                if k in expected["outputTS"]
            }
            self.wait(service_path, expected, self.schemas.operation(SERVICES, "POST"))
            new_pids = self.get(service_path + "/Pids")
            in_flow = any(m["service_id"] == source_service["id"] for m in entry.get("flows", []))
            if self.verify_pid_assignment(service["pids"], new_pids, allow_es_order=in_flow):
                change = {"old": [es.get("pid") for es in service["pids"].get("es", [])],
                          "new": [es.get("pid") for es in new_pids.get("es", [])]}
                entry.setdefault("es_order_changes", {})[source_service["id"]] = change
                logging.warning("Flow output service %s: identical ES set, PMT order changed %s -> %s",
                                source_service["id"], change["old"], change["new"])
                self.checkpoint()
            new_route = self.get(resource_path(ROUTES, mapping[source_service["id"]]))
            for key in ("inputService", "descrambledService", "processedService"):
                if difference(service["routing"].get(key), new_route.get(key)):
                    raise MigrationError(f"Routing source {key} changed for {source_service['id']}")
            self.verify_parts(service_path, service_parts[source_service["id"]], mapping, apply=False)
            self.verify_activation(service_path, service["parts"]["Alternates"]["raw"], apply=False)
        self.verify_flows(entry, final=False)
        entry["state"] = "replacement_verified"
        self.checkpoint()
        self.assert_unchanged(snapshot)
        payload["streaming"] = original["streaming"]
        self.write("PATCH", new_path, {"streaming": original["streaming"]})
        self.wait(new_path, payload, schema)
        entry["verified_replacement"] = self.signature(self.snapshot(new_id))
        self.checkpoint()
        return new_id

    def flow_service_parts(self, service: dict[str, Any], service_path: str,
                           entry: dict[str, Any]) -> dict[str, Any]:
        """Expected service parts, allowing vDCM's own LSS acquisitionPointId on flow outputs.

        On transcoding-flow outputs vDCM assigns lss.acquisitionPointId itself and silently ignores
        writes while LSS mode is Disabled (where the value is inactive). Only that case is tolerated;
        the old/new values are recorded. With LSS Enabled every LSS field is restored and verified.
        """
        parts = service["parts"]
        source_id = service["output"]["id"]
        lss = ((parts.get("Processing") or {}).get("config") or {}).get("lss")
        if (not any(m["service_id"] == source_id for m in entry.get("flows", []))
                or not isinstance(lss, dict) or lss.get("mode") != "Disabled"):
            return parts
        assigned = (self.get(service_path + "/Processing").get("lss") or {}).get("acquisitionPointId")
        if assigned == lss.get("acquisitionPointId"):
            return parts
        parts = dict(parts)
        parts["Processing"] = copy.deepcopy(parts["Processing"])
        parts["Processing"]["config"]["lss"]["acquisitionPointId"] = assigned
        entry.setdefault("acquisition_point_changes", {})[source_id] = {
            "old": lss.get("acquisitionPointId"), "new": assigned}
        logging.warning("LSS is Disabled on flow output service %s; vDCM assigned acquisitionPointId "
                        "%r (was %r)", source_id, assigned, lss.get("acquisitionPointId"))
        self.checkpoint()
        return parts

    @staticmethod
    def verify_pid_assignment(original: Any, replacement: Any, *, allow_es_order: bool = False) -> bool:
        """Raise on any PID change; return True if only the ES order differs (flow outputs only).

        vDCM appends merged components/track rules added after creating a flow output to the end of
        the PMT ES list, so for flow outputs an identical ES set in a different order is accepted.
        """
        def stable(value: Any) -> Any:
            if isinstance(value, dict):
                return {k: stable(v) for k, v in value.items()
                        if k not in IDENTITY | {"serviceUUID", "serviceName", "pidProperty"}}
            if isinstance(value, list):
                return [stable(v) for v in value]
            return value
        changes = difference(stable(original), stable(replacement))
        if not changes:
            return False
        if allow_es_order and isinstance(original, dict) and isinstance(replacement, dict):
            def unordered(value: dict[str, Any]) -> Any:
                result = stable(value)
                if isinstance(result.get("es"), list):
                    result["es"] = sorted(result["es"], key=lambda es: json.dumps(es, sort_keys=True))
                return result
            if not difference(unordered(original), unordered(replacement)):
                return True
        raise MigrationError("Output PID assignment changed: " + "; ".join(changes))

    def delete_verified_source(self, snapshot: dict[str, Any], new_id: str,
                               entry: dict[str, Any]) -> None:
        original = snapshot["output"]
        source_path = resource_path(TS, original["id"])
        if original["id"] == new_id:
            raise MigrationError("Refusing to delete the migrated in-place output")
        self.assert_unchanged(snapshot)
        expected_replacement = entry.get("verified_replacement")
        if expected_replacement is not None:
            changes = difference(expected_replacement, self.signature(self.snapshot(new_id)))
            if changes:
                raise MigrationError("Replacement changed before source deletion: " + "; ".join(changes))
        entry["state"] = "source_delete_started"
        deletion_error = None
        try:
            self.write("DELETE", source_path)
        except APIError as exc:
            deletion_error = str(exc)
            logging.error("Source deletion response: %s", exc)
        # Even an error/timeout may follow an applied DELETE; inspect before stopping source.
        deadline = time.monotonic() + self.verify_timeout
        while True:
            deleted = False
            try:
                self.get(source_path)
            except APIError as exc:
                if exc.status != 404:
                    raise
                deleted = True
            if deleted:
                entry["state"] = "source_deleted"
                self.checkpoint()
                if original.get("name"):
                    self.write("PATCH", resource_path(TS, new_id), {"name": original["name"]})
                if expected_replacement is not None:
                    final_expected = copy.deepcopy(expected_replacement)
                    if original.get("name"):
                        final_expected["output"]["name"] = original["name"]
                        for service in final_expected["services"]:
                            for output_ref in (
                                service["output"]["outputTS"],
                                service["routing"]["outputService"]["outputTS"],
                            ):
                                if "name" in output_ref:
                                    output_ref["name"] = original["name"]
                    self.wait(resource_path(TS, new_id), final_expected["output"],
                              self.schemas.operation(TS, "POST"))
                    final_actual = self.signature(self.snapshot(new_id))
                    changes = difference(final_expected, final_actual)
                    if changes:
                        raise MigrationError("Source deleted, but final replacement verification failed: "
                                             + "; ".join(changes))
                return
            if deletion_error or time.monotonic() >= deadline:
                break
            time.sleep(0.5)
        entry["cleanup_error"] = deletion_error or "DELETE accepted, but source still exists"
        logging.error("%s", entry["cleanup_error"])
        # Only stop an unchanged source after the fully verified replacement is active.
        self.assert_unchanged(snapshot)
        self.write("PATCH", source_path, {"streaming": "Off"})
        if self.get(source_path).get("streaming") != "Off":
            raise MigrationError("Source deletion failed and streaming Off was not verified")
        entry["state"] = "source_retained_off"
        raise MigrationError("Replacement verified, but source could not be deleted; source streaming is Off. "
                             + entry["cleanup_error"])

    def migrate(self, snapshot: dict[str, Any], destination: str,
                entry: dict[str, Any]) -> None:
        original = snapshot["output"]
        path = resource_path(TS, original["id"])
        entry["state"] = "started"
        self.assert_unchanged(snapshot)
        self.capture_flows(entry)
        if not self.interface_patch_rejected:
            try:
                self.write("PATCH", path, {"interface": destination})
            except APIError as exc:
                if not exc.interface_restricted():
                    raise
                entry["patch_rejection"] = str(exc)
                logging.warning("In-place PATCH unavailable: %s", exc)
                self.interface_patch_rejected = True
            else:
                expected = self.schemas.payload(TS, "POST", original)
                expected["interface"] = destination
                self.wait(path, expected, self.schemas.operation(TS, "POST"))
                after = self.snapshot(original["id"])
                expected_signature = self.signature(snapshot)
                expected_signature["output"]["interface"] = destination
                # Embedded references legitimately reflect the moved interface.
                for service in expected_signature["services"]:
                    service["output"]["outputTS"]["interface"] = destination
                    service["routing"]["outputService"]["outputTS"]["interface"] = destination
                errors = difference(expected_signature, self.signature(after))
                if errors:
                    raise MigrationError("In-place migration changed other configuration: " + "; ".join(errors))
                self.verify_flows(entry, final=True)
                entry.update(state="migrated", method="PATCH", new_id=original["id"])
                return
        entry["method"] = "create-verify-delete"
        new_id = self.clone(snapshot, destination, entry)
        self.delete_verified_source(snapshot, new_id, entry)
        self.verify_flows(entry, final=True)
        entry["state"] = "migrated"

    def capture_flows(self, entry: dict[str, Any]) -> None:
        """Re-read every owning transcoding flow immediately before changing anything."""
        entry["flow_before"] = {}
        for member in entry.get("flows", []):
            flow = self.get(resource_path(member["base"], member["flow_id"]))
            config, outputs, output_ids = flow_view(flow)
            if member["service_id"] not in outputs:
                raise MigrationError(f"Output service {member['service_id']} is no longer part of "
                                     f"flow {member['flow_name']} ({member['flow_id']})")
            entry["flow_before"][member["flow_id"]] = {
                "base": member["base"], "name": member["flow_name"], "type": member["flow_type"],
                "config": config, "outputs": outputs, "output_ids": output_ids,
            }
        self.checkpoint()

    def verify_flows(self, entry: dict[str, Any], *, final: bool) -> None:
        """Flow settings and every flow output must be unchanged; replacements must join the flow.

        Before source deletion both old and new outputs are members; afterwards only the new one.
        vDCM assigns the flow-level outputId itself, so it is recorded rather than compared.
        """
        mapping = entry.get("service_id_map", {})
        for flow_id, before in entry.get("flow_before", {}).items():
            expected: dict[str, Any] = {}
            for service_id, settings in before["outputs"].items():
                if service_id in mapping:
                    expected[mapping[service_id]] = remap(settings, mapping)
                    if final and mapping[service_id] != service_id:
                        continue
                expected[service_id] = settings
            deadline = time.monotonic() + self.verify_timeout
            while True:
                config, outputs, output_ids = flow_view(self.get(resource_path(before["base"], flow_id)))
                errors = difference(before["config"], config, "/flow") + difference(
                    expected, outputs, "/outputs")
                if not errors:
                    break
                if time.monotonic() >= deadline:
                    raise MigrationError(f"Transcoding flow {before['name']} ({flow_id}) verification "
                                         "failed: " + "; ".join(errors))
                time.sleep(0.5)
            for old, new in mapping.items():
                # Linear Transcode outputs have no outputId; only ABR outputs are named.
                if before["output_ids"].get(old) is not None or output_ids.get(new) is not None:
                    entry.setdefault("flow_output_ids", {})[old] = {
                        "flow_id": flow_id, "flow_name": before["name"], "flow_type": before["type"],
                        "new_service_id": new, "old_output_id": before["output_ids"][old],
                        "new_output_id": output_ids.get(new),
                    }
                    duplicates = [sid for sid, oid in output_ids.items()
                                  if sid != new and oid is not None and oid == output_ids.get(new)]
                    if final and duplicates:
                        # vDCM chose the name; it cannot be changed through the API (flow PATCH fails).
                        warning = (f"vDCM assigned outputId {output_ids.get(new)!r} to {new} in flow "
                                   f"{before['name']} ({flow_id}), already used by {', '.join(duplicates)}")
                        entry["flow_warning"] = warning
                        logging.warning("%s", warning)
        self.checkpoint()

    def cleanup_failed_replacement(self, entry: dict[str, Any]) -> None:
        new_id = entry.get("new_id")
        if new_id and entry.get("state") not in (
            "source_delete_started", "source_deleted", "source_retained_off", "migrated",
        ):
            try:
                self.write("PATCH", resource_path(TS, new_id), {"streaming": "Off"})
                if self.get(resource_path(TS, new_id)).get("streaming") != "Off":
                    raise MigrationError("Failed replacement streaming Off was not verified")
                entry["replacement_disabled"] = True
            except MigrationError as exc:
                entry["replacement_cleanup_error"] = str(exc)
                logging.error("Replacement cleanup failed: %s", exc)
        # Keep failed replacements for inspection; never delete an unidentified resource.

    def report(self, source: dict[str, Any], destination: dict[str, Any]) -> None:
        headers = ["Original ID", "Name", "Target IP", "UDP", "Source port", "Source interface",
                   "Destination port", "Destination interface", "State", "Method", "New ID",
                   "Service count", "Error", "PATCH rejection", "Cleanup error",
                   "Duration seconds", "Worker", "Folder", "Transcoding flow", "Flow type",
                   "Flow ID", "Flow outputId (original)", "Flow outputId (new)",
                   "LSS acquisitionPointId (original -> vDCM assigned)",
                   "PMT ES order (original -> new)", "Flow note"]
        output_rows: list[list[Any]] = [headers]
        service_rows: list[list[Any]] = [["Original output ID", "Service ID", "Name", "SID", "Routing"]]
        config_rows: list[list[Any]] = [["Original output ID", "JSON chunk", "Original snapshot JSON"]]
        error_rows: list[list[Any]] = [["Original output ID", "Field", "Chunk", "Complete detail"]]
        for entry in self.entries:
            output = entry["output"]
            snapshot = entry.get("snapshot")
            flows = entry.get("flows", [])
            changes = entry.get("flow_output_ids", {})
            flow_note = entry.get("flow_note") or entry.get("flow_warning") or (
                "Transcoding-flow output: vDCM assigns the replacement's flow outputId (ABR) and, "
                "while LSS is Disabled, its lss.acquisitionPointId; merged components/track-rule ES "
                "may move to the end of the PMT ES list (same PIDs)"
                if flows and entry["state"] == "planned" else "")
            output_rows.append([
                output.get("id"), output.get("name"), output.get("targetIp"), output.get("targetUdp"),
                source.get("portName"), source["interface"], destination.get("portName"),
                destination["interface"], entry["state"], entry.get("method"), entry.get("new_id"),
                len(snapshot["services"]) if snapshot else "",
                (entry.get("error") or "")[:30000],
                (entry.get("patch_rejection") or "")[:30000],
                (entry.get("cleanup_error") or "")[:30000],
                entry.get("duration_seconds"), entry.get("worker"),
                ", ".join(output.get("tags", [])),
                "; ".join(dict.fromkeys(m["flow_name"] for m in flows)),
                "; ".join(dict.fromkeys(m["flow_type"] for m in flows)),
                "; ".join(dict.fromkeys(m["flow_id"] for m in flows)),
                "; ".join(str(m["output_id"]) for m in flows if m["output_id"] is not None),
                "; ".join(str(changes[m["service_id"]]["new_output_id"]) for m in flows
                          if m["service_id"] in changes),
                "; ".join(f"{v['old']} -> {v['new']}"
                          for v in entry.get("acquisition_point_changes", {}).values()),
                "; ".join(f"{v['old']} -> {v['new']}" for v in entry.get("es_order_changes", {}).values()),
                flow_note,
            ])
            for field in ("error", "patch_rejection", "cleanup_error", "replacement_cleanup_error",
                          "flow_note", "flow_warning"):
                detail = entry.get(field, "")
                for index in range(0, len(detail), 30000):
                    error_rows.append([output["id"], field, index // 30000 + 1,
                                       detail[index:index + 30000]])
            if snapshot:
                for service in snapshot["services"]:
                    detail = service["output"]
                    service_rows.append([
                        output["id"], detail["id"], detail.get("name"), detail.get("sid"),
                        json.dumps({k: v for k, v in service["routing"].items()
                                    if k != "outputService"}, ensure_ascii=True),
                    ])
                serialized = json.dumps(snapshot, ensure_ascii=True)
                for index in range(0, len(serialized), 30000):
                    config_rows.append([output["id"], index // 30000 + 1, serialized[index:index + 30000]])
        write_excel(self.run_dir / "migration.xlsx", {
            "Outputs": output_rows, "Services": service_rows,
            "Original configuration": config_rows, "Errors": error_rows,
        })
        self.checkpoint()


SOURCES = "/api/v2/Sources"
INPUT_SERVICES = "/api/v2/InputServices"
IGMP = "/api/v2/AppSettings/Xgress/igmp"
LTS = "/api/v2/LinearTranscodeServices"
ABR = "/api/v3/ABRServices"
OUTPUT_ALTERNATES = "/api/v2/OutputServices/BulkAlternates"
PROCESSING_ALTERNATES = "/api/v2/ProcessingServices/BulkAlternates"
LOOP_THROUGH = "/api/v2/OutputTSRoutings"
LTTS = "/api/v2/OutputLTTSs"
FLOW_BASES = {"Transcoding": LTS, "MultiresTranscoding": ABR}
FLOW_COLLECTIONS = {LTS: LTS, ABR: ABR, "/api/v2/ABRServices": ABR}
# Transcoding flows that can own output services: (collection, list key, label).
OUTPUT_FLOWS = ((ABR, "ABRServices", "ABR"), (LTS, "LinearTranscodeServices", "Linear Transcode"))


def flow_view(flow: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Split a flow into stable settings, per-output-service settings and assigned outputIds."""
    config = {k: v for k, v in flow.items() if k not in IDENTITY | {"outputs"}}
    outputs: dict[str, Any] = {}
    output_ids: dict[str, Any] = {}
    for item in flow.get("outputs") or []:
        service_id = (item.get("outputService") or {}).get("id")
        if not service_id:
            raise MigrationError(f"Flow {flow.get('id')} has an output without an output service ID")
        outputs[service_id] = {k: v for k, v in item.items() if k not in ("outputId", "outputService")}
        output_ids[service_id] = item.get("outputId")
    return config, outputs, output_ids


def flow_index(client: Client) -> dict[str, list[dict[str, Any]]]:
    """Map every output service ID to the transcoding flow(s) that own it."""
    index: dict[str, list[dict[str, Any]]] = {}
    for base, key, label in OUTPUT_FLOWS:
        try:
            flows = rows(client.request("GET", base), key)
        except APIError as exc:
            if exc.status not in (403, 404):
                raise
            logging.info("%s flows unavailable on this node (%s); none can own outputs", label, exc.status)
            continue
        for flow in flows:
            for service_id, output_id in flow_view(flow)[2].items():
                index.setdefault(service_id, []).append({
                    "flow_type": label, "base": base, "flow_id": flow["id"],
                    "flow_name": flow.get("name"), "service_id": service_id, "output_id": output_id,
                })
    return index
ROUTE_COLLECTIONS = (ROUTES, "/api/v2/OutputServiceRoutings")
# Endpoints that can embed input references and are repointed by this tool.
CONSUMER_ENDPOINTS = (SOURCES, INPUT_SERVICES, *ROUTE_COLLECTIONS, OUTPUT_ALTERNATES,
                      PROCESSING_ALTERNATES, LTS, ABR, LOOP_THROUGH)
SCAN_EXCLUDE = re.compile(
    r"(?i)status|statistic|bitrate|/log|alarm|swagger|metric|license|event|backup/|"
    r"history|reset|bulkactivate|settingsbackup")
OBSERVED_SOURCE_FIELDS = {"sourceIp", "TSID", "ONID"}
INPUT_DONE = ("migrated", "planned")


def prune_empty(value: Any) -> Any:
    """Drop empty values so {} and {"name": ""} (vDCM-materialized defaults) compare equal."""
    if isinstance(value, dict):
        pruned = {k: prune_empty(v) for k, v in value.items()}
        return {k: v for k, v in pruned.items() if v not in (None, "", {}, [])}
    if isinstance(value, list):
        return [prune_empty(v) for v in value]
    return value


def reference_hits(value: Any, targets: dict[str, str], path: str = "") -> list[tuple[str, str]]:
    """Find typed API URLs (".../Sources/<id>", ".../InputServices/<id>") pointing at targets."""
    hits: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            hits.extend(reference_hits(child, targets, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            hits.extend(reference_hits(child, targets, f"{path}[{index}]"))
    elif isinstance(value, str) and "/" in value:
        url_path = urllib.parse.urlsplit(value).path
        for suffix, target in targets.items():
            if url_path.endswith(suffix):
                hits.append((path, target))
    return hits


def collection_items(response: Any) -> list[tuple[str, Any]]:
    """Split a GET response into (path prefix, item) pairs for reference classification."""
    if isinstance(response, dict):
        lists = [k for k, v in response.items() if isinstance(v, list)]
        if len(lists) == 1 and set(response) <= {lists[0], "count"}:
            return [(f".{lists[0]}[{i}]", item) for i, item in enumerate(response[lists[0]])]
    return [("", response)]


def classify_reference(endpoint: str, item: Any, path: str, old_source: str,
                       old_services: set[str]) -> tuple[Any, ...] | None:
    """Return a consumer key, None for the input's own records, or raise if unsupported."""
    item = item if isinstance(item, dict) else {}
    item_id = item.get("id")
    if endpoint == SOURCES:
        if item_id == old_source and path == ".url":
            return None
        match = re.fullmatch(r"\.(\w+Settings)\.tsBackup\.backupTSs\[\d+\]\.url", path)
        if match and isinstance(item_id, str):
            return ("ts_backup", item_id, match.group(1))
    elif endpoint == INPUT_SERVICES:
        if item_id in old_services and path in (".url", ".source.url"):
            return None
    elif endpoint in ROUTE_COLLECTIONS:
        service_id = (item.get("outputService") or {}).get("id") or item_id
        if path in (".inputService.url", ".inputService.source.url") and isinstance(service_id, str):
            return ("output_service", service_id)
    elif endpoint == OUTPUT_ALTERNATES:
        if re.fullmatch(r"\.alternateSources\[\d+\]\..*Url", path) and item.get("outputServiceId"):
            return ("output_service", item["outputServiceId"])
    elif endpoint == PROCESSING_ALTERNATES:
        base = FLOW_BASES.get(item.get("processingType"))
        if (base and item.get("processingServiceId")
                and re.fullmatch(r"\.alternateSources\[\d+\]\..*Url", path)):
            return ("flow", base, item["processingServiceId"])
    elif endpoint in FLOW_COLLECTIONS:
        if path in (".inputService.url", ".inputService.source.url") and isinstance(item_id, str):
            return ("flow", FLOW_COLLECTIONS[endpoint], item_id)
    elif endpoint == LOOP_THROUGH:
        if path == ".source.url" and isinstance(item_id, str):
            return ("loop_through", item_id)
    raise MigrationError(f"Unsupported reference at {endpoint} {path}")


class InputMigrator(Migrator):
    """Move XGRESS inputs between interfaces and repoint every consumer.

    vDCM accepts but silently ignores PATCH/PUT of a source interface, so a new source is
    created on the destination, consumers are repointed and the original is deleted.
    """

    def __init__(self, *args: Any, input_timeout: float = 60, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.input_timeout = input_timeout
        self.unscanned: list[list[str]] = []

    # ----- planning -----------------------------------------------------------------
    def scan_endpoints(self) -> list[str]:
        paths = self.schemas.swagger["paths"]
        return sorted(p for p, ops in paths.items()
                      if "get" in ops and "{" not in p and p.startswith("/api/")
                      and not SCAN_EXCLUDE.search(p))

    def reference_scan(self, endpoints: list[str], workers: int) -> dict[str, Any]:
        """GET every collection once; endpoints that cannot be read are reported, not ignored."""
        results: dict[str, Any] = {}
        lock = threading.Lock()

        def fetch(endpoint: str) -> None:
            try:
                value = self.get(endpoint)
            except APIError as exc:
                with lock:
                    self.unscanned.append([endpoint, str(exc)[:500]])
                return
            with lock:
                results[endpoint] = value

        run_batch(list(endpoints), workers, fetch)
        return results

    def consumers_of(self, scan: dict[str, Any], source: dict[str, Any],
                     services: list[dict[str, Any]]) -> tuple[list[tuple[Any, ...]], list[str]]:
        old_services = {s["id"] for s in services}
        targets = {"/Sources/" + source["id"]: source["id"]}
        targets.update({"/InputServices/" + s: s for s in old_services})
        consumers: list[tuple[Any, ...]] = []
        unsupported: list[str] = []
        for endpoint, response in sorted(scan.items()):
            for prefix, item in collection_items(response):
                for path, _ in reference_hits(item, targets):
                    try:
                        key = classify_reference(endpoint, item, path, source["id"], old_services)
                    except MigrationError as exc:
                        unsupported.append(f"{exc} (resource {prefix})")
                        continue
                    if key is not None and key not in consumers:
                        consumers.append(key)
        return consumers, unsupported

    def flow_outputs(self, flow: dict[str, Any]) -> list[str]:
        return [o["outputService"]["outputTS"]["id"] for o in flow.get("outputs", [])
                if isinstance(o.get("outputService", {}).get("outputTS", {}).get("id"), str)]

    def snapshot_consumer(self, key: tuple[Any, ...]) -> dict[str, Any]:
        kind = key[0]
        if kind == "output_service":
            service_path = resource_path(SERVICES, key[1])
            service = self.get(service_path)
            return {"key": list(key), "routing": self.get(resource_path(ROUTES, key[1])),
                    "alternates": self.get(service_path + "/Alternates"),
                    "outputs": [service["outputTS"]["id"]]}
        if kind == "flow":
            flow_path = resource_path(key[1], key[2])
            flow = self.get(flow_path)
            return {"key": list(key), "flow": flow, "alternates": self.get(flow_path + "/Alternates"),
                    "outputs": self.flow_outputs(flow)}
        if kind == "ts_backup":
            return {"key": list(key), "source": self.get(resource_path(SOURCES, key[1])), "outputs": []}
        if kind == "loop_through":
            routing = self.get(resource_path(LOOP_THROUGH, key[1]))
            return {"key": list(key), "routing": routing,
                    "ltts": self.get(resource_path(LTTS, routing["outputLTTS"]["id"])), "outputs": []}
        raise MigrationError(f"Unsupported consumer {key}")

    def plan_input(self, entry: dict[str, Any], scan: dict[str, Any], joins: list[dict[str, Any]],
                   sources: list[dict[str, Any]], destination: str) -> None:
        # Planning runs in parallel; results are merged under the journal lock so a
        # concurrent checkpoint never serialises an entry while it is being changed.
        result: dict[str, Any] = {}
        try:
            source = self.get(resource_path(SOURCES, entry["input"]["id"]))
            settings = source.get("xgressSettings") or {}
            if source.get("inputType") != "XGRESS":
                raise MigrationError("Not an XGRESS input")
            self.ensure_no_hidden_secrets(source)
            listed = rows(self.get(resource_path(SOURCES, source["id"]) + "/InputServices"), "InputServices")
            # The per-source listing can be abbreviated; the item endpoint is authoritative.
            services = [self.get(resource_path(INPUT_SERVICES, s["id"])) for s in listed]
            consumers, unsupported = self.consumers_of(scan, source, services)
            blocking = list(unsupported)
            group = (settings.get("targetIp"), settings.get("vlanId"))
            old_joins = [j for j in joins if j.get("interface") == settings.get("interface")
                         and (j.get("targetIp"), j.get("vlanId")) == group]
            reused = []
            for join in [j for j in joins if j.get("interface") == destination
                         and (j.get("targetIp"), j.get("vlanId")) == group]:
                wanted = [{k: j.get(k) for k in ("filter", "sourceIps", "enable")} for j in old_joins]
                if {k: join.get(k) for k in ("filter", "sourceIps", "enable")} not in wanted:
                    blocking.append(f"Destination IGMP join {join['id']} exists with different settings")
                reused.append(join["id"])
            duplicates = [s["id"] for s in sources if s.get("inputType") == "XGRESS"
                          and (s.get("xgressSettings") or {}).get("interface") == destination
                          and all((s["xgressSettings"].get(k)) == settings.get(k)
                                  for k in ("targetIp", "targetUdp", "vlanId"))]
            if duplicates:
                blocking.append(f"Destination already receives this multicast/UDP/VLAN: {duplicates}")
            snapshots = [self.snapshot_consumer(key) for key in consumers]
            outputs = sorted({o for c in snapshots for o in c["outputs"]})
            output_snapshots = {o: self.snapshot(o) for o in outputs}
            try:
                status = self.get(resource_path(SOURCES, source["id"]) + "/Status")
            except APIError:
                status = None
            in_backup = isinstance(status, dict) and (status.get("xgressStatus") or {}).get("tsInBackup") is True
            for value in output_snapshots.values():
                self.signature(value)
            result["snapshot"] = {"source": source, "services": services, "joins": old_joins,
                                 "reused_joins": reused, "consumers": snapshots,
                                 "outputs": output_snapshots, "source_in_backup": in_backup}
            atomic_json(self.run_dir / (urllib.parse.quote(source["id"], safe="") + ".json"),
                        result["snapshot"])
            if not old_joins:
                result["warning"] = "No IGMP join found on the source interface; none will be copied"
            if blocking:
                result.update(state="blocked", error="; ".join(blocking))
                logging.error("BLOCKED %s: %s", source["id"], result["error"])
        except Exception as exc:  # journal every failure; never abort the batch
            exc = failure(exc)
            result = {"state": "failed", "error": str(exc)}
            logging.error("Planning failed for %s: %s", entry["input"]["id"], exc)
        with self._journal_lock:
            entry.update(result)
            self.checkpoint()

    def replan_input(self, entry: dict[str, Any], endpoints: list[str], workers: int,
                     destination: str) -> None:
        """Refresh the snapshot of an input that waits behind earlier migrations in this run.

        Earlier inputs legitimately rewrite shared consumers (merged alternates, TS backups), so a
        snapshot taken during planning would restore or apply stale references.
        """
        logging.info("Re-planning %s against the current configuration", entry["input"]["id"])
        unread = len(self.unscanned)
        scan = self.reference_scan(endpoints, workers)
        known = {row[0] for row in self.unscanned[:unread]}
        self.unscanned[unread:] = [row for row in self.unscanned[unread:] if row[0] not in known]
        joins = rows(self.get(IGMP), "igmp")
        sources = rows(self.get(SOURCES), "Sources")
        for key in ("snapshot", "warning", "error"):
            entry.pop(key, None)
        entry["replanned"] = True
        self.plan_input(entry, scan, joins, sources, destination)

    # ----- execution ----------------------------------------------------------------
    def execute_input(self, entry: dict[str, Any], destination: str) -> None:
        if entry["state"] != "planned":
            return
        start = time.monotonic()
        logging.info("START input %s", entry["input"]["id"])
        entry["applied"], entry["created_joins"], entry["service_id_map"] = [], [], {}
        try:
            self.migrate_input(entry, destination)
            entry["state"] = "migrated"
            logging.info("SUCCESS input %s -> %s", entry["input"]["id"], entry["new_source_id"])
        except Exception as exc:  # journal every failure; never abort the batch
            exc = failure(exc)
            entry["state_before_failure"] = entry.get("state")
            entry["error"] = str(exc)
            logging.error("FAILED input %s: %s", entry["input"]["id"], exc)
            self.rollback_input(entry)
        finally:
            entry["duration_seconds"] = time.monotonic() - start
            self.checkpoint()

    def source_config(self, source: dict[str, Any]) -> dict[str, Any]:
        return self.schemas.payload(SOURCES, "POST", source)

    def service_settings(self, service: dict[str, Any]) -> dict[str, Any]:
        return self.schemas.payload(INPUT_SERVICES + "/x", "PUT", service)["xgressSettings"]

    def migrate_input(self, entry: dict[str, Any], destination: str) -> None:
        snap = entry["snapshot"]
        source = snap["source"]
        entry["state"] = "started"
        current = self.get(resource_path(SOURCES, source["id"]))
        if difference(self.source_config(source), self.source_config(current)):
            raise MigrationError("Input configuration changed since planning; re-run the dry-run")
        payload = self.source_config(source)
        payload["xgressSettings"]["interface"] = destination
        backup = payload["xgressSettings"].get("tsBackup")
        if isinstance(backup, dict) and "backupTSs" in backup:
            backup["backupTSs"] = [{"id": b["id"]} for b in source["xgressSettings"]["tsBackup"]["backupTSs"]]
        new_source = self.create(SOURCES, "Sources", payload, entry, "new_source_id")["id"]
        entry["state"] = "source_created"
        mapping = {source["id"]: new_source}
        for service in snap["services"]:
            service_payload = self.schemas.payload(INPUT_SERVICES, "POST", service)
            service_payload["source"] = {"id": new_source}
            existing = self.service_by_sid(new_source, service["xgressSettings"]["serviceId"])
            new_id = existing or self.create(INPUT_SERVICES, "InputServices", service_payload,
                                              entry, "last_new_service_id")["id"]
            mapping[service["id"]] = new_id
            entry["service_id_map"][service["id"]] = new_id
            self.checkpoint()
        entry["mapping"] = mapping
        # An identical join already present on the destination is reused, never duplicated.
        for join in [] if snap["reused_joins"] else snap["joins"]:
            join_payload = self.schemas.payload(IGMP, "POST", join)
            join_payload["interface"] = destination
            entry["created_joins"].append(self.create(IGMP, "igmp", join_payload, entry, "last_join_id")["id"])
            self.checkpoint()
        original_ip = (snap["source"].get("xgressSettings") or {}).get("sourceIp")
        self.wait_input_present(new_source, snap.get("source_in_backup") is True,
                                original_ip not in (None, "", "0.0.0.0"))
        entry["state"] = "input_present"
        self.verify_new_input(snap, new_source, mapping, entry)
        consumers = sorted(snap["consumers"], key=lambda c: c["key"][0] == "loop_through")
        for consumer in consumers:
            entry["applied"].append(consumer["key"])
            self.checkpoint()
            self.apply_consumer(consumer, mapping, entry)
        entry["state"] = "consumers_repointed"
        self.restore_outputs(snap["outputs"], mapping)
        remaining = self.remaining_references(source, snap["services"])
        if remaining:
            raise MigrationError("Original input is still referenced: " + "; ".join(remaining))
        self.delete_original_input(entry, destination)

    def service_by_sid(self, source_id: str, sid: Any) -> str | None:
        services = rows(self.get(resource_path(SOURCES, source_id) + "/InputServices"), "InputServices")
        matches = [s["id"] for s in services if (s.get("xgressSettings") or {}).get("serviceId") == sid]
        return matches[0] if len(matches) == 1 else None

    def wait_input_present(self, source_id: str, original_in_backup: bool = False,
                           require_main_stream: bool = False) -> None:
        deadline = time.monotonic() + self.input_timeout
        while True:
            status = self.get(resource_path(SOURCES, source_id) + "/Status")
            # With a TS backup, inputPresent is already true while the backup stream is used, and
            # in Floating mode tsInBackup stays false. Only the observed sender address proves
            # that the input's own multicast arrives.
            in_backup = isinstance(status, dict) and (status.get("xgressStatus") or {}).get("tsInBackup") is True
            main_ip = ((self.get(resource_path(SOURCES, source_id)).get("xgressSettings") or {}).get("sourceIp")
                       if require_main_stream else None)
            if isinstance(status, dict) and status.get("inputPresent") is True and (
                    original_in_backup or not in_backup) and (
                    not require_main_stream or main_ip not in (None, "", "0.0.0.0")):
                return
            if time.monotonic() >= deadline:
                raise MigrationError(f"New input {source_id} received no stream within {self.input_timeout:g}s "
                                     f"(Status: {json.dumps(status)[:500]}). Check multicast/IGMP on the destination port")
            time.sleep(1)

    def verify_new_input(self, snap: dict[str, Any], new_source: str, mapping: dict[str, str],
                         entry: dict[str, Any]) -> None:
        for service in snap["services"]:
            path = resource_path(INPUT_SERVICES, mapping[service["id"]])
            expected = self.service_settings(service)
            if difference(expected, self.service_settings(self.get(path))):
                # Stream discovery may rename the service (e.g. "Service 1"); restore it.
                self.write("PATCH", path, {"inputType": "XGRESS", "xgressSettings": expected})
            deadline = time.monotonic() + self.verify_timeout
            while errors := difference(expected, self.service_settings(self.get(path))):
                if time.monotonic() >= deadline:
                    raise MigrationError(f"Input service verification failed for {path}: " + "; ".join(errors))
                time.sleep(0.5)
        services = rows(self.get(resource_path(SOURCES, new_source) + "/InputServices"), "InputServices")
        extra = sorted({s["id"] for s in services} - set(mapping.values()))
        if extra:
            entry["warning"] = (entry.get("warning", "") + f" Extra services discovered on new input: {extra}").strip()
        expected = self.source_config(snap["source"])
        expected["xgressSettings"]["interface"] = entry["destination"]
        source_path = resource_path(SOURCES, new_source)
        want_active = (expected["xgressSettings"].get("tsBackup") or {}).get("activeBackupTS")
        active = lambda v: (v["xgressSettings"].get("tsBackup") or {}).get("activeBackupTS")
        if want_active is not None and active(self.get(source_path)) != want_active:
            # A new input switches to its backup TS until its own stream arrives; restore the selection.
            self.write("PATCH", source_path, {"inputType": "XGRESS",
                                              "xgressSettings": {"tsBackup": {"activeBackupTS": want_active}}})
            self.wait_value(source_path, lambda v: active(v) == want_active, f"activeBackupTS {want_active}")
        actual = self.source_config(self.get(source_path))
        changes = difference(remap(expected, mapping), actual)
        volatile = [c for c in changes if c.split(":")[0].split("/")[-1] in OBSERVED_SOURCE_FIELDS]
        if volatile:
            entry["observed_differences"] = volatile
        hard = [c for c in changes if c not in volatile]
        if hard:
            raise MigrationError("New input configuration differs from original: " + "; ".join(hard))

    def alternates_target(self, path: str, original: Any, mapping: dict[str, str]) -> Any:
        return remap(self.schemas.payload(path, "PUT", original), mapping)

    def apply_alternates(self, path: str, original: Any, mapping: dict[str, str]) -> None:
        expected = self.alternates_target(path, original, mapping)
        schema = self.schemas.operation(path, "PUT")
        if difference(expected, self.schemas.project(self.get(path), schema, location=path)):
            self.write("PUT", path + "?results=true", expected)
        self.wait(path, expected, schema)
        if difference(alternate_activation(remap(original, mapping)), alternate_activation(self.get(path))):
            raise MigrationError(f"Active alternate/backup selection changed for {path}")

    def apply_consumer(self, consumer: dict[str, Any], mapping: dict[str, str],
                       entry: dict[str, Any]) -> None:
        """Bring a consumer to remap(original, mapping); mapping {} restores the original."""
        kind = consumer["key"][0]
        if kind == "output_service":
            service_id = consumer["key"][1]
            route_path = resource_path(ROUTES, service_id)
            original = consumer["routing"]
            old_input = (original.get("inputService") or {}).get("id")
            target = mapping.get(old_input, old_input)
            if old_input and (self.get(route_path).get("inputService") or {}).get("id") != target:
                projected = self.schemas.payload(route_path, "PUT", original)
                output_service = {k: v for k, v in projected["outputService"].items() if k != "outputTS"}
                output_service["outputTS"] = {"id": original["outputService"]["outputTS"]["id"]}
                self.write("PUT", route_path + "?results=true",
                           {"inputService": {"id": target}, "outputService": output_service})
                self.wait_value(route_path, lambda v: (v.get("inputService") or {}).get("id") == target,
                                f"routing input {target}")
            self.apply_alternates(resource_path(SERVICES, service_id) + "/Alternates",
                                  consumer["alternates"], mapping)
        elif kind == "flow":
            flow_path = resource_path(consumer["key"][1], consumer["key"][2])
            original = consumer["flow"]
            old_input = (original.get("inputService") or {}).get("id")
            target = mapping.get(old_input, old_input)
            if old_input and (self.get(flow_path).get("inputService") or {}).get("id") != target:
                if consumer["key"][1] == ABR:
                    # vDCM V26 answers any v3 ABR PUT with HTTP 500 and deletes the flow (and its
                    # output TSs without keepOutputTSs); PATCH of inputService swaps only the input.
                    self.write("PATCH", flow_path + "?keepInputs=true&results=true",
                               {"inputService": {"id": target}})
                else:
                    body = self.schemas.payload(flow_path, "PUT", original)
                    body["inputService"] = {"id": target}
                    for projected, raw in zip(body.get("outputs", []), original.get("outputs", [])):
                        ts_id = raw.get("outputService", {}).get("outputTS", {}).get("id")
                        if ts_id:
                            projected["outputService"]["outputTS"] = {"id": ts_id}
                    self.write("PUT", flow_path + "?keepInputs=true&keepOutputTSs=true&results=true", body)
                self.wait_value(flow_path, lambda v: (v.get("inputService") or {}).get("id") == target,
                                f"flow input {target}")
                expected = self.flow_config(flow_path, original)
                changes = difference(expected, self.flow_config(flow_path, self.get(flow_path)))
                if changes:
                    raise MigrationError(f"Flow {flow_path} changed beyond its input: " + "; ".join(changes))
            self.apply_alternates(flow_path + "/Alternates", consumer["alternates"], mapping)
        elif kind == "ts_backup":
            source_path = resource_path(SOURCES, consumer["key"][1])
            settings_key = consumer["key"][2]
            backups = consumer["source"][settings_key]["tsBackup"]["backupTSs"]
            target = [mapping.get(b["id"], b["id"]) for b in backups]
            current = lambda v: [b["id"] for b in v[settings_key]["tsBackup"]["backupTSs"]]
            if current(self.get(source_path)) != target:
                self.write("PATCH", source_path, {
                    "inputType": consumer["source"]["inputType"],
                    settings_key: {"tsBackup": {"backupTSs": [{"id": t} for t in target]}},
                })
            self.wait_value(source_path, lambda v: current(v) == target, f"TS backup list {target}")
            expected = self.source_config(consumer["source"])
            changes = difference(self.strip_backup_refs(expected, settings_key),
                                 self.strip_backup_refs(self.source_config(self.get(source_path)), settings_key))
            if changes:
                raise MigrationError(f"TS backup update changed {source_path}: " + "; ".join(changes))
        elif kind == "loop_through":
            if not mapping:
                raise MigrationError("Loop-through routing was recreated by vDCM and cannot be rolled back")
            self.apply_loop_through(consumer, mapping, entry)
        else:
            raise MigrationError(f"Unsupported consumer {consumer['key']}")

    @staticmethod
    def strip_backup_refs(config: dict[str, Any], settings_key: str) -> dict[str, Any]:
        config = copy.deepcopy(config)
        config.get(settings_key, {}).get("tsBackup", {}).pop("backupTSs", None)
        return config

    def flow_config(self, path: str, flow: dict[str, Any]) -> Any:
        """Projected flow without input identity, which legitimately changes."""
        projected = self.schemas.payload(path, "PUT", flow)
        projected.pop("inputService", None)
        return prune_empty(projected)

    def wait_value(self, path: str, check: Callable[[Any], bool], label: str) -> Any:
        deadline = time.monotonic() + self.verify_timeout
        while True:
            value = self.get(path)
            if check(value):
                return value
            if time.monotonic() >= deadline:
                raise MigrationError(f"Verification failed for {path}: expected {label}")
            time.sleep(0.5)

    def apply_loop_through(self, consumer: dict[str, Any], mapping: dict[str, str],
                           entry: dict[str, Any]) -> None:
        original = consumer["routing"]
        target = mapping[original["source"]["id"]]
        route_path = resource_path(LOOP_THROUGH, original["id"])
        body = self.schemas.payload(route_path, "PUT", original)
        body["source"] = {"id": target}
        # vDCM recreates the routing and LTTS with new IDs when the source changes.
        response = self.write("PUT", route_path + "?results=true", body)
        entry["loop_through_response"] = response
        ltts = consumer["ltts"]
        settings = ltts.get("xgressSettings") or {}
        deadline = time.monotonic() + self.verify_timeout
        while True:
            matches = [r for r in rows(self.get(LOOP_THROUGH), "OutputTSRoutings")
                       if (r.get("source") or {}).get("id") == target
                       and all(((r.get("outputLTTS") or {}).get("xgressSettings") or {}).get(k) == settings.get(k)
                               for k in ("interface", "targetIp", "targetUdp"))]
            if len(matches) == 1:
                break
            if time.monotonic() >= deadline:
                raise MigrationError("Loop-through routing to the new input could not be identified")
            time.sleep(0.5)
        new_ltts = matches[0]["outputLTTS"]["id"]
        entry.setdefault("loop_through_map", {})[original["id"]] = matches[0]["id"]
        entry["loop_through_map"][ltts["id"]] = new_ltts
        self.checkpoint()
        ltts_path = resource_path(LTTS, new_ltts)
        expected = self.schemas.payload(ltts_path, "PUT", ltts)
        schema = self.schemas.operation(ltts_path, "PUT")
        if difference(expected, self.schemas.project(self.get(ltts_path), schema, location=ltts_path)):
            self.write("PUT", ltts_path, expected)
        self.wait(ltts_path, expected, schema)

    def normalized_signature(self, snapshot: dict[str, Any]) -> Any:
        signature = self.signature(snapshot)
        for service in signature["services"]:
            routing = service["routing"]
            service["routing"] = {
                "inputService": (routing.get("inputService") or {}).get("id"),
                "descrambledService": routing.get("descrambledService"),
                "processedService": routing.get("processedService"),
            }
        return signature

    def restore_outputs(self, outputs: dict[str, Any], mapping: dict[str, str]) -> None:
        """Undo vDCM side effects of re-routing (forced names, VCT short names) and verify."""
        for ts_id, original in outputs.items():
            expected = remap(self.normalized_signature(original), mapping)
            current_snapshot = self.snapshot(ts_id)
            ids = [s["output"]["id"] for s in original["services"]]
            if ids != [s["output"]["id"] for s in current_snapshot["services"]]:
                raise MigrationError(f"Output services of {ts_id} changed during re-routing")
            current = self.normalized_signature(current_snapshot)
            ts_path = resource_path(TS, ts_id)
            delta = {k: v for k, v in expected["output"].items() if current["output"].get(k) != v}
            if delta:
                self.write("PATCH", ts_path, delta)
            for suffix, part in original["parts"].items():
                if difference(expected["parts"][suffix], current["parts"][suffix]):
                    self.write(part["method"], ts_path + "/" + suffix, expected["parts"][suffix])
            for service_id, want, have, raw in zip(ids, expected["services"], current["services"],
                                                   original["services"]):
                service_path = resource_path(SERVICES, service_id)
                delta = {k: v for k, v in want["output"].items()
                         if k != "outputTS" and have["output"].get(k) != v}
                if delta:
                    self.write("PATCH", service_path, delta)
                for suffix, config in want["parts"].items():
                    if suffix != "Alternates" and difference(config, have["parts"][suffix]):
                        self.write(raw["parts"][suffix]["method"], service_path + "/" + suffix, config)
            deadline = time.monotonic() + self.verify_timeout
            while changes := difference(expected, self.normalized_signature(self.snapshot(ts_id))):
                if time.monotonic() >= deadline:
                    raise MigrationError(f"Output {ts_id} configuration not restored: " + "; ".join(changes))
                time.sleep(1)

    def remaining_references(self, source: dict[str, Any], services: list[dict[str, Any]]) -> list[str]:
        unread = len(self.unscanned)
        scan = self.reference_scan(list(CONSUMER_ENDPOINTS), 1)
        if len(self.unscanned) != unread:
            failures = self.unscanned[unread:]
            del self.unscanned[unread:]
            raise MigrationError(f"Cannot verify remaining references; unreadable: {failures}")
        consumers, unsupported = self.consumers_of(scan, source, services)
        return [str(c) for c in consumers] + unsupported

    def delete_resource(self, path: str) -> None:
        try:
            self.write("DELETE", path)
        except APIError as exc:
            if exc.status != 404:
                raise
        deadline = time.monotonic() + self.verify_timeout
        while True:
            try:
                self.get(path)
            except APIError as exc:
                if exc.status == 404:
                    return
                raise
            if time.monotonic() >= deadline:
                raise MigrationError(f"DELETE accepted but {path} still exists")
            time.sleep(0.5)

    def delete_source(self, source_id: str) -> None:
        """DELETE of a source also tries to delete its backup sources, so detach them first."""
        path = resource_path(SOURCES, source_id)
        try:
            source = self.get(path)
        except APIError as exc:
            if exc.status == 404:
                return
            raise
        for key, settings in source.items():
            backups = ((settings or {}).get("tsBackup") or {}).get("backupTSs") if isinstance(settings, dict) else None
            if backups:
                self.write("PATCH", path, {"inputType": source["inputType"],
                                           key: {"tsBackup": {"activeBackupTS": 0, "backupTSs": []}}})
                self.wait_value(path, lambda v, k=key: not (v[k].get("tsBackup") or {}).get("backupTSs"),
                                "backup TSs detached")
        self.delete_resource(path)

    def delete_original_input(self, entry: dict[str, Any], destination: str) -> None:
        snap = entry["snapshot"]
        source = snap["source"]
        settings = source["xgressSettings"]
        entry["state"] = "original_delete_started"
        self.checkpoint()
        # vDCM keeps a source alive while its IGMP join exists, so remove the join first.
        others = [s for s in rows(self.get(SOURCES), "Sources") if s["id"] != source["id"]
                  and (s.get("xgressSettings") or {}).get("interface") == settings["interface"]
                  and (s["xgressSettings"].get("targetIp"), s["xgressSettings"].get("vlanId"))
                  == (settings.get("targetIp"), settings.get("vlanId"))]
        if others:
            entry["warning"] = (entry.get("warning", "") + " Original IGMP join kept; still used by "
                                + ", ".join(s["id"] for s in others)).strip()
        else:
            for join in snap["joins"]:
                self.delete_resource(resource_path(IGMP, join["id"]))
                entry.setdefault("deleted_joins", []).append(join["id"])
        # vDCM answers "Delete not possible" for a source that still owns input services.
        for service in snap["services"]:
            self.delete_resource(resource_path(INPUT_SERVICES, service["id"]))
        self.delete_source(source["id"])
        entry["state"] = "original_deleted"
        self.checkpoint()

    def rollback_input(self, entry: dict[str, Any]) -> None:
        if entry.get("state") in ("original_delete_started", "original_deleted") or any(
                key[0] == "loop_through" for key in entry.get("applied", [])):
            entry["state"] = "failed_manual_action"
            entry["rollback"] = "Not attempted: original input was already deleted or recreated by vDCM"
            return
        if entry.get("state") == "planned" or not entry.get("new_source_id"):
            entry["state"] = "failed"
            entry["rollback"] = "Nothing was created"
            return
        snap = entry["snapshot"]
        steps: list[str] = []
        errors: list[str] = []
        entry.pop("rollback_error", None)
        # Restore every consumer even if one fails, so a single rejection does not leave the
        # remaining consumers on the new input.
        for key in reversed(entry.get("applied", [])):
            try:
                consumer = next(c for c in snap["consumers"] if c["key"] == key)
                self.apply_consumer(consumer, {}, entry)
                steps.append(f"restored {key}")
            except (MigrationError, KeyError, TypeError, ValueError, StopIteration) as exc:
                errors.append(f"restore {key}: {exc}")
        try:
            self.restore_outputs(snap["outputs"], {})
            steps.append("restored impacted outputs")
        except (MigrationError, KeyError, TypeError, ValueError) as exc:
            errors.append(f"restore outputs: {exc}")
        try:
            new_services = rows(self.get(resource_path(SOURCES, entry["new_source_id"]) + "/InputServices"),
                                "InputServices")
            if errors:
                # The new input may only be removed when nothing uses it any more.
                still_used = self.remaining_references({"id": entry["new_source_id"]}, new_services)
                if still_used:
                    raise MigrationError(f"New input {entry['new_source_id']} kept; still referenced by "
                                         + "; ".join(still_used))
                steps.append("verified nothing references the new input")
            for join_id in reversed(entry.get("created_joins", [])):
                self.delete_resource(resource_path(IGMP, join_id))
                steps.append(f"deleted join {join_id}")
            for service in new_services:
                self.delete_resource(resource_path(INPUT_SERVICES, service["id"]))
            self.delete_source(entry["new_source_id"])
            steps.append(f"deleted new input {entry['new_source_id']}")
        except (MigrationError, KeyError, TypeError, ValueError) as exc:
            errors.append(str(exc))
        if errors:
            entry["state"] = "rollback_failed"
            entry["rollback_error"] = " | ".join(errors)
            if "HTTP 404" in entry["rollback_error"]:
                entry["rollback_error"] += (" | A resource no longer exists; recreate it manually from "
                                            "the original snapshot saved in this run directory")
            logging.error("ROLLBACK FAILED for %s: %s", entry["input"]["id"], entry["rollback_error"])
        else:
            entry["state"] = "rolled_back"
        entry["rollback"] = "; ".join(steps)
        self.checkpoint()

    # ----- reporting ----------------------------------------------------------------
    def input_report(self, source: dict[str, Any], destination: dict[str, Any]) -> None:
        input_rows: list[list[Any]] = [[
            "Original input ID", "Service names", "Target IP", "UDP", "VLAN", "Source port",
            "Source interface", "Destination port", "Destination interface", "State",
            "New input ID", "Service ID map", "Consumers", "Impacted outputs", "IGMP joins",
            "Error", "Rollback", "Warning", "Duration seconds", "Folder"]]
        consumer_rows: list[list[Any]] = [["Original input ID", "Consumer type", "Resource", "Detail",
                                           "Impacted outputs"]]
        config_rows: list[list[Any]] = [["Original input ID", "JSON chunk", "Original snapshot JSON"]]
        error_rows: list[list[Any]] = [["Original input ID", "Field", "Chunk", "Complete detail"]]
        for entry in self.entries:
            item = entry["input"]
            settings = item.get("xgressSettings") or {}
            snap = entry.get("snapshot") or {}
            consumers = snap.get("consumers", [])
            input_rows.append([
                item["id"], ", ".join(str((s.get("xgressSettings") or {}).get("serviceUserName"))
                                      for s in snap.get("services", [])),
                settings.get("targetIp"), settings.get("targetUdp"), settings.get("vlanId"),
                source.get("portName"), source["interface"], destination.get("portName"),
                destination["interface"], entry["state"], entry.get("new_source_id"),
                json.dumps(entry.get("service_id_map", {})), len(consumers), len(snap.get("outputs", {})),
                ", ".join(j["id"] for j in snap.get("joins", [])),
                (entry.get("error") or "")[:30000], (entry.get("rollback") or "")[:30000],
                (entry.get("warning") or "")[:30000], entry.get("duration_seconds"),
                ", ".join(settings.get("tags", [])),
            ])
            for consumer in consumers:
                kind, resource = consumer["key"][0], consumer["key"][-1]
                detail = {"output_service": lambda c: (c["routing"].get("outputService") or {}).get("name"),
                          "flow": lambda c: c["flow"].get("name"),
                          "ts_backup": lambda c: "TS backup of input " + c["source"]["id"],
                          "loop_through": lambda c: "Loop-through to " + json.dumps(
                              (c["ltts"].get("xgressSettings") or {}))}[kind](consumer)
                consumer_rows.append([item["id"], kind, resource, detail, ", ".join(consumer["outputs"])])
            for field in ("error", "rollback_error", "warning"):
                detail = entry.get(field, "")
                for index in range(0, len(detail), 30000):
                    error_rows.append([item["id"], field, index // 30000 + 1, detail[index:index + 30000]])
            if snap:
                serialized = json.dumps(snap, ensure_ascii=True)
                for index in range(0, len(serialized), 30000):
                    config_rows.append([item["id"], index // 30000 + 1, serialized[index:index + 30000]])
        write_excel(self.run_dir / "migration.xlsx", {
            "Inputs": input_rows, "Consumers": consumer_rows,
            "Original configuration": config_rows, "Errors": error_rows,
            "Unscanned endpoints": [["Endpoint", "Reason"], *self.unscanned],
        })
        self.checkpoint()


def run_inputs(args: argparse.Namespace, client: Client, schemas: Schemas, config: Any,
               source: dict[str, Any], destination: dict[str, Any], run_dir: Path) -> int:
    if destination.get("portType") not in ("Input", "InputOutput") or destination.get("active") is not True:
        raise MigrationError("Destination is not an active input-capable video port")
    migrator = InputMigrator(client, schemas, run_dir, verify_timeout=args.verify_timeout,
                             input_timeout=args.input_timeout)
    sources = rows(client.request("GET", SOURCES), "Sources")
    selected = [s for s in sources if s.get("inputType") == "XGRESS"
                and (s.get("xgressSettings") or {}).get("interface") == source["interface"]]
    if args.input_id:
        missing = set(args.input_id) - {s["id"] for s in selected}
        if missing:
            raise MigrationError(f"Requested IDs are not XGRESS inputs on the source: {sorted(missing)}")
        selected = [s for s in selected if s["id"] in args.input_id]
    joins = rows(client.request("GET", IGMP), "igmp")
    atomic_json(run_dir / "backup.json", {"host": args.host, "source": source, "destination": destination,
                "sources": sources, "igmp": joins, "configuration": config})
    logging.info("%d XGRESS inputs selected: %s (%s) -> %s (%s)", len(selected), source.get("portName"),
                 source["interface"], destination.get("portName"), destination["interface"])
    migrator.entries = [{"input": s, "state": "planned", "destination": destination["interface"]}
                        for s in selected]
    migrator.checkpoint()
    started = time.monotonic()
    endpoints = migrator.scan_endpoints()
    logging.info("Scanning %d API collections for input references", len(endpoints))
    scan = migrator.reference_scan(endpoints, args.workers) if selected else {}
    for endpoint, reason in migrator.unscanned:
        logging.warning("Reference scan could not read %s: %s", endpoint, reason)
    atomic_json(run_dir / "reference-scan.json", scan)
    run_batch(migrator.entries, min(args.workers, max(1, len(selected))),
              lambda e: migrator.plan_input(e, scan, joins, sources, destination["interface"]))
    migrator.input_report(source, destination)
    if args.execute:
        # Inputs share consumers (e.g. one output service with main and backup inputs), so
        # executions are serialized to keep every read-modify-write consistent.
        for index, entry in enumerate(migrator.entries):
            if entry["state"] == "planned" and any(e.get("new_source_id") for e in migrator.entries[:index]):
                migrator.replan_input(entry, endpoints, args.workers, destination["interface"])
            migrator.execute_input(entry, destination["interface"])
        migrator.input_report(source, destination)
    failed = sum(e["state"] not in INPUT_DONE for e in migrator.entries)
    successful = sum(e["state"] == "migrated" for e in migrator.entries)
    logging.info("SUMMARY inputs selected=%d migrated=%d planned=%d unsuccessful=%d",
                 len(selected), successful, sum(e["state"] == "planned" for e in migrator.entries), failed)
    for entry in migrator.entries:
        if entry["state"] not in INPUT_DONE:
            logging.error("NOT MIGRATED %s (%s): %s %s", entry["input"]["id"], entry["state"],
                          entry.get("error", ""), entry.get("rollback", ""))
    atomic_json(run_dir / "summary.json", {
        "mode": "inputs", "selected": len(selected), "migrated": successful, "unsuccessful": failed,
        "unscanned_endpoints": migrator.unscanned, "total_seconds": time.monotonic() - started,
    })
    return 1 if failed else 0


def positive_number(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("Value must be finite and greater than zero")
    return number


def default_report_dir() -> Path:
    application = Path(sys.executable) if getattr(sys, "frozen", False) else Path(__file__)
    return application.resolve().parent / "runs"


def worker_count(value: str) -> int:
    count = int(value)
    if not 1 <= count <= 16:
        raise argparse.ArgumentTypeError("Workers must be between 1 and 16")
    return count


def run_batch(entries: list[dict[str, Any]], workers: int,
              operation: Callable[[dict[str, Any]], None]) -> None:
    if workers == 1:
        for entry in entries:
            operation(entry)
        return
    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="migrate")
    futures = []
    try:
        for entry in entries:
            futures.append(executor.submit(operation, entry))
        for future in as_completed(futures):
            future.result()
    except BaseException:
        for future in futures:
            future.cancel()
        executor.shutdown(wait=True, cancel_futures=True)
        raise
    else:
        executor.shutdown(wait=True)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--host", type=host_url, default=host_url("localhost"),
                        help="HTTPS URL or IP/hostname (default https://localhost:8443)")
    result.add_argument("--username", required=True)
    result.add_argument("--password", help="Prefer VDCM_PASSWORD or the interactive prompt")
    result.add_argument("--source", "--source-interface", required=True,
                        help='Exact port name or interface, e.g. "Port 3" or Data2')
    result.add_argument("--destination", "--destination-interface", required=True,
                        help='Exact port name or interface, e.g. "Port 5" or Data3')
    result.add_argument("--output-id", action="append", default=[],
                        help="Limit to this source output ID; repeat to select several")
    result.add_argument("--migrate", choices=("outputs", "inputs"), default="outputs",
                        help="What to migrate: XGRESS outputs (default) or XGRESS inputs")
    result.add_argument("--input-id", action="append", default=[],
                        help="With --migrate inputs: limit to this source input ID; repeatable")
    result.add_argument("--input-timeout", type=positive_number, default=60,
                        help="Seconds to wait for a migrated input to receive its stream (default 60)")
    mode = result.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Read-only Excel plan and JSON backup")
    mode.add_argument("--execute", action="store_true", help="Perform migration")
    tls = result.add_mutually_exclusive_group()
    tls.add_argument("--insecure", action="store_true", help="Explicitly disable TLS certificate validation")
    tls.add_argument("--ca-file", help="Trusted CA certificate PEM file")
    result.add_argument("--timeout", type=positive_number, default=20)
    result.add_argument("--verify-timeout", type=positive_number, default=30)
    result.add_argument("--workers", type=worker_count, default=4,
                        help="Maximum simultaneous output migrations/backups, 1-16 (default 4)")
    result.add_argument("--report-dir", type=Path, default=default_report_dir(),
                        help="Parent directory for unique per-run reports and backups")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    started = time.monotonic()
    password = args.password if args.password is not None else os.environ.get("VDCM_PASSWORD")
    if password is None:
        password = getpass.getpass("REST API password: ")
    run_dir = args.report_dir / (dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                                 + "-" + uuid.uuid4().hex[:8])
    run_dir.mkdir(parents=True, mode=0o700)
    log_path = run_dir / "migration.log"
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(threadName)s] %(message)s",
                        handlers=[logging.FileHandler(log_path, encoding="utf-8"),
                                  logging.StreamHandler()], force=True)
    logging.info("Run directory: %s", run_dir)
    if args.insecure:
        logging.warning("TLS certificate verification disabled by --insecure")
    lock_path = args.report_dir / (".migration-" + hashlib.sha256(args.host.encode()).hexdigest()[:16] + ".lock")
    lock_owned = False
    migrator = None
    source = destination = None
    try:
        if args.execute:
            try:
                lock_fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError as exc:
                raise MigrationError(f"Another migration or stale lock exists: {lock_path}. "
                                     "Check its PID before removing the lock.") from exc
            with os.fdopen(lock_fd, "w") as stream:
                stream.write(str(os.getpid()) + "\n")
            lock_owned = True
        client = Client(args.host, args.username, password, insecure=args.insecure,
                        ca_file=args.ca_file, timeout=args.timeout)
        config = client.request("GET", "/api/v2/AppSettings/Xgress/Configuration")
        source, destination = resolve_port(config, args.source), resolve_port(config, args.destination)
        if source["interface"] == destination["interface"]:
            raise MigrationError("Source and destination resolve to the same interface")
        if args.migrate == "inputs":
            if args.output_id:
                raise MigrationError("--output-id cannot be used with --migrate inputs")
            swagger = client.request("GET", "/api/swagger.json")
            return run_inputs(args, client, Schemas(swagger), config, source, destination, run_dir)
        if args.input_id:
            raise MigrationError("--input-id requires --migrate inputs")
        if destination.get("portType") not in ("Output", "InputOutput") or destination.get("active") is not True:
            raise MigrationError("Destination is not an active output-capable video port")
        swagger = client.request("GET", "/api/swagger.json")
        migrator = Migrator(client, Schemas(swagger), run_dir, verify_timeout=args.verify_timeout)
        migrator.flow_members = flow_index(client)
        outputs = rows(client.request("GET", TS), "OutputTSs")
        selected = [o for o in outputs if o.get("outputType") == "XGRESS"
                    and o.get("interface") == source["interface"]]
        if args.output_id:
            missing = set(args.output_id) - {o["id"] for o in selected}
            if missing:
                raise MigrationError(f"Requested IDs are not XGRESS outputs on the source: {sorted(missing)}")
            selected = [o for o in selected if o["id"] in args.output_id]
        selected_ids = [o.get("id") for o in selected]
        if (any(not isinstance(value, str) or not value for value in selected_ids)
                or len(set(selected_ids)) != len(selected_ids)):
            raise MigrationError("Selected outputs have missing or duplicate IDs; refusing concurrent migration")
        atomic_json(run_dir / "backup.json", {"host": args.host, "source": source,
                    "destination": destination, "outputs": outputs, "configuration": config,
                    "swagger": swagger})
        logging.info("%d XGRESS outputs selected: %s (%s) -> %s (%s)", len(selected),
                     source.get("portName"), source["interface"],
                     destination.get("portName"), destination["interface"])
        workers = min(args.workers, max(1, len(selected)))
        logging.info("Concurrency requested=%d effective=%d", args.workers, workers)
        migrator.entries = [{"output": output, "state": "planned"} for output in selected]
        (run_dir / "journals").mkdir(mode=0o700)
        migrator.checkpoint()
        backup_start = time.monotonic()
        # Wait for every original backup before permitting any migration.
        run_batch(migrator.entries, workers, lambda entry: migrator.worker(entry).backup_entry(entry))
        backup_seconds = time.monotonic() - backup_start
        migrator.report(source, destination)
        migration_seconds = 0.0
        if args.execute:
            migration_start = time.monotonic()
            run_batch(migrator.entries, workers, lambda entry: migrator.worker(entry).execute_entry(
                entry, destination["interface"],
            ))
            migration_seconds = time.monotonic() - migration_start
            migrator.report(source, destination)
        failed = sum(e["state"] not in ("migrated", "planned") for e in migrator.entries)
        successful = sum(e["state"] == "migrated" for e in migrator.entries)
        logging.info("SUMMARY selected=%d migrated=%d planned=%d unsuccessful=%d",
                     len(selected), successful,
                     sum(e["state"] == "planned" for e in migrator.entries), failed)
        durations = sorted(e["duration_seconds"] for e in migrator.entries if "duration_seconds" in e)
        atomic_json(run_dir / "summary.json", {
            "selected": len(selected), "migrated": successful, "unsuccessful": failed,
            "workers_requested": args.workers, "workers_effective": workers,
            "backup_seconds": backup_seconds, "migration_seconds": migration_seconds,
            "total_seconds": time.monotonic() - started,
            "outputs_per_second": successful / migration_seconds if migration_seconds else 0,
            "duration_p95_seconds": durations[math.ceil(len(durations) * 0.95) - 1] if durations else 0,
        })
        return 1 if failed else 0
    except (MigrationError, OSError, ValueError) as exc:
        logging.error("FATAL: %s", exc)
        return 2
    except KeyboardInterrupt:
        logging.error("Interrupted; inspect results.json and host before restarting")
        if migrator:
            migrator.checkpoint()
        return 130
    finally:
        if lock_owned:
            lock_path.unlink()


if __name__ == "__main__":
    sys.exit(main())
