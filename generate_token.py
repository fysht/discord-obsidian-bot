import os
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
import logging

# --- 設定 ---
# このスクリプトは、credentials.json を使って token.json を生成します。
# credentials.json はGoogle Cloud Consoleからダウンロードしたものである必要があります。
CREDENTIALS_FILE = 'credentials.json'
TOKEN_FILE = 'token.json'
# calendar_cog.pyで指定したものと同じスコープ（権限）を設定します
SCOPES = ['https://www.googleapis.com/auth/calendar']

def main():
    """
    Googleの認証フローを実行し、token.jsonを生成・更新します。
    """
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    creds = None
    # 既存のtoken.jsonを読み込もうと試みる
    if os.path.exists(TOKEN_FILE):
        try:
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        except Exception as e:
            logging.warning(f"既存の{TOKEN_FILE}の読み込みに失敗しました: {e}")

    # 認証情報が無効な場合、または存在しない場合は、新しい認証情報を取得
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                logging.info("トークンが期限切れです。リフレッシュを試みます...")
                creds.refresh(Request())
            except Exception as e:
                logging.warning(f"トークンのリフレッシュに失敗しました: {e}")
                logging.info("新しい認証フローを開始します。")
                flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
                creds = flow.run_local_server(port=0)
        else:
            logging.info("新しい認証フローを開始します。")
            # credentials.jsonから認証フローを作成
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            # ローカルサーバーを起動し、ブラウザで認証を実行
            creds = flow.run_local_server(port=0)

    # 新しく取得した認証情報をtoken.jsonに保存
    with open(TOKEN_FILE, 'w') as token:
        token.write(creds.to_json())
    
    logging.info(f"'{TOKEN_FILE}' が正常に作成・更新されました。")
    logging.info("このファイルをサーバーにアップロードしてください。")

if __name__ == '__main__':
    main()