import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('artifact_routing', Path(__file__).resolve().parents[1] / 'artifact_routing.py')
routing = importlib.util.module_from_spec(spec)
spec.loader.exec_module(routing)


class ArtifactRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'project'
        self.root.mkdir()
        mock = patch.object(routing, 'discover_root', return_value=self.root)
        mock.start()
        self.addCleanup(mock.stop)

    def event(self, prompt='测试并轮询日志，记录性能和截图'):
        return {'hook_event_name':'UserPromptSubmit', 'cwd':str(self.root), 'session_id':'session-one', 'prompt':prompt}

    def test_environment_wins_over_dotenv_and_prompt_selects_categories(self):
        (self.root/'.env').write_text('DATA_ROOT=/data/tmp/from-dotenv\nSECRET=not-read\n')
        plan = routing.make_plan(self.event(), {'DATA_ROOT':'/data/tmp/from-env'})
        self.assertEqual(plan['data_root'], '/data/tmp/from-env')
        self.assertTrue(plan['run_root'].startswith('/data/tmp/from-env/codex-artifacts/'))
        self.assertEqual({x['kind'] for x in plan['artifacts']}, {'evidence','logs','metrics','screenshots','scratch'})

    def test_dotenv_quotes_export_and_comments_without_executing_shell(self):
        (self.root/'.env').write_text('export DATA_ROOT="/data/tmp/a folder" # comment\nOTHER=$(touch something)\n')
        self.assertEqual(routing.make_plan(self.event(), {})['data_root'], '/data/tmp/a folder')
        self.assertFalse((self.root/'something').exists())

    def test_lowercase_data_root_and_environment_expansion(self):
        self.assertEqual(routing.make_plan(self.event(), {'data_root':'${ARTIFACT_BASE}/data','ARTIFACT_BASE':'/data/tmp/test'})['data_root'], '/data/tmp/test/data')

    def test_no_data_root_uses_project_fallback_without_creating_directories(self):
        before=list(self.root.iterdir())
        plan=routing.make_plan(self.event('修复接口'), {})
        self.assertIsNone(plan['data_root'])
        self.assertTrue(plan['run_root'].startswith('/data/tmp/project/codex-artifacts/'))
        self.assertEqual([x['kind'] for x in plan['artifacts']], ['scratch'])
        self.assertEqual(list(self.root.iterdir()),before)
        self.assertFalse(Path(plan['run_root']).exists())

    def test_data_root_inside_repository_uses_external_fallback(self):
        for configured in ['runtime',str(self.root/'data')]:
            plan=routing.make_plan(self.event(), {'DATA_ROOT':configured})
            self.assertIsNone(plan['data_root'])
            self.assertIn('位于仓库内',plan['source'])
            self.assertTrue(plan['run_root'].startswith('/data/tmp/project/'))

    def test_symlink_back_into_repository_is_rejected(self):
        link=Path(self.temp.name)/'data-link'
        link.symlink_to(self.root,target_is_directory=True)
        self.assertIsNone(routing.make_plan(self.event(),{'DATA_ROOT':str(link)})['data_root'])

    def test_unexpanded_expressions_do_not_become_relative_output_paths(self):
        self.assertIsNone(routing.make_plan(self.event(),{'DATA_ROOT':'${NOT_SET}/data'})['data_root'])

    def test_stable_session_and_distinct_sessions(self):
        one=routing.make_plan(self.event(),{})
        two=routing.make_plan(self.event('导出 CSV 和 Parquet'),{})
        self.assertEqual(one['run_root'],two['run_root'])
        self.assertIn('exports',{x['kind'] for x in two['artifacts']})
        event=self.event();event['session_id']='another'
        self.assertNotEqual(one['run_root'],routing.make_plan(event,{})['run_root'])

    def test_same_named_projects_do_not_share_session_directory(self):
        one=routing.make_plan(self.event(),{})
        another=Path(self.temp.name)/'other'/'project';another.mkdir(parents=True)
        with patch.object(routing,'discover_root',return_value=another):
            two=routing.make_plan(self.event(),{})
        self.assertNotEqual(one['run_root'],two['run_root'])

    def test_hook_injects_only_non_html_routing_and_ignores_other_events(self):
        self.assertIsNone(routing.handle({'hook_event_name':'SessionStart'}))
        with patch.dict(os.environ,{'DATA_ROOT':'/data/tmp/evidence'},clear=True):
            result=routing.handle(self.event())
        text=result['hookSpecificOutput']['additionalContext']
        self.assertIn('/data/tmp/evidence/codex-artifacts/',text)
        self.assertIn('不改变 HTML 保存',text)
        self.assertIn('结合完整会话',text)
        self.assertFalse((self.root/'.codex').exists())


if __name__ == '__main__':
    unittest.main()
