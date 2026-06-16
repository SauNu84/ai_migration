"""SAU-308 — env-resolved Snowflake config: required vars, auth modes, no secret leak."""
import unittest

from core.snowflake_conn import SnowflakeConfig, SnowflakeConfigError

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


if __name__ == "__main__":
    unittest.main()
