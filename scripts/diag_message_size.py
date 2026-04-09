#!/usr/bin/env python3
import os
import sys

try:
    from app.api import md_to_markdown, _escape_markdown
    from app.config.settings import settings
except Exception as e:
    print('Import failed:', e)
    raise

filenames = [
    "Tom Torero Books.mp4",
    "Tom Torero Hotline.mp4",
    "Tom Torero - Slovenia.mp4",
    "Tom Torero Video Vault.mp4",
    "Tom Torero - Beta Bait.mp4",
    "Tom Torero Gets Angry!.mp4",
    "Tom Torero's Vibe Juice.mp4",
    "Tom Torero- Awe & Wonder.mp4",
    "Tom Torero Falls In Love.mp4",
    "Tom Torero's Photo Pings.mp4",
    "Tom Torero's Texting Tips.mp4",
    "Tom Torero  Winter Daygame.mp4",
    "Tom Torero - Daygame Intel.mp4",
    "Tom Torero - Street Hustle.mp4",
    "Tom Torero - Daygame travel.mp4",
    "Tom Torero - Daygame is hard.mp4",
    "Tom Torero - Empty your mind.mp4",
    "Tom Torero- Stranded In Miami.mp4",
    "Tom Torero's Ten Commandments.mp4",
    "Tom Torero - Beyond the number.mp4",
    "Tom Torero - Daygame dropout rate.mp4",
    "Tom Torero - False time constraint.mp4",
    "Tom Torero - Daygame Misconceptions.mp4",
    "Tom Torero - How To Flirt With Girls.pdf",
    "Tom Torero - Saturday Sarge - Part 1.mp4",
    "Tom Torero - How To Flirt With Girls.epub",
    "Tom Torero On How To Keep Daygame Real.mp4",
    "Tom Torero Post-SDL Interview (Infield).mp4",
    "Tom Torero's Story   Daygame As Therapy.mp4",
    "Tom Torero Same Day Lay (Daygame Infield).mp4",
    "Tom Torero Conversation King \\[Br2KX8jnAMU\\].mp4",
    "Tom Torero Podcast #037 - Conversation Ninja.mp4",
    "Tom Torero - Difficulties of day gaming abroad.mp4",
    "Tom Torero's Explains Origin Of Game - Mystery Method.mp4",
    "Tom Torero Word Tour Day 7 (1080p_30fps_H264-128kbit_AAC).mp4",
    "Tom Torero World Tour Day 1 (720p_60fps_H264-128kbit_AAC).mp4",
    "Tom Torero World Tour Day 11 (720p_30fps_H264-192kbit_AAC).mp4",
    "Tom Torero World Tour Day 9 (1080p_30fps_H264-128kbit_AAC).mp4",
    "Tom Torero World Tour Day 5 (1080p_30fps_H264-128kbit_AAC).mp4",
    "Tom Torero World Tour Day 4 (1080p_30fps_H264-128kbit_AAC).mp4",
    "Tom Torero World Tour Day 2 (1080p_30fps_H264-128kbit_AAC).mp4",
    "Tom Torero World Tour Day 27 (1080p_30fps_H264-128kbit_AAC).mp4",
    "Tom Torero World Tour Day 26 (1080p_30fps_H264-128kbit_AAC).mp4",
    "Tom Torero World Tour Day 22 (1080p_30fps_H264-128kbit_AAC).mp4",
    "Tom Torero World Tour Day 21 (1080p_30fps_H264-128kbit_AAC).mp4",
    "Tom Torero World Tour Day 20 (1080p_30fps_H264-128kbit_AAC).mp4",
    "Tom Torero World Tour Day 18 (1080p_30fps_H264-128kbit_AAC).mp4",
    "Tom Torero World Tour Day 13 (1080p_30fps_H264-128kbit_AAC).mp4",
    "Tom Torero World Tour Day 12 (1080p_30fps_H264-128kbit_AAC).mp4",
    "Tom Torero World Tour Day 10 (1080p_30fps_H264-128kbit_AAC).mp4",
    "Tom_Torero_Word_Tour_Day_24_Powder_Day_1080p_30fps_H264_128kbit.mp4",
    "Tom_Torero_World_Tour_Day_8_How_To_Film_A_Vlog_1080p_30fps_H264.mp4",
    "Tom_Torero_World_Tour_Day_28_Lions_Head_Infield_1080p_30fps_H264.mp4",
    "Tom_Torero_World_Tour_Day_19_Mount_Doom_1080p_30fps_H264_128kbit.mp4",
    "Tom_Torero_World_Tour_Day_29_Sharks!_1080p_30fps_H264_128kbit_AAC.mp4",
    "Tom_Torero_World_Tour_Day_23_Where_s_The_Daygame_1080p_30fps_H264.mp4",
    "Tom_Torero_World_Tour_Day_25_Mountain_Porn_1080p_30fps_H264_128kbit.mp4",
    "Tom_Torero_World_Tour_Day_14_Sydney_Daygame_720p_30fps_H264_192kbit.mp4",
    "Tom_Torero_World_Tour_Day_15_Blue_Mountains_1080p_30fps_H264_128kbit.mp4",
    "Tom_Torero_World_Tour_Day_3_New_York_Daygame_720p_60fps_H264_128kbit.mp4",
    "Tom_Torero_World_Tour_Day_30_Minimal_Luggage_1080p_30fps_H264_128kbit.mp4",
]

lines = []
for i, name in enumerate(filenames, start=1):
    safe = name.replace('\n', ' ')
    url = f"https://t.me/c/12345/{1000 + i}"
    lines.append(f"{i}) [{safe}]({url})")

content = "\n".join(lines)
content_md = md_to_markdown(content)
query = "tom torero"
from app.api import _escape_markdown as escape
header = f"*Search:* {escape(query)} — {len(filenames)} results\n\n"

MAX_MSG = getattr(settings, 'MAX_MSG', 4000)
TELEGRAM_LIMIT = min(int(MAX_MSG), 4096)
note = "\n\n_Showing truncated results due to size limits._"

max_body_len = max(0, TELEGRAM_LIMIT - len(header) - len(note))
lines_body = content_md.splitlines()
kept_lines = []
cur_len = 0
for ln in lines_body:
    add = len(ln) + 1
    if cur_len + add > max_body_len:
        break
    kept_lines.append(ln)
    cur_len += add

if len(kept_lines) < len(lines_body):
    if kept_lines:
        body = "\n".join(kept_lines) + note
    else:
        body = "_Results too large to display. Use navigation to view parts._"
else:
    body = content_md

text_to_send = header + body

print('TELEGRAM_LIMIT=', TELEGRAM_LIMIT)
print('header_len=', len(header))
print('content_md_len=', len(content_md))
print('text_to_send_len=', len(text_to_send))
print('lines_body_total=', len(lines_body))
print('kept_lines_count=', len(kept_lines))
print('last kept lines:')
for ln in kept_lines[-10:]:
    print(ln)
