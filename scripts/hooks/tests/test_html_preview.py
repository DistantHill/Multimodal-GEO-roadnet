import importlib.util
from pathlib import Path
import tempfile
import errno
import os
import subprocess
import sys
import threading
import json
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import urlopen
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "html_preview", Path(__file__).resolve().parents[1] / "html_preview.py")
preview = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preview)


class HtmlPreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()
        self.state = Path(self.temp.name) / "runtime"
        self.state.mkdir()
        self.patch = patch.object(preview, "shared_runtime", return_value=self.state)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        registry_patch = patch.object(preview, "registry_root", return_value=self.state)
        registry_patch.start()
        self.addCleanup(registry_patch.stop)
        healthy_patch = patch.object(preview, "healthy", return_value=True)
        healthy_patch.start()
        self.addCleanup(healthy_patch.stop)

    def event(self, kind):
        return {"hook_event_name": kind, "cwd": str(self.root)}

    def test_initial_baseline_then_create_and_update(self):
        page = self.root / "report space.html"
        page.write_text("old")
        preview.handle(self.event("SessionStart"), self.root)
        with patch.object(preview, "ensure_preview") as ensure:
            self.assertIsNone(preview.handle(self.event("PostToolUse"), self.root))
            ensure.reset_mock()
            page.write_text("new")
            result = preview.handle(self.event("PostToolUse"), self.root)
            self.assertIn("http://localhost:18765" + preview.project_url(self.root, "report space.html"), result)
            ensure.assert_called_once()
            self.assertIsNone(preview.handle(self.event("PostToolUse"), self.root))
            (self.root / "new.htm").write_text("new")
            self.assertIn("new.htm", preview.handle(self.event("Stop"), self.root))

    def test_failed_server_retries_after_resume(self):
        preview.handle(self.event("SessionStart"), self.root)
        (self.root / "report.html").write_text("new")
        with patch.object(preview, "ensure_preview", side_effect=RuntimeError("offline")):
            self.assertIn("失败", preview.handle(self.event("PostToolUse"), self.root))
        preview.handle(self.event("SessionStart"), self.root)
        with patch.object(preview, "ensure_preview"):
            self.assertIn("localhost", preview.handle(self.event("PostToolUse"), self.root))

    def test_session_start_and_unchanged_event_both_ensure_service(self):
        with patch.object(preview, "ensure_preview") as ensure:
            preview.handle(self.event("SessionStart"), self.root)
            ensure.assert_called_once()
            ensure.reset_mock()
            preview.handle(self.event("PostToolUse"), self.root)
            ensure.assert_called_once()

    def test_unchanged_marked_html_is_discovered_in_first_session(self):
        page = self.root / "old.html"
        page.write_text('<head><meta content="artifact" name="codex-preview"></head><h1>old</h1>')
        preview.handle(self.event("SessionStart"), self.root)
        records = preview.projects(self.state)
        self.assertEqual(records[0]["files"][0]["name"], "old.html")

    def test_publish_marks_html_and_rebuild_recovers_it_without_changes(self):
        page = self.root / "report.html"
        page.write_text('<html><head><title>Report</title></head><body>report</body></html>')
        preview.handle(self.event("SessionStart"), self.root)
        timestamp=page.stat().st_mtime_ns
        preview.handle(self.event("PostToolUse"), self.root, "report.html")
        self.assertEqual(page.stat().st_mtime_ns,timestamp)
        self.assertTrue(preview.is_marked(page))
        self.assertEqual(page.read_text().count(preview.PREVIEW_MARKER), 1)
        before = page.read_bytes()
        preview.handle(self.event("PostToolUse"), self.root, "report.html")
        self.assertEqual(page.read_bytes(), before)
        (preview.runtime(self.root) / "state.json").unlink()
        preview.handle(self.event("SessionStart"), self.root)
        self.assertEqual(preview.projects(self.state)[0]["files"][0]["name"], "report.html")
        self.assertEqual(page.read_bytes(), before)

    def test_marker_in_comment_or_script_does_not_publish_unmarked_existing_file(self):
        page = self.root / "template.html"
        page.write_text('<!-- '+preview.PREVIEW_MARKER+' --><script>const x = `'+preview.PREVIEW_MARKER+'`;</script>')
        self.assertFalse(preview.is_marked(page))
        preview.handle(self.event("SessionStart"), self.root)
        self.assertEqual(preview.projects(self.state), [])

    def test_external_storage_preserves_project_identity_and_ignores_repo_html(self):
        storage = Path(self.temp.name) / 'external-previews'
        storage.mkdir()
        page = storage / 'report.html'
        page.write_text(preview.PREVIEW_MARKER + '<title>External report</title>')
        directory = preview.runtime(self.root)
        preview.write_state(directory, {'root': str(self.root), 'storage_root': str(storage),
                                        'known': {}, 'published': []})
        preview.handle(self.event('SessionStart'), self.root)
        (self.root / 'source.html').write_text(preview.PREVIEW_MARKER + 'source')
        preview.handle(self.event('PostToolUse'), self.root)
        project = preview.projects(self.state)[0]
        self.assertEqual(project['id'], preview.project_id(self.root))
        self.assertEqual(project['name'], self.root.name)
        self.assertEqual([f['name'] for f in project['files']], ['report.html'])
        result = preview.handle(self.event('PostToolUse'), self.root, str(page))
        self.assertIn(preview.project_url(self.root, 'report.html'), result)

    def test_scan_generated_html_and_skip_dependencies_and_external_symlinks(self):
        (self.root / "generated").mkdir()
        (self.root / "generated" / "plot.HTML").write_text("plot")
        (self.root / "node_modules").mkdir()
        (self.root / "node_modules" / "index.html").write_text("dependency")
        external = Path(self.temp.name) / "outside.html"
        external.write_text("outside")
        (self.root / "link.html").symlink_to(external)
        self.assertEqual(list(preview.scan(self.root)), ["generated/plot.HTML"])

    def test_http_allowlist(self):
        folder = self.root / "report"
        folder.mkdir()
        for name in ("index.html", "unpublished.html", "chart.js", ".env", "code.py"):
            (folder / name).write_text("content")
        published = ["report/index.html"]
        for name in ("index.html", "chart.js"):
            self.assertTrue(preview.allowed_file(self.root, "report/" + name, published))
        for name in ("unpublished.html", ".env", "code.py", "../../outside.html"):
            self.assertFalse(preview.allowed_file(self.root, "report/" + name, published))

    def test_existing_html_can_be_published_explicitly(self):
        (self.root / "old.html").write_text("old")
        preview.handle(self.event("SessionStart"), self.root)
        with patch.object(preview, "ensure_preview"):
            self.assertIn("old.html", preview.handle(self.event("PostToolUse"), self.root, "old.html"))

    def test_healthy_server_does_not_connect_to_windows(self):
        with patch.object(preview, "healthy", return_value=True), patch.object(preview.subprocess, "run") as run, patch.object(preview.subprocess, "Popen") as start:
            preview.ensure_preview(self.root, self.state)
            run.assert_not_called()
            start.assert_not_called()

    def test_publish_explains_client_forward_without_claiming_connected(self):
        (self.root / "report.html").write_text("new")
        with patch.object(preview, "ensure_preview"):
            result = preview.handle(self.event("PostToolUse"), self.root)
        self.assertIn("ssh -N -L 18765:127.0.0.1:18765 reid@100.120.6.3", result)
        self.assertIn("hook 不检查 Windows 转发是否已建立", result)
        self.assertNotIn("已通过 SSH", result)

    def test_projects_with_same_names_have_distinct_urls_and_deleted_files_are_removed(self):
        other = Path(self.temp.name) / "another" / self.root.name
        other.mkdir(parents=True)
        for root in (self.root, other):
            (root / "index.html").write_text("<title>测试</title>")
            with patch.object(preview, "ensure_preview"):
                preview.handle({"hook_event_name": "PostToolUse", "cwd": str(root)}, root)
        records = preview.projects(self.state)
        self.assertEqual(len(records), 2)
        self.assertNotEqual(records[0]["files"][0]["url"], records[1]["files"][0]["url"])
        (other / "index.html").unlink()
        preview.handle({"hook_event_name": "PostToolUse", "cwd": str(other)}, other)
        self.assertEqual(sum(len(item["files"]) for item in preview.projects(self.state)), 1)
        (other / "index.html").write_text("<title>测试</title>")
        with patch.object(preview, "ensure_preview"):
            self.assertIn("localhost", preview.handle(
                {"hook_event_name": "PostToolUse", "cwd": str(other)}, other))


