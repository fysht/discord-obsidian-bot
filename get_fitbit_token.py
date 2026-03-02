import urllib.request
import urllib.parse
import json
import base64
import os

# python-dotenvがインストールされている環境なら.envを読み込む
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ==========================================
# .env ファイルから環境変数を読み込む（直接書き込まない）
# ==========================================
CLIENT_ID = os.getenv("FITBIT_CLIENT_ID")
CLIENT_SECRET = os.getenv("FITBIT_CLIENT_SECRET")

def main():
    if not CLIENT_ID or not CLIENT_SECRET:
        print("エラー: 環境変数 (FITBIT_CLIENT_ID, FITBIT_CLIENT_SECRET) が見つかりません。")
        print(".env ファイルが同じフォルダにあるか確認してください。")
        return

    # 2. 認証URLの生成
    auth_url = f"https://www.fitbit.com/oauth2/authorize?client_id={CLIENT_ID}&response_type=code&scope=activity%20heartrate%20location%20nutrition%20profile%20settings%20sleep%20social%20weight"
    print("【ステップ1】ブラウザの「新しい空のタブ」を開き、以下のURLを貼り付けてアクセスしてください。")
    print("-" * 60)
    print(auth_url)
    print("-" * 60)

    # 3. リダイレクトURLの入力
    print("\n【ステップ2】Fitbitの画面で「すべて許可」をクリックしてください。")
    redirected_url = input("その後、飛ばされた画面（真っ白でOK）のURLをすべてコピーし、ここに貼り付けてEnterを押してください:\n> ")

    # URLからcodeを抽出
    parsed_url = urllib.parse.urlparse(redirected_url)
    query_params = urllib.parse.parse_qs(parsed_url.query)
    
    if 'code' not in query_params:
        print("\n❌ エラー：貼り付けたURLから認証コード(code)が見つかりませんでした。")
        return

    code = query_params['code'][0]
    if code.endswith('#_=_'):
        code = code[:-4]

    # 4. トークンの取得
    print("\n通信中... トークンを取得しています...")
    token_url = "https://api.fitbit.com/oauth2/token"
    auth_str = f"{CLIENT_ID}:{CLIENT_SECRET}"
    b64_auth_str = base64.b64encode(auth_str.encode()).decode()

    headers = {
        "Authorization": f"Basic {b64_auth_str}",
        "Content-Type": "application/x-www-form-urlencoded"
    }
    data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code
    }).encode()

    req = urllib.request.Request(token_url, data=data, headers=headers)

    try:
        with urllib.request.urlopen(req) as response:
            res_data = json.loads(response.read().decode())
            print("\n" + "=" * 60)
            print("🎉 認証大成功！ 新しいリフレッシュトークンを取得しました。")
            print("=" * 60)
            print(res_data["refresh_token"])
            print("=" * 60)
            print("\n✅ この文字列をコピーして、Google Driveの『fitbit_refresh_token.txt』に上書き保存してください！")
            
    except urllib.error.HTTPError as e:
        error_info = e.read().decode()
        print(f"\n❌ エラーが発生しました (ステータスコード: {e.code})")
        print(error_info)

if __name__ == "__main__":
    main()