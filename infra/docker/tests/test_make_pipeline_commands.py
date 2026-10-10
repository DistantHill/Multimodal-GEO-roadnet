"""Check the smoke entrypoint without running Docker or writing pipeline results."""

from pathlib import Path
import os
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[3]


class MakePipelineCommandsTest(unittest.TestCase):
    def dry_run(self, *arguments):
        return subprocess.run(
            ["make", "--no-print-directory", "-n", *arguments],
            cwd=ROOT, capture_output=True, text=True,
        )

    def test_smoke_runs_pipeline_with_explicit_source_and_smoke_config(self):
        result = self.dry_run("pipeline-smoke-test", "SOURCE_TABLE=public.day01")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "PIPELINE_CONFIG_PATH=/app/config/pipeline-roadnet-test.toml",
            result.stdout,
        )
        self.assertIn('python -m pipeline run --source-table "public.day01"', result.stdout)
        self.assertNotIn("pytest", result.stdout)

    def test_full_selects_march_first_in_beijing_time(self):
        result = self.dry_run("pipeline-full-test", "SOURCE_TABLE=trajectory_points")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--time-start "2023-03-01T00:00:00+08:00"', result.stdout)
        self.assertIn('--time-end "2023-03-02T00:00:00+08:00"', result.stdout)
        smoke = self.dry_run("pipeline-smoke-test", "SOURCE_TABLE=trajectory_points")
        self.assertNotIn("--time-start", smoke.stdout)

    def test_full_time_range_can_be_overridden_or_blank(self):
        result = self.dry_run(
            "pipeline-full-test", "SOURCE_TABLE=trajectory_points",
            "FULL_TEST_TIME_START=2023-03-02T00:00:00+08:00",
            "FULL_TEST_TIME_END=2023-03-03T00:00:00+08:00",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--time-end "2023-03-03T00:00:00+08:00"', result.stdout)
        blank = self.dry_run("pipeline-full-test", "REFINE_ID=11111111-1111-1111-1111-111111111111",
                             "FULL_TEST_TIME_START=", "FULL_TEST_TIME_END=")
        self.assertEqual(blank.returncode, 0, blank.stderr)
        self.assertIn('--time-start "" --time-end ""', blank.stdout)
        smoke = self.dry_run("pipeline-smoke-test", "REFINE_ID=11111111-1111-1111-1111-111111111111",
                             "TIME_START=2023-03-01T00:00:00+08:00", "TIME_END=2023-03-02T00:00:00+08:00")
        self.assertIn('--time-start "2023-03-01T00:00:00+08:00"', smoke.stdout)

    def test_smoke_requires_source(self):
        result = self.dry_run("pipeline-smoke-test", "SOURCE_TABLE=")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SOURCE_TABLE 不能为空", result.stderr)

    def test_full_runs_pipeline_with_explicit_source_and_full_config(self):
        result = self.dry_run("pipeline-full-test", "SOURCE_TABLE=public.day01")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PIPELINE_CONFIG_PATH=/app/config/pipeline.toml", result.stdout)
        self.assertNotIn("pipeline-roadnet-test.toml", result.stdout)
        self.assertIn('python -m pipeline run --source-table "public.day01"', result.stdout)
        self.assertNotIn("pytest", result.stdout)

    def test_full_requires_source(self):
        result = self.dry_run("pipeline-full-test", "SOURCE_TABLE=")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SOURCE_TABLE 不能为空", result.stderr)

    def test_smoke_accepts_explicit_refine_id_without_injecting_bool(self):
        result = self.dry_run(
            "pipeline-smoke-test", "REFINE_ID=11111111-1111-1111-1111-111111111111"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("pipeline-roadnet-test.toml", result.stdout)
        self.assertIn("--refine-id \"11111111-1111-1111-1111-111111111111\"", result.stdout)
        self.assertNotIn("--refine-resume-last", result.stdout)

    def test_smoke_passes_resume_last_true(self):
        result = self.dry_run(
            "pipeline-smoke-test", "SOURCE_TABLE=public.day01", "REFINE_RESUME_LAST=true"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--refine-resume-last "true"', result.stdout)

    def test_resume_last_true_needs_no_source_or_refine_id(self):
        for target in ("pipeline-smoke-test", "pipeline-full-test"):
            with self.subTest(target=target):
                result = self.dry_run(target, "REFINE_RESUME_LAST=true")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('pipeline run  --refine-resume-last "true"', result.stdout)
                self.assertNotIn("--source-table", result.stdout)
                self.assertNotIn("--refine-id", result.stdout)

    def test_full_passes_false_as_an_explicit_choice(self):
        result = self.dry_run(
            "pipeline-full-test", "SOURCE_TABLE=public.day01", "REFINE_RESUME_LAST=false"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("pipeline.toml", result.stdout)
        self.assertIn('--refine-resume-last "false"', result.stdout)

    def test_selection_rejects_conflicts_and_invalid_boolean_before_services(self):
        for arguments, expected in (
            (("SOURCE_TABLE=public.day01", "REFINE_ID=11111111-1111-1111-1111-111111111111"), "只能指定一个"),
            (("SOURCE_TABLE=public.day01", "REFINE_RESUME_LAST="), "不能为空"),
            (("REFINE_ID=11111111-1111-1111-1111-111111111111", "REFINE_RESUME_LAST=true"), "只能与 SOURCE_TABLE 配合使用"),
            (("SOURCE_TABLE=public.day01", "REFINE_RESUME_LAST=yes"), "只能是 true 或 false"),
            (("SOURCE_TABLE=public.day01", "REFINE_RESUME_LAST=true false"), "必须是单个 true 或 false"),
            (("REFINE_RESUME_LAST=false",), "请指定 SOURCE_TABLE"),
        ):
            with self.subTest(arguments=arguments):
                result = self.dry_run("pipeline-full-test", *arguments)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected, result.stderr)
                self.assertNotIn("services.yml up -d --wait", result.stdout)

    def test_invalid_selection_does_not_invoke_services_up(self):
        with tempfile.TemporaryDirectory() as directory:
            stub = Path(directory) / "docker"
            calls = Path(directory) / "docker-calls.log"
            stub.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$DOCKER_STUB_LOG\"\n",
                encoding="utf-8",
            )
            stub.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{directory}{os.pathsep}{env['PATH']}"
            env["DOCKER_STUB_LOG"] = str(calls)
            result = subprocess.run(
                ["make", "--no-print-directory", "pipeline-smoke-test",
                 "SOURCE_TABLE=public.day01", "REFINE_RESUME_LAST=true false"],
                cwd=ROOT, env=env, capture_output=True, text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("必须是单个 true 或 false", result.stderr)
            self.assertFalse(calls.exists())

    def test_regular_test_target_keeps_pytest(self):
        result = self.dry_run("test")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("pipeline python -m pytest tests", result.stdout)

    def test_experiment_uses_one_external_config_and_host_commit(self):
        result = self.dry_run("pipeline-experiment", "ARGS=run --scheme turn")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PIPELINE_GIT_COMMIT=", result.stdout)
        self.assertIn('experiment --config "/data/experiments/experiment.toml" run --scheme turn', result.stdout)

    def test_hpo_uses_external_config_without_starting_full_pipeline(self):
        result = self.dry_run("pipeline-hpo", "ARGS=audit", "HPO_CONFIG_PATH=/data/experiments/custom.toml")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PIPELINE_GIT_COMMIT=", result.stdout)
        self.assertIn('track.v2.experiments.intersection_hpo --config "/data/experiments/custom.toml" audit', result.stdout)
        self.assertNotIn("pipeline run", result.stdout)


if __name__ == "__main__":
    unittest.main()
