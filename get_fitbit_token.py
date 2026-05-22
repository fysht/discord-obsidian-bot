"""
Fitbit OAuth トークン取得スクリプト。

ブラウザで認証 → リフレッシュトークンを取得 → Google Drive の
`fitbit_refresh_token.txt` に**直接保存**する。Bot 起動時はこの Drive 上の
トークンを参照するため、これ以降の再認証は不要。

依存: services.google_drive_service が動作する環境
（=Bot と同じ環境変数・認証情報が利用可能であること）。
"""

import asyncio
import base64
import json
import os
import urllib.parse
import urllib.request

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

CLIENT_ID = os.getenv("FITBIT_CLIENT_ID")
CLIENT_SECRET = os.getenv("FITBIT_CLIENT_SECRET")
TOKEN_FILE_NAME = "fitbit_refresh_token.txt"


async def _save_to_drive(refresh_token: str) -> bool:
    """取得したリフレッシュトークンを Google Drive へ保存する。"""
    try:
        from services.google_drive_service import GoogleDriveService
    except Exception as e:
        print(f"\n⚠️  Drive モジュール読込失敗: {e}")
        return False

    folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
    if not folder_id:
        print("\n⚠️  GOOGLE_DRIVE_FOLDER_ID が未設定のため Drive 保存をスキップします。")
        return False

    drive = GoogleDriveService(folder_id)
    service = drive.get_service()
    if not service:
        print("\n⚠️  Drive サービスを初期化できませんでした（token.json 未設定？）。")
        return False

    f_id = await drive.find_file(service, folder_id, TOKEN_FILE_NAME)
    if f_id:
        await drive.update_text(service, f_id, refresh_token)
    else:
        await drive.upload_text(service, folder_id, TOKEN_FILE_NAME, refresh_token)
    return True


def main():
    if not CLIENT_ID or not CLIENT_SECRET:
        print("エラー: 環境変数 (FITBIT_CLIENT_ID, FITBIT_CLIENT_SECRET) が見つかりません。")
        print(".env ファイルが同じフォルダにあるか確認してください。")
        return

    auth_url = (
        f"https://www.fitbit.com/oauth2/authorize?client_id={CLIENT_ID}"
        "&response_type=code"
        "&scope=activity%20heartrate%20location%20nutrition%20profile%20settings%20sleep%20social%20weight"
    )
    print("【ステップ1】ブラウザの「新しい空のタブ」を開き、以下のURLにアクセスしてください。")
    print("-" * 60)
    print(auth_url)
    print("-" * 60)

    print("\n【ステップ2】Fitbitの画面で「すべて許可」をクリック。")
    redirected_url = input(
        "その後、飛ばされた画面のURLをすべてコピーし、ここに貼り付けてEnterを押してください:\n> "
    )

    parsed_url = urllib.parse.urlparse(redirected_url)
    query_params = urllib.parse.parse_qs(parsed_url.query)
    if "code" not in query_params:
        print("\n❌ エラー：貼り付けたURLから認証コード(code)が見つかりませんでした。")
        return
    code = query_params["code"][0]
    if code.endswith("#_=_"):
        code = code[:-4]

    print("\n通信中... トークンを取得しています...")
    token_url = "https://api.fitbit.com/oauth2/token"
    auth_str = f"{CLIENT_ID}:{CLIENT_SECRET}"
    b64_auth_str = base64.b64encode(auth_str.encode()).decode()
    headers = {
        "Authorization": f"Basic {b64_auth_str}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = urllib.parse.urlencode(
        {"grant_type": "authorization_code", "code": code}
    ).encode()

    req = urllib.request.Request(token_url, data=data, headers=headers)

    try:
        with urllib.request.urlopen(req) as response:
            res_data = json.loads(response.read().decode())
    except urllib.error.HTTPError as e:
        error_info = e.read().decode()
        print(f"\n❌ エラー (ステータス: {e.code})")
        print(error_info)
        return

    refresh_token = res_data["refresh_token"]
    print("\n" + "=" * 60)
    print("🎉 認証成功！リフレッシュトークンを取得しました。")
    print("=" * 60)

    # Drive へ自動保存
    print("\n📤 Google Drive へ自動保存中...")
    saved = asyncio.run(_save_to_drive(refresh_token))
    if saved:
        print(f"✅ Drive の '{TOKEN_FILE_NAME}' に保存しました。Bot を再起動すれば反映されます。")
    else:
        print("\nDrive 保存に失敗したため、以下のトークンを手動で")
        print(f"Google Drive の '{TOKEN_FILE_NAME}' に上書き保存してください:")
        print("-" * 60)
        print(refresh_token)
        print("-" * 60)


if __name__ == "__main__":
    main()
