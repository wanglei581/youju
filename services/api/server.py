#!/usr/bin/env python3
"""Local HTTP API and browser UI for the evidence-first library pilot."""

from __future__ import annotations

import argparse
import base64
import binascii
import csv
import hmac
import io
import json
import os
import re
import sys
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import formats
import store


ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
MAX_JSON = 3_000_000
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/admin": ("admin.html", "text/html; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/admin.js": ("admin.js", "application/javascript; charset=utf-8"),
}


class LibraryHandler(BaseHTTPRequestHandler):
    db_path = ROOT / "var" / "library.sqlite3"
    admin_token = ""
    server_version = "IndustryPolicyLocal/0.1"

    def log_message(self, format_string: str, *args: object) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), format_string % args))

    def _headers(self, status: int, content_type: str, length: int, filename: str = "") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()

    def send_json(self, status: int, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(body))
        self.wfile.write(body)

    def send_bytes(self, status: int, body: bytes, content_type: str, filename: str = "") -> None:
        self._headers(status, content_type, len(body), filename)
        self.wfile.write(body)

    def require_admin(self) -> None:
        expected = "Bearer " + self.admin_token
        supplied = self.headers.get("Authorization", "")
        if not self.admin_token or not hmac.compare_digest(supplied, expected):
            raise PermissionError("管理令牌无效")

    def read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise store.InputError("请求长度无效") from exc
        if length < 1 or length > MAX_JSON:
            raise store.InputError("请求大小无效")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise store.InputError("请求须为 UTF-8 JSON") from exc
        if not isinstance(payload, dict):
            raise store.InputError("请求须为 JSON 对象")
        return payload

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        try:
            self._get()
        except Exception as exc:
            self.handle_error(exc)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        try:
            self._post()
        except Exception as exc:
            self.handle_error(exc)

    def handle_error(self, exc: Exception) -> None:
        if isinstance(exc, PermissionError):
            self.send_json(HTTPStatus.UNAUTHORIZED, {"error": str(exc)})
        elif isinstance(exc, (store.InputError, ValueError)):
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        else:
            traceback.print_exc()
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "服务内部错误"})

    def _get(self) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path
        query = parse_qs(parsed.query, keep_blank_values=True)
        if path in STATIC_FILES:
            filename, content_type = STATIC_FILES[path]
            self.send_bytes(200, (STATIC / filename).read_bytes(), content_type)
            return
        if path == "/api/health":
            self.send_json(200, {"status": "ok", "product": "industry-policy-local"})
            return
        with store.connection(self.db_path) as db:
            if path == "/api/public/sources":
                sources = [source for source in store.list_sources(db) if source["status"] == "active"]
                public = [{key: source[key] for key in ("id", "name", "publisher", "homepage_url", "rights_level")} for source in sources]
                self.send_json(200, {"items": public})
                return
            if path == "/api/public/resources":
                def param(key: str) -> str:
                    value = query.get(key, [""])[0].strip()
                    if len(value) > 100:
                        raise store.InputError("筛选条件过长")
                    return value
                kind = param("kind")
                if kind and kind not in store.KINDS:
                    raise store.InputError("类型无效")
                items = store.public_search(db, param("q"), kind, param("industry"), param("region"))
                self.send_json(200, {"items": items, "count": len(items)})
                return
            match = re.fullmatch(r"/api/public/resources/(\d+)", path)
            if match:
                item = store.public_record(db, int(match.group(1)))
                if item is None:
                    self.send_json(404, {"error": "资料不存在或尚未发布"})
                else:
                    self.send_json(200, item)
                return
            if path.startswith("/api/admin/"):
                self.require_admin()
                if path == "/api/admin/sources":
                    self.send_json(200, {"items": store.list_sources(db)})
                    return
                if path == "/api/admin/records":
                    self.send_json(200, {"items": store.list_admin_records(db)})
                    return
                if path == "/api/admin/audit":
                    rows = [dict(row) for row in db.execute("SELECT * FROM audit_events ORDER BY id DESC LIMIT 100")]
                    self.send_json(200, {"items": rows})
                    return
                if path == "/api/admin/template.csv":
                    self.send_bytes(200, formats.import_template_csv(), "text/csv; charset=utf-8", "industry-import-template.csv")
                    return
                if path == "/api/admin/inventory.csv":
                    output = io.StringIO()
                    writer = csv.writer(output)
                    writer.writerow(["record_id", "source", "url", "draft_version", "draft_title", "published_version", "published_title", "rights_level"])
                    for item in store.list_admin_records(db):
                        writer.writerow([item["id"], item["source_name"], item["original_url"], item["draft_version_no"], item["draft_title"], item["published_version_no"], item["published_title"], item["rights_level"]])
                    self.send_bytes(200, ("\ufeff" + output.getvalue()).encode("utf-8"), "text/csv; charset=utf-8", "industry-inventory.csv")
                    return
        self.send_json(404, {"error": "接口不存在"})

    def _post(self) -> None:
        path = urlsplit(self.path).path
        if path == "/api/public/exports":
            payload = self.read_json()
            ids = payload.get("ids")
            if not isinstance(ids, list):
                raise store.InputError("ids 须为数组")
            format_name = payload.get("format")
            if format_name not in ("xlsx", "docx"):
                raise store.InputError("只支持 xlsx 或 docx")
            with store.connection(self.db_path) as db:
                records = store.public_selected(db, ids)
            if format_name == "xlsx":
                body = formats.export_xlsx(records)
                content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            else:
                body = formats.export_docx(records)
                content_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            self.send_bytes(200, body, content_type, f"industry-results.{format_name}")
            return
        if not path.startswith("/api/admin/"):
            self.send_json(404, {"error": "接口不存在"})
            return
        self.require_admin()
        payload = self.read_json()
        with store.connection(self.db_path) as db:
            if path == "/api/admin/sources":
                self.send_json(201, store.create_source(db, payload))
                return
            match = re.fullmatch(r"/api/admin/sources/(\d+)/restrict", path)
            if match:
                self.send_json(200, store.restrict_source(
                    db, int(match.group(1)), str(payload.get("mode") or ""), str(payload.get("reason") or "")
                ))
                return
            if path == "/api/admin/imports/preview":
                source_id = int(payload.get("source_id") or 0)
                filename = str(payload.get("filename") or "")
                try:
                    content = base64.b64decode(str(payload.get("content_base64") or ""), validate=True)
                except (ValueError, binascii.Error) as exc:
                    raise store.InputError("文件内容不是有效 Base64") from exc
                rows = formats.parse_import_file(filename, content)
                self.send_json(200, store.stage_import(db, source_id, filename, rows))
                return
            if path == "/api/admin/imports/commit":
                self.send_json(200, store.commit_import(db, str(payload.get("batch_id") or "")))
                return
            match = re.fullmatch(r"/api/admin/records/(\d+)/(publish|withdraw)", path)
            if match:
                record_id = int(match.group(1))
                if match.group(2) == "publish":
                    self.send_json(200, store.publish_record(db, record_id, str(payload.get("review_note") or "")))
                else:
                    self.send_json(200, store.withdraw_record(db, record_id, str(payload.get("reason") or "")))
                return
        self.send_json(404, {"error": "接口不存在"})


def make_server(host: str, port: int, db_path: Path, admin_token: str) -> ThreadingHTTPServer:
    if len(admin_token) < 16:
        raise ValueError("LIBRARY_ADMIN_TOKEN 须至少 16 个字符")
    with store.connection(db_path) as db:
        store.init_db(db)
    handler = type("ConfiguredLibraryHandler", (LibraryHandler,), {
        "db_path": db_path,
        "admin_token": admin_token,
    })
    return ThreadingHTTPServer((host, port), handler)


def main() -> None:
    parser = argparse.ArgumentParser(description="本地资料库试用服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--db", type=Path, default=ROOT / "var" / "library.sqlite3")
    args = parser.parse_args()
    token = os.environ.get("LIBRARY_ADMIN_TOKEN", "")
    server = make_server(args.host, args.port, args.db, token)
    print(f"用户端 http://{args.host}:{server.server_port}/")
    print(f"管理端 http://{args.host}:{server.server_port}/admin")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
