"""The pre-publish check finds private data and leaves synthetic placeholders alone."""

import importlib.util
import re
from pathlib import Path

spec = importlib.util.spec_from_file_location("check_public", Path(__file__).resolve().parents[1] / "tools" / "check_public.py")
check_public = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_public)


def write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_finds_ids_hosts_addresses_and_private_files(tmp_path):
    write(tmp_path, "src/a.py", "\n".join([
        'WS = "1a2b3c4d-1111-2222-3333-444455556666"',
        'HOST = "abcdefghijklmnop.datawarehouse.fabric.microsoft.com"',
        "# contact jane.doe@contoso.com",
        'P = "C:/Users/jane/Documents/x"',
        'URL = "https://dev.azure.com/contoso/_git/repo"',
    ]))
    write(tmp_path, "out.private.json", "{}")
    found = "\n".join(check_public.scan(tmp_path, extra=[]))
    for label in ("GUID", "SQL endpoint", "e-mail", "home folder", "Azure DevOps", "private file"):
        assert label in found, (label, found)


def test_synthetic_placeholders_and_ignored_files_pass(tmp_path):
    write(tmp_path, "src/a.py", "\n".join([
        'ID = "00000000-0000-0000-0000-000000000001"',
        'HOST = "<id>.datawarehouse.fabric.microsoft.com"',
        'PATH = "abfss://ws@onelake.dfs.fabric.microsoft.com/lh.Lakehouse/Tables/x"',
        'ORG = "https://dev.azure.com/<org>"',
        "AUDIENCE = '499b84ac-1321-427f-aa17-267ca6975798'",
    ]))
    write(tmp_path, ".gitignore", "*.private.json\ndata/\n")
    write(tmp_path, "x.private.json", '{"secret": "jane.doe@contoso.com"}')
    write(tmp_path, "data/real.json", '{"secret": "jane.doe@contoso.com"}')
    assert check_public.scan(tmp_path, extra=[]) == []


def test_local_patterns_come_from_an_ignored_file(tmp_path):
    write(tmp_path, ".gitignore", ".publiccheck.local\n")
    write(tmp_path, ".publiccheck.local", "# private words\ncontoso\n")
    write(tmp_path, "README.md", "Built for Contoso Ltd.")
    assert [p.pattern for p in check_public.local_patterns(tmp_path)] == ["contoso"]
    found = check_public.scan(tmp_path)
    assert len(found) == 1 and "local pattern" in found[0]
    assert not re.search("Contoso", found[0])            # the match itself is masked in the report
