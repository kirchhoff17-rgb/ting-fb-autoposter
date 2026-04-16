#!/usr/bin/env python3
"""
一方石自動發文腳本（FB + Threads）
流程：讀取 Google Sheet 中狀態為「待發」且日期 <= 今天的列
      → 若 FB/Threads 文案為空則分別用 Claude 生成
      → 若有圖片從 Google Drive 下載並上傳
      → 發文到 FB 粉絲專頁 & Threads
      → 回寫各自的狀態、發文時間、Post ID
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
from groq import Groq

# 載入環境變數
load_dotenv(Path(__file__).parent / ".env")

FB_PAGE_ID = os.getenv("FB_PAGE_ID")
FB_PAGE_ACCESS_TOKEN = os.getenv("FB_PAGE_ACCESS_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
THREADS_USER_ID = os.getenv("THREADS_USER_ID", "")
THREADS_ACCESS_TOKEN = os.getenv("THREADS_ACCESS_TOKEN", "")

SPREADSHEET_ID = "1rf4oOZ_QCTiGRTdc7yGU5-bQowMeirEMZFuM_oNN40s"
CREDENTIALS_FILE = Path(__file__).parent / "google_credentials.json"
TZ = ZoneInfo("Asia/Taipei")

# 欄位索引（從 0 開始）
COL_DATE = 0                  # A 發文日期
COL_TOPIC = 1                 # B 主題
COL_FB_CONTENT = 2            # C FB 文案
COL_FB_STATUS = 3             # D FB 狀態
COL_FB_POSTED_AT = 4          # E FB 發文時間
COL_POST_ID = 5               # F FB Post ID
COL_IMAGE = 6                 # G 圖片（Drive 網址或留空）
COL_THREADS_CONTENT = 7       # H Threads 文案
COL_THREADS_STATUS = 8        # I Threads 狀態
COL_THREADS_POSTED_AT = 9     # J Threads 發文時間
COL_THREADS_POST_ID = 10      # K Threads Post ID

FB_STYLE_NOTES = """品牌文案風格：結合「職人」「美學」「實用性」，強調職人質感。
語氣溫暖有個性，像朋友推薦而非廣告。台灣繁體中文，偶爾夾一兩個日文詞。
不超過 200 字，結尾加 1~2 個 hashtag。中文遇英文或數字時加半形空格。"""

THREADS_STYLE_NOTES = """品牌文案風格：結合「職人」「美學」「實用性」，強調職人質感。
語氣更口語輕鬆，像朋友隨手分享。台灣繁體中文，不超過 150 字。
結尾可加 1 個 hashtag 或完全不加，避免過多 hashtag。中文遇英文或數字時加半形空格。"""

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


def generate_content(topic: str, platform: str = "fb") -> str:
    client = Groq(api_key=GROQ_API_KEY)
    if platform == "threads":
        style = THREADS_STYLE_NOTES
        platform_name = "Threads"
    else:
        style = FB_STYLE_NOTES
        platform_name = "Facebook"
    resp = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": f"你是一方石工作室（Arrowrockman）的文案創作者。\n請針對「{topic}」寫一則 {platform_name} 貼文。\n{style}\n只輸出貼文內容，不要任何說明。"
        }],
    )
    return resp.choices[0].message.content.strip()


def drive_url_to_threads_url(url: str) -> str:
    """將 Google Drive 分享連結轉換成 Threads 可直接存取的圖片網址"""
    file_id = extract_drive_file_id(url)
    if file_id:
        return f"https://lh3.googleusercontent.com/d/{file_id}"
    return url


def post_to_threads(message: str, image_urls: list = None, dry_run: bool = False) -> dict:
    """發文到 Threads（需先在 .env 設定 THREADS_USER_ID 與 THREADS_ACCESS_TOKEN）"""
    if not THREADS_USER_ID or not THREADS_ACCESS_TOKEN:
        return {"skipped": True, "reason": "Threads Token 尚未設定"}

    base_url = f"https://graph.threads.net/v1.0/{THREADS_USER_ID}"

    # 將 Drive 連結轉換成 lh3 直連格式
    public_urls = [drive_url_to_threads_url(u) for u in (image_urls or [])]

    if dry_run:
        log(f"[DRY RUN] Threads 預計發文（{len(public_urls)} 張圖）：\n{message}")
        return {"dry_run": True, "id": "dry_run_threads"}

    # 有圖片：發 carousel 或單圖
    if public_urls:
        if len(public_urls) == 1:
            resp = requests.post(f"{base_url}/threads", data={
                "media_type": "IMAGE",
                "image_url": public_urls[0],
                "text": message,
                "access_token": THREADS_ACCESS_TOKEN,
            })
            container_id = resp.json().get("id")
            if not container_id:
                return {"error": f"建立 container 失敗：{resp.json()}"}
        else:
            # 多圖 carousel
            child_ids = []
            for img_url in public_urls:
                r = requests.post(f"{base_url}/threads", data={
                    "media_type": "IMAGE",
                    "image_url": img_url,
                    "is_carousel_item": "true",
                    "access_token": THREADS_ACCESS_TOKEN,
                })
                cid = r.json().get("id")
                if cid:
                    child_ids.append(cid)
                else:
                    log(f"Threads carousel 圖片失敗：{r.json()}，略過此圖")
            if not child_ids:
                return {"error": "所有圖片都無法建立 carousel item"}
            resp = requests.post(f"{base_url}/threads", data={
                "media_type": "CAROUSEL",
                "children": ",".join(child_ids),
                "text": message,
                "access_token": THREADS_ACCESS_TOKEN,
            })
            container_id = resp.json().get("id")
            if not container_id:
                return {"error": f"建立 carousel container 失敗：{resp.json()}"}
    else:
        # 純文字
        resp = requests.post(f"{base_url}/threads", data={
            "media_type": "TEXT",
            "text": message,
            "access_token": THREADS_ACCESS_TOKEN,
        })
        container_id = resp.json().get("id")
        if not container_id:
            return {"error": f"建立 container 失敗：{resp.json()}"}

    # 發布
    pub_resp = requests.post(f"{base_url}/threads_publish", data={
        "creation_id": container_id,
        "access_token": THREADS_ACCESS_TOKEN,
    })
    return pub_resp.json()


def post_to_facebook(message: str, photo_ids: list = None, dry_run: bool = False) -> dict:
    if dry_run:
        count = len(photo_ids) if photo_ids else 0
        log(f"[DRY RUN] 預計發文（{count} 張圖片）：\n{message}")
        return {"dry_run": True, "id": "dry_run"}

    url = f"https://graph.facebook.com/v19.0/{FB_PAGE_ID}/feed"
    payload = {"message": message, "access_token": FB_PAGE_ACCESS_TOKEN}
    if photo_ids:
        payload["attached_media"] = json.dumps([{"media_fbid": pid} for pid in photo_ids])

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
        fb_content = row[COL_FB_CONTENT].strip() if len(row) > COL_FB_CONTENT else ""
        fb_status = row[COL_FB_STATUS].strip() if len(row) > COL_FB_STATUS else ""
        image_ref = row[COL_IMAGE].strip() if len(row) > COL_IMAGE else ""
        threads_content = row[COL_THREADS_CONTENT].strip() if len(row) > COL_THREADS_CONTENT else ""
        threads_status = row[COL_THREADS_STATUS].strip() if len(row) > COL_THREADS_STATUS else ""

        # 兩個都已發，跳過整列
        if fb_status == "已發" and threads_status in ("已發", "不發", "跳過"):
            continue
        # 日期還沒到
        try:
            row_date = date.fromisoformat(row_date_str)
        except ValueError:
            log(f"第 {i} 列日期格式錯誤：{row_date_str}，跳過")
            continue
        if row_date > today:
            log(f"第 {i} 列日期 {row_date_str} 未到，跳過")
            continue

        log(f"處理第 {i} 列：{row_date_str} / {topic}")

        # 解析圖片連結（G 欄每行一個）
        image_urls_raw = [u.strip() for u in image_ref.splitlines() if u.strip()]

        # ── FB 發文 ──────────────────────────────────────────
        if fb_status != "已發":
            if not fb_content or fb_content == "（待填）":
                log("FB 文案為空，呼叫 Claude 生成...")
                try:
                    fb_content = generate_content(topic, platform="fb")
                    log(f"FB 生成文案：{fb_content[:80]}...")
                    ws.update_cell(i, COL_FB_CONTENT + 1, fb_content)
                except Exception as e:
                    log(f"FB Claude 生成失敗：{e}，跳過此列")
                    continue

            photo_ids = []
            if image_urls_raw and not dry_run:
                for img_url in image_urls_raw:
                    file_id = extract_drive_file_id(img_url)
                    if file_id:
                        log(f"下載 Drive 圖片：{file_id}")
                        try:
                            img_bytes, mime = download_from_drive(file_id)
                            pid = upload_photo_to_fb(img_bytes, mime)
                            photo_ids.append(pid)
                            log(f"圖片上傳 FB 成功，photo_id: {pid}")
                        except Exception as e:
                            log(f"圖片處理失敗：{e}，略過此圖")
                    else:
                        log(f"無法解析圖片連結：{img_url}，略過")

            result = post_to_facebook(fb_content, photo_ids=photo_ids, dry_run=dry_run)
            if "id" in result and not dry_run:
                log(f"FB 發文成功！Post ID: {result['id']}")
                ws.update_cell(i, COL_FB_STATUS + 1, "已發")
                ws.update_cell(i, COL_FB_POSTED_AT + 1, now_str)
                ws.update_cell(i, COL_POST_ID + 1, result["id"])
                posted_count += 1
            elif dry_run and "id" in result:
                log("[DRY RUN] FB 完成，不寫回 Sheet")
                posted_count += 1
            else:
                log(f"FB 發文失敗：{json.dumps(result, ensure_ascii=False)}")

        # ── Threads 發文 ─────────────────────────────────────
        if threads_status not in ("已發", "不發"):
            if not threads_content or threads_content == "（待填）":
                log("Threads 文案為空，呼叫 Claude 生成...")
                try:
                    threads_content = generate_content(topic, platform="threads")
                    log(f"Threads 生成文案：{threads_content[:80]}...")
                    ws.update_cell(i, COL_THREADS_CONTENT + 1, threads_content)
                except Exception as e:
                    log(f"Threads Claude 生成失敗：{e}，跳過")
                    threads_content = ""

            if threads_content:
                t_result = post_to_threads(threads_content, image_urls=image_urls_raw, dry_run=dry_run)
                if t_result.get("skipped"):
                    log(f"Threads 跳過：{t_result['reason']}")
                elif "id" in t_result and not dry_run:
                    log(f"Threads 發文成功！Post ID: {t_result['id']}")
                    ws.update_cell(i, COL_THREADS_STATUS + 1, "已發")
                    ws.update_cell(i, COL_THREADS_POSTED_AT + 1, now_str)
                    ws.update_cell(i, COL_THREADS_POST_ID + 1, t_result["id"])
                elif dry_run and "id" in t_result:
                    log("[DRY RUN] Threads 完成，不寫回 Sheet")
                else:
                    log(f"Threads 發文失敗：{json.dumps(t_result, ensure_ascii=False)}")

    log(f"=== 完成，共發出 {posted_count} 篇 ===\n")


if __name__ == "__main__":
    main()
