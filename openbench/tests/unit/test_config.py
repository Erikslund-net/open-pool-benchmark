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
