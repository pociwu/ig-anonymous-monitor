"""Compare current-account avatar content independently of its encoded file hash.

Never delete history or scan other accounts. Ambiguous comparisons retain the
normal change notification rather than silently hiding a possible new avatar.
"""
from io import BytesIO
from pathlib import Path
import warnings

import imagehash
from PIL import Image, ImageChops, ImageOps, ImageStat

from .models import ProfileSnapshot
from .utils import sha256_bytes


MAX_BYTES = 20 * 1024 * 1024
MAX_PIXELS = 4_000_000


def _load(snapshot: ProfileSnapshot):
    if not snapshot.avatar_path or not snapshot.avatar_sha256:
        raise ValueError("Missing avatar evidence")
    with Path(snapshot.avatar_path).open("rb") as stream:
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES or sha256_bytes(data) != snapshot.avatar_sha256:
        raise ValueError("Unverified avatar file")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(BytesIO(data)) as image:
            if image.width * image.height > MAX_PIXELS or getattr(image, "n_frames", 1) != 1:
                raise ValueError("Unsupported avatar dimensions or animation")
            image = ImageOps.exif_transpose(image)
            rgba = image.convert("RGBA")
            background = Image.new("RGBA", rgba.size, "white")
            background.alpha_composite(rgba)
            rgb = background.convert("RGB")
    return rgb, (rgb.width * rgb.height, len(data))


def _same_content(first: Image.Image, second: Image.Image) -> bool:
    ratio_a, ratio_b = first.width / first.height, second.width / second.height
    if abs(ratio_a - ratio_b) / max(ratio_a, ratio_b) > 0.01:
        return False
    if imagehash.phash(first) - imagehash.phash(second) > 4:
        return False
    # A perceptual hash alone can miss color changes or small overlays. Compare
    # aligned RGB pixels too; do not crop, align faces, or match arbitrary regions.
    first = first.resize((64, 64), Image.Resampling.LANCZOS)
    second = second.resize((64, 64), Image.Resampling.LANCZOS)
    difference = ImageChops.difference(first, second)
    if max(ImageStat.Stat(difference).mean) > 4:
        return False
    for y in range(0, 64, 8):
        for x in range(0, 64, 8):
            if max(ImageStat.Stat(difference.crop((x, y, x + 8, y + 8))).mean) > 12:
                return False
    return True


def reconcile_avatar(old: ProfileSnapshot | None, new: ProfileSnapshot) -> bool:
    """Select the best matching file; return True only for equivalent content.

    SHA-256 always describes the selected actual bytes, including after an
    upgrade. Callers suppress only the avatar change event, not other fields.
    Pixel area then encoded size are conservative quality proxies; ties keep old.
    """
    if old is None or not old.avatar_sha256 or not new.avatar_sha256:
        return False
    if old.avatar_sha256 == new.avatar_sha256:
        return True
    try:
        before, old_rank = _load(old)
        after, new_rank = _load(new)
        if not _same_content(before, after):
            return False
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        return False
    if old_rank >= new_rank:
        new.avatar_sha256 = old.avatar_sha256
        new.avatar_path = old.avatar_path
    return True
