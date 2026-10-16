"""Unit tests for the pools.yml loader and validation."""

from __future__ import annotations

from openbench import config
from openbench.tests import base


class LoadRegistryTests(base.TempDirTestCase):
    def test_valid_registry_parses(self) -> None:
        registry = config.load_registry(self.write_registry(base.VALID_REGISTRY_YAML))
        self.assertEqual(sorted(registry.pools), ["disabled-pool", "pogolo"])
        self.assertEqual(registry.enabled_pools(), ["pogolo"])
        self.assertEqual(registry.regtest.rpc_user, "openbench")
        self.assertEqual(registry.profile("validation").difficulty, 1000000.0)

    def test_pool_spec_fields(self) -> None:
        registry = config.load_registry(self.write_registry(base.VALID_REGISTRY_YAML))
        pogolo = registry.pool("pogolo")
        self.assertEqual(pogolo.source, "image")
        self.assertEqual(pogolo.stratum_port, 5661)
        self.assertEqual(pogolo.api_port, 5662)
        self.assertEqual(pogolo.config.format, "toml")
        self.assertEqual(pogolo.readiness.kind, "http")

    def test_root_is_registry_parent(self) -> None:
        path = self.write_registry(base.VALID_REGISTRY_YAML)
        registry = config.load_registry(path)
        self.assertEqual(registry.root, path.parent.resolve())

    def test_unknown_pool_lists_known(self) -> None:
        registry = config.load_registry(self.write_registry(base.VALID_REGISTRY_YAML))
        with self.assertRaises(config.ConfigError) as exc:
            registry.pool("nope")
        self.assertIn("pogolo", str(exc.exception))


class PinningTests(base.TempDirTestCase):
    def test_default_pinning_pins_one_core_each(self) -> None:
        registry = config.load_registry(self.write_registry(base.VALID_REGISTRY_YAML))
        self.assertTrue(registry.pinning.enabled)
        self.assertEqual(registry.pinning.bitcoind_cpus, "0")
        self.assertEqual(registry.pinning.pool_cpus, "1")
        self.assertEqual(registry.pinning.bench_cpus, "2")

    def test_explicit_pinning_overrides_defaults(self) -> None:
        text = base.VALID_REGISTRY_YAML + "\npinning:\n  enabled: false\n  pool_cpus: '4-7'\n"
        registry = config.load_registry(self.write_registry(text))
        self.assertFalse(registry.pinning.enabled)
        self.assertEqual(registry.pinning.pool_cpus, "4-7")
        self.assertEqual(registry.pinning.bitcoind_cpus, "0")

    def test_bitcoind_sharing_a_pool_core_raises(self) -> None:
        with self.assertRaises(config.ConfigError) as exc:
            base.sample_pinning(bitcoind_cpus="0-1", pool_cpus="1-4", bench_cpus="5")
        self.assertIn("on 1;", str(exc.exception))

    def test_bitcoind_sharing_a_bench_core_raises(self) -> None:
        with self.assertRaises(config.ConfigError):
            base.sample_pinning(bitcoind_cpus="0-1", pool_cpus="2", bench_cpus="1-3")

    def test_overlap_is_allowed_when_pinning_is_disabled(self) -> None:
        pinning = base.sample_pinning(enabled=False, bitcoind_cpus="0-1", pool_cpus="1")
        self.assertFalse(pinning.enabled)

    def test_lb_cpus_parse_and_default_empty(self) -> None:
        registry = config.load_registry(self.write_registry(base.VALID_REGISTRY_YAML))
        self.assertEqual(registry.pinning.lb_cpus, "")
        text = base.VALID_REGISTRY_YAML + "\npinning:\n  lb_cpus: '14-15'\n"
        self.assertEqual(config.load_registry(self.write_registry(text)).pinning.lb_cpus, "14-15")

    def test_lb_pinning_needs_lb_cpus(self) -> None:
        with self.assertRaises(config.ConfigError):
            config.check_lb_pinning(base.sample_pinning())

    def test_lb_pinning_rejects_shared_cores(self) -> None:
        with self.assertRaises(config.ConfigError) as exc:
            config.check_lb_pinning(base.sample_pinning(bench_cpus="2-15", lb_cpus="14-15"))
        self.assertIn("on 14, 15;", str(exc.exception))

    def test_lb_pinning_accepts_own_cores_or_no_pinning(self) -> None:
        config.check_lb_pinning(base.sample_pinning(bench_cpus="2-13", lb_cpus="14-15"))
        config.check_lb_pinning(base.sample_pinning(enabled=False))


