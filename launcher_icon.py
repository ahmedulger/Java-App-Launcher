from pathlib import Path

from PIL import Image, ImageDraw


def create_run_icon():
    scale = 4
    image = Image.new("RGBA", (256 * scale, 256 * scale), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.polygon([(64 * scale, 27 * scale), (222 * scale, 128 * scale),
                  (64 * scale, 229 * scale)], fill="#25b86a")
    return image.resize((256, 256), Image.Resampling.LANCZOS)


def write_run_icon(directory):
    directory = Path(directory)
    image = create_run_icon()
    image.save(directory / "app_icon.png")
    image.save(directory / "app_icon.ico", sizes=[(16, 16), (24, 24), (32, 32),
                                                (48, 48), (64, 64), (128, 128), (256, 256)])


if __name__ == "__main__":
    write_run_icon(Path(__file__).parent)