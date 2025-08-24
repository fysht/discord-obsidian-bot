import google_auth_oauthlib.flow

# このスクリプトは、サーバー上ではなく、あなたのPCで一度だけ実行します。

def main():
    # 必要な権限（スコープ）を指定します。今回はYouTubeの読み取り専用権限です。
    scopes = ["https://www.googleapis.com/auth/youtube.force-ssl"]

    # client_secret.jsonファイルから設定を読み込み、認証フローを開始します。
    flow = google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file(
        "client_secret.json", scopes)
    
    # ブラウザが開き、Googleアカウントでのログインと権限の許可を求められます。
    credentials = flow.run_local_server(port=0)

    # 認証が成功すると、資格情報が取得できます。
    print("\n認証に成功しました！")
    print(f"アクセストークン: {credentials.token}")
    print(f"リフレッシュトークン: {credentials.refresh_token}")
    print(f"クライアントID: {credentials.client_id}")
    print(f"クライアントシークレット: {credentials.client_secret}")
    
    print("\n---")
    print("上記の「リフレッシュトークン」をコピーして、Renderの環境変数に設定してください。")
    print("---")

if __name__ == "__main__":
    main()