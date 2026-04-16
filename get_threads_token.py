"""
Threads OAuth Token 取得工具
執行後會自動開啟瀏覽器，授權完畢自動換成長效 Token 並寫入 .env
"""
import http.server
import threading
import webbrowser
import urllib.parse
import requests
import re
from pathlib import Path

THREADS_APP_ID = "1457603099184463"
THREADS_APP_SECRET = "ce19373fa742004f7a5b0e2361cad7d4"
REDIRECT_URI = "http://localhost:8888/"
SCOPE = "threads_basic,threads_content_publish"

auth_code = None

class CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        global auth_code
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.end_headers()

        if "code" in params:
            auth_code = params["code"][0]
            self.wfile.write("<h2>授權成功！請回到終端機查看結果。</h2>".encode("utf-8"))
        elif "error_message" in params:
            msg = params.get("error_message", ["未知錯誤"])[0]
            self.wfile.write(f"<h2>授權失敗：{msg}</h2>".encode("utf-8"))
        else:
            self.wfile.write("<h2>等待中...</h2>".encode("utf-8"))

    def log_message(self, format, *args):
        pass  # 不印 server log

def main():
    global auth_code

    # 啟動本機伺服器
    server = http.server.HTTPServer(("localhost", 8888), CallbackHandler)
    thread = threading.Thread(target=server.handle_request)
    thread.start()

    # 組授權網址
    auth_url = (
        f"https://www.threads.net/oauth/authorize"
        f"?client_id={THREADS_APP_ID}"
        f"&redirect_uri={urllib.parse.quote(REDIRECT_URI, safe='')}"
        f"&scope={SCOPE}"
        f"&response_type=code"
    )

    print("正在開啟瀏覽器進行 Threads 授權...")
    print(f"若瀏覽器沒自動開啟，請手動複製以下網址：\n{auth_url}\n")
    webbrowser.open(auth_url)

    # 等待授權碼
    thread.join(timeout=120)
    server.server_close()

    if not auth_code:
        print("錯誤：未收到授權碼，請重新執行。")
        return

    print(f"收到授權碼：{auth_code[:20]}...")

    # 換短效 Token
    resp = requests.post("https://graph.threads.net/oauth/access_token", data={
        "client_id": THREADS_APP_ID,
        "client_secret": THREADS_APP_SECRET,
        "grant_type": "authorization_code",
        "redirect_uri": REDIRECT_URI,
        "code": auth_code,
    })
    data = resp.json()
    if "access_token" not in data:
        print(f"換 Token 失敗：{data}")
        return

    short_token = data["access_token"]
    user_id = data.get("user_id", "")
    print(f"短效 Token 取得成功，User ID：{user_id}")

    # 換長效 Token
    resp2 = requests.get("https://graph.threads.net/access_token", params={
        "grant_type": "th_exchange_token",
        "client_id": THREADS_APP_ID,
        "client_secret": THREADS_APP_SECRET,
        "access_token": short_token,
    })
    data2 = resp2.json()
    if "access_token" not in data2:
        print(f"換長效 Token 失敗：{data2}")
        return

    long_token = data2["access_token"]
    expires_in = data2.get("expires_in", 0)
    print(f"長效 Token 取得成功（有效 {expires_in // 86400} 天）")

    # 寫入 .env
    env_path = Path(__file__).parent / ".env"
    env_text = env_path.read_text(encoding="utf-8")
    env_text = re.sub(r"THREADS_USER_ID=.*", f"THREADS_USER_ID={user_id}", env_text)
    env_text = re.sub(r"THREADS_ACCESS_TOKEN=.*", f"THREADS_ACCESS_TOKEN={long_token}", env_text)
    env_path.write_text(env_text, encoding="utf-8")

    print(f"\n已寫入 .env：")
    print(f"  THREADS_USER_ID={user_id}")
    print(f"  THREADS_ACCESS_TOKEN={long_token[:30]}...")
    print("\n完成！可以開始用 Threads 自動發文了。")

if __name__ == "__main__":
    main()