class RegistryMigrationTests(unittest.TestCase):
    def test_registry_survives_runtime_removal_and_migration_preserves_newer_records(self):
        import shutil
        with tempfile.TemporaryDirectory() as tmp:
            temporary = Path(tmp) / "runtime"
            legacy = temporary / "projects" / "abc" / "state.json"
            legacy.parent.mkdir(parents=True)
            legacy.write_text(json.dumps({"root":"/example", "known":{}, "published":["old.html"]}))
            with patch.dict(os.environ, {"XDG_STATE_HOME":str(Path(tmp)/"state")}), patch.object(preview,"shared_runtime",return_value=temporary):
                registry = preview.registry_root()
                target = registry / "projects" / "abc" / "state.json"
                self.assertEqual(json.loads(target.read_text())["published"], ["old.html"])
                newer = {"root":"/example", "known":{}, "published":["new.html"]}
                target.write_text(json.dumps(newer))
                preview.registry_root()
                self.assertEqual(json.loads(target.read_text()), newer)
                shutil.rmtree(temporary)
                self.assertEqual(preview.registry_root(), registry)
                self.assertEqual(json.loads(target.read_text()), newer)



class ServiceHealthTests(unittest.TestCase):
    def test_pid_namespace_isolation_requires_fresh_same_boot_heartbeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp)
            marker={"pid":99999999,"service_id":preview.SERVICE_ID,"pid_namespace":"host-pid-namespace",
                    "boot_id":Path("/proc/sys/kernel/random/boot_id").read_text().strip(),"heartbeat":preview.time.time()}
            path=directory/"server.ready.json"
            with patch.object(preview,"shared_runtime",return_value=directory):
                path.write_text(json.dumps(marker))
                self.assertTrue(preview.ready_process())
                marker["heartbeat"]-=30
                path.write_text(json.dumps(marker))
                self.assertFalse(preview.ready_process())
                marker["heartbeat"]=preview.time.time()
                marker["boot_id"]="previous-boot"
                path.write_text(json.dumps(marker))
                self.assertFalse(preview.ready_process())

    def test_sandbox_network_denial_uses_live_process_marker_but_rejects_stale_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "--serve"])
            try:
                marker = {"pid": child.pid, "script": sys.executable,
                          "service_id": preview.SERVICE_ID, "start": preview.process_start(child.pid)}
                path = directory / "server.ready.json"
                path.write_text(json.dumps(marker))
                with patch.object(preview, "shared_runtime", return_value=directory), patch.object(
                    preview, "urlopen", side_effect=URLError(PermissionError(errno.EPERM, "Operation not permitted"))):
                    self.assertTrue(preview.healthy())
                    marker["start"] = "stale"
                    path.write_text(json.dumps(marker))
                    self.assertFalse(preview.healthy())
                marker["start"] = preview.process_start(child.pid)
                path.write_text(json.dumps(marker))
                with patch.object(preview, "shared_runtime", return_value=directory), patch.object(
                    preview, "urlopen", side_effect=URLError(ConnectionRefusedError(errno.ECONNREFUSED, "refused"))):
                    self.assertFalse(preview.healthy())
            finally:
                child.terminate()
                child.wait()



