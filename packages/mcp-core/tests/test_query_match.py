import pytest
from mcp_core.query_match import names_query


@pytest.mark.parametrize(
    ("query", "title"),
    [
        ("холодильник atlant", "Холодильник ATLANT ХМ 4012-022"),
        ("часы", "Смарт-часы Realme Watch 5"),
        ("видеокарты", "Видеокарта GIGABYTE RTX 5070"),
        ("ёлка", "Елка искусственная 180 см"),
        ("5070", "Видеокарта RTX5070 12 ГБ"),
    ],
)
def test_a_title_that_starts_a_word_with_the_query_names_it(query, title):
    assert names_query(query, [title])


@pytest.mark.parametrize(
    ("query", "title"),
    [
        ("бра", "Абразивный круг для шлифовки"),
        ("фен", "Дефендер для розетки"),
        ("абырвалгщзх", "ARZOPA M1RC игровой монитор 27 дюймов 2K QHD"),
    ],
)
def test_a_query_inside_another_word_does_not_name_it(query, title):
    assert not names_query(query, [title])


def test_any_haystack_is_enough():
    assert names_query("видеокарта", ["GIGABYTE GeForce RTX 5070 EAGLE", "Видеокарты"])
