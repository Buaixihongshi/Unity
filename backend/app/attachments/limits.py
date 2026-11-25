MAX_IMAGE_BYTES = 3_932_160
MAX_EDGE = 8000
MAX_PIXELS = 40_000_000
MAX_MESSAGE_IMAGES = 4
MAX_MISSION_IMAGES = 8
MAX_MISSION_ENCODED_BYTES = 16_777_216
MIME_EXTENSIONS = {'image/png': 'png', 'image/jpeg': 'jpg', 'image/webp': 'webp', 'image/gif': 'gif'}


def encoded_size(size):
    return 4 * ((size + 2) // 3)


def public_limits():
    return dict(limit_bytes=MAX_IMAGE_BYTES, max_edge=MAX_EDGE, max_pixels=MAX_PIXELS,
                max_message_images=MAX_MESSAGE_IMAGES, max_mission_images=MAX_MISSION_IMAGES,
                max_mission_encoded_bytes=MAX_MISSION_ENCODED_BYTES, mime_types=list(MIME_EXTENSIONS))