class SharedPreviewHttpTests(unittest.TestCase):
    def test_two_projects_assets_encoded_names_and_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            shared = Path(tmp) / "runtime"
            shared.mkdir()
            roots = [Path(tmp) / "a", Path(tmp) / "b"]
            with patch.object(preview, "shared_runtime", return_value=shared), patch.object(preview, "registry_root", return_value=shared), patch.object(preview, "ensure_preview"):
                for root in roots:
                    root.mkdir()
                    (root / "报告 space.html").write_text(f"<title>{root.name}</title><script src='chart.js'></script>")
                    (root / "chart.js").write_text(root.name)
                    (root / ".env").write_text("secret")
                    preview.handle({"hook_event_name": "PostToolUse", "cwd": str(root)}, root)
            server = preview.create_server(shared, 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                with urlopen(base + "/") as response:
                    self.assertIn("Preview Studio", response.read().decode())
                with urlopen(base + "/api/projects") as response:
                    self.assertEqual(len(json.load(response)), 2)
                for root in roots:
                    with urlopen(base + preview.project_url(root, "报告 space.html")) as response:
                        self.assertIn(f"<title>{root.name}</title>", response.read().decode())
                    with urlopen(base + preview.project_url(root, "chart.js") + "?v=1") as response:
                        self.assertEqual(response.read().decode(), root.name)
                for name in (".env", "../b/chart.js", "../../b/chart.js"):
                    with self.assertRaises(HTTPError) as failure:
                        urlopen(base + preview.project_url(roots[0], name))
                    self.assertEqual(failure.exception.code, 404)
                with self.assertRaises(HTTPError):
                    urlopen(base + "/p/invalid/chart.js")
                # A legacy URL shared by two projects must not choose an arbitrary project.
                with self.assertRaises(HTTPError):
                    urlopen(base + "/" + preview.quote("报告 space.html"))
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == "__main__":
    unittest.main()
