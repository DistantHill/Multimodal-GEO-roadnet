"""Behavior tests for the repository SQL-change hook without Docker or a DB."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import uuid

spec = importlib.util.spec_from_file_location('sql_spatial_index', Path(__file__).resolve().parents[1] / 'sql_spatial_index.py')
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)


class SqlSpatialHookTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        self.session = uuid.uuid4().hex
        self.source = self.root / 'query.py'
        self.source.write_text('query = "SELECT id FROM points"\n')
        self.assertIsNone(self.event('SessionStart'))
        token = hook.hashlib.sha256((str(self.root) + '\0' + self.session).encode()).hexdigest()
        state_dir = Path(tempfile.gettempdir()) / 'roadnet-sql-index-hooks'
        for suffix in ('.json', '.lock'):
            self.addCleanup((state_dir / (token + suffix)).unlink, missing_ok=True)

    def event(self, kind):
        return hook.handle({'cwd': str(self.root), 'session_id': self.session,
                            'hook_event_name': kind, 'tool_name': 'Bash'}, self.root)

    def edit(self):
        self.source.write_text('query = "SELECT id FROM points WHERE ST_Covers(%s, geom)"\n')

    def review(self, record):
        key = next(iter(hook.scan(self.root)))
        path = self.root / hook.REVIEW_FILE
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({key: record}))

    def test_existing_dirty_sql_is_baseline(self):
        self.assertIsNone(self.event('PostToolUse'))

    def test_project_config_keeps_review_out_of_pretool(self):
        root = Path(__file__).resolve().parents[3]
        config = json.loads((root / '.codex/hooks.json').read_text())['hooks']
        def registered(event):
            return any('sql_spatial_index.py' in item.get('command', '')
                       for group in config.get(event, []) for item in group.get('hooks', []))
        self.assertFalse(registered('PreToolUse'))
        for event in ('SessionStart', 'PostToolUse', 'Stop'):
            self.assertTrue(registered(event))

    def test_pending_review_allows_repair_and_still_blocks_stop(self):
        self.edit()
        self.assertEqual(self.event('PostToolUse')['decision'], 'block')
        self.assertIsNone(self.event('PreToolUse'))
        self.assertEqual(self.event('Stop')['decision'], 'block')

    def test_sql_change_blocks_post_and_stop(self):
        self.edit()
        self.assertEqual(self.event('PostToolUse')['decision'], 'block')
        self.assertEqual(self.event('Stop')['decision'], 'block')

    def test_unrelated_edit_does_not_require_sql_review(self):
        self.source.write_text('query = "SELECT id FROM points"\nother = 3\n')
        self.assertIsNone(self.event('PostToolUse'))

    def test_new_sql_file_blocks(self):
        (self.root / 'new.sql').write_text('SELECT ST_Buffer(geom, 10) FROM points;')
        self.assertEqual(self.event('PostToolUse')['decision'], 'block')

    def test_fstring_edit_blocks(self):
        self.source.write_text('query = f"SELECT * FROM {table} WHERE geom && %s"\n')
        self.assertEqual(self.event('PostToolUse')['decision'], 'block')

    def test_concatenated_sql_suffix_edit_blocks(self):
        self.source.write_text('query = "SELECT id FROM points " + "WHERE id=%s"\n')
        self.event('PostToolUse')
        self.review({'status': 'not-applicable', 'reason': 'Primary key query without spatial filtering.'})
        self.assertIsNone(self.event('PostToolUse'))
        self.source.write_text('query = "SELECT id FROM points " + "WHERE id=%s LIMIT 2"\n')
        self.assertEqual(self.event('PostToolUse')['decision'], 'block')

    def test_unrelated_pre_call_does_not_reset_pending(self):
        self.edit()
        self.event('PostToolUse')
        self.event('PreToolUse')
        self.assertEqual(self.event('Stop')['decision'], 'block')

    def test_nonspatial_review_requires_reason(self):
        self.edit()
        self.review({'status': 'not-applicable'})
        self.assertEqual(self.event('PostToolUse')['decision'], 'block')
        # This is a declared review outcome, not automated semantic inference.
        self.source.write_text('query = "SELECT status FROM runs WHERE id=%s"\n')
        self.review({'status': 'not-applicable', 'reason': 'Metadata lookup by run primary key; no spatial rows.'})
        self.assertIsNone(self.event('PostToolUse'))
        self.assertIsNone(self.event('Stop'))

    def test_actual_index_plan_passes_and_reedit_invalidates(self):
        self.edit()
        (self.root / 'plan.json').write_text(json.dumps([{'Plan': {
            'Node Type': 'Bitmap Heap Scan', 'Plans': [{'Node Type': 'Bitmap Index Scan',
            'Index Name': 'points_geom', 'Index Cond': '(geom && $1)'}]}}]))
        self.review({'status': 'uses-index', 'indexes': ['points_geom'], 'crs': 'EPSG:4326', 'plan': 'plan.json'})
        self.assertIsNone(self.event('PostToolUse'))
        self.source.write_text('query = "SELECT id FROM points WHERE ST_Covers(%s, geom) LIMIT 5"\n')
        self.assertEqual(self.event('PostToolUse')['decision'], 'block')

    def test_seqscan_and_index_mentioned_in_filter_do_not_pass(self):
        self.edit()
        (self.root / 'plan.json').write_text(json.dumps([{'Plan': {'Node Type': 'Seq Scan',
            'Index Name': 'points_geom', 'Filter': 'ST_Covers($1, geom)'}}]))
        self.review({'status': 'uses-index', 'indexes': ['points_geom'], 'crs': 'EPSG:4326', 'plan': 'plan.json'})
        self.assertEqual(self.event('PostToolUse')['decision'], 'block')

    def test_missing_and_external_plan_fail(self):
        self.edit()
        for plan in ('missing.json', '../outside.json'):
            self.review({'status': 'uses-index', 'indexes': ['points_geom'], 'crs': 'EPSG:4326', 'plan': plan})
            self.assertEqual(self.event('PostToolUse')['decision'], 'block')

    def test_deleting_pending_sql_clears_gate(self):
        self.edit()
        self.event('PostToolUse')
        self.source.unlink()
        self.assertIsNone(self.event('Stop'))

    def test_external_plan_requires_approved_data_root_and_matching_digest(self):
        with tempfile.TemporaryDirectory() as data:
            plan = Path(data) / 'codex-artifacts' / 'sql-plans' / 'plan.json'
            plan.parent.mkdir(parents=True)
            plan.write_text(json.dumps([{'Plan': {'Node Type': 'Bitmap Index Scan',
                'Index Name': 'points_geom', 'Index Cond': '(geom && $1)'}}]))
            record = {'status': 'uses-index', 'indexes': ['points_geom'],
                      'crs': 'EPSG:4326', 'plan': str(plan)}
            with patch.dict(hook.os.environ, {'DATA_ROOT': data}):
                self.assertIn('plan_sha256', hook.review_error(self.root, record))
                record['plan_sha256'] = hook.hashlib.sha256(plan.read_bytes()).hexdigest()
                self.assertIsNone(hook.review_error(self.root, record))
                plan.write_text(json.dumps([{'Plan': {'Node Type': 'Seq Scan'}}]))
                self.assertIn('plan_sha256', hook.review_error(self.root, record))
                record['plan_sha256'] = hook.hashlib.sha256(plan.read_bytes()).hexdigest()
                self.assertIn('扫描节点', hook.review_error(self.root, record))
            with patch.dict(hook.os.environ, {'DATA_ROOT': str(self.root / 'different')}):
                self.assertIn('外部计划文件必须', hook.review_error(self.root, record))

    def test_external_plan_symlink_must_not_escape_approved_directory(self):
        with tempfile.TemporaryDirectory() as data, tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / 'plan.json'
            target.write_text('[]')
            link = Path(data) / 'codex-artifacts' / 'plan.json'
            link.parent.mkdir()
            link.symlink_to(target)
            record = {'status': 'uses-index', 'indexes': ['points_geom'],
                      'crs': 'EPSG:4326', 'plan': str(link),
                      'plan_sha256': hook.hashlib.sha256(target.read_bytes()).hexdigest()}
            with patch.dict(hook.os.environ, {'DATA_ROOT': data}):
                self.assertIn('外部计划文件必须', hook.review_error(self.root, record))

    def test_tools_outside_project_do_not_touch_state(self):
        self.assertIsNone(hook.handle({'cwd': '/', 'hook_event_name': 'PostToolUse'}, self.root))

    def test_sql_comments_and_dollar_quotes(self):
        units = hook.sql_units("-- comment\nSELECT ';'; DO $$BEGIN SELECT 1; END$$; SELECT 2;")
        self.assertEqual(len(units), 3)
        self.assertTrue(all(hook.is_sql(value) for _, value in units))

    def test_javascript_and_shell_embedded_sql(self):
        for name, text in [('query.js', 'const q = `SELECT * FROM points WHERE geom && ${bounds}`;'),
                           ('query.sh', "psql <<'SQL'\nSELECT * FROM points;\nSQL\n")]:
            path = self.root / name
            path.write_text(text)
            self.assertTrue(hook.extract(path))


if __name__ == '__main__':
    unittest.main()
