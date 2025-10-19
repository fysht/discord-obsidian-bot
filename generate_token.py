import os
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
import logging

# --- 設定 ---
CREDENTIALS_FILE = 'credentials.json'
TOKEN_FILE = 'token.json'
# --- スコープにDocs APIを追加 ---
SCOPES = [
    'https://www.googleapis.com/auth/calendar', # Calendar Cog用
    'https://www.googleapis.com/auth/documents' # Google Docs Handler用
]
# --- ここまで ---

def main():
    """
    Googleの認証フローを実行し、token.jsonを生成・更新します。
    """
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    creds = None
    if os.path.exists(TOKEN_FILE):
        try:
            # 読み込み時にもSCOPESを指定する
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
            logging.info(f"{TOKEN_FILE} を読み込みました。")
        except ValueError as e:
             logging.warning(f"既存の{TOKEN_FILE}のスコープが不足している可能性があります: {e}。再認証が必要です。")
             creds = None # スコープ不一致の場合は再認証へ
        except Exception as e:
            logging.warning(f"既存の{TOKEN_FILE}の読み込みに失敗しました: {e}")
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                logging.info("トークンが期限切れです。リフレッシュを試みます...")
                creds.refresh(Request())
                logging.info("トークンのリフレッシュに成功しました。")
            except Exception as e:
                logging.warning(f"トークンのリフレッシュに失敗しました: {e}")
                logging.info("新しい認証フローを開始します。")
                flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
                creds = flow.run_local_server(port=0)
        else:
            logging.info("新しい認証フローを開始します。")
            if not os.path.exists(CREDENTIALS_FILE):
                 logging.error(f"{CREDENTIALS_FILE} が見つかりません。Google Cloud Consoleからダウンロードしてください。")
                 return # credentials.json がなければ終了
            try:
                flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
                creds = flow.run_local_server(port=0)
            except Exception as e:
                 logging.error(f"認証フローの実行中にエラーが発生しました: {e}")
                 return # エラー時は終了

    if creds: # credsが取得できた場合のみ保存
        # 保存前にスコープを確認（デバッグ用）
        logging.info(f"取得/更新された認証情報のスコープ: {creds.scopes}")
        with open(TOKEN_FILE, 'w') as token:
            token.write(creds.to_json())
        logging.info(f"'{TOKEN_FILE}' が正常に作成・更新されました。")
        logging.info("このファイルをサーバーにアップロードしてください。")
    else:
         logging.error("認証情報の取得に失敗したため、token.json は更新されませんでした。")


if __name__ == '__main__':
    main()