class LoadBalancerTests(base.TempDirTestCase):
    _LB_YAML = """
loadbalancer:
  description: test lb
  source: build
  build_context: ../erikslund-pool-lb
  stratum_port: 3333
  api_port: 7778
  config:
    format: yaml
    template: pools/erikslund-pool-lb/bench.yml
    mount: /etc/erikslund-pool-lb/pool-lb.yml
  readiness:
    kind: http
    url: http://${POOL_HOST}:${API_PORT}/healthz
"""

    def test_loadbalancer_is_optional(self) -> None:
        registry = config.load_registry(self.write_registry(base.VALID_REGISTRY_YAML))
        self.assertIsNone(registry.loadbalancer)
        self.assertFalse(registry.via_lb)

    def test_loadbalancer_parses_like_a_pool(self) -> None:
        registry = config.load_registry(
            self.write_registry(base.VALID_REGISTRY_YAML + self._LB_YAML)
        )
        lb = registry.loadbalancer
        assert lb is not None
        self.assertEqual(
            (lb.name, lb.source, lb.stratum_port, lb.api_port),
            ("loadbalancer", "build", 3333, 7778),
        )
        self.assertNotIn("loadbalancer", registry.pools)  # not a pool under test

    def test_invalid_loadbalancer_names_its_section(self) -> None:
        text = base.VALID_REGISTRY_YAML + "\nloadbalancer:\n  source: build\n"
        with self.assertRaises(config.ConfigError) as exc:
            config.load_registry(self.write_registry(text))
        self.assertIn("loadbalancer", str(exc.exception))


class AdversarialRegistryTests(base.TempDirTestCase):
    def test_missing_file_raises(self) -> None:
        with self.assertRaises(config.ConfigError):
            config.load_registry(self.make_tempdir() / "absent.yml")

    def test_top_level_must_be_mapping(self) -> None:
        with self.assertRaises(config.ConfigError):
            config.load_registry(self.write_registry("- just\n- a\n- list\n"))

    def test_invalid_source_rejected(self) -> None:
        text = base.VALID_REGISTRY_YAML.replace("source: image", "source: kubernetes")
        with self.assertRaises(config.ConfigError) as exc:
            config.load_registry(self.write_registry(text))
        self.assertIn("source", str(exc.exception))

    def test_image_source_requires_image(self) -> None:
        text = """
regtest: {address: a}
profiles: {p: {difficulty: 1}}
pools:
  x:
    source: image
    stratum_port: 1
    config: {format: none}
    readiness: {kind: tcp}
"""
        with self.assertRaises(config.ConfigError) as exc:
            config.load_registry(self.write_registry(text))
        self.assertIn("image", str(exc.exception))

    def test_http_readiness_requires_url(self) -> None:
        text = """
regtest: {address: a}
profiles: {p: {difficulty: 1}}
pools:
  x:
    source: image
    image: i
    stratum_port: 1
    config: {format: none}
    readiness: {kind: http}
"""
        with self.assertRaises(config.ConfigError) as exc:
            config.load_registry(self.write_registry(text))
        self.assertIn("url", str(exc.exception))

    def test_config_template_format_requires_template(self) -> None:
        text = """
regtest: {address: a}
profiles: {p: {difficulty: 1}}
pools:
  x:
    source: image
    image: i
    stratum_port: 1
    config: {format: toml}
    readiness: {kind: tcp}
"""
        with self.assertRaises(config.ConfigError) as exc:
            config.load_registry(self.write_registry(text))
        self.assertIn("template", str(exc.exception))

    def test_regtest_requires_address(self) -> None:
        text = """
regtest: {rpc_user: u}
profiles: {p: {difficulty: 1}}
pools:
  x: {source: image, image: i, stratum_port: 1, config: {format: none}, readiness: {kind: tcp}}
"""
        with self.assertRaises(config.ConfigError) as exc:
            config.load_registry(self.write_registry(text))
        self.assertIn("address", str(exc.exception))
