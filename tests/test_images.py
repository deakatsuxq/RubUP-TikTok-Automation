import hashlib
from pathlib import Path
import pytest
from PIL import Image
from image_manager import ImageManager


def make_image(tmp_path, slot=1, extension=".png"):
    path = tmp_path / f"original_{slot}{extension}"
    Image.new("RGB", (13 + slot, 17 + slot), (slot * 50, 40, 80)).save(path)
    return path


def test_four_exact_originals_and_persistent_rotation(tmp_path):
    manager = ImageManager(tmp_path / "images")
    for slot in (1, 2, 3, 4):
        original = make_image(tmp_path, slot)
        result = manager.set_image(slot, original)
        assert Path(result["path"]).read_bytes() == original.read_bytes()
        assert result["sha256"] == hashlib.sha256(original.read_bytes()).hexdigest()
        assert manager.verify(slot)
    assert manager.select("rotate", order=[3, 1, 4, 2])["slot"] == 3
    manager = ImageManager(tmp_path / "images")
    assert manager.select("rotate", order=[3, 1, 4, 2])["slot"] == 1
    assert manager.select("none") is None
    assert manager.select("specific", specific=4)["slot"] == 4
    assert manager.select("task", required_slot=2)["slot"] == 2
    manager.record_usage(2, 17)
    manager.record_usage(2, 17)
    assert len(ImageManager(tmp_path / "images").usage_history()) == 1


def test_corruption_and_missing_images_are_blocked(tmp_path):
    manager = ImageManager(tmp_path / "images")
    result = manager.set_image(1, make_image(tmp_path))
    with pytest.raises(ValueError, match="все четыре"):
        manager.select("rotate")
    Path(result["path"]).write_bytes(b"corrupt")
    assert not manager.verify(1)
    with pytest.raises(ValueError):
        manager.select("specific")


def test_invalid_original_does_not_replace_previous(tmp_path):
    manager = ImageManager(tmp_path / "images")
    result = manager.set_image(1, make_image(tmp_path))
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"not an image")
    with pytest.raises(ValueError):
        manager.set_image(1, bad)
    assert manager.verify(1)
    assert manager.get_image(1) == result


def test_replacing_slot_preserves_previous_original_and_history(tmp_path):
    manager = ImageManager(tmp_path / "images")
    first = make_image(tmp_path, 1)
    first_bytes = first.read_bytes()
    initial = manager.set_image(1, first)
    manager.record_usage(1, 19)
    replacement = make_image(tmp_path, 2, ".jpg")
    updated = manager.set_image(1, replacement)
    assert initial["sha256"] != updated["sha256"]
    archive = tmp_path / "images" / "archive" / (initial["sha256"] + ".png")
    assert archive.read_bytes() == first_bytes
    assert first.read_bytes() == first_bytes
    assert Path(updated["path"]).read_bytes() == replacement.read_bytes()
    manager.record_usage(1, 19)
    assert len(manager.usage_history()) == 2
    assert manager.usage_history()[0]["sha256"] == initial["sha256"]


def test_corrupt_manifest_prevents_overwriting_original(tmp_path):
    manager = ImageManager(tmp_path / "images")
    initial = manager.set_image(1, make_image(tmp_path, 1))
    original_bytes = Path(initial["path"]).read_bytes()
    manager.manifest_path.write_text("{not-json}")
    with pytest.raises(ValueError, match="журнал"):
        manager.set_image(1, make_image(tmp_path, 2))
    assert Path(initial["path"]).read_bytes() == original_bytes
