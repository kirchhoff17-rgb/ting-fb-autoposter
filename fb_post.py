#!/usr/bin/env python3
"""
一方石 FB 自動發文腳本
流程：讀取 Google Sheet 中狀態為「待發」且日期 <= 今天的列
      → 若文案為空則用 Claude 生成
      → 若有圖片從 Google Drive 下載並上傳到 FB
      → 發文到一方石工作室粉絲專頁
      → 回寫狀態、發文時間、FB Post ID
"""

import os
import sys
import io
import re
import json
import requests
import traceback
from datetime import datetime, date
from pathlib import Path
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import anthropic

# 載入環境變數
load_dotenv(Path(__file__).parent / ".env")

FB_PAGE_ID = os.getenv("FB_PAGE_ID")
FB_PAGE_ACCESS_TOKEN = os.getenv("FB_PAGE_ACCESS_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

SPREADSHEET_ID = "1rf4oOZ_QCTiGRTdc7yGU5-bQowMeirEMZFuM_oNN40s"
CREDENTIALS_FILE = Path(__file__).parent / "google_credentials.json"
TZ = ZoneInfo("Asia/Taipei")

# 欄位索引（從 0 開始）
COL_DATE = 0       # A 發文日期
COL_TOPIC = 1      # B 主題
COL_CONTENT = 2    # C 文案內容
COL_STATUS = 3     # D 狀態
COL_POSTED_AT = 4  # E 實際發文時間
COL_POST_ID = 5    # F FB Post ID
COL_IMAGE = 6      # G 圖片（Drive 網址或留空）

STYLE_NOTES = """品牌文案風格：結合「職人」「美學」「實用性」，強調職人質感。
語氣溫暖有個性，像朋友推薦而非廣告。台灣繁體中文，偶爾夾一兩個日文詞。
不超過 200 字，結尾加 1~2 個 hashtag。中文遇英文或數字時加半形空格。"""

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]


def log(msg: str):
    ts = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    log_path = Path(__file__).parent / "post_log.txt"
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def get_google_creds():
    return Credentials.from_service_account_file(str(CREDENTIALS_FILE), scopes=GOOGLE_SCOPES)


def get_sheet():
    gc = gspread.authorize(get_google_creds())
    return gc.open_by_key(SPREADSHEET_ID).sheet1


def extract_drive_file_id(url_or_id: str) -> str:
    """從 Google Drive 網址或直接 ID 取出 file ID"""
    # 格式：https://drive.google.com/file/d/FILE_ID/view
    match = re.search(r"/d/([a-zA-Z0-9_-]+)", url_or_id)
    if match:
        return match.group(1)
    # 格式：https://drive.google.com/open?id=FILE_ID
    match = re.search(r"[?&]id=([a-zA-Z0-9_-]+)", url_or_id)
    if match:
        return match.group(1)
    # 直接是 ID
    if re.match(r"^[a-zA-Z0-9_-]{20,}$", url_or_id):
        return url_or_id
    return None


def download_from_drive(file_id: str) -> tuple[bytes, str]:
    """從 Google Drive 下載圖片，回傳 (bytes, mime_type)"""
    service = build("drive", "v3", credentials=get_google_creds())
    meta = service.files().get(fileId=file_id, fields="name,mimeType").execute()
    mime_type = meta.get("mimeType", "image/jpeg")

    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, service.files().get_media(fileId=file_id))
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue(), mime_type


def upload_photo_to_fb(image_bytes: bytes, mime_type: str) -> str:
    """上傳圖片到 FB，回傳 photo_id（unpublished）"""
    url = f"https://graph.facebook.com/v19.0/{FB_PAGE_ID}/photos"
    ext = mime_type.split("/")[-1].replace("jpeg", "jpg")
    resp = requests.post(url, data={
        "published": "false",
        "access_token": FB_PAGE_ACCESS_TOKEN,
    }, files={"source": (f"photo.{ext}", image_bytes, mime_type)})
    result = resp.json()
    if "id" not in result:
        raise Exception(f"圖片上傳失敗：{result}")
    return result["id"]


