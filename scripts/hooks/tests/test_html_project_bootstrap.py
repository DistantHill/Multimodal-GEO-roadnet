import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import html_project_bootstrap as bootstrap


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'project'
        self.root.mkdir()
        self.shared = Path(self.temp.name) / 'runtime'
        self.shared.mkdir()
        for name, value in [('shared_runtime',self.shared),('discover_root',self.root)]:
            mock = patch.object(bootstrap,name,return_value=value)
            mock.start()
            self.addCleanup(mock.stop)
        self.config = self.root / '.codex' / 'hooks.json'

    def event(self, session='one'):
        return {'hook_event_name':'UserPromptSubmit','session_id':session,'cwd':str(self.root)}

    def test_first_prompt_installs_preserves_hooks_and_subsequent_prompt_keeps_policy(self):
        self.config.parent.mkdir()
        initial={'description':'existing','hooks':{'Stop':[{'hooks':[{'type':'command','command':'existing-check'}]}]}}
        self.config.write_text(json.dumps(initial))
        result=bootstrap.handle(self.event())
        self.assertIn('已补齐',result['hookSpecificOutput']['additionalContext'])
        current=json.loads(self.config.read_text())
        self.assertEqual(current['hooks']['Stop'],initial['hooks']['Stop'])
        self.assertEqual(len(current['hooks']['UserPromptSubmit']),1)
        before=self.config.read_bytes()
        second=bootstrap.handle(self.event())
        self.assertEqual(before,self.config.read_bytes())
        self.assertIn('可交互 HTML',second['hookSpecificOutput']['additionalContext'])
        self.assertNotIn('首次对话检查',second['hookSpecificOutput']['additionalContext'])

    def test_existing_generation_hook_not_duplicated(self):
        self.config.parent.mkdir()
        data={'hooks':{'UserPromptSubmit':[{'hooks':[{'command':'python3 existing-html-generation.py','statusMessage':'Generate interactive HTML'}]}]}}
        self.config.write_text(json.dumps(data))
        before=self.config.read_bytes()
        self.assertIn('已有',bootstrap.handle(self.event())['hookSpecificOutput']['additionalContext'])
        self.assertEqual(before,self.config.read_bytes())

    def test_inline_toml_generation_is_recognized(self):
        self.config.parent.mkdir()
        (self.config.parent/'config.toml').write_text('[[hooks.UserPromptSubmit]]\n[[hooks.UserPromptSubmit.hooks]]\ntype="command"\ncommand="html-generation"\n')
        self.assertIn('已有',bootstrap.handle(self.event())['hookSpecificOutput']['additionalContext'])
        self.assertFalse(self.config.exists())

    def test_corrupt_config_is_not_overwritten(self):
        self.config.parent.mkdir()
        self.config.write_text('{broken')
        self.assertIn('未能完成',bootstrap.handle(self.event())['hookSpecificOutput']['additionalContext'])
        self.assertEqual(self.config.read_text(),'{broken')

    def test_each_new_session_checks_again(self):
        bootstrap.handle(self.event())
        self.config.unlink()
        bootstrap.handle(self.event())
        self.assertFalse(self.config.exists())
        bootstrap.handle(self.event('two'))
        self.assertTrue(self.config.exists())

    def test_other_events_and_missing_session(self):
        self.assertIsNone(bootstrap.handle({'hook_event_name':'SessionStart'}))
        result=bootstrap.handle({'hook_event_name':'UserPromptSubmit'})
        self.assertIn('缺少 session_id',result['hookSpecificOutput']['additionalContext'])
        self.assertFalse(self.config.exists())


if __name__ == '__main__':
    unittest.main()
