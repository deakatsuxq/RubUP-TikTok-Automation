import pytest

from validators import ValidationError, canonical_tiktok_url, contains_rubup, normalize_comment, validate_comment


@pytest.mark.parametrize("word", ["RubUP", "@RubUP", "rubup", "РУБАП", "рубапе", "рубапом", "рубапа", "рубапу", "рубапами"])
def test_real_brand_forms(word):
    assert validate_comment(f"что там в {word}?")


@pytest.mark.parametrize("word", ["myRubUP", "RubUPx", "рубапушка", "суперрубап", "рубап123", "рубап_бот"])
def test_brand_inside_other_words_rejected(word):
    assert not contains_rubup(word)
    with pytest.raises(ValidationError):
        validate_comment(f"интересно {word}")


def test_length_and_duplicate_punctuation_casefold():
    assert normalize_comment("  Реклама: RubUP! ") == "реклама rubup"
    with pytest.raises(ValidationError, match="использовался"):
        validate_comment("Реклама, RubUP?", recent_comments=["реклама: rubup!"])
    with pytest.raises(ValidationError, match="длиннее"):
        validate_comment("RubUP " + "а" * 100)


def test_disclosure_required_only_in_advertising_mode():
    assert validate_comment("интересно, RubUP", advertising=False)
    with pytest.raises(ValidationError, match="рекламы"):
        validate_comment("интересно, RubUP", advertising=True)
    assert validate_comment("реклама: что там в RubUP?", advertising=True)
    with pytest.raises(ValidationError):
        validate_comment("суперреклама, RubUP", advertising=True)


@pytest.mark.parametrize("url", [
    "http://www.tiktok.com/@a/video/123",
    "https://www.tiktok.com.evil.org/@a/video/123",
    "https://www.tiktok.com@evil.org/@a/video/123",
    "https://user:password@www.tiktok.com/@a/video/123",
    "https://www.tiktok.com:443/@a/video/123",
    "https://vm.tiktok.com/hello/",
    "https://www.tiktok.com/@a/video/123/other",
    "https://www.tiktok.com/@a/video/123\nhttps://evil.org",
    "https://www.tiktok.com/%40a/video/123",
    "https://www.tiktok.com/@a/video/notanumber",
    "https://www.tiktok.com/../../@a/video/123",
])
def test_bad_urls_do_not_resolve_or_get_accepted(url):
    with pytest.raises(ValidationError):
        canonical_tiktok_url(url)


def test_url_tracking_removed_and_id_retained():
    assert canonical_tiktok_url("https://tiktok.com/@user.name/video/123/?lang=ru#thing") == (
        "https://www.tiktok.com/@user.name/video/123", "123")