def generate_content(topic: str) -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": f"你是一方石工作室（Arrowrockman）的文案創作者。\n請針對「{topic}」寫一則 Facebook 貼文。\n{STYLE_NOTES}\n只輸出貼文內容，不要任何說明。"
        }],
    )
    return msg.content[0].text.strip()


def post_to_facebook(message: str, photo_id: str = None, dry_run: bool = False) -> dict:
    if dry_run:
        log(f"[DRY RUN] 預計發文{'（含圖片）' if photo_id else ''}：\n{message}")
        return {"dry_run": True, "id": "dry_run"}

    url = f"https://graph.facebook.com/v19.0/{FB_PAGE_ID}/feed"
    payload = {"message": message, "access_token": FB_PAGE_ACCESS_TOKEN}
    if photo_id:
        payload["attached_media"] = json.dumps([{"media_fbid": photo_id}])

    resp = requests.post(url, data=payload)
    return resp.json()


def main():
    dry_run = "--dry-run" in sys.argv
    today = date.today()
    now_str = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")

    log("=== 一方石自動發文開始 ===")

    ws = get_sheet()
    rows = ws.get_all_values()
    posted_count = 0

    for i, row in enumerate(rows[1:], start=2):
        if len(row) < 4:
            continue

        row_date_str = row[COL_DATE].strip()
        topic = row[COL_TOPIC].strip()
        content = row[COL_CONTENT].strip()
        status = row[COL_STATUS].strip()
        image_ref = row[COL_IMAGE].strip() if len(row) > COL_IMAGE else ""

        if status != "待發":
            continue

        try:
            row_date = date.fromisoformat(row_date_str)
        except ValueError:
            log(f"第 {i} 列日期格式錯誤：{row_date_str}，跳過")
            continue

        if row_date > today:
            log(f"第 {i} 列日期 {row_date_str} 未到，跳過")
            continue

        log(f"處理第 {i} 列：{row_date_str} / {topic}")

        # 生成文案
        if not content or content == "（待填）":
            log("文案為空，呼叫 Claude 生成...")
            try:
                content = generate_content(topic)
                log(f"生成文案：{content[:80]}...")
                ws.update_cell(i, COL_CONTENT + 1, content)
            except Exception as e:
                log(f"Claude 生成失敗：{e}，跳過此列")
                continue

        # 處理圖片
        photo_id = None
        if image_ref and not dry_run:
            file_id = extract_drive_file_id(image_ref)
            if file_id:
                log(f"下載 Drive 圖片：{file_id}")
                try:
                    img_bytes, mime = download_from_drive(file_id)
                    photo_id = upload_photo_to_fb(img_bytes, mime)
                    log(f"圖片上傳成功，photo_id: {photo_id}")
                except Exception as e:
                    log(f"圖片處理失敗：{e}，改發純文字")
            else:
                log(f"無法解析圖片連結：{image_ref}，改發純文字")

        # 發文
        result = post_to_facebook(content, photo_id=photo_id, dry_run=dry_run)

        if "id" in result and not dry_run:
            post_id = result["id"]
            log(f"發文成功！Post ID: {post_id}")
            ws.update_cell(i, COL_STATUS + 1, "已發")
            ws.update_cell(i, COL_POSTED_AT + 1, now_str)
            ws.update_cell(i, COL_POST_ID + 1, post_id)
            posted_count += 1
        elif dry_run and "id" in result:
            log("[DRY RUN] 完成，不寫回 Sheet")
            posted_count += 1
        else:
            log(f"發文失敗：{json.dumps(result, ensure_ascii=False)}")

    log(f"=== 完成，共發出 {posted_count} 篇 ===\n")


if __name__ == "__main__":
    main()
