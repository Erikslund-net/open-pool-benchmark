"""Unit tests for config rendering and substitution (the pure, Docker-free core of an adapter)."""

from __future__ import annotations

import json
import pathlib
import subprocess
import tomllib
import unittest
from unittest import mock

import yaml

from openbench import adapters
from openbench import config
from openbench.tests import base

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]


class GitCloneTests(unittest.TestCase):
    def test_shallow_clone_args_with_ref(self) -> None:
        with mock.patch("openbench.adapters.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            adapters._git_clone("URL", pathlib.Path("/tmp/dest"), "main", "pool")
        called = run.call_args[0][0]
        self.assertEqual(called[:5], ["git", "clone", "--depth", "1", "--branch"])
        self.assertIn("main", called)
        self.assertEqual(called[-2:], ["URL", str(pathlib.Path("/tmp/dest"))])

    def test_no_ref_omits_branch(self) -> None:
        with mock.patch("openbench.adapters.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            adapters._git_clone("URL", pathlib.Path("/tmp/d"), None, "pool")
        self.assertNotIn("--branch", run.call_args[0][0])

    def test_failed_clone_surfaces_git_stderr(self) -> None:
        with mock.patch("openbench.adapters.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 1, "", "fatal: repo not found")
            with self.assertRaises(adapters.AdapterError) as caught:
                adapters._git_clone("URL", pathlib.Path("/tmp/d"), None, "mypool")
        self.assertIn("mypool", str(caught.exception))
        self.assertIn("fatal: repo not found", str(caught.exception))

    def test_missing_git_is_a_clear_error(self) -> None:
        with mock.patch("openbench.adapters.subprocess.run", side_effect=FileNotFoundError()):
            with self.assertRaises(adapters.AdapterError) as caught:
                adapters._git_clone("URL", pathlib.Path("/tmp/d"), None, "pool")
        self.assertIn("git is not installed", str(caught.exception))


class FormatDifficultyTests(unittest.TestCase):
    def test_integral_drops_decimal(self) -> None:
        self.assertEqual(adapters.format_difficulty(1000000.0), "1000000")

    def test_fractional_kept(self) -> None:
        self.assertEqual(adapters.format_difficulty(0.001), "0.001")


class BuildSubstitutionsTests(unittest.TestCase):
    def test_maps_backend_and_profile(self) -> None:
        mapping = adapters.build_substitutions(
            base.sample_pool(), base.sample_profile(), base.sample_regtest(), base.sample_pinning()
        )
        self.assertEqual(mapping["RPC_HOST"], "bitcoind")
        self.assertEqual(mapping["RPC_PORT"], "18443")
        self.assertEqual(mapping["DIFFICULTY"], "1000000")
        self.assertEqual(mapping["MIN_DIFFICULTY"], "1")  # the vardiff floor (default)
        self.assertEqual(mapping["MAX_DIFFICULTY"], "1000000")  # the vardiff ceiling (default)
        self.assertEqual(mapping["TAG"], "/openbench/")
        self.assertEqual(mapping["API_PORT"], "5662")
        self.assertEqual(mapping["ADDRESS"], "bcrt1qexampleaddress")
        self.assertEqual(mapping["POOL_HOST"], adapters.POOL_CONTAINER)

    def test_min_difficulty_comes_from_the_profile(self) -> None:
        mapping = adapters.build_substitutions(
            base.sample_pool(),
            base.sample_profile(min_difficulty=0.5),
            base.sample_regtest(),
            base.sample_pinning(),
        )
        self.assertEqual(mapping["MIN_DIFFICULTY"], "0.5")

    def test_api_port_blank_when_absent(self) -> None:
        mapping = adapters.build_substitutions(
            base.sample_pool(api_port=None),
            base.sample_profile(),
            base.sample_regtest(),
            base.sample_pinning(),
        )
        self.assertEqual(mapping["API_PORT"], "")

    def test_pool_cores_tracks_cpuset(self) -> None:
        single = adapters.build_substitutions(
            base.sample_pool(),
            base.sample_profile(),
            base.sample_regtest(),
            base.sample_pinning(pool_cpus="1"),
        )
        self.assertEqual(single["POOL_CORES"], "1")
        quad = adapters.build_substitutions(
            base.sample_pool(),
            base.sample_profile(),
            base.sample_regtest(),
            base.sample_pinning(pool_cpus="1-4"),
        )
        self.assertEqual(quad["POOL_CORES"], "4")

    def test_pool_cores_is_max_when_unpinned(self) -> None:
        mapping = adapters.build_substitutions(
            base.sample_pool(),
            base.sample_profile(),
            base.sample_regtest(),
            base.sample_pinning(enabled=False),
        )
        self.assertEqual(mapping["POOL_CORES"], "max")

    def test_lb_mapping_points_its_backend_at_the_pool(self) -> None:
        lb = base.sample_pool(name="loadbalancer", stratum_port=3333, api_port=7778)
        mapping = adapters.build_lb_substitutions(
            lb,
            base.sample_pool(stratum_port=5661),
            base.sample_profile(),
            base.sample_regtest(),
            base.sample_pinning(lb_cpus="14-15"),
        )
        self.assertEqual(mapping["POOL_HOST"], adapters.LB_CONTAINER)  # its own readiness target
        self.assertEqual(mapping["API_PORT"], "7778")
        self.assertEqual(mapping["BACKEND_HOST"], adapters.POOL_CONTAINER)
        self.assertEqual(mapping["BACKEND_PORT"], "5661")
        self.assertEqual(mapping["LB_CORES"], "2")

    def test_lb_cores_is_auto_when_unpinned(self) -> None:
        mapping = adapters.build_substitutions(
            base.sample_pool(),
            base.sample_profile(),
            base.sample_regtest(),
            base.sample_pinning(enabled=False, lb_cpus="14-15"),
        )
        self.assertEqual(mapping["LB_CORES"], "0")


class RenderConfigTests(unittest.TestCase):
    def _mapping(self) -> dict[str, str]:
        return adapters.build_substitutions(
            base.sample_pool(), base.sample_profile(), base.sample_regtest(), base.sample_pinning()
        )

    def test_pogolo_toml_renders_and_is_valid_toml(self) -> None:
        template = (
            "[backend]\n"
            "host = '${RPC_HOST}:${RPC_PORT}'\n"
            "rpcauth = '${RPC_USER}:${RPC_PASS}'\n"
            "[pogolo]\n"
            "default_difficulty = ${DIFFICULTY}\n"
            "tag = '${TAG}'\n"
        )
        rendered = adapters.render_config(template, "toml", self._mapping())
        parsed = tomllib.loads(rendered)
        self.assertEqual(parsed["backend"]["host"], "bitcoind:18443")
        self.assertEqual(parsed["backend"]["rpcauth"], "openbench:openbenchpass")
        self.assertEqual(parsed["pogolo"]["default_difficulty"], 1000000)
        self.assertEqual(parsed["pogolo"]["tag"], "/openbench/")

    def test_public_pool_env_renders(self) -> None:
        template = "BITCOIN_RPC_URL=http://${RPC_HOST}\nBITCOIN_RPC_PORT=${RPC_PORT}\n# a comment\n"
        rendered = adapters.render_config(template, "env", self._mapping())
        self.assertIn("BITCOIN_RPC_URL=http://bitcoind", rendered)
        self.assertIn("BITCOIN_RPC_PORT=18443", rendered)

    def test_unknown_variable_is_hard_error(self) -> None:
        with self.assertRaises(adapters.AdapterError) as caught:
            adapters.render_config("x = ${NOPE}", "none", self._mapping())
        self.assertIn("NOPE", str(caught.exception))

    def test_invalid_rendered_toml_is_caught(self) -> None:
        with self.assertRaises(adapters.AdapterError) as caught:
            adapters.render_config("host = '${RPC_HOST}", "toml", self._mapping())
        self.assertIn("TOML", str(caught.exception).upper())

    def test_env_line_without_equals_is_caught(self) -> None:
        with self.assertRaises(adapters.AdapterError) as caught:
            adapters.render_config("BITCOIN_RPC_URL http://${RPC_HOST}", "env", self._mapping())
        self.assertIn("KEY=VALUE", str(caught.exception))

    def test_none_format_skips_validation(self) -> None:
        self.assertEqual(
            adapters.render_config("anything ${TAG}", "none", self._mapping()),
            "anything /openbench/",
        )


class SubstituteTests(unittest.TestCase):
    def test_prose_dollar_brace_left_intact(self) -> None:
        result = adapters.substitute("# see ${...} for details", {"TAG": "/t/"})
        self.assertEqual(result, "# see ${...} for details")

    def test_known_placeholder_substituted(self) -> None:
        self.assertEqual(adapters.substitute("a=${TAG}", {"TAG": "/t/"}), "a=/t/")

    def test_unknown_identifier_is_hard_error(self) -> None:
        with self.assertRaises(adapters.AdapterError):
            adapters.substitute("${NOPE}", {"TAG": "/t/"})


class RealTemplateRenderTests(unittest.TestCase):
    """Render the actual committed templates via the real registry, not inline fixtures.

    Guards against blockers (e.g. `${...}` in comments) the inline-template tests miss.
    """

    def setUp(self) -> None:
        self.registry = config.load_registry(_REPO_ROOT / "pools.yml")

    def _render(self, pool_name: str, profile_name: str = "validation") -> str:
        spec = self.registry.pool(pool_name)
        profile = self.registry.profile(profile_name)
        mapping = adapters.build_substitutions(
            spec, profile, self.registry.regtest, self.registry.pinning
        )
        assert spec.config.template is not None
        text = (self.registry.root / spec.config.template).read_text(encoding="utf-8")
        return adapters.render_config(text, spec.config.format, mapping)

    def test_pogolo_real_template_renders_to_valid_toml(self) -> None:
        parsed = tomllib.loads(self._render("pogolo"))
        self.assertEqual(parsed["backend"]["host"], "bitcoind:18443")
        self.assertEqual(parsed["pogolo"]["default_difficulty"], 0.001)  # low start
        self.assertIs(parsed["pogolo"]["disable_vardiff"], False)  # vardiff on by default
        self.assertIs(parsed["pogolo"]["ignore_suggested_difficulty"], False)

    def test_public_pool_real_template_renders_fully(self) -> None:
        rendered = self._render("public-pool")
        self.assertIn("NETWORK=regtest", rendered)
        self.assertIn("BITCOIN_RPC_URL=http://bitcoind", rendered)
        self.assertNotIn("${", rendered)

    def test_ckpool_real_template_renders_to_valid_json(self) -> None:
        parsed = json.loads(self._render("ckpool"))
        self.assertEqual(parsed["btcd"][0]["url"], "bitcoind:18443")
        self.assertEqual(parsed["serverurl"], ["0.0.0.0:3333"])
        self.assertLess(parsed["mindiff"], parsed["maxdiff"])  # vardiff has room to retarget
        self.assertEqual(parsed["maxdiff"], 1000000)

    def test_erikslund_real_template_enables_variable_difficulty(self) -> None:
        rendered = self._render("erikslund-cpp")
        self.assertIn("variable_difficulty: true", rendered)
        self.assertNotIn("${", rendered)

    def test_loadbalancer_real_template_targets_each_pool(self) -> None:
        lb = self.registry.loadbalancer
        assert lb is not None and lb.config.template is not None
        text = (self.registry.root / lb.config.template).read_text(encoding="utf-8")
        for pool in self.registry.pools.values():
            mapping = adapters.build_lb_substitutions(
                lb,
                pool,
                self.registry.profile("validation"),
                self.registry.regtest,
                self.registry.pinning,
            )
            parsed = yaml.safe_load(adapters.render_config(text, lb.config.format, mapping))
            backend = parsed["pools"][0]["backends"][0]
            self.assertEqual(backend["address"], f"{adapters.POOL_CONTAINER}:{pool.stratum_port}")
            self.assertIs(backend["send_proxy_v2"], False)  # pogolo/ckpool can't parse PROXY v2
            self.assertEqual(parsed["io_workers"], 2)  # one per lb_cpus core
            assert lb.readiness.url is not None
            self.assertEqual(
                adapters.substitute(lb.readiness.url, mapping),
                f"http://{adapters.LB_CONTAINER}:7778/healthz",
            )

    def test_readiness_urls_render(self) -> None:
        for spec in self.registry.pools.values():
            if spec.readiness.kind == "http":
                assert spec.readiness.url is not None
                mapping = adapters.build_substitutions(
                    spec,
                    self.registry.profile("validation"),
                    self.registry.regtest,
                    self.registry.pinning,
                )
                rendered = adapters.substitute(spec.readiness.url, mapping)
                self.assertNotIn("${", rendered)
