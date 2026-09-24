"""Test suite (standard library unittest): `python -m unittest discover -s tests -t .`

Importing this package points the app at a throwaway SQLite database, so tests never touch a real one. Network
access is always faked; nothing here calls Codeforces, AtCoder or Kilonova.
"""
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="squadforces-tests-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_TMP, "test.db").replace("\\", "/")
os.environ["ADMIN_PASSWORD"] = "test-admin-pw"
