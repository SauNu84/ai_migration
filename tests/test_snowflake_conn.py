"""SAU-308 — env-resolved Snowflake config: required vars, auth modes, no secret leak."""
import os
import tempfile
import unittest
from pathlib import Path

from core.snowflake_conn import SnowflakeConfig, SnowflakeConfigError, load_dotenv

_BASE = {
    "SNOWFLAKE_ACCOUNT": "ab12345.eu-central-1",
    "SNOWFLAKE_USER": "MIGRATION_SVC",
    "SNOWFLAKE_PASSWORD": "s3cr3t-value",
}


class TestSnowflakeConfig(unittest.TestCase):
    def test_missing_required_lists_all(self):
        with self.assertRaises(SnowflakeConfigError) as ctx:
            SnowflakeConfig.from_env({})
        msg = str(ctx.exception)
        self.assertIn("SNOWFLAKE_ACCOUNT", msg)
        self.assertIn("SNOWFLAKE_USER", msg)
        self.assertIn("SNOWFLAKE_PASSWORD", msg)

    def test_password_auth_resolves(self):
        cfg = SnowflakeConfig.from_env(dict(_BASE, SNOWFLAKE_WAREHOUSE="MIGRATION_WH"))
        self.assertEqual(cfg.account, "ab12345.eu-central-1")
        self.assertEqual(cfg.warehouse, "MIGRATION_WH")
        kw = cfg.connect_kwargs()
        self.assertEqual(kw["password"], "s3cr3t-value")
        self.assertEqual(kw["warehouse"], "MIGRATION_WH")

    def test_key_pair_satisfies_auth_requirement(self):
        env = {"SNOWFLAKE_ACCOUNT": "a", "SNOWFLAKE_USER": "u",
               "SNOWFLAKE_PRIVATE_KEY_PATH": "/run/secrets/key.p8"}
        cfg = SnowflakeConfig.from_env(env)  # no password, but key path present
        self.assertEqual(cfg.private_key_path, "/run/secrets/key.p8")

    def test_no_auth_is_an_error(self):
        with self.assertRaises(SnowflakeConfigError) as ctx:
            SnowflakeConfig.from_env({"SNOWFLAKE_ACCOUNT": "a", "SNOWFLAKE_USER": "u"})
        self.assertIn("SNOWFLAKE_PASSWORD", str(ctx.exception))

    def test_blank_values_treated_as_missing(self):
        with self.assertRaises(SnowflakeConfigError):
            SnowflakeConfig.from_env(dict(_BASE, SNOWFLAKE_ACCOUNT="   "))

    def test_secret_never_in_repr_or_redacted(self):
        cfg = SnowflakeConfig.from_env(_BASE)
        self.assertNotIn("s3cr3t-value", repr(cfg))
        self.assertNotIn("s3cr3t-value", str(cfg.redacted()))
        self.assertEqual(cfg.redacted()["auth"], "password")


class TestDotenvLoader(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ[k] for k in list(os.environ) if k.startswith("SNOWFLAKE_")}
        for k in self._saved:
            del os.environ[k]

    def tearDown(self):
        for k in [k for k in os.environ if k.startswith("SNOWFLAKE_")]:
            del os.environ[k]
        os.environ.update(self._saved)

    def _write(self, text: str) -> str:
        fd, path = tempfile.mkstemp(suffix=".env")
        os.write(fd, text.encode()); os.close(fd)
        self.addCleanup(lambda: Path(path).unlink(missing_ok=True))
        return path

    def test_loads_keys_and_strips_quotes_comments_export(self):
        path = self._write(
            "# comment\n\nexport SNOWFLAKE_ACCOUNT=\"ab12345.eu-central-1\"\n"
            "SNOWFLAKE_USER='svc'\nSNOWFLAKE_PASSWORD=p@ss=word\n")
        keys = load_dotenv(path)
        self.assertEqual(set(keys),
                         {"SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_PASSWORD"})
        self.assertEqual(os.environ["SNOWFLAKE_ACCOUNT"], "ab12345.eu-central-1")
        self.assertEqual(os.environ["SNOWFLAKE_USER"], "svc")
        self.assertEqual(os.environ["SNOWFLAKE_PASSWORD"], "p@ss=word")  # value '=' kept

    def test_real_env_wins_over_file(self):
        os.environ["SNOWFLAKE_ACCOUNT"] = "from-env"
        path = self._write("SNOWFLAKE_ACCOUNT=from-file\n")
        load_dotenv(path)
        self.assertEqual(os.environ["SNOWFLAKE_ACCOUNT"], "from-env")

    def test_missing_file_is_noop(self):
        self.assertEqual(load_dotenv("/no/such/file.env"), [])


if __name__ == "__main__":
    unittest.main()
