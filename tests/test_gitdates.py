"""fetch-dates against a fake transport: no network, no credentials."""

import json
from urllib.parse import parse_qs, urlparse

import pytest

from columntrace.gitdates import AdoClient, DateFetchError, fetch_dates, load_dates, save_dates


def _item(path, folder=True, commit="1234567890abcdef", date="2026-05-01T10:00:00Z"):
    d = {"path": path, "isFolder": folder}
    if commit:
        d["latestProcessedChange"] = {"commitId": commit, "committer": {"date": date, "name": "Someone"},
                                      "comment": "private message"}
    return d


def make_transport(calls):
    def transport(url, headers):
        q = parse_qs(urlparse(url).query)
        scope, level = q["scopePath"][0], q["recursionLevel"][0]
        calls.append((scope, level, headers["Authorization"]))
        assert q["latestProcessedChange"] == ["true"] and q["api-version"] == ["7.1"]
        if scope == "/":
            value = [_item("/"), _item("/sales"), _item("/root_nb.Notebook", commit="aaaaaaaa11112222"),
                     _item("/README.md", folder=False)]
        elif scope == "/sales":
            value = [_item("/sales"), _item("/sales/nb_a.Notebook", commit="bbbbbbbb11112222"),
                     _item("/sales/nb_a.Notebook/notebook-content.py", folder=False),
                     _item("/sales/nested"),
                     _item("/sales/nested/pl_b.DataPipeline", commit="cccccccc11112222", date="2025-01-02T00:00:00Z"),
                     _item("/sales/nested/pl_nodate.DataPipeline", commit=None)]
        else:
            raise AssertionError(scope)
        return 200, {}, json.dumps({"value": value})
    return transport


def test_fetch_collects_item_folders_at_any_depth_and_root():
    calls = []
    client = AdoClient("org", "proj", "repo", token="T", transport=make_transport(calls))
    doc = fetch_dates(client)
    assert set(doc["items"]) == {"root_nb.Notebook", "sales/nb_a.Notebook", "sales/nested/pl_b.DataPipeline"}
    assert doc["items"]["sales/nested/pl_b.DataPipeline"] == {"lastChange": "2025-01-02T00:00:00Z", "commit": "cccccccc"}
    assert ("/sales", "Full", "Bearer T") in calls and doc["apiCalls"] == 2


def test_no_author_or_message_is_kept(tmp_path):
    client = AdoClient("org", "proj", "repo", token="T", transport=make_transport([]))
    doc = fetch_dates(client)
    text = json.dumps(doc)
    assert "Someone" not in text and "private message" not in text
    out = tmp_path / "d.json"
    save_dates(doc, str(out))
    assert load_dates(str(out)) == doc["items"]


def test_folder_filter_limits_calls():
    calls = []
    client = AdoClient("org", "proj", "repo", token="T", transport=make_transport(calls))
    doc = fetch_dates(client, folders=["nothing"])
    assert doc["items"] == {} and len(calls) == 1      # only the root listing


def test_retries_on_429_then_succeeds():
    attempts = []

    def transport(url, headers):
        attempts.append(1)
        if len(attempts) < 3:
            return 429, {"Retry-After": "1"}, ""
        return 200, {}, json.dumps({"value": []})

    waited = []
    client = AdoClient("o", "p", "r", token="T", transport=transport, sleep=waited.append)
    assert client.items("/", "OneLevel") == []
    assert len(attempts) == 3 and waited == [1.0, 1.0]


def test_auth_failure_is_reported_clearly():
    client = AdoClient("o", "p", "r", token="bad", transport=lambda u, h: (401, {}, ""))
    with pytest.raises(DateFetchError, match="token rejected"):
        client.items("/", "OneLevel")


def test_gives_up_after_max_retries():
    client = AdoClient("o", "p", "r", token="T", transport=lambda u, h: (503, {}, ""),
                       sleep=lambda s: None, max_retries=2)
    with pytest.raises(DateFetchError, match="gave up"):
        client.items("/", "OneLevel")
