from app.utils.helpers import md_to_plain_text

examples = [
    r"Tom\\_Torero\\_Word\\_Tour\\_Day\\_24\\_Powder\\_Day\\_1080p\\_30fps\\_H264\\_128kbit.mp4",
    r"Tom\\_Torero Conversation King \\[Br2KX8jnAMU\\].mp4",
    r"1) [Sample video](https://t.me/c/12345/1001)",
]

for s in examples:
    print("ORIG:", s)
    print("PLAIN:", md_to_plain_text(s))
    print()
