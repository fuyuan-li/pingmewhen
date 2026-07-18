from relay_agent.names import normalize_display_name


def test_display_name_normalizes_simple_lowercase_names_without_changing_existing_case():
    assert normalize_display_name(" tom ") == "Tom"
    assert normalize_display_name("mary jane") == "Mary Jane"
    assert normalize_display_name("AJ") == "AJ"
    assert normalize_display_name("e.e. cummings") == "e.e. cummings"
