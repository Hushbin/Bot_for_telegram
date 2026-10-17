"""Validate panel uploads and produce a bounded, metadata-free Telegram photo."""
import io

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_PHOTO_BYTES = 5 * 1024 * 1024
MAX_PHOTO_PIXELS = 16_000_000


def prepare_photo(raw):
    if not raw or len(raw) > MAX_PHOTO_BYTES:
        raise ValueError('Choose a JPEG or PNG photo up to 5 MB.')
    try:
        with Image.open(io.BytesIO(raw), formats=('JPEG', 'PNG')) as image:
            if image.width * image.height > MAX_PHOTO_PIXELS:
                raise ValueError('Photo dimensions are too large. Use an image up to 16 megapixels.')
            image.verify()
        with Image.open(io.BytesIO(raw), formats=('JPEG', 'PNG')) as image:
            image.load()
            image = ImageOps.exif_transpose(image)
            image.thumbnail((2048, 2048))
            # Composite transparent PNGs on white; discard EXIF/GPS and other metadata.
            rgba = image.convert('RGBA')
            clean = Image.new('RGB', rgba.size, 'white')
            clean.paste(rgba, mask=rgba.getchannel('A'))
            output = io.BytesIO()
            clean.save(output, format='JPEG', quality=88)
            result = output.getvalue()
            if len(result) > MAX_PHOTO_BYTES:
                raise ValueError('This image is too complex. Try a smaller photo.')
            return result
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise ValueError('This file is not a valid JPEG or PNG photo.') from exc
