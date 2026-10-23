import pytest

from comment_similarity import comments_too_similar


@pytest.mark.parametrize("first,second", [
    ("реклама: RubUP, какой скин выбрать?", "РЕКЛАМА: @RUBUP — какой скин выбрать!"),
    ("реклама: в рубапе какой скин выбрать?", "RubUP, выбрать какой скин? реклама"),
    ("реклама: RubUP, какой скин выбрать 101?", "реклама: RubUP, какой скин выбрать 102?"),
    ("RubUP, какой скин выбрать, аккаунт №1", "RubUP, какой скин выбрать, аккаунт №2"),
    ("RubUP, какой скин выбрать @alice", "RubUP, какой скин выбрать @bob"),
    ("RubUP, какой скин выбрать?", "рубап, какие скины брать?"),
    ("RubUP, какой скин выбрать?", "RubUP, какой же скин выбрать?"),
    ("RubUP", "реклама: @RubUP"),
    ("тест: RubUP, обсудим ролик 123", "тест: рубап, обсудить видео 456"),
])
def test_templates_cannot_be_made_unique_by_brand_position_or_identifiers(first, second):
    assert comments_too_similar(first, second)
    assert comments_too_similar(second, first)


@pytest.mark.parametrize("first,second", [
    ("реклама: RubUP, какой скин выбрать?", "реклама: в рубапе есть что обсудить?"),
    ("реклама: что думаете о RubUP?", "реклама: в рубапе какой кейс интереснее?"),
    ("RubUP, обсуждаем подарки?", "RubUP, а какой момент со стрима лучший?"),
    ("RubUP, топовый скин", "RubUP, забавный ролик"),
    ("RubUP", "RubUP, кто смотрел стрим?"),
    ("RubUP, выбор скина для cs2?", "RubUP, новый предмет для dota2?"),
])
def test_distinct_short_comments_are_not_rejected(first, second):
    assert not comments_too_similar(first, second)
    assert not comments_too_similar(second, first)
