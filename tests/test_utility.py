import io

import pytest
from PIL import Image

from kevin.cogs.utility import image_to_gif, safe_calculate


def test_safe_calculator() -> None:
    assert safe_calculate("2 + 3 * 4") == 14
    assert safe_calculate("(10 - 2) / 4") == 2


@pytest.mark.parametrize("expression", ["__import__('os')", "2 ** 100", "[1, 2]", "x + 1"])
def test_calculator_rejects_unsafe_expressions(expression: str) -> None:
    with pytest.raises(ValueError):
        safe_calculate(expression)


def make_image_bytes(format_name: str = "PNG") -> bytes:
    data = io.BytesIO()
    Image.new("RGBA", (12, 8), (255, 0, 0, 128)).save(data, format=format_name)
    return data.getvalue()


def test_image_to_gif_converts_an_uploaded_image() -> None:
    result = image_to_gif(make_image_bytes())

    assert result.tell() == 0
    with Image.open(result) as converted:
        assert converted.format == "GIF"
        assert converted.size == (12, 8)


def test_image_to_gif_rejects_non_images() -> None:
    with pytest.raises(ValueError, match="not a supported image"):
        image_to_gif(b"definitely not an image")


def test_image_to_gif_rejects_excessive_dimensions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("kevin.cogs.utility.MAX_IMAGE_PIXELS", 50)

    with pytest.raises(ValueError, match="maximum 16 megapixels"):
        image_to_gif(make_image_bytes())
