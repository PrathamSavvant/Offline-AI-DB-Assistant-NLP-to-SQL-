from core.business_dictionary import BusinessDictionaryStore


def test_rich_entry_persists_and_resolves_in_both_directions(tmp_path):
    store = BusinessDictionaryStore(tmp_path / "dictionary.json", seed_if_empty=False)
    store.set(
        "CC", "Central Command", synonyms=["HQ", "Headquarters"],
        aliases=["Central Office"], description="Primary command site", example="CC-01",
    )

    reloaded = BusinessDictionaryStore(tmp_path / "dictionary.json", seed_if_empty=False)
    assert reloaded.lookup("employees in Central Office") == ["CC"]
    assert reloaded.lookup("HQ") == ["CC"]
    assert reloaded.resolve_text("Central Command")[0]["token"] == "CC"
    assert reloaded.get("CC")["example"] == "CC-01"


def test_search_includes_aliases(tmp_path):
    store = BusinessDictionaryStore(tmp_path / "dictionary.json", seed_if_empty=False)
    store.set("REF_NO", "Reference Number", aliases=["Ref No"])
    assert [entry["abbreviation"] for entry in store.search("ref no")] == ["REF_NO"]
