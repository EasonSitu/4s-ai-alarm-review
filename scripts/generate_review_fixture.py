"""Create a synthetic image fixture locally; no real photographs are needed."""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

root = Path(__file__).resolve().parents[1]
image = Image.new("RGB", (960, 540), "#142e27")
draw = ImageDraw.Draw(image)
large = ImageFont.load_default(size=70)
small = ImageFont.load_default(size=28)
draw.text((45, 105), "TEST IMAGE", font=large, fill="white")
draw.text((48, 245), "NO SITE PHOTO", font=small, fill="#d4e5d3")
for i, color in enumerate(("#d2e7bf", "#96baa3", "#557f6c", "#e8c77f", "#b27956", "#435e60")):
    draw.rectangle((50 + i * 143, 340, 190 + i * 143, 455), fill=color)
path = root / "examples" / "test-card.png"
path.parent.mkdir(parents=True, exist_ok=True)
image.save(path, "PNG")
print("Generated synthetic fixture:", path)